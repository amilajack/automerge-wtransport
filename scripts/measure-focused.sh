#!/bin/sh
# Full 3x(10s warmup + 60s measurement) edits, sequential environments.
set -eu
cd "$(dirname "$0")/.."
SUITES=edits OUTPUT=results/localhost-edits.json node bench/run.mjs
SUITES=edits python3 bench/controlled.py > results/controlled-edits.log 2>&1
if EXPERIMENT_TOKEN="$(cat certs/token)" BASE_URL=https://52.9.189.226:4433 \
  BENCH_ENV=remote SUITES=edits RESET_COMMAND='python3 deploy/remote.py reset' \
  OUTPUT=results/remote-edits.json node bench/run.mjs > results/remote-edits.log 2>&1; then
  echo 'Remote comparisons completed'
else
  echo 'Remote comparison recorded failures; inspect results/remote-edits.json'
fi
python3 deploy/remote.py status > results/remote-status-final.txt
python3 deploy/remote.py stop > results/remote-stop.txt
python3 bench/report.py results/localhost-coverage.json results/localhost-edits.json results/controlled-combined.json results/remote-edits.json
