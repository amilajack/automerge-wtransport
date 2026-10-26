// A common initial change makes simultaneous first edits address the SAME text object.
// Keep this snapshot stable: replacing it changes the document schema history.
import * as A from "@automerge/automerge";
import { writeFile } from "node:fs/promises";
const doc = A.change(
  A.init({ actor: "00000000000000000000000000000001" }),
  { time: 0 },
  (d) => {
    d.text = "";
  },
);
const bytes = A.save(doc);
await writeFile("schema.automerge", bytes);
await writeFile(
  "web/schema.json",
  JSON.stringify(Buffer.from(bytes).toString("base64")) + "\n",
);
console.log(A.getHeads(doc));
