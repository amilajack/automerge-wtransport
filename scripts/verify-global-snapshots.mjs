import assert from "node:assert/strict";
import * as A from "@automerge/automerge";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const documents = JSON.parse(input);
const scalar = value => {
  assert.ok(A.isImmutableString(value), "expected a Rust scalar string");
  return value.toString();
};
for (const item of documents) {
  const doc = A.load(Buffer.from(item.snapshot, "base64"));
  assert.deepEqual(A.getHeads(doc).sort(), item.heads.toSorted(), item.document);
  assert.equal(scalar(doc.done), "yes", item.document);
  assert.equal(scalar(doc.finished), "yes", item.document);
  assert.equal(scalar(doc.request), scalar(doc.response), item.document);
  assert.equal(scalar(doc.request), item.expected_marker, item.document);
  A.free(doc);
}
console.log(JSON.stringify({
  passed: true,
  documents: documents.length,
  verified_with: "@automerge/automerge 3.2.0",
  checks: ["snapshot heads match both endpoints", "done/finished persisted", "request and response match"],
}, null, 2));
