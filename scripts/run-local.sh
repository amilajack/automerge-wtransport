#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
: "${EXPERIMENT_TOKEN:=local-experiment-token}"
export EXPERIMENT_TOKEN
[ -f certs/cert.pem ] || ./scripts/cert.sh
npm ci
npm run build
cargo build --release --locked
exec target/release/automerge-wtransport
