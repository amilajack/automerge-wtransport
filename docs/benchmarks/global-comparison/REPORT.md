# Worldwide WebTransport vs WebSocket

17 enabled AWS regions, 289 directed region pairs, 3 repetitions per transport. **289 pairs have all repetitions converged on both transports.**

| Transport | Passed attempts | Failed | RTT samples | Pooled median ms | Pooled p95 ms | Median connection ms |
|---|---:|---:|---:|---:|---:|---:|
| webtransport | 867/867 | 0 | 8670 | 259.61 | 406.82 | 408.88 |
| websocket | 867/867 | 0 | 8670 | 259.53 | 403.66 | 407.96 |

Pooled percentiles combine different geographic paths; use the region-pair table for a specific path. Connection readiness excludes Lambda invocation startup and is not part of message RTT.

Among complete pairs: WebTransport median is >5% lower in 0, WebSocket median is >5% lower in 1, and 288 are within ±5%. These are descriptive thresholds, not statistical significance tests.

## Method

Fresh measurements for both transports on the same t4g.small relay in us-west-1; 256 MiB ARM64 Lambda clients. Each attempt has 2 warmups, 10 measured request/reply exchanges, 64-byte bodies plus marker/envelope metadata, and 100 ms between exchanges. At most 4 region-pair groups run concurrently. Transports run in separate phases, reversing phase order each repetition, with the same seeded pair schedule. Before each phase the relay restarts with a fresh data directory, retaining previous snapshots. Each phase has 289 documents, below the resident worker cap.

Both transports share the exact Rust Automerge sync engine, schema, edit sequence, bounded queues, TLS 1.3 certificate verifier, and server durable snapshot persistence. Each attempt has a fresh document UUID and independent actors. WebSocket uses TCP_NODELAY on client and server, immediate writes, and no negotiated compression. WebTransport uses one reliable bidirectional stream; this suite opens one document per endpoint. Both endpoints must finish with equal document heads, acknowledged changes, and every expected message received.

RTT is measured with the sender monotonic clock and includes Automerge processing and relay snapshot fsync. Traffic travels source → Northern California relay → responder and back. It is not direct Lambda-to-Lambda transport, one-way latency, browser performance, or pure network RTT. This small-message workload does not test stream isolation or bulk transfer. The earlier diagnostic attempts with server Nagle enabled are stored separately and excluded. Cloud scheduling, shared resources and time-varying routes remain sources of variation. With 30 observations per complete pair, p95/p99 estimates are coarse. Failed attempts remain recorded and are excluded from latency percentiles.

Distances use analyst-chosen representative city coordinates from OpenStreetMap. Direct and via-relay distances are one-way great-circle estimates, not AWS facility positions, cable lengths, or observed routes; RTT includes the return exchange. [Coordinate provenance](region-city-proxies.json) · [OpenStreetMap attribution](https://www.openstreetmap.org/copyright).

## Results

- [Interactive region and distance comparison (open locally)](comparison.html)
- [All region pairs CSV](comparison.csv)
- [Full comparison JSON](comparison.json)
- [Every measured RTT](observations.csv)
- [Every attempt, including failures](attempts.csv)

Reproduce with `python3 infra/global-latency/run.py` then `python3 infra/global-latency/compare.py --plots`. See [deployment and cleanup](../../../infra/global-latency/README.md).

Independent JavaScript Automerge verification passed for all 1734 measured successful snapshots. [Evidence](snapshot-verification.json).

After relay restart, a fresh native client downloaded the final-phase document with the same heads and acknowledgement. [Evidence](durability.json).

Cleanup verified: the Terraform state is empty. Dedicated benchmark cloud resources were destroyed; snapshots and raw results remain local. [State check](terraform-state-after-cleanup.txt).

The published bundle includes summaries, observations and verification evidence. Private infrastructure state, runtime credentials, binary snapshots and duplicate raw invocation files remain excluded.
