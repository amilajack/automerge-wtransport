#!/usr/bin/env python3
"""Restart the measured relay and retrieve a final-phase document with a fresh client."""
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
HERE = pathlib.Path(__file__).resolve().parent
OUT = ROOT/'results/global-comparison'
run = json.loads((OUT/'latency.json').read_text())
row = next(r for r in reversed(run['rows']) if r['status'] == 'passed')
variables = json.loads((HERE/'experiment.auto.tfvars.json').read_text())
subprocess.run(['python3', str(HERE/'manage.py'), 'restart'], check=True, cwd=ROOT)
(OUT/'fresh-after-restart.automerge').unlink(missing_ok=True)
result = json.loads(subprocess.check_output([
    str(ROOT/'target/debug/wt-client'), '--url', 'https://'+run['metadata']['relay_ip']+':4433',
    '--doc', row['document'], '--cert-hash', str(HERE/'artifacts/hash.hex'),
    '--snapshot', str(OUT/'fresh-after-restart.automerge'),
], env=os.environ | {'EXPERIMENT_TOKEN': variables['experiment_token']}, text=True, cwd=ROOT))
expected = row['source']['response']['heads']
assert sorted(result['heads']) == sorted(expected)
assert result['acknowledged']
record = dict(passed=True, document=row['document'], original_transport=row['transport'],
              expected_heads=expected, fresh_after_restart=result)
(OUT/'durability.json').write_text(json.dumps(record, indent=2)+'\n')
print('PASS restart and fresh-client heads; original transport:', row['transport'])
