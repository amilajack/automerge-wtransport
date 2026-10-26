#!/usr/bin/env python3
"""Join disjoint workload batches, retaining each immutable checkpoint and provenance."""
import json
import pathlib
import shutil

ROOT=pathlib.Path(__file__).resolve().parents[1]
BASE=ROOT/'results/resilience'
EXTRA=ROOT/'results/resilience-reconnect'
matrix=json.loads((BASE/'matrix-results.json').read_text())
reconnect=json.loads((EXTRA/'results.json').read_text())
assert len(matrix['rows'])==42 and len(reconnect['rows'])==6
a,b=matrix['metadata'],reconnect['metadata']
variable={'requested_profiles','requested_suites','edit_intervals_ms'}
assert {k:v for k,v in a.items() if k not in variable}=={k:v for k,v in b.items() if k not in variable}
for key,value in a['edit_intervals_ms'].items():assert b['edit_intervals_ms'][key]==value
meta=a|{'requested_profiles':list(dict.fromkeys(a['requested_profiles']+b['requested_profiles'])),
        'requested_suites':list(dict.fromkeys(a['requested_suites']+b['requested_suites'])),
        'edit_intervals_ms':a['edit_intervals_ms']|b['edit_intervals_ms']}
rows=matrix['rows']+reconnect['rows']
assert len({(r['profile'],r['suite'],r['kind'],r['rep']) for r in rows})==48
first=json.loads((BASE/'matrix-deployment-manifest.json').read_text())
second=json.loads((EXTRA/'deployment-manifest.json').read_text())
assert first['images']==second['images']
for path,digest in first['measured_source_sha256'].items():
    if path!='bench/resilience.py':assert second['measured_source_sha256'][path]==digest,path
for path in (EXTRA/'raw').iterdir():
    dest=BASE/'raw'/path.name
    if dest.exists():assert dest.read_bytes()==path.read_bytes(),path.name
    else:shutil.copy2(path,dest)
shutil.copy2(EXTRA/'results.json',BASE/'reconnect-results.json')
shutil.copy2(EXTRA/'deployment-manifest.json',BASE/'reconnect-deployment-manifest.json')
shutil.copy2(EXTRA/'measured-source.tgz',BASE/'reconnect-source.tgz')
provenance=dict(common_images=first['images'],
    method='42 loss/isolation/outage attempts plus six reconnect attempts. Identical runtime images and browser/server code; controller extended only to select and label the existing churn workload.',
    components={
        'matrix':dict(results='matrix-results.json',manifest='matrix-deployment-manifest.json',source='matrix-source.tgz'),
        'reconnect':dict(results='reconnect-results.json',manifest='reconnect-deployment-manifest.json',source='reconnect-source.tgz')})
(BASE/'deployment-manifest.json').write_text(json.dumps(provenance,indent=2)+'\n')
tmp=BASE/'results.tmp'
tmp.write_text(json.dumps(dict(metadata=meta,rows=rows,components=provenance['components']),indent=2)+'\n')
tmp.replace(BASE/'results.json')
print('Merged 48 disjoint attempts; original checkpoints and both source versions retained')
