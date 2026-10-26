import ts from "typescript";
import { readFile } from "node:fs/promises";
import assert from "node:assert/strict";
const js = ts.transpileModule(await readFile("web/protocol.ts", "utf8"), {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;
const { encode, decode, frame, Decoder } = await import(
  "data:text/javascript;base64," + Buffer.from(js).toString("base64")
);
const id = "12345678-1234-1234-1234-123456789abc",
  b = encode(2, id, new Uint8Array([7, 8, 9])),
  f = frame(b);
const d = new Decoder(1024),
  decoded = [];
for (const byte of f) decoded.push(...d.push(new Uint8Array([byte])));
d.end();
assert.equal(decoded.length, 1);
assert.equal(decode(decoded[0], 1024).id, id);
const combined = new Uint8Array(f.length * 3);
combined.set(f);
combined.set(f, f.length);
combined.set(f, f.length * 2);
assert.equal(new Decoder(1024).push(combined).length, 3);
for (let i = 1; i < f.length; i++) {
  const d = new Decoder(1024);
  d.push(f.slice(0, i));
  assert.throws(() => d.end());
}
const huge = new Uint8Array(4);
new DataView(huge.buffer).setUint32(0, 1025);
assert.throws(() => new Decoder(1024).push(huge));
assert.throws(() => decode(new Uint8Array([1, 2]), 1024));
assert.throws(() => encode(1, "not-uuid"));
console.log(
  "PASS incremental decoder: fragmentation, coalescing, truncated and oversized frames",
);
