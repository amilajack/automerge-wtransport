import { browser, pageClient, waitConverged } from "./browser.mjs";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
const base = process.env.BASE_URL ?? "https://52.9.189.226:4433";
const token =
  process.env.EXPERIMENT_TOKEN ??
  (await readFile("certs/token", "utf8")).trim();
const b = await browser();
try {
  const p = await pageClient(b.context, { base, token, kind: "wt" });
  const q = await pageClient(b.context, { base, token, kind: "ws" });
  const id = randomUUID();
  await Promise.all([p, q].map((p) => p.evaluate((id) => c.open(id), id)));
  await p.evaluate(
    (id) => c.setText(id, "Final ARM64 mixed-transport check 🌍"),
    id,
  );
  const state = await waitConverged([p, q], [id]);
  await writeFile(
    "results/remote-smoke-final.json",
    JSON.stringify(
      { at: new Date().toISOString(), base, id, passed: true, clients: state },
      null,
      2,
    ),
  );
  console.log("PASS final ARM64 WebTransport ↔ WSS convergence");
} finally {
  await b.close();
}
