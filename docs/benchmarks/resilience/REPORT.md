# WebTransport vs WebSocket on unreliable links

48/48 planned attempts completed: **42 converged, 6 failed**. 3 repetitions, configured 10 seconds warmup and 60 seconds measurement. Failed attempts stop early.

| Profile | Workload | Transport | Converged | Successful-run p95 ms | ≤500 ms target coverage | Bulk p95 ms | Bulk duration ms | Fresh recovery ms |
|---|---|---|---:|---:|---:|---:|---:|---:|
| Baseline | Edits · 5/s per client | wt | 3/3 | 139.5 | 97.5% | — | — | — |
| Baseline | Edits · 5/s per client | ws | 3/3 | 115.6 | 99.8% | — | — | — |
| Baseline | Control 1/s + 4 bulk documents | wt | 3/3 | 86.2 | 100.0% | 353.2 | 1325.0 | — |
| Baseline | Control 1/s + 4 bulk documents | ws | 3/3 | 87.2 | 99.7% | 490.7 | 1463.0 | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Edits · 5/s per client | wt | 3/3 | 277.5 | 100.0% | — | — | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Edits · 5/s per client | ws | 3/3 | 291.1 | 99.7% | — | — | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Control 1/s + 4 bulk documents | wt | 3/3 | 315.7 | 97.2% | 1169.1 | 2846.0 | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Control 1/s + 4 bulk documents | ws | 3/3 | 3133.6 | 85.8% | 6086.2 | 7737.0 | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Three-second blackout · control 1/s | wt | 3/3 | 1433.9 | 92.5% | — | — | 1079.4 |
| 1% random loss · 100 ms RTT · 5 Mbps | Three-second blackout · control 1/s | ws | 3/3 | 2418.8 | 90.8% | — | — | 2070.9 |
| ≈3% burst loss · 150 ms RTT · 1 Mbps | Edits · 5/s per client | wt | 0/3 | — | 0.1% | — | — | — |
| ≈3% burst loss · 150 ms RTT · 1 Mbps | Edits · 5/s per client | ws | 0/3 | — | 15.1% | — | — | — |
| ≈3% burst loss · 150 ms RTT · 1 Mbps | Control 1/s + 4 bulk documents | wt | 3/3 | 1608.4 | 81.7% | 4148.0 | 8621.0 | — |
| ≈3% burst loss · 150 ms RTT · 1 Mbps | Control 1/s + 4 bulk documents | ws | 3/3 | 3759.0 | 78.9% | 6890.5 | 10794.0 | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Repeated reconnects · control 1/s | wt | 3/3 | 686.9 | 86.7% | — | — | — |
| 1% random loss · 100 ms RTT · 5 Mbps | Repeated reconnects · control 1/s | ws | 3/3 | 668.4 | 88.6% | — | — | — |

Failure diagnostics (attempts): document_queue_full: 4, stream_cancelled_or_overloaded: 2. The precise cause of stream cancellation is not distinguished by the retained logs. Raw logs remain linked in comparison.json.


## What this tests

Two real Chromium clients exchange concurrent Automerge edits through the Rust durable sync server. Latency measures a sender edit until observation by the other browser, with both clocks on the same browser host. The worldwide Lambda suite measures request/reply RTT; compare transports within each suite. The isolated Linux router impairs TCP and UDP equally in both directions; the Mac host network is unchanged. GRO/GSO/TSO are disabled on routed interfaces to avoid comparing large offloaded TCP packets against individual UDP packets. Server quota is one CPU / 1 GiB; browser clients have four CPUs / 5 GiB. Both transports use identical TLS certificates, 64-message queue limits, snapshots, edit schedules and data. WSS has TCP_NODELAY and no compression. The server and its data directory are recreated for every attempt; transport order reverses each repetition.

The edits workload retains five edits per client per second. The isolation workload uses one edit per client per second while four independent 256 KiB documents (1 MiB total seeded text) download on the same transport connection. WT uses independent document streams; WSS multiplexes them into one ordered connection. Low-rate control probes help distinguish network blocking from saturation of application queues. Bulk latency includes only control edits generated while the bulk transfers are active. Bulk-window sample counts are exported; at one probe per second, their tail percentiles can be coarse. All successful attempts check equal heads, equal contents, acknowledgements and the expected delivery count.

The outage workload adds a target three-second bidirectional packet blackout to the 1% loss profile. Actual start/end times and router counters are recorded, including command scheduling overhead. Fresh recovery is the time from the recorded restore point until both clients observe a newly generated post-restore edit; It includes waiting until the next scheduled one-second probe. Connections survive and recover or the attempt fails; no automatic reconnect is added to the blackout workload. The separate reconnect workload deliberately disconnects and reconnects one client approximately every two seconds while both clients keep editing; local documents remain in memory and sync state resets on reconnect.

## Read the metrics together

Latency summaries are medians of successful per-repetition percentiles; failed attempts are excluded from those percentiles and remain visible in completion counts and raw data. A low successful latency does not imply an available service. Deadline coverage includes every attempt and divides timely deliveries by the nominal full-window edit schedule. Generation stalls, disconnects, missing deliveries and late observations count against coverage. Generated edit counts and delivery fractions among generated edits are separately retained. An attempt that fails before measurement has zero target coverage.

Independent streams can avoid ordered-delivery blocking across streams, but still share congestion control and application resources ([QUIC RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html), [WebTransport specification](https://www.w3.org/TR/webtransport/)). Reliable Automerge sync still requires delivery and retransmission; this experiment does not send CRDT sync over unreliable datagrams. Datagram freshness and IP migration are outside this application suite.

These are application-level observations, including browser CRDT processing, full snapshot fsync and configured write timeouts. Queue exhaustion or a stream reset is an implementation limit, not proof of an intrinsic transport limit. The severe-loss preflight failures remain in separate diagnostic results. Netem uses fresh random loss realizations: configured distributions match, but the lost packet sequences cannot be identical across protocols. Three repetitions are descriptive evidence rather than a statistical significance test. The burst model has 1% transition to bad state, 25% transition to good, 75% loss in bad and 0.1% in good (approximately 2.98% long-run directional loss). Each impaired interface has a 256-packet queue limit; bandwidth shaping can also cause queue drops. Jitter is independently applied in each direction.

## Artifacts

- [Interactive comparison (open locally)](report.html)
- [All attempt outcomes](attempts.csv)
- [All edit observations, including partial failures](deliveries.csv)
- [Server resource and queue samples](resources.csv)
- [Comparison CSV](comparison.csv)
- [Full results, events, netem counters and resource samples](results.json)
- [Per-attempt server logs](raw/)
- [Outcome and measured-source verification](verification.json)
- [Container/network cleanup](cleanup.json)
- [Router offload settings](router-offloads.txt)
- [Worldwide AWS comparison on unmodified networks](../global-comparison/REPORT.md)

Reproduce with `docker compose -f bench/compose.yml build` then `python3 bench/resilience.py` and `python3 bench/resilience-report.py --plots`. See [benchmark instructions](../../../bench/RESILIENCE.md).

The published bundle includes summaries, observations and verification evidence. Private infrastructure state, runtime credentials, binary snapshots and duplicate raw invocation files remain excluded.
