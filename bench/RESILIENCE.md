# Unreliable-link comparison

Run from the project root. Requires Docker Desktop; all impairment stays inside the two isolated lab networks.

```sh
npm run build
docker compose -f bench/compose.yml build
python3 bench/resilience.py
# Standalone charts use an isolated Python environment:
python3 -m venv /tmp/amwt-resilience-plots
/tmp/amwt-resilience-plots/bin/python -m pip install matplotlib==3.10.8
/tmp/amwt-resilience-plots/bin/python bench/resilience-report.py --plots
open results/resilience/report.html
```

The default suite has 48 attempts: three profiles × edits/isolation × two transports × three repetitions, plus six outage and six reconnect-churn attempts on the random-loss profile. Each has ten seconds warmup and sixty seconds measurement. Setup and final convergence can extend the duration. Expect approximately 60–90 minutes or more, depending on losses and setup timeouts. Use `--resume` to continue the exact same configuration; recorded failures are retained.

Profiles are baseline, 1% random directional loss with 100 ms added RTT / jitter / 5 Mbps, and a roughly 3% Gilbert-Elliott burst-loss process with 150 ms added RTT / jitter / 1 Mbps. Parameters and actual router counters are preserved in JSON. These profiles differ in multiple network properties; compare transports within a profile. Loss is stochastic, not an identical packet trace across TCP and UDP. GRO/GSO/TSO are disabled on all routed interfaces. Router commands never modify the Mac host network.

The edits workload generates five edits per client per second. Isolation uses one control edit per client per second alongside four 256 KiB document downloads over the same session, exercising independent WT document streams against a multiplexed WSS connection. Outage recovery uses the low-rate control workload with a target three-second bidirectional blackout, about fifteen seconds into measurement. Actual boundary timestamps are recorded inside the Linux VM; both browser endpoint clocks also run there. The blackout workload does not add automatic reconnect behavior. A separate churn workload deliberately disconnects and reconnects one client after each two-second connected interval while both clients keep editing. Connection setup adds to the cycle period; completed reconnect counts are recorded.

Both transports use two browser clients, a 64 KiB control document, matching queues (64), the same certificate, persistence and quotas. Server data directories are fresh for every attempt. Transport execution order alternates across repetitions. The Docker server has TCP_NODELAY enabled for WSS; compression is disabled. Existing write timeouts remain in force, so an implementation may fail under conditions its underlying protocol could tolerate with different application policies.

Failures are part of the results. Successful latency, all-attempt completion, full-window delivery deadline coverage, bulk transfer duration, recovery time, raw edit observations, client/server logs, queue pressure and resource samples remain separate. Container/resource failure stops further load. Normal convergence failures remain recorded and the fixed two-client suite continues.

Quick preflight (not a full timing run):

```sh
python3 bench/resilience.py --profiles baseline --suites isolation \
  --repetitions 1 --warmup 2 --duration 10 --output results/resilience-quick
python3 bench/resilience-report.py results/resilience-quick
```

The runner removes its containers and isolated networks in `finally`. Manual cleanup after interruption:

```sh
docker compose -f bench/compose.yml down --remove-orphans
```

This tests the reliable Automerge application. Unreliable datagrams and connection migration need separate workloads; CRDT sync messages must not be silently discarded. Independent streams still share congestion control, CPU and persistence resources, so this suite makes no advance assumption that WT wins.
