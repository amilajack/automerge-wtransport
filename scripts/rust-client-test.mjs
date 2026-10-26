import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { server } from "./server.mjs";
import { browser, pageClient, waitConverged } from "./browser.mjs";
const exec = promisify(execFile),
  s = await server({ port: 4448 }),
  b = await browser();
const id = randomUUID(),
  snapshot = s.data + "/native-client.automerge";
const env = { ...process.env, EXPERIMENT_TOKEN: s.token };
async function cli(args) {
  const { stdout } = await exec(
    "target/debug/wt-client",
    ["--url", s.base, "--cert-hash", "certs/hash.hex", "--doc", id, ...args],
    { env, timeout: 15000 },
  );
  return JSON.parse(stdout);
}
try {
  const first = await cli(["--set", "Native Rust 🌍", "--snapshot", snapshot]);
  assert.equal(first.acknowledged, true);
  const p = await pageClient(b.context, {
    base: s.base,
    token: s.token,
    kind: "ws",
  });
  await p.evaluate((id) => c.open(id), id);
  await p.waitForFunction((id) => c.text(id) === "Native Rust 🌍", id);
  await p.evaluate((id) => c.append(id, " + browser 你好"), id);
  await waitConverged([p], [id]);
  const resumed = await cli([
    "--append",
    " + resumed 🦀",
    "--snapshot",
    snapshot,
  ]);
  assert.match(resumed.text, /browser 你好/);
  assert.match(resumed.text, /resumed 🦀/);
  await p.waitForFunction(({ id, text }) => c.text(id) === text, {
    id,
    text: resumed.text,
  });
  const watcher = spawn(
    "target/debug/wt-client",
    ["--url", s.base, "--cert-hash", "certs/hash.hex", "--doc", id, "--watch"],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  let output = "";
  watcher.stdout.on("data", (c) => (output += c));
  let errors = "";
  watcher.stderr.on("data", (c) => (errors += c));
  const exited = new Promise((resolve) =>
    watcher.once("exit", (code) => resolve(code)),
  );
  async function waitFor(check) {
    const start = Date.now();
    while (Date.now() - start < 10000) {
      if (check()) return;
      await new Promise((r) => setTimeout(r, 25));
    }
    throw Error("Watch timeout " + errors);
  }
  try {
    await waitFor(() => output.includes("\n"));
    await p.evaluate((id) => c.append(id, " watch-update"), id);
    await waitFor(() => output.includes("watch-update"));
    watcher.kill("SIGINT");
    assert.equal(
      await Promise.race([
        exited,
        new Promise((_, rej) =>
          setTimeout(() => rej(Error("watch did not exit")), 5000),
        ),
      ]),
      0,
    );
  } finally {
    if (watcher.exitCode === null) watcher.kill("SIGKILL");
  }
  await writeFile(
    "results/rust-client.json",
    JSON.stringify(
      { passed: true, initial: first, resumed, watch: true },
      null,
      2,
    ),
  );
  console.log(
    "PASS Rust CLI ↔ Chromium WSS, Unicode, snapshot resume and watch/interrupt",
  );
} finally {
  await b.close();
  await s.stop();
}
