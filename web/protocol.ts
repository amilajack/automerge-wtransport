export const OPEN = 1,
  SYNC = 2,
  CLOSE = 3,
  HEADER = 18;
export function encode(
  kind: number,
  id: string,
  payload: Uint8Array = new Uint8Array(),
): Uint8Array {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)
  )
    throw Error("Invalid document UUID");
  const b = new Uint8Array(HEADER + payload.length);
  b.set([1, kind]);
  b.set(
    id
      .replaceAll("-", "")
      .match(/../g)!
      .map((x) => parseInt(x, 16)),
    2,
  );
  b.set(payload, HEADER);
  return b;
}
export function decode(b: Uint8Array, max: number) {
  if (
    b.length < HEADER ||
    b.length > max ||
    b[0] !== 1 ||
    b[1] < 1 ||
    b[1] > 3 ||
    (b[1] !== SYNC && b.length !== HEADER)
  )
    throw Error("Invalid envelope");
  const h = Array.from(b.slice(2, 18), (v) =>
    v.toString(16).padStart(2, "0"),
  ).join("");
  return {
    kind: b[1],
    id: `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`,
    payload: b.slice(18),
  };
}
export function frame(b: Uint8Array) {
  const out = new Uint8Array(b.length + 4);
  new DataView(out.buffer).setUint32(0, b.length);
  out.set(b, 4);
  return out;
}
// Incremental bounded allocation, including when a peer fragments every header byte.
export class Decoder {
  private header = new Uint8Array(4);
  private hn = 0;
  private body?: Uint8Array;
  private n = 0;
  constructor(private max: number) {}
  push(chunk: Uint8Array): Uint8Array[] {
    const frames: Uint8Array[] = [];
    let i = 0;
    while (i < chunk.length) {
      if (!this.body) {
        const n = Math.min(4 - this.hn, chunk.length - i);
        this.header.set(chunk.subarray(i, i + n), this.hn);
        this.hn += n;
        i += n;
        if (this.hn < 4) continue;
        const len = new DataView(this.header.buffer).getUint32(0);
        if (len < HEADER || len > this.max) throw Error("Invalid frame length");
        this.body = new Uint8Array(len);
        this.n = 0;
      }
      const n = Math.min(this.body.length - this.n, chunk.length - i);
      this.body.set(chunk.subarray(i, i + n), this.n);
      this.n += n;
      i += n;
      if (this.n === this.body.length) {
        frames.push(this.body);
        this.body = undefined;
        this.hn = 0;
      }
    }
    return frames;
  }
  end() {
    if (this.hn || this.body) throw Error("Truncated frame");
  }
}
