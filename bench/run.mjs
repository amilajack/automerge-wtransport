import { browser, pageClient, waitConverged, waitConvergedCompact } from "../scripts/browser.mjs";
import { server, getJSON } from "../scripts/server.mjs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { writeFile, readFile, mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import os from "node:os";
const exec = promisify(execFile);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const env = process.env;
const environment = env.BENCH_ENV ?? "localhost";
const warmup = Number(env.WARMUP_SECONDS ?? 10),
  duration = Number(env.MEASURE_SECONDS ?? 60),
  reps = Number(env.REPETITIONS ?? 3);
const editInterval=Number(env.EDIT_INTERVAL_MS ?? 200);
if(!Number.isFinite(editInterval)||editInterval<100||editInterval>2000)throw Error("Invalid edit interval");
const quick =
  warmup !== 10 ||
  duration !== 60 ||
  Number(env.EXPECTED_REPETITIONS ?? reps) !== 3;
const remote = environment === "remote";
const sizes = (
  env.SIZES ?? (remote ? "65536,1048576" : "65536,1048576,8388608")
)
  .split(",")
  .map(Number);
const counts = (env.CLIENTS ?? (remote ? "2,4,8" : "2,8,32,128"))
  .split(",")
  .map(Number);
if (remote && (Math.max(...sizes) > 1048576 || Math.max(...counts) > 8))
  throw Error("Remote safety limits exceeded");
const suites = (
  env.SUITES ?? "initial,edits,isolation,stress,churn,stalled,oversized"
).split(",");
const output =
  env.OUTPUT ??
  `results/${environment}-${env.NETWORK_PROFILE ?? "baseline"}-${Date.now()}.json`;
await mkdir("results", { recursive: true });
const metadata = {
  at: new Date().toISOString(),
  environment,
  networkProfile: env.NETWORK_PROFILE ?? "baseline",
  sharedConstrainedHost: remote,
  quick,
  warmupSeconds: warmup,
  measurementSeconds: duration,
  repetitions: Number(env.EXPECTED_REPETITIONS ?? reps),
  seed: 49217,
  os: { platform: os.platform(), release: os.release(), arch: os.arch() },
  hardware: {
    cpu: os.cpus()[0]?.model,
    logicalCPUs: os.cpus().length,
    memory: os.totalmem(),
  },
  node: process.version,
  dependencies: JSON.parse(await readFile("package.json", "utf8")),
  serverHost: remote
    ? {
        instance: "i-0a4a44c4ac1e0b42b",
        name: "xwing-coop",
        type: "t4g.micro",
        region: "us-west-1",
        arch: "arm64",
        os: "Ubuntu 24.04",
      }
    : undefined,
  tls: "same ECDSA P-256 certificate; TLS 1.3 for QUIC; WSS rustls; no WebSocket compression",
  latencyClock:
    "performance.timeOrigin + performance.now(), all clients in one browser host",
  applicationBytes:
    "binary envelope bytes, excludes WT length prefix and TLS/QUIC/TCP overhead",
  quota: remote
    ? "MemoryMax=256MiB CPUQuota=25%"
    : (env.SERVER_QUOTA ?? "unrestricted process on shared host"),
};
let interrupted = false,
  activeFailure;
for (const signal of ["SIGINT", "SIGTERM"])
  process.once(signal, () => {
    interrupted = true;
    activeFailure?.(`Interrupted by ${signal}`);
  });
const rows = [];
const save = () =>
  writeFile(output, JSON.stringify({ metadata, rows }, null, 2));
const percentile = (xs, p) =>
  xs.length
    ? xs.toSorted((a, b) => a - b)[
        Math.min(xs.length - 1, Math.ceil(xs.length * p) - 1)
      ]
    : null;
