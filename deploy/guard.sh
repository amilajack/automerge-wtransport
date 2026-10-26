#!/bin/sh
# Runs only for this experiment. Stop our service if shared-host health degrades.
set -eu
while sleep 2; do
  if ! systemctl is-active --quiet xwing.service || ! systemctl is-active --quiet xwing-proxy.service ||
     ! curl -fsS --max-time 2 http://127.0.0.1:8080/health >/dev/null ||
     [ "$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)" -lt 131072 ]; then
    echo 'Existing-service or host-memory guard triggered; stopping experiment'
    systemctl stop automerge-wtransport.service
    exit 1
  fi
done
