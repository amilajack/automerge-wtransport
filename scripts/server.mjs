import { spawn } from "node:child_process";
import { mkdtemp, rm, open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import https from "node:https";
export function getJSON(url) {
  return new Promise((res, rej) => {
    https
      .get(url, { rejectUnauthorized: false, timeout: 10000 }, (r) => {
        let b = "";
        r.on("data", (c) => (b += c));
        r.on("end", () => {
          try {
            if (r.statusCode !== 200) throw Error(`HTTP ${r.statusCode}`);
            res(JSON.parse(b));
          } catch (e) {
            rej(e);
          }
        });
      })
      .on("error", rej)
      .on("timeout", function () {
        this.destroy(Error("HTTP request timed out"));
      });
  });
}
export async function server({ port = 4444, dir, env = {} } = {}) {
  const data = dir ?? (await mkdtemp(join(tmpdir(), "automerge-wt-data-")));
  const token = process.env.EXPERIMENT_TOKEN ?? "local-experiment-token";
  const log = await open(`results/server-${port}.log`, "a");
  const child = spawn(
    process.env.SERVER_BINARY ?? "target/debug/automerge-wtransport",
    [],
    {
      env: {
        ...process.env,
        DATA_DIR: data,
        EXPERIMENT_TOKEN: token,
        PORT: String(port),
        ...env,
      },
      stdio: ["ignore", log.fd, log.fd],
    },
  );
  await new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", reject);
  });
  const base = `https://localhost:${port}`;
  try {
    for (let i = 0; i < 100; i++) {
      if (child.exitCode !== null)
        throw Error(`Server exited ${child.exitCode}`);
      try {
        const cfg = await getJSON(`${base}/config`);
        if (cfg.serverPid && cfg.serverPid !== child.pid)
          throw Error("Port belongs to a different server");
        return {
          child,
          data,
          base,
          token,
          async stop({ preserve = false } = {}) {
            if (child.exitCode === null && child.signalCode === null) {
              const done = new Promise((r) => child.once("exit", r));
              child.kill("SIGKILL");
              await done;
            }
            await log.close();
            if (!preserve && !dir)
              await rm(data, { recursive: true, force: true });
          },
        };
      } catch {}
      await new Promise((r) => setTimeout(r, 100));
    }
    throw Error("Server readiness timeout");
  } catch (e) {
    child.kill("SIGKILL");
    await log.close();
    throw e;
  }
}
