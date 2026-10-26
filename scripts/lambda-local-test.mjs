import http from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";
import { server } from "./server.mjs";
const transport = process.env.BENCH_TRANSPORT ?? "webtransport";
const exec = promisify(execFile),
  s = await server({ port: 4448 }),
  id = randomUUID();
const responses = new Map(),
  containers = [],
  apis = [];
let finish;
const done = new Promise((resolve) => (finish = resolve));
try {
  for (const role of ["responder", "source"]) {
    let delivered = false;
    const api = http.createServer((req, res) => {
      if (req.url.endsWith("/next")) {
        if (delivered) return;
        delivered = true;
        res.writeHead(200, {
          "content-type": "application/json",
          "lambda-runtime-aws-request-id": role,
          "lambda-runtime-deadline-ms": String(Date.now() + 150000),
          "lambda-runtime-invoked-function-arn":
            "arn:aws:lambda:us-west-1:123456789012:function:local-test",
        });
        res.end(
          JSON.stringify({
            role,
            transport,
            document: id,
            samples: 3,
            warmup: 1,
            payload_bytes: 64,
            interval_ms: 10,
          }),
        );
      } else {
        let b = "";
        req.on("data", (c) => (b += c));
        req.on("end", () => {
          responses.set(role, { path: req.url, payload: JSON.parse(b) });
          res.end();
          if (responses.size === 2) finish();
        });
      }
    });
    apis.push(api);
    await new Promise((r) => api.listen(0, "0.0.0.0", r));
    const port = api.address().port;
    const name = "amwt-lambda-local-" + role + "-" + id;
    containers.push(name);
    void exec(
      "docker",
      [
        "run",
        "--rm",
        "--name",
        name,
        "-e",
        `AWS_LAMBDA_RUNTIME_API=host.docker.internal:${port}`,
        "-e",
        "AWS_LAMBDA_FUNCTION_NAME=local-test",
        "-e",
        "AWS_LAMBDA_FUNCTION_MEMORY_SIZE=256",
        "-e",
        "AWS_LAMBDA_FUNCTION_VERSION=$LATEST",
        "-e",
        `AWS_REGION=local-${role}`,
        "-e",
        "RELAY_URL=https://host.docker.internal:4448",
        "-e",
        `EXPERIMENT_TOKEN=${s.token}`,
        "-e",
        `CERTIFICATE_HASH=${(await readFile("certs/hash.hex", "utf8")).trim()}`,
        "automerge-wtransport:lambda",
      ],
      { timeout: 150000 },
    ).catch((e) => {
      if (!responses.has(role)) {
        console.error(role, e.stderr || e.message);
        finish();
      }
    });
  }
  let timer;
  await Promise.race([
    done,
    new Promise(
      (_, rej) =>
        (timer = setTimeout(
          () => rej(Error("local Lambda runtime deadline")),
          140000,
        )),
    ),
  ]).finally(() => clearTimeout(timer));
  assert.equal(responses.size, 2);
  const source = responses.get("source"),
    responder = responses.get("responder");
  assert.ok(source.path.endsWith("/response"), JSON.stringify(source));
  assert.ok(responder.path.endsWith("/response"), JSON.stringify(responder));
  assert.equal(source.payload.rtt_ms.length, 3);
  assert.equal(responder.payload.messages_received, 4);
  assert.deepEqual(source.payload.heads, responder.payload.heads);
  assert.equal(source.payload.acknowledged, true);
  await writeFile(
    `results/lambda-local-${transport}.json`,
    JSON.stringify(Object.fromEntries(responses), null, 2),
  );
  console.log(
    `PASS ${transport} static ARM64 Lambda runtime, request/reply RTT and converged heads`,
  );
} finally {
  for (const name of containers)
    await exec("docker", ["rm", "-f", name]).catch(() => {});
  for (const api of apis) {
    api.closeAllConnections();
    api.close();
  }
  await s.stop();
}
