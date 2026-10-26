# Global Lambda WebTransport vs WebSocket benchmark

This Terraform deployment creates a dual-transport Rust Lambda client in **every commercial AWS region enabled in the current account**. It does not enable opt-in regions, use China/GovCloud partitions, or change the existing game host. The current account exposes 17 enabled regions, recorded in `regions.json`.

Lambda can initiate QUIC/WebTransport connections but cannot expose an inbound QUIC listener. Consequently, two Lambda invocations exchange request/reply markers through a dedicated Automerge relay in `us-west-1`. The sender measures RTT with `tokio::time::Instant`. This is application RTT through the relay, including durable Automerge sync, rather than direct region-to-region packet latency or a one-way measurement. Same-region cells use two concurrent invocations, not a single function echoing to itself.

## Infrastructure

Terraform owns all resources in this directory’s independent state:

- One ARM64 `t4g.small` relay, one-CPU quota, 1 GiB service memory limit, 64-message queues, an encrypted 12 GiB gp3 root volume, its IAM/SSM profile and security group.
- A private encrypted artifact bucket for the server deployment and snapshot export.
- A private ECR repository, three-day CloudWatch log group, and ARM64 Lambda function in each enabled region. Functions have 256 MiB memory, a 150-second timeout, and a 120-second handler deadline. There are no public function URLs, VPCs, NAT gateways or scheduled triggers.
- Dedicated Lambda execution IAM roles/policies. Regional repository policies let Lambda retrieve its code. Only the public certificate pin is compiled/configured for clients; private keys stay in the relay artifact and private local directory.

The disposable relay accepts TCP and UDP 4433 publicly because Lambda public egress IPs are not stable. A random experiment token and pinned ten-day ECDSA certificate protect sessions. Both sync endpoints require the experiment token; metrics also require it. WSS and WT use the same pinned TLS certificate. TCP_NODELAY is enabled on both WSS endpoints; compression is disabled. There is no SSH ingress; management uses SSM. This is a temporary benchmark deployment.

Terraform provider aliases must be static. `generate.py` discovers enabled regions with AWS CLI, creates `regions.tf.json`, and prepares ignored mode-600 private variables/artifacts. `.terraform.lock.hcl` locks the AWS provider. All infrastructure creation/deletion is through Terraform; the manager only pushes code images, invokes SSM commands, and exports data.

## Deploy

Requires Terraform ≥1.5.7, Docker with ARM64 builds, AWS CLI credentials, Python 3, and OpenSSL. Run from the project root:

```sh
# Server binary + statically linked custom Lambda runtime, built locally:
npm run build
docker build --platform linux/arm64 --target server -t automerge-wtransport:server .
docker build --platform linux/arm64 --provenance=false -f lambda/Dockerfile \
  -t automerge-wtransport:lambda .
python3 infra/global-latency/generate.py
terraform -chdir=infra/global-latency init
terraform -chdir=infra/global-latency validate
python3 infra/global-latency/manage.py bootstrap
python3 infra/global-latency/manage.py wait
# Publish identical immutable-tag images to each regional ECR repository:
python3 infra/global-latency/manage.py publish
python3 infra/global-latency/manage.py functions
```

Deployment is in two phases because the regional image must exist before Lambda can use it. `publish` uses an experiment-private Docker auth directory, never prints registry passwords, and leaves the operator’s Docker login configuration alone. Lambda image URIs are pinned by digest. `manage.py` writes a concrete Terraform plan, rejects unrelated deletion/replacement during deployment, and applies that saved plan. To intentionally replace this experiment’s relay after a bootstrap change, use `bootstrap --replace-relay`; this exception only permits replacement of `aws_instance.relay` in this state. Snapshot export should precede replacement of a relay containing useful data.

A single-region preflight is available before publishing worldwide:

```sh
python3 infra/global-latency/manage.py publish --regions us-west-1
python3 infra/global-latency/manage.py functions --regions us-west-1
python3 infra/global-latency/run.py --regions us-west-1 --samples 3 --warmup 1 \
  --repetitions 1 --output results/global/preflight.json
# Then publish all regions and run functions without --regions to enable all.
```

