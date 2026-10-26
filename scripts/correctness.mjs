import assert from "node:assert/strict";
import * as A from "@automerge/automerge";
import { randomUUID } from "node:crypto";
import { readFile, writeFile, rm } from "node:fs/promises";
import { browser, pageClient, waitConverged } from "./browser.mjs";
import { server, getJSON } from "./server.mjs";
const b = await browser();
let s = await server();
const results = [];
const check = async (name, fn) => {
  const t = Date.now();
  await fn();
  results.push({ name, passed: true, ms: Date.now() - t });
  console.log("PASS", name);
};
try {
  const opts = { base: s.base, token: s.token };
  const p = await pageClient(b.context, { ...opts, kind: "wt", persist: true }),
    q = await pageClient(b.context, { ...opts, kind: "ws" });
  const id = randomUUID(),
    other = randomUUID();
  await check(
    "Rust/JS WT compatibility and mixed WT/WSS convergence",
    async () => {
      await Promise.all(
        [p, q].map((p) =>
          p.evaluate(
            async ({ id, other }) => {
              await c.open(id);
              await c.open(other);
            },
            { id, other },
          ),
        ),
      );
      await p.evaluate(
        (id) => c.setText(id, "Unicode: 你好 🌍 café e\u0301\n"),
        id,
      );
      await waitConverged([p, q], [id]);
      await Promise.all(
        [p, q].map((p, i) =>
          p.evaluate(({ id, i }) => c.append(id, `concurrent-${i} 🦀\n`), {
            id,
            i,
          }),
        ),
      );
      const state = await waitConverged([p, q], [id]);
      assert.match(state[0][0].text, /concurrent-0/);
      assert.match(state[0][0].text, /concurrent-1/);
    },
  );
  await check(
    "Document isolation, stream close and offline reconnect",
    async () => {
      await p.evaluate(
        ({ id, other }) => {
          c.disconnect();
          c.append(id, "offline 🛜");
          c.setText(other, "separate");
        },
        { id, other },
      );
      await p.evaluate(() => c.connect());
      await waitConverged([p, q], [id, other]);
      assert.equal(
        await q.evaluate((other) => c.text(other), other),
        "separate",
      );
      await p.evaluate(
        async ({ id, other }) => {
          await c.transport.closeDocument(other);
          c.docs.delete(other);
          c.append(id, "after-close");
        },
        { id, other },
      );
      await waitConverged([p, q], [id]);
    },
  );
  let expected;
  await check(
    "Acknowledged changes survive SIGKILL, snapshot and fresh-client reload",
    async () => {
      expected = (await waitConverged([p, q], [id]))[0][0];
      await p.evaluate(() => c.flush());
      await s.stop({ preserve: true });
      const snapshot = A.load(await readFile(`${s.data}/${id}.automerge`));
      assert.deepEqual(A.getHeads(snapshot).sort(), expected.heads);
      assert.equal(snapshot.text, expected.text);
      s = await server({ dir: s.data });
      await Promise.all([p, q].map((p) => p.evaluate(() => c.connect())));
      const fresh = await pageClient(b.context, opts);
      await fresh.evaluate((id) => c.open(id), id);
      const state = await waitConverged([p, q, fresh], [id]);
      assert.deepEqual(state[0][0].heads, expected.heads);
      assert.equal(state[2][0].text, expected.text);
      await fresh.close();
    },
  );
  await check(
    "IndexedDB restores document under an independent actor",
    async () => {
      await p.reload();
      await p.waitForFunction(() => window.lab?.ready);
      await p.evaluate(
        async ({ id, opts }) => {
          window.c = new lab.Client(opts.base, opts.token, "wt", true);
          await c.open(id);
          await c.connect();
        },
        { id, opts },
      );
      await waitConverged([p, q], [id]);
      assert.equal(await p.evaluate((id) => c.text(id), id), expected.text);
      assert.notEqual(
        await p.evaluate((id) => c.actor(id), id),
        await q.evaluate((id) => c.actor(id), id),
      );
    },
  );
  await check(
    "Fragmented/coalesced WT frames and malformed/oversized rejection",
    async () => {
      const result = await p.evaluate(
        async ({ token, base }) => {
          const cfg = await fetch(`${base}/config`).then((r) => r.json());
          const hash = Uint8Array.from(cfg.certificateHash.match(/../g), (h) =>
            parseInt(h, 16),
          );
          async function session() {
            const w = new WebTransport(`${base}/sync?token=${token}`, {
              serverCertificateHashes: [{ algorithm: "sha-256", value: hash }],
            });
            await w.ready;
            const stream = await w.createBidirectionalStream();
            return {
              w,
              stream,
              writer: stream.writable.getWriter(),
              reader: stream.readable.getReader(),
            };
          }
          function framed(kind, id, payload = new Uint8Array()) {
            const body = new Uint8Array(18 + payload.length);
            body.set([1, kind]);
            body.set(id, 2);
            body.set(payload, 18);
            const b = new Uint8Array(body.length + 4);
            new DataView(b.buffer).setUint32(0, body.length);
            b.set(body, 4);
            return b;
          }
          const id = crypto.getRandomValues(new Uint8Array(16));
          let a = await session();
          const open = framed(1, id);
          for (const byte of open) await a.writer.write(new Uint8Array([byte]));
          const response = await a.reader.read();
          a.w.close();
          a = await session();
          const both = new Uint8Array(open.length + framed(3, id).length);
          both.set(open);
          both.set(framed(3, id), open.length);
          await a.writer.write(both);
          a.w.close();
          async function rejected(bytes) {
            const x = await session();
            await x.writer.write(bytes);
            let rejected = false;
            try {
              const r = await Promise.race([
                x.reader.read(),
                new Promise((_, rej) =>
                  setTimeout(() => rej(Error("timeout")), 3000),
                ),
              ]);
              rejected = r.done;
            } catch (e) {
              rejected = !String(e).includes("timeout");
            }
            x.w.close();
            return rejected;
          }
          const huge = new Uint8Array(4);
          new DataView(huge.buffer).setUint32(0, cfg.maxFrame + 1);
          const bad = open.slice();
          bad[4] = 9;
          return {
            response: response.value.length > 0,
            oversized: await rejected(huge),
            malformed: await rejected(bad),
          };
        },
        { token: s.token, base: s.base },
      );
      assert.deepEqual(result, {
        response: true,
        oversized: true,
        malformed: true,
      });
    },
  );
  await check(
    "WSS rejects unauthorized, sync-before-open and malformed envelopes",
    async () => {
      const result = await p.evaluate(
        async ({ base, token }) => {
          async function rejected(suffix, bytes) {
            return new Promise((resolve) => {
              const ws = new WebSocket(
                base.replace("https:", "wss:") + "/ws?token=" + suffix,
              );
              const timer = setTimeout(() => {
                ws.close();
                resolve(false);
              }, 4000);
              ws.onopen = () => ws.send(bytes);
              ws.onclose = () => {
                clearTimeout(timer);
                resolve(true);
              };
            });
          }
          return [
            await rejected("bad", new Uint8Array()),
            await rejected(
              token,
              new Uint8Array([1, 2, ...new Uint8Array(16)]),
            ),
            await rejected(token, new Uint8Array([9])),
          ];
        },
        { base: s.base, token: s.token },
      );
      assert.deepEqual(result, [true, true, true]);
    },
  );
  await check(
    "Reconnect churn resets sync state and removes old subscriptions",
    async () => {
      for (let i = 0; i < 6; i++) {
        await p.evaluate(() => {
          c.disconnect();
          return c.connect();
        });
        await p.evaluate(({ id, i }) => c.append(id, `churn-${i}`), { id, i });
        await waitConverged([p, q], [id]);
      }
      const m = await getJSON(`${s.base}/metrics?token=${s.token}`);
      assert.equal(m.overloads, 0);
      assert.equal(m.activeSubscriptions, 3);
    },
  );
  await check(
    "Concurrent offline edits converge after independent reconnects",
    async () => {
      await Promise.all(
        [p, q].map((page, i) =>
          page.evaluate(
            ({ id, i }) => {
              c.disconnect();
              c.append(id, `offline-actor-${i}🦀`);
            },
            { id, i },
          ),
        ),
      );
      await Promise.all([p, q].map((page) => page.evaluate(() => c.connect())));
      const state = await waitConverged([p, q], [id]);
      assert.match(state[0][0].text, /offline-actor-0/);
      assert.match(state[0][0].text, /offline-actor-1/);
    },
  );
  await check(
    "Simultaneous first offline edits share a canonical text object",
    async () => {
      const freshId = randomUUID();
      await Promise.all(
        [p, q].map((page, i) =>
          page.evaluate(
            async ({ freshId, i }) => {
              c.disconnect();
              await c.open(freshId);
              c.append(freshId, `first-${i}🌍`);
            },
            { freshId, i },
          ),
        ),
      );
      await Promise.all([p, q].map((page) => page.evaluate(() => c.connect())));
      const state = await waitConverged([p, q], [freshId]);
      assert.match(state[0][0].text, /first-0/);
      assert.match(state[0][0].text, /first-1/);
    },
  );
  await check(
    "Demo controls create, edit, connect and expose a shareable document",
    async () => {
      const ui = await b.context.newPage();
      await ui.goto(s.base);
      await ui.waitForFunction(() => window.lab?.ready);
      await ui.locator("#create").click();
      await ui.waitForFunction(() => location.hash.length === 37);
      const docId = await ui.evaluate(() => location.hash.slice(1));
      await ui.locator("#token").fill(s.token);
      await ui.locator("#connect").click();
      await ui.waitForFunction(() => lab.client.status === "connected");
      await ui
        .locator("#text")
        .fill("A durable shared note 🌍\nTyped through the demo.");
      await ui.waitForFunction((id) => lab.client.acknowledged(id), docId);
      const fresh = await pageClient(b.context, opts);
      await fresh.evaluate((id) => c.open(id), docId);
      await fresh.waitForFunction(
        (id) => c.text(id).includes("Typed through the demo."),
        docId,
      );
      await ui.screenshot({ path: "results/demo.png" });
      await fresh.close();
      await ui.close();
    },
  );
  await check(
    "Concurrent process cannot open the same data directory",
    async () => {
      await assert.rejects(
        server({ port: 4447, dir: s.data }),
        /Server exited/,
      );
    },
  );

  await writeFile(
    "results/correctness.json",
    JSON.stringify(
      {
        at: new Date().toISOString(),
        browser: b.context.browser()?.version(),
        results,
      },
      null,
      2,
    ),
  );
  console.log(`${results.length} correctness scenarios passed`);
} finally {
  await b.close();
  await s.stop();
  await rm(s.data, { recursive: true, force: true });
}
