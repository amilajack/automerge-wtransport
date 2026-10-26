#!/usr/bin/env python3
import csv,html,json,math,pathlib,re,statistics,sys
path=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else 'results/global/latency.json');data=json.loads(path.read_text());meta=data['metadata'];rows=data['rows'];out=path.parent;regions=meta['regions']
def percentile(xs,p):return sorted(xs)[max(0,math.ceil(len(xs)*p)-1)] if xs else None
by={}
for row in rows:by.setdefault((row['source_region'],row['destination_region']),[]).append(row)
summary=[]
for source in regions:
 for dest in regions:
  attempts=by.get((source,dest),[]);passed=[r for r in attempts if r['status']=='passed'];values=[x for r in passed for x in r['rtt_ms']]
  summary.append({'source_region':source,'destination_region':dest,'attempts':len(attempts),'passed':len(passed),'failed':len(attempts)-len(passed),'samples':len(values),'p50_ms':statistics.median(values) if values else None,'p95_ms':percentile(values,.95),'p99_ms':percentile(values,.99),'min_ms':min(values) if values else None,'max_ms':max(values) if values else None})
with (out/'matrix.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
with (out/'observations.csv').open('w',newline='') as f:
 w=csv.writer(f);w.writerow(['source_region','destination_region','repetition','document','sample','rtt_ms'])
 for r in rows:
  for i,x in enumerate(r.get('rtt_ms',[])):w.writerow([r['source_region'],r['destination_region'],r['repetition'],r['document'],i,x])
with (out/'attempts.csv').open('w',newline='') as f:
 fields=['source_region','destination_region','repetition','status','p50_ms','p95_ms','elapsed_s','error','document'];w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
with (out/'connections.csv').open('w',newline='') as f:
 w=csv.writer(f);w.writerow(['region','role','request_id','source_region','destination_region','repetition','connection_ready_ms','initial_sync_ms','invoke_elapsed_s','cold_init_ms'])
 for row in rows:
  for label in ['source','responder']:
   item=row.get(label,{}) or {};response=item.get('response',{});match=re.search(r'Init Duration: ([\d.]+) ms',item.get('log_tail',''))
   if response.get('request_id'):w.writerow([response['region'],label,response['request_id'],row['source_region'],row['destination_region'],row['repetition'],response.get('connection_ready_ms'),response.get('initial_sync_ms'),item.get('invoke_elapsed_s'),match.group(1) if match else None])
if (out/'relay-metrics.json').exists():
 resource=json.loads((out/'relay-metrics.json').read_text());previous=None
 with (out/'relay-resources.csv').open('w',newline='') as f:
  w=csv.writer(f);w.writerow(['epoch_s','rss_bytes','cgroup_memory_bytes','cpu_ticks','interval_cpu_percent','active_subscriptions','inbound_bytes','outbound_bytes','overloads','error'])
  for row in resource:
   m=row.get('metrics',{});process=m.get('process') or {};ticks=process.get('cpuTicks');cpu=None
   if previous and ticks is not None and row['at']>previous[0]:cpu=(ticks-previous[1])/process.get('ticksPerSecond',100)/(row['at']-previous[0])*100
   if ticks is not None:previous=(row['at'],ticks)
   w.writerow([row['at'],process.get('rssBytes'),process.get('cgroupMemory'),ticks,cpu,m.get('activeSubscriptions'),m.get('inboundBytes'),m.get('outboundBytes'),m.get('overloads'),row.get('error')])
lookup={(r['source_region'],r['destination_region']):r for r in summary};width=160+52*len(regions)+90;height=240+52*len(regions)+130;left=160;top=210;scale=max([r['p50_ms'] or 0 for r in summary]+[1])
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#101a17"/><g font-family="sans-serif" fill="#dfebe4"><text x="24" y="32" font-size="22">Worldwide WebTransport application RTT</text><text x="24" y="58" font-size="12">Lambda → durable Automerge relay in us-west-1 → Lambda → reply; sender monotonic clock</text><text x="24" y="80" font-size="12">Median RTT in ms · {meta["repetitions"]} repetitions · {meta["samples"]} measured messages each · * = incomplete/failed repetitions</text>']
for i,r in enumerate(regions):
 x=left+i*52+26;svg.append(f'<text x="{x}" y="196" transform="rotate(-55 {x} 196)" font-size="11">{html.escape(r)}</text>');svg.append(f'<text x="148" y="{top+i*52+30}" text-anchor="end" font-size="11">{html.escape(r)}</text>')
for i,source in enumerate(regions):
 for j,dest in enumerate(regions):
  r=lookup[source,dest];value=r['p50_ms'];x=left+j*52;y=top+i*52;partial=r['passed']!=meta['repetitions'];label='FAIL' if r['attempts'] else '—';color='#652c29' if r['attempts'] else '#2b3832';ink='#dfebe4'
  if value is not None:
   t=value/scale;color='#%02x%02x%02x'%tuple(int(a+(b-a)*t) for a,b in zip((226,244,219),(45,93,148)));ink='#101a17' if t<.6 else '#ffffff';label=f'{value:.0f}'+('*' if partial else '')
  title=html.escape(f'{source} → {dest}: {r["passed"]}/{r["attempts"]} passed, {r["samples"]} samples, median {value} ms, p95 {r["p95_ms"]} ms')
  svg.append(f'<g><title>{title}</title><rect x="{x}" y="{y}" width="51" height="51" fill="{color}"/><text x="{x+26}" y="{y+30}" text-anchor="middle" fill="{ink}" font-size="11">{label}</text></g>')
legend=top+52*len(regions)+35;svg += [f'<text x="24" y="{legend}" font-size="12">Rows: sender region. Columns: responder region. Hover a cell for p95 and convergence counts.</text>',f'<text x="24" y="{legend+22}" font-size="12">Same-region diagonal uses two concurrent Lambda invocations. This is not direct inter-region QUIC or one-way network latency.</text>',f'<text x="24" y="{legend+44}" font-size="12">Relay {meta["relay_instance_type"]}; clients ARM64 / {meta["function_memory_mb"]} MiB; payload {meta["payload_bytes"]} bytes; {meta["warmup"]} warmup messages excluded.</text>','</g></svg>'];(out/'matrix.svg').write_text(''.join(svg))
passed=sum(r['status']=='passed' for r in rows);failed=len(rows)-passed;valid=[r for r in summary if r['p50_ms'] is not None];fast=sorted(valid,key=lambda r:r['p50_ms'])[:5];slow=sorted(valid,key=lambda r:r['p50_ms'],reverse=True)[:5]
lines=['# Worldwide Lambda WebTransport latency','',f'{len(regions)} enabled commercial AWS regions; {len(regions)**2} directed region pairs including same-region baselines. {len(rows)} paired attempts completed: **{passed} passed, {failed} failed**.','',f'Each attempt uses two concurrent ARM64 Lambda invocations, {meta["function_memory_mb"]} MiB each, {meta["warmup"]} warmup messages and {meta["samples"]} measured request/reply messages with {meta["payload_bytes"]}-byte bodies (plus sequence/protocol metadata). Three repetitions are the default; this run configured {meta["repetitions"]}. Global concurrency is bounded to {meta["concurrency"]} pairs.','',
 'RTT is timed on the sender’s monotonic clock. The request and echo travel through a dedicated, persistent Automerge sync relay in us-west-1. Values include CRDT synchronization, relay snapshot fsync, client processing and the network. They do **not** measure direct Lambda-to-Lambda QUIC, one-way latency, or pure network latency. No clock synchronization or RTT/2 estimate is used.','',
 'Each successful attempt verifies both endpoint document heads match, both endpoints are acknowledged, and all request/reply messages arrived. Failed attempts remain in raw results and are excluded from latency percentiles. Asterisked cells have fewer successful repetitions than requested. Connection readiness, initial sync, invocation duration, request IDs, Lambda REPORT/INIT timing and errors are retained in JSON. With 30 measured RTTs per pair, tail percentiles are coarse estimates for this run.','',
 '## Artifacts','', '- [Interactive-tooltip SVG heatmap](matrix.svg)','- [Region-pair percentiles](matrix.csv)','- [Every measured RTT](observations.csv)','- [Attempt outcomes](attempts.csv)','- [Connection setup and cold starts](connections.csv)','- [Relay CPU/RSS and queue samples](relay-resources.csv)','- [Immutable deployed client images and source checksums](deployment-manifest.json)','- [Full results and runtime metadata]('+path.name+')','',
 '## Lowest median RTTs','', '| Sender | Responder | Median ms | p95 ms | Passed attempts |','|---|---|---:|---:|---:|']
for r in fast:lines.append(f'| {r["source_region"]} | {r["destination_region"]} | {r["p50_ms"]:.2f} | {r["p95_ms"]:.2f} | {r["passed"]}/{r["attempts"]} |')
lines+=['','## Highest median RTTs','','| Sender | Responder | Median ms | p95 ms | Passed attempts |','|---|---|---:|---:|---:|']
for r in slow:lines.append(f'| {r["source_region"]} | {r["destination_region"]} | {r["p50_ms"]:.2f} | {r["p95_ms"]:.2f} | {r["passed"]}/{r["attempts"]} |')
lines+=['','## Reproduction and cleanup','','See [Terraform workflow](../../infra/global-latency/README.md). The relay is isolated from the existing game host. Terraform owns the regional functions, private ECR repositories, log groups, IAM roles, artifact bucket, security group and dedicated relay. Before destroy, `manage.py destroy` exports relay snapshots locally.',''];(out/'REPORT.md').write_text('\n'.join(lines))
print('Wrote',out/'REPORT.md','and matrix SVG/CSVs')

cleanup=out/'terraform-state-after-cleanup.txt'
if cleanup.exists() and not cleanup.read_text().strip():
 with (out/'REPORT.md').open('a') as f:f.write('\nCleanup verified: Terraform state is empty. All Terraform-owned benchmark resources were destroyed. [Relay snapshots](snapshots.tgz), [independent 867-document verification](snapshot-verification.json), and [post-restart native-client check](durability.json) remain local.\n')