Do not run `generate.py` between saving and applying a Terraform plan: regenerating the deployment archive changes its checksum. Keep private variables/state/artifacts local; they contain the experiment token and certificate material. Re-run `publish` and `functions` after rebuilding changed Lambda code.

## Measure

```sh
# 17×17 pairs × 3 repetitions × 2 transports = 1,734 attempts / 3,468 invocations:
python3 infra/global-latency/run.py
python3 infra/global-latency/compare.py --plots
# Resume an interrupted run without discarding recorded failures:
python3 infra/global-latency/run.py --resume
```

Defaults: ten measured 64-byte requests, two warmups, three repetitions, 100 ms between messages, and at most four concurrent pairs. The fixed schedule seed is 49217. Every attempt gets a fresh document UUID and new sync state. A responder acknowledges readiness, echoes every marker, and finishes a final handshake. Both endpoint heads and acknowledgement states must agree. Warmup messages are excluded from RTT percentiles. Cold-start/connection costs are separate; Lambda log tails retain `Init Duration`, billed duration and peak memory when reported.

`--transports webtransport websocket` (default), `--regions`, `--samples`, `--warmup`, `--repetitions`, `--concurrency`, `--payload-bytes`, `--interval-ms`, and `--output` select a run. Limits are enforced (100 measured messages, ten warmups, 16 KiB payload, four concurrent pairs). The handler has a hard deadline and SDK retries are disabled for invocation to avoid accidentally running duplicate ambiguous requests. Failures and partial raw responses are preserved. Only the orchestrator writes the checkpoint, atomically, after each completed pair.

A fresh run restarts only the dedicated relay, retaining on-disk snapshots while releasing old in-memory workers. The relay restarts with a fresh data directory before each transport phase. Each phase has 289 fresh documents, below the 1,024 resident-worker cap. The same seeded region-pair schedule runs for both transports; phase order reverses each repetition. Previous phases’ snapshots remain on disk. The client never estimates one-way delay by dividing RTT by two. Results describe this application configuration; Lambda CPU allocation, cold environments, relay placement and storage all affect it.

Outputs: full JSON, raw per-invocation responses and log tails, attempt CSV, every RTT observation, region-pair p50/p95/p99 CSV, and a standalone SVG heatmap with tooltip convergence counts. The generated `results/global-comparison/REPORT.md` explains the methodology and limitations.

## Validate locally

```sh
cargo test --locked
cargo clippy --all-targets --features lambda -- -D warnings
node scripts/rust-client-test.mjs
BENCH_TRANSPORT=webtransport node scripts/lambda-local-test.mjs
BENCH_TRANSPORT=websocket node scripts/lambda-local-test.mjs
```

The Lambda-local test runs the actual static ARM64 container against a mock Lambda Runtime API and the real sync server, asserting every request/reply and identical endpoint heads. It does not need AWS resources.

## Status and cleanup

```sh
python3 infra/global-latency/manage.py status
python3 infra/global-latency/manage.py logs
python3 infra/global-latency/manage.py archive
# Archives snapshots first, then plans/applies deletion of only this state:
AMWT_RESULTS_DIR=results/global-comparison python3 infra/global-latency/manage.py destroy
terraform -chdir=infra/global-latency state list
```

`destroy` stops the dedicated service, archives its snapshot directory through a narrowly scoped S3 export permission, downloads it to `$AMWT_RESULTS_DIR/snapshots.tgz` (defaults to `results/global-comparison/snapshots.tgz`), and then removes the Terraform-owned resources. Results and local snapshot archives remain. The existing `xwing-coop` instance and game services are outside this Terraform state and remain untouched. Retain state until cleanup succeeds. No schedules invoke these functions after the experiment.

The historical WT-only results in `results/global` are preserved. New paired results live in `results/global-comparison`. Do not compare new WSS measurements against the historical WT run. `compare.py` generates an offline browser report, CSV/JSON, and optional Matplotlib comparison and distance charts. For plots, install `matplotlib==3.10.8` in a temporary environment.
