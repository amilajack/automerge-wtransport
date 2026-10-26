# Automerge transport experiment results

This historical report retains the earlier measurements. Its archival `results/` files are local outputs and are not bundled in Git. See the [published benchmark results](README.md#measured-results) for the versioned charts, observations and current methodology.

The [new worldwide WebTransport/WebSocket comparison](docs/benchmarks/global-comparison/REPORT.md) uses explicit TCP_NODELAY on both WebSocket endpoints. The historical browser measurements below used the server’s default TCP settings, which could add delay to small WebSocket messages; interpret their transport differences with that configuration in mind. Their original measured source and results remain preserved.

Generated from the JSON files linked below. “Full” means three configured repetitions with ten seconds of warmup and sixty seconds of measurement. Disconnects stop failed attempts early. “Quick” means shorter functional coverage; those observations do not carry the same weight as full timing runs.

The full-duration measurements cover the edit suite on localhost, three controlled Linux network profiles, and the shared constrained remote host. Other full-duration suite/environment combinations have not been run. The harness and documented commands support them.

## Correctness

Eleven real-Chromium scenarios, six Rust tests, the incremental browser-frame decoder test, TypeScript compilation, and Clippy passed. This includes mixed transports, Unicode and offline edits, simultaneous first edits from a common schema, independent actors, abrupt restart and direct snapshot-head/content checks, malformed and oversized frames, inbound/outbound overloads, failed persistence, and the live demo controls. See correctness evidence (`results/correctness.json`, local archive) and demo screenshot (`results/demo.png`, local archive).

## Observations

| Environment / profile | Mode | Suite | Document | Clients | WebTransport | WSS |
|---|---|---|---:|---:|---|---|
| localhost / baseline | quick | initial | 64 KiB | 2 | 1/1 converged; initial 111.0 ms | 1/1 converged; initial 110.0 ms |
| localhost / baseline | quick | initial | 1024 KiB | 2 | 1/1 converged; initial 1477.5 ms | 1/1 converged; initial 1414.5 ms |
| localhost / baseline | quick | initial | 8192 KiB | 2 | 1/1 converged; initial 13985.0 ms | 1/1 converged; initial 13915.0 ms |
| localhost / baseline | quick | isolation | 64 KiB | 2 | 0/1 converged | 1/1 converged; p95 10641.1 ms |
| localhost / baseline | quick | churn | 64 KiB | 2 | 1/1 converged; p95 72.3 ms | 1/1 converged; p95 72.1 ms |
| localhost / baseline | quick | stalled | 64 KiB | 2 | 1/1 converged | 1/1 converged |
| localhost / baseline | quick | oversized | 64 KiB | 2 | 1/1 converged | 1/1 converged |
| localhost / baseline | quick | stress | 64 KiB | 2 | 1/1 converged; p95 71.7 ms | 1/1 converged; p95 69.7 ms |
| localhost / baseline | quick | stress | 64 KiB | 8 | 0/1 converged | 0/1 converged |
| localhost / baseline | full | edits | 64 KiB | 2 | 3/3 converged; p95 76.7 ms | 3/3 converged; p95 76.2 ms |
| controlled / baseline | full | edits | 64 KiB | 2 | 3/3 converged; p95 76.3 ms | 3/3 converged; p95 116.3 ms |
| controlled / rtt50-loss01-20mbit | full | edits | 64 KiB | 2 | 3/3 converged; p95 144.2 ms | 3/3 converged; p95 237.9 ms |
| controlled / rtt150-loss1-5mbit | full | edits | 64 KiB | 2 | 0/3 converged | 0/3 converged |
| remote / baseline | full | edits | 64 KiB | 2 | 0/3 converged | 0/3 converged |

Percentiles in the table are medians of successful per-repetition p95 values, not pooled percentiles. Failed attempts are excluded from latency summaries and retained in counts and raw observations. A failed attempt can have partial latency observations; those are not a success result.

The localhost short stress test exhausted queues at eight clients; 32 and 128 clients were therefore skipped. The short 8 MiB isolation case experienced long small-document propagation delays and a WT disconnect. Browser CRDT application, full snapshots, CPU, and storage are shared costs, so these measurements do not isolate a transport-only effect.

Remote results are from an existing Ubuntu ARM64 t4g.micro running game services, with only 25% of one CPU and 256 MiB allowed for this experiment (queue capacity 4, versus 8 on localhost and controlled Linux). Ingress was limited to the benchmark Mac’s public IPv4. Management used SSM; game services on port 443 were preserved. Final status and stop evidence are in remote status (`results/remote-status-final.txt`, local archive) and stop log (`results/remote-stop.txt`, local archive). The experiment is stopped after measurements; snapshots remain.

## Artifacts and interpretation

- Edit comparison chart (`results/edit-latency.svg`, local archive) and initial-sync chart (`results/initial-sync.svg`, local archive) are standalone SVGs. Asterisks mark quick checks.
- Comparison CSV (`results/comparison.csv`, local archive), all edit observations (`results/latencies.csv`, local archive), resource samples (`results/resources.csv`, local archive), and readiness/transfer durations (`results/durations.csv`, local archive).
- Toolchain and host (`results/toolchain.txt`, local archive), artifact checksums (`results/artifacts.sha256`, local archive), and complete dependency lockfiles in this repository.

All propagation clocks are on the same client host. CPU/RSS methods and quotas differ across environments; compare transports within the same environment. Reported sync-byte throughput is offered envelope traffic per nominal measurement second, not wire throughput. Initial-transfer loops can finish after the nominal window, so use transfer durations and document sizes for useful initial-download throughput. No advance assumption or general conclusion that WebTransport wins is made.

The stalled scenario freezes a renderer; Chromium networking and OS buffers can still receive. A deterministic Rust test separately forces an unread outbound queue to fill. The benchmark therefore does not claim that renderer freezing is a TCP socket that has stopped reading.

WSS uses Axum-server’s default TCP acceptor, without explicitly enabling TCP_NODELAY. The approximately 40 ms baseline Linux tail difference may reflect Nagle/delayed-ACK behavior; this is an inference, not a measured packet-level cause. These results compare the current implementations rather than optimized transport limits.

The measured source archive (`results/measured-source.tgz`, local archive), measured macOS server (`results/measured-server-macos`, local archive), and checksums (`results/artifacts.sha256`, local archive) preserve the measured server implementation and a harness snapshot. Final verification subsequently fixed a cancelled-OPEN cleanup race that could count a closed queue as overload; the core Automerge/persistence path is unchanged. The browser harness bounds shutdown and records any cleanup timeouts.

## Sources

- localhost-coverage.json (`results/localhost-coverage.json`, local archive)
- localhost-edits.json (`results/localhost-edits.json`, local archive)
- controlled-combined.json (`results/controlled-combined.json`, local archive)
- remote-edits.json (`results/remote-edits.json`, local archive)