function seeded(n) {
  let x = 49217;
  let a = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    a[i] = 32 + ((x >>> 0) % 95);
  }
  return new TextDecoder().decode(a);
}
async function run(kind, suite, size, n, rep) {
  const row = {
    kind,
    suite,
    size,
    clients: n,
    rep,
    editIntervalMs:editInterval,
    targetDeliveries:n*(n-1)*duration*1000/editInterval,
    status: "failed",
    disconnects: 0,
    convergenceFailures: 0,
    latencyMs: [],
    readyMs: [],
    initialSyncMs: [],
    samples: [],
  };
  let s,
    b,
    pages = [],
    timer;
  let signalFailure;
  const failedTransport = new Promise((resolve) => {
    signalFailure = resolve;
    activeFailure = resolve;
  });
  const pause = (ms) => {
    let timer;
    return Promise.race([
      new Promise((resolve) => {
        timer = setTimeout(resolve, ms);
      }),
      failedTransport.then((message) => {
        throw Error(`Transport disconnected: ${message}`);
      }),
    ]).finally(() => clearTimeout(timer));
  };
  let metricsBefore, metricsFn;
  rows.push(row);
  await save();
  try {
    console.log("START", kind, suite, size, n, rep);
    if (env.RESET_COMMAND)
      await exec("/bin/sh", ["-c", env.RESET_COMMAND], {
        timeout: 120000,
        maxBuffer: 1024 * 1024,
      });
    if (!env.BASE_URL) {
      process.env.SERVER_BINARY ??= "target/release/automerge-wtransport";
      s = await server({ port: Number(env.BENCH_PORT ?? 4446) });
    }
    const base = env.BASE_URL ?? s.base,
      token = env.EXPERIMENT_TOKEN ?? s.token;
    const metrics = (metricsFn = () =>
      getJSON(`${base}/metrics?token=${encodeURIComponent(token)}`));
    b = await browser();
    metadata.browser = b.context.browser()?.version();
    const addPage = async () => {
      const p = await pageClient(b.context, { base, token, kind });
      await p.exposeFunction("__labFailure", (message) =>
        signalFailure(message),
      );
      await p.evaluate(() => {
        c.onchange = () => {
          if (c.status === "disconnected") void window.__labFailure(c.error);
        };
      });
      pages.push(p);
      row.readyMs.push(await p.evaluate(() => window.connectReadyMs));
      metadata.browser = await p.evaluate(() => navigator.userAgent);
      return p;
    };
    const donor = await addPage(),
      id = randomUUID(),
      large = randomUUID(),
      bulkIds = Array.from({length:Number(env.BULK_DOCUMENTS ?? 4)},()=>randomUUID());
    await donor.evaluate(
      async ({ id, text }) => {
        await c.open(id);
        c.setText(id, text);
      },
      { id, text: seeded(size) },
    );
    await waitConverged([donor], [id], 60000);
    console.log("Seed acknowledged", kind, suite, size);
    const browserProcesses = await b.context.browser()?.newBrowserCDPSession();
    const checkBrowserMemory = async () => {
      if (!browserProcesses || os.platform() !== "darwin") return;
      const { processInfo } = await browserProcesses.send(
        "SystemInfo.getProcessInfo",
      );
      const { stdout } = await exec("ps", [
        "-p",
        processInfo.map((p) => p.id).join(","),
        "-o",
        "rss=",
      ]);
      const rss =
        stdout
          .trim()
          .split(/\s+/)
          .map(Number)
          .reduce((a, b) => a + b, 0) * 1024;
      row.browserPeakRSS = Math.max(row.browserPeakRSS ?? 0, rss);
      if (rss > Number(env.MAX_BROWSER_RSS_MB ?? 4096) * 1024 * 1024) {
        row.resourcePressure = true;
        throw Error("Browser RSS memory guard triggered");
      }
    };
    for (let i = 1; i < n; i++) {
      if (i >= 8) await checkBrowserMemory();
      const p = await addPage();
      const t = Date.now();
      await p.evaluate((id) => c.open(id), id);
      await waitConverged([donor, p], [id], 60000);
      (row.setupInitialSyncMs ??= []).push(Date.now() - t);
    }
    await browserProcesses?.detach();
    if (suite === "isolation") {
      const bulkBytes = Number(env.BULK_BYTES ?? (remote ? 1048576 : 8388608));
      const count = Number(env.BULK_DOCUMENTS ?? 1);
      bulkIds.splice(count);
      if(!Number.isInteger(count) || count<1 || count>8 || bulkBytes>1048576 && env.BENCH_ENV==='controlled') throw Error('Invalid controlled bulk limits');
      row.bulkBytesPerDocument=bulkBytes;row.bulkDocuments=bulkIds.length;
      for(const bulk of bulkIds) {
        await donor.evaluate(async({bulk,text})=>{await c.open(bulk);c.setText(bulk,text);},{bulk,text:seeded(bulkBytes)});
        await waitConvergedCompact([donor],[bulk],90000);
      }
    }
    const install = async () =>
      Promise.all(
        pages.map((p, i) =>
          p.evaluate(
            ({ id, i, interval }) => {
              window.events = [];
              window.sent = [];
              window.seen = new Set();
              window.seq = 0;
              window.c.onreceive = (doc, text) => {
                if (doc !== id) return;
                const now = performance.timeOrigin + performance.now();
                for (const m of text.matchAll(/~(\d+):(\d+):([\d.]+)~/g)) {
                  const key = m[0];
                  if (Number(m[1]) !== i && !seen.has(key)) {
                    seen.add(key);
                    events.push({
                      at: now,
                      sent: Number(m[3]),
                      source: Number(m[1]),
                      seq: Number(m[2]),
                    });
                  }
                }
              };
              window.startEdits = () => {
                window.editTimer = setInterval(() => {
                  const at = performance.timeOrigin + performance.now();
                  const seq = window.seq++;
                  c.append(id, `~${i}:${seq}:${at.toFixed(3)}~`);
                  sent.push({ at, seq });
                }, interval);
              };
              window.startEdits();
            },
            { id, i, interval:editInterval },
          ),
        ),
      );
    if (suite !== "initial" && suite !== "oversized" && suite !== "stalled")
      await install();
    await pause(warmup * 1000);
    metricsBefore = await metrics();
    row.metricsBefore = metricsBefore;
    const start = Date.now(),
      end = start + duration * 1000;
    row.measurementStart = start;
    row.measurementEnd = end;
    await save();
    let sampling = false;
    timer = setInterval(async () => {
      if (sampling) return;
      sampling = true;
      try {
        const m = await metrics();
        let process = m.process;
        if (s) {
          const { stdout } = await exec("ps", [
            "-p",
            String(s.child.pid),
            "-o",
            "%cpu=,rss=",
          ]);
          const [cpu, rss] = stdout.trim().split(/\s+/).map(Number);
          process = { cpuPercent: cpu, rssBytes: rss * 1024 };
        }
        row.samples.push({ at: Date.now(), ...m, process });
        if (
          remote &&
          ((process?.rssBytes ?? 0) > 220 * 1024 * 1024 ||
            (process?.cgroupMemory ?? 0) > 235 * 1024 * 1024)
        ) {
          row.resourcePressure = true;
          for (const p of pages)
            void p.evaluate(() => {
              clearInterval(window.editTimer);
              c.disconnect();
            });
        }
      } catch (e) {
        row.sampleError = String(e);
      } finally {
        sampling = false;
      }
    }, 1000);
    if (suite === "initial") {
      // Repeated fresh clients download the same seeded document during the measurement window.
      do {
        const p = await addPage();
        const t = Date.now();
        await p.evaluate((id) => c.open(id), id);
        await waitConverged([donor, p], [id], 60000);
        row.initialSyncMs.push(Date.now() - t);
        await p.close();
        pages = pages.filter((x) => x !== p);
      } while (Date.now() < end);
    } else if (suite === "isolation") {
      row.bulkStart=Date.now();
      const transfer=(async()=>{
        for(const p of pages.slice(1)) for(const bulk of bulkIds) await p.evaluate(id=>c.open(id),bulk);
        await waitConvergedCompact(pages,bulkIds,120000);
        row.bulkEnd=Date.now();row.bulkTransferMs=row.bulkEnd-row.bulkStart;
      })();
      // Attach rejection handling immediately while the measurement timer runs.
      const outcome=transfer.then(()=>null,e=>e);
      await pause(Math.max(0,end-Date.now()));
      const error=await outcome;if(error)throw error;
    } else if (suite === "churn") {
      while (Date.now() < end) {
        await pause(Math.min(2000, end - Date.now()));
        if (Date.now() >= end) break;
        const p = pages[1];
        await p.evaluate(() => {
          c.onchange = () => {};
          c.disconnect();
          return c.connect();
        });
        await p.evaluate(() => {
          c.onchange = () => {
            if (c.status === "disconnected") void window.__labFailure(c.error);
          };
        });
        row.churns = (row.churns ?? 0) + 1;
      }
    } else if (suite === "stalled") {
      // Suspend one real renderer after sync: network stacks continue to receive, JS stops reading/applying.
      const session = await b.context.newCDPSession(pages[1]);
      await session.send("Page.setWebLifecycleState", { state: "frozen" });
      await donor.evaluate(
        ({ id }) => {
          window.editTimer = setInterval(
            () => c.append(id, "x".repeat(16384)),
            200,
          );
        },
        { id },
      );
      await pause(duration * 1000);
      await donor.evaluate(() => clearInterval(window.editTimer));
      await session.send("Page.setWebLifecycleState", { state: "active" });
      row.stallMode = "renderer frozen; transport/OS buffers may still drain";
      await session.detach();
    } else if (suite === "oversized") {
      row.rejection = await donor.evaluate(
        async ({ base, token, kind }) => {
          if (kind === "ws")
            return new Promise((resolve) => {
              const w = new WebSocket(
                base.replace("https:", "wss:") + "/ws?token=" + token,
              );
              const t = setTimeout(() => {
                w.close();
                resolve(false);
              }, 10000);
              w.onopen = () => w.send(new Uint8Array(16 * 1024 * 1024 + 1));
              w.onclose = () => {
                clearTimeout(t);
                resolve(true);
              };
            });
          const cfg = await fetch(base + "/config").then((r) => r.json());
          const w = new WebTransport(base + "/sync?token=" + token, {
            serverCertificateHashes: [
              {
                algorithm: "sha-256",
                value: Uint8Array.from(cfg.certificateHash.match(/../g), (x) =>
                  parseInt(x, 16),
                ),
              },
            ],
          });
          await w.ready;
          const stream = await w.createBidirectionalStream(),
            writer = stream.writable.getWriter(),
            reader = stream.readable.getReader();
          const h = new Uint8Array(4);
          new DataView(h.buffer).setUint32(0, cfg.maxFrame + 1);
          await writer.write(h);
          try {
            await Promise.race([
              reader.read(),
              new Promise((_, rej) =>
                setTimeout(() => rej(Error("timeout")), 10000),
              ),
            ]);
            return true;
          } catch (e) {
            return !String(e).includes("timeout");
          } finally {
            w.close();
          }
        },
        { base, token, kind },
      );
      if (!row.rejection) throw Error("Oversized frame not rejected");
      await pause(Math.max(0, end - Date.now()));
    } else await pause(duration * 1000);
    await Promise.all(
      pages.map((p) => p.evaluate(() => clearInterval(window.editTimer))),
    );
    await Promise.race([
      waitConvergedCompact(pages,[id,...(suite==='isolation'?bulkIds:[])],remote?60000:30000),
      failedTransport.then(message=>{throw Error('Transport disconnected: '+message)})
    ]);
    const measured = await Promise.all(
      pages.map((p) =>
        p.evaluate(() => ({
          events: window.events ?? [],
          sent: window.sent ?? [],
          status: c.status,
          error: c.error,
          bytesIn: c.transport?.bytesIn ?? 0,
          bytesOut: c.transport?.bytesOut ?? 0,
        })),
      ),
    );
    row.disconnects = measured.filter((x) => x.status !== "connected").length;
    row.latencyMs = measured.flatMap((x) =>
      x.events
        .filter((e) => e.sent >= start && e.sent < end)
        .map((e) => e.at - e.sent),
    );
    row.edits = measured.reduce(
      (n, x) => n + x.sent.filter((e) => e.at >= start && e.at < end).length,
      0,
    );
    row.deliveries=measured.flatMap((x,receiver)=>x.events.filter(e=>e.sent>=start&&e.sent<end).map(e=>({receiver,source:e.source,sequence:e.seq,sent:e.sent,received:e.at,latency:e.at-e.sent})));
    row.expectedDeliveries=row.edits*(n-1);
    row.observedDeliveries=row.latencyMs.length;
    row.deliveryRatio=row.expectedDeliveries ? row.observedDeliveries/row.expectedDeliveries : null;
    row.deadlinesMs=Object.fromEntries([200,500,1000,5000].map(deadline=>[deadline,{
      delivered:row.latencyMs.filter(v=>v<=deadline).length, expected:row.expectedDeliveries,
      fraction:row.expectedDeliveries ? row.latencyMs.filter(v=>v<=deadline).length/row.expectedDeliveries : null
    }]));
    if(suite==='isolation') {
      row.bulkLatencyMs=measured.flatMap(x=>x.events.filter(e=>e.sent>=row.bulkStart&&e.sent<Math.min(end,row.bulkEnd)).map(e=>e.at-e.sent));
      row.bulkP95=percentile(row.bulkLatencyMs,.95);
    }
    if(row.expectedDeliveries && row.observedDeliveries!==row.expectedDeliveries) throw Error('Missing or duplicate edit delivery observations');
    row.applicationBytes = measured.reduce(
      (n, x) => n + x.bytesIn + x.bytesOut,
      0,
    );
    row.metricsAfter = await metrics();
    row.serverApplicationBytes =
      row.metricsAfter.inboundBytes +
      row.metricsAfter.outboundBytes -
      (metricsBefore.inboundBytes + metricsBefore.outboundBytes);
    row.throughputBytesPerSecond = row.serverApplicationBytes / duration;
    row.editThroughput = row.edits / duration;
    row.p50 = percentile(row.latencyMs, 0.5);
    row.p95 = percentile(row.latencyMs, 0.95);
    row.p99 = percentile(row.latencyMs, 0.99);
    row.status = "passed";
    row.convergence = "passed";
    if (row.resourcePressure) throw Error("Resource pressure guard triggered");
  } catch (e) {
    row.status = "failed";
    row.error = String(e);
    row.convergenceFailures = /Convergence/.test(String(e)) ? 1 : 0;
    console.error("FAIL", kind, suite, size, n, row.error);
    await save();
  } finally {
    clearInterval(timer);
    activeFailure = undefined;
    if (row.status !== "passed" && pages.length) {
      const observations = await Promise.allSettled(
        pages.map((p) =>
          p.evaluate(() => ({
            status: c.status,
            error: c.error,
            events: window.events ?? [],
            sent: window.sent ?? [],
          })),
        ),
      );
      const obs = observations
        .filter((x) => x.status === "fulfilled")
        .map((x) => x.value);
      row.disconnects = obs.filter((x) => x.status !== "connected").length;
      row.clientErrors = obs.map((x) => x.error);
      row.latencyMs = obs.flatMap((x) =>
        x.events
          .filter(
            (e) =>
              e.sent >= row.measurementStart && e.sent < row.measurementEnd,
          )
          .map((e) => e.at - e.sent),
      );
      row.deliveries=obs.flatMap((x,receiver)=>x.events.filter(e=>e.sent>=row.measurementStart&&e.sent<row.measurementEnd).map(e=>({receiver,source:e.source,sequence:e.seq,sent:e.sent,received:e.at,latency:e.at-e.sent})));
      row.edits=obs.reduce((sum,x)=>sum+x.sent.filter(e=>e.at>=row.measurementStart&&e.at<row.measurementEnd).length,0);
      row.expectedDeliveries=row.edits*(n-1);row.observedDeliveries=row.latencyMs.length;
      row.deliveryRatio=row.expectedDeliveries?row.observedDeliveries/row.expectedDeliveries:null;
      row.deadlinesMs=Object.fromEntries([200,500,1000,5000].map(deadline=>[deadline,{delivered:row.latencyMs.filter(v=>v<=deadline).length,expected:row.expectedDeliveries,fraction:row.expectedDeliveries?row.latencyMs.filter(v=>v<=deadline).length/row.expectedDeliveries:null}]));
      row.p50 = percentile(row.latencyMs, 0.5);
      row.p95 = percentile(row.latencyMs, 0.95);
      row.p99 = percentile(row.latencyMs, 0.99);
      if (metricsFn)
        row.metricsAfter = await metricsFn().catch((e) => ({
          error: String(e),
        }));
    }
    await save();
    if (b)
      await b.close().catch((e) => {
        row.cleanupError = String(e);
      });
    if (s) await s.stop();
  }

  row.deadlineCoverage=Object.fromEntries([200,500,1000,5000].map(deadline=>[deadline,{
    delivered:(row.latencyMs??[]).filter(v=>v<=deadline).length,target:row.targetDeliveries,
    fraction:row.targetDeliveries?Math.min(1,(row.latencyMs??[]).filter(v=>v<=deadline).length/row.targetDeliveries):null
  }]));
  await save();
  console.log(
    JSON.stringify({
      kind,
      suite,
      size,
      clients: n,
      rep,
      status: row.status,
      p50: row.p50,
      p95: row.p95,
      initialMs: row.initialSyncMs,
    }),
  );
  return row;
}
for (const suite of suites) {
  let stop = false;
  const cases =
    suite === "initial"
      ? sizes.map((s) => [s, 2])
      : suite === "stress"
        ? counts.map((n) => [65536, n])
        : [[65536, 2]];
  for (const [size, n] of cases) {
    if (stop) break;
    let failed = false;
    for (let ri = 0; ri < reps; ri++) {
      const rep = ri + Number(env.REPETITION_INDEX ?? 0);
      const order = env.KINDS
        ? env.KINDS.split(",")
        : rep % 2
          ? ["ws", "wt"]
          : ["wt", "ws"];
      for (const kind of order) {
        const row = await run(kind, suite, size, n, rep);
        if (interrupted) {
          await save();
          process.exit(130);
        }
        if (row.status !== "passed") {
          failed = true;
          if (remote) process.exitCode = 1;
        }
        if (row.resourcePressure) {
          await save();
          console.error("Stopping on resource pressure");
          process.exit(1);
        }
      }
    }
    stop = failed;
  }
}
await save();
console.log("Saved", output);
process.exit(process.exitCode ?? 0);
