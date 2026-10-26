#!/usr/bin/env python3
"""Independently verify retained durable snapshots, never extract archive paths."""
import argparse
import base64
import json
import pathlib
import subprocess
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--directory', default='results/global-comparison')
args = parser.parse_args()
out = ROOT/args.directory
run = json.loads((out/'latency.json').read_text())
passed = [r for r in run['rows'] if r['status'] == 'passed']
assert len({r['document'] for r in run['rows']}) == len(run['rows']), 'Duplicate document UUID'
expected = {r['document']: r for r in passed}
documents = []
with tarfile.open(out/'snapshots.tgz') as archive:
    for entry in archive:
        ident = pathlib.PurePosixPath(entry.name).stem
        if not entry.isfile() or ident not in expected or not entry.name.endswith('.automerge'):
            continue
        row = expected.pop(ident)
        src = row['source']['response']
        dst = row['responder']['response']
        assert src['heads'] == dst['heads'] and src['acknowledged'] and dst['acknowledged']
        documents.append(dict(document=ident, heads=src['heads'], expected_marker=str(src['samples']+src['warmup']-1)+':'+('x'*src['payload_bytes']),
            snapshot=base64.b64encode(archive.extractfile(entry).read()).decode()))
assert not expected, f'Missing {len(expected)} persisted snapshots'
result = subprocess.check_output(['node', str(ROOT/'scripts/verify-global-snapshots.mjs')],
                                 input=json.dumps(documents), text=True, cwd=ROOT)
(out/'snapshot-verification.json').write_text(result)
print(result)
