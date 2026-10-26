import { encode, decode, frame, Decoder, OPEN, SYNC, CLOSE } from "./protocol";
export type Kind = "wt" | "ws";
export type Config = { certificateHash: string; maxFrame: number };
type Receive = (id: string, payload: Uint8Array) => void;
export interface Transport {
  open(id: string): Promise<void>;
  send(id: string, payload: Uint8Array): Promise<void>;
  closeDocument(id: string): Promise<void>;
  close(): void;
  bytesIn: number;
  bytesOut: number;
}
// WebTransport typings are not yet shipped in TypeScript's DOM library.
type Bi = {
  readable: ReadableStream<Uint8Array>;
  writable: WritableStream<Uint8Array>;
};
type WT = {
  ready: Promise<void>;
  closed: Promise<unknown>;
  createBidirectionalStream(): Promise<Bi>;
  close(): void;
};
declare global {
  interface Window {
    WebTransport: new (url: string, options: unknown) => WT;
  }
}
const timeout = <T>(p: Promise<T>, ms = 10000): Promise<T> =>
  new Promise((res, rej) => {
    const t = setTimeout(() => rej(Error("Transport timed out")), ms);
    p.then(
      (x) => {
        clearTimeout(t);
        res(x);
      },
      (e) => {
        clearTimeout(t);
        rej(e);
      },
    );
  });
export async function connect(
  kind: Kind,
  base: string,
  token: string,
  config: Config,
  receive: Receive,
  failed: (e: unknown) => void,
): Promise<Transport> {
  const url = new URL(base);
  url.pathname = kind === "wt" ? "/sync" : "/ws";
  url.searchParams.set("token", token);
  let bytesIn = 0,
    bytesOut = 0,
    closed = false;
  const fail = (e: unknown) => {
    if (!closed) {
      closed = true;
      failed(e);
    }
  };
  const consume = (b: Uint8Array, id?: string) => {
    bytesIn += b.length;
    const e = decode(b, config.maxFrame);
    if (e.kind !== SYNC || (id && e.id !== id))
      throw Error("Unexpected sync envelope");
    receive(e.id, e.payload);
  };
  if (kind === "ws") {
    url.protocol = "wss:";
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    await timeout(
      new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.onerror = reject;
      }),
    );
    ws.onmessage = (e) => {
      try {
        consume(new Uint8Array(e.data));
      } catch (e) {
        ws.close();
        fail(e);
      }
    };
    ws.onclose = () => fail(Error("WebSocket closed"));
    ws.onerror = fail;
    const write = async (b: Uint8Array) => {
      if (closed || ws.readyState !== WebSocket.OPEN)
        throw Error("Disconnected");
      if (
        b.length > config.maxFrame ||
        ws.bufferedAmount + b.length > config.maxFrame * 2
      ) {
        ws.close();
        throw Error("Outbound queue exceeded");
      }
      ws.send(b as Uint8Array<ArrayBuffer>);
      bytesOut += b.length;
    };
    return {
      open: (id) => write(encode(OPEN, id)),
      send: (id, p) => write(encode(SYNC, id, p)),
      closeDocument: (id) => write(encode(CLOSE, id)),
      close() {
        closed = true;
        ws.close();
      },
      get bytesIn() {
        return bytesIn;
      },
      get bytesOut() {
        return bytesOut;
      },
    };
  }
  const wt = new window.WebTransport(url.toString(), {
    serverCertificateHashes: [
      {
        algorithm: "sha-256",
        value: Uint8Array.from(config.certificateHash.match(/../g)!, (h) =>
          parseInt(h, 16),
        ),
      },
    ],
  });
  await timeout(wt.ready);
  wt.closed.then(() => fail(Error("WebTransport closed")), fail);
  const streams = new Map<
    string,
    {
      writer: WritableStreamDefaultWriter<Uint8Array>;
      pending: number;
      chain: Promise<void>;
    }
  >();
  const write = (id: string, b: Uint8Array) => {
    const s = streams.get(id);
    if (closed || !s) return Promise.reject(Error("Disconnected"));
    if (
      b.length > config.maxFrame ||
      s.pending + b.length > config.maxFrame * 2
    ) {
      wt.close();
      return Promise.reject(Error("Outbound queue exceeded"));
    }
    s.pending += b.length;
    bytesOut += b.length;
    s.chain = s.chain
      .then(() => timeout(s.writer.write(frame(b))))
      .finally(() => {
        s.pending -= b.length;
      });
    s.chain.catch(fail);
    return s.chain;
  };
  return {
    async open(id) {
      if (streams.has(id)) throw Error("Already open");
      const bi = await timeout(wt.createBidirectionalStream());
      streams.set(id, {
        writer: bi.writable.getWriter(),
        pending: 0,
        chain: Promise.resolve(),
      });
      void (async () => {
        const reader = bi.readable.getReader(),
          d = new Decoder(config.maxFrame);
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              d.end();
              break;
            }
            for (const b of d.push(value)) consume(b, id);
          }
          if (!closed && streams.has(id)) fail(Error("Document stream closed"));
        } catch (e) {
          if (streams.has(id)) fail(e);
        }
      })();
      await write(id, encode(OPEN, id));
    },
    send: (id, p) => write(id, encode(SYNC, id, p)),
    async closeDocument(id) {
      const sent = write(id, encode(CLOSE, id));
      streams.delete(id);
      await sent;
    },
    close() {
      closed = true;
      wt.close();
      streams.clear();
    },
    get bytesIn() {
      return bytesIn;
    },
    get bytesOut() {
      return bytesOut;
    },
  };
}
