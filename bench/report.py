#!/usr/bin/env python3
"""Export raw observations to CSV and standalone SVG comparison charts; no plotting dependencies."""
import csv,json,pathlib,statistics,sys,html
files=[pathlib.Path(x) for x in sys.argv[1:]];out=pathlib.Path('results');out.mkdir(exist_ok=True)
rows=[]
for f in files:
 data=json.loads(f.read_text());meta=data['metadata']
 for r in data['rows']:
  r=dict(r);r['environment']=meta['environment'];r['profile']=r.get('networkProfile',meta['networkProfile']);r['quick']=meta['quick'];r['source']=str(f)
  r['readinessMedianMs']=statistics.median(r['readyMs']) if r.get('readyMs') else None
  r['initialSyncMedianMs']=statistics.median(r['initialSyncMs']) if r.get('initialSyncMs') else None
  r['peakRSSBytes']=max([(s.get('process') or {}).get('rssBytes') or 0 for s in r.get('samples',[])]+[((r.get('metricsAfter') or {}).get('process') or {}).get('rssBytes') or 0,((r.get('metricsBefore') or {}).get('process') or {}).get('rssBytes') or 0]) or None
  r['propagationObservationsPerSecond']=len(r.get('latencyMs',[]))/meta['measurementSeconds']
  metrics=r.get('metricsAfter') or {};r['overloads']=metrics.get('overloads');r['queueHighWater']=metrics.get('queueHighWater');r['outboundQueueHighWater']=metrics.get('outboundQueueHighWater')
  rows.append(r)
fields=['environment','profile','quick','kind','suite','size','clients','rep','status','readinessMedianMs','initialSyncMedianMs','p50','p95','p99','peakRSSBytes','overloads','queueHighWater','outboundQueueHighWater','propagationObservationsPerSecond','editThroughput','throughputBytesPerSecond','serverApplicationBytes','disconnects','convergenceFailures','error','source']
with (out/'comparison.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
with (out/'latencies.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['source','kind','suite','rep','latency_ms'])
 for r in rows:
  for x in r.get('latencyMs',[]):w.writerow([r['source'],r['kind'],r['suite'],r['rep'],x])
with (out/'resources.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['source','kind','suite','rep','epoch_ms','rss_bytes','cpu_percent','cpu_ticks','inbound_bytes','outbound_bytes','overloads','queue_high_water'])
 for r in rows:
  for s in r.get('samples',[]):
   p=s.get('process') or {};w.writerow([r['source'],r['kind'],r['suite'],r['rep'],s['at'],p.get('rssBytes'),p.get('cpuPercent'),p.get('cpuTicks'),s['inboundBytes'],s['outboundBytes'],s['overloads'],s['queueHighWater']])
with (out/'durations.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['source','kind','suite','rep','metric','duration_ms'])
 for r in rows:
  for field in ['readyMs','initialSyncMs','setupInitialSyncMs']:
   for value in r.get(field,[]):w.writerow([r['source'],r['kind'],r['suite'],r['rep'],field,value])
def chart(metric,title,filename,unit,suites=None):
 groups={}
 for r in rows:
  if suites and r['suite'] not in suites:continue
  key=f"{r['environment']} / {r['profile']} / {r['suite']} / {r['size']//1024} KiB / {r['clients']} clients"
  groups.setdefault(key,{}).setdefault(r['kind'],[]).append(r)
 bars=[]
 for key,kinds in groups.items():
  for kind,rs in kinds.items():
   vals=[v for r in rs if r['status']=='passed' for v in ([statistics.median(r.get('initialSyncMs',[]))] if metric=='initial' and r.get('initialSyncMs') else [r.get(metric)] if metric!='initial' else []) if v is not None]
   if metric=='initial' and not vals and all(r['suite']!='initial' for r in rs):continue
   if metric!='initial' and not vals and all(r['suite'] not in ('edits','isolation','stress','churn') for r in rs):continue
   bars.append((key,kind,statistics.median(vals) if vals else None,sum(r['status']!='passed' for r in rs),len(rs),any(r['quick'] for r in rs)))
 maxval=max([x[2] or 0 for x in bars]+[1]);height=100+len(bars)*48
 svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}" viewBox="0 0 1400 {height}"><rect width="100%" height="100%" fill="#101a17"/><g font-family="sans-serif" fill="#dfebe4"><text x="24" y="32" font-size="20">{html.escape(title)}</text><text x="24" y="56" font-size="12">Median across repetitions · {unit} · lower is better · failures retained · quick runs marked *</text>']
 for i,(key,kind,v,fail,n,quick) in enumerate(bars):
  y=85+i*48;label=html.escape(f'{key} / {kind.upper()}{" *" if quick else ""}');svg.append(f'<text x="24" y="{y+15}" font-size="11">{label}</text>')
  if v is not None:svg.append(f'<rect x="700" y="{y}" width="{max(1,v/maxval*500):.1f}" height="25" fill="{"#bfe7c9" if kind=="wt" else "#8ab9e0"}"/><text x="1220" y="{y+17}" font-size="12">{v:.2f} {unit} ({n-fail}/{n} passed)</text>')
  else:svg.append(f'<text x="710" y="{y+17}" font-size="12" fill="#ffbca9">FAILED / no measurement ({fail}/{n})</text>')
 svg.append('</g></svg>');(out/filename).write_text(''.join(svg))
chart('p95','Automerge transport comparison — edit propagation p95','edit-latency.svg','ms',['edits'])
chart('initial','Automerge transport comparison — initial synchronization','initial-sync.svg','ms',['initial'])
print('Wrote comparison.csv, latencies.csv, resources.csv, edit-latency.svg, initial-sync.svg')

# A reviewable report distinguishes completed timing runs from short functional coverage.
groups={}
for r in rows:
 key=(r['environment'],r['profile'],'quick' if r['quick'] else 'full',r['suite'],r['size'],r['clients'])
 groups.setdefault(key,{}).setdefault(r['kind'],[]).append(r)
def cell(rs):
 if not rs:return '—'
 good=[r for r in rs if r['status']=='passed'];s=f'{len(good)}/{len(rs)} converged'
 p=[r['p95'] for r in good if r.get('p95') is not None]
 initial=[r['initialSyncMedianMs'] for r in good if r.get('initialSyncMedianMs') is not None]
 if p:s+=f'; p95 {statistics.median(p):.1f} ms'
 if initial:s+=f'; initial {statistics.median(initial):.1f} ms'
 return s
lines=['# Automerge transport experiment results','',
 'Generated from the JSON files linked below. “Full” means three configured repetitions with ten seconds of warmup and sixty seconds of measurement. Disconnects stop failed attempts early. “Quick” means shorter functional coverage; those observations do not carry the same weight as full timing runs.','',
 'The full-duration measurements cover the edit suite on localhost, three controlled Linux network profiles, and the shared constrained remote host. Other full-duration suite/environment combinations have not been run. The harness and documented commands support them.','',
 '## Correctness','',
 'Eleven real-Chromium scenarios, six Rust tests, the incremental browser-frame decoder test, TypeScript compilation, and Clippy passed. This includes mixed transports, Unicode and offline edits, simultaneous first edits from a common schema, independent actors, abrupt restart and direct snapshot-head/content checks, malformed and oversized frames, inbound/outbound overloads, failed persistence, and the live demo controls. See [correctness evidence](results/correctness.json) and [demo screenshot](results/demo.png).','',
 '## Observations','',
 '| Environment / profile | Mode | Suite | Document | Clients | WebTransport | WSS |',
 '|---|---|---|---:|---:|---|---|']
for (environment,profile,mode,suite,size,n),kinds in groups.items():
 lines.append(f'| {environment} / {profile} | {mode} | {suite} | {size//1024} KiB | {n} | {cell(kinds.get("wt",[]))} | {cell(kinds.get("ws",[]))} |')
lines += ['', 'Percentiles in the table are medians of successful per-repetition p95 values, not pooled percentiles. Failed attempts are excluded from latency summaries and retained in counts and raw observations. A failed attempt can have partial latency observations; those are not a success result.', '',
 'The localhost short stress test exhausted queues at eight clients; 32 and 128 clients were therefore skipped. The short 8 MiB isolation case experienced long small-document propagation delays and a WT disconnect. Browser CRDT application, full snapshots, CPU, and storage are shared costs, so these measurements do not isolate a transport-only effect.', '',
 'Remote results are from an existing Ubuntu ARM64 t4g.micro running game services, with only 25% of one CPU and 256 MiB allowed for this experiment (queue capacity 4, versus 8 on localhost and controlled Linux). Ingress was limited to the benchmark Mac’s public IPv4. Management used SSM; game services on port 443 were preserved. Final status and stop evidence are in [remote status](results/remote-status-final.txt) and [stop log](results/remote-stop.txt). The experiment is stopped after measurements; snapshots remain.', '',
 '## Artifacts and interpretation', '',
 '- [Edit comparison chart](results/edit-latency.svg) and [initial-sync chart](results/initial-sync.svg) are standalone SVGs. Asterisks mark quick checks.',
 '- [Comparison CSV](results/comparison.csv), [all edit observations](results/latencies.csv), [resource samples](results/resources.csv), and [readiness/transfer durations](results/durations.csv).',
 '- [Toolchain and host](results/toolchain.txt), [artifact checksums](results/artifacts.sha256), and complete dependency lockfiles in this repository.', '',
 'All propagation clocks are on the same client host. CPU/RSS methods and quotas differ across environments; compare transports within the same environment. Reported sync-byte throughput is offered envelope traffic per nominal measurement second, not wire throughput. Initial-transfer loops can finish after the nominal window, so use transfer durations and document sizes for useful initial-download throughput. No advance assumption or general conclusion that WebTransport wins is made.', '',
 'The stalled scenario freezes a renderer; Chromium networking and OS buffers can still receive. A deterministic Rust test separately forces an unread outbound queue to fill. The benchmark therefore does not claim that renderer freezing is a TCP socket that has stopped reading.', '',
 'WSS uses Axum-server’s default TCP acceptor, without explicitly enabling TCP_NODELAY. The approximately 40 ms baseline Linux tail difference may reflect Nagle/delayed-ACK behavior; this is an inference, not a measured packet-level cause. These results compare the current implementations rather than optimized transport limits.', '',
 'The [measured source archive](results/measured-source.tgz), [measured macOS server](results/measured-server-macos), and [checksums](results/artifacts.sha256) preserve the measured server implementation and a harness snapshot. Final verification subsequently fixed a cancelled-OPEN cleanup race that could count a closed queue as overload; the core Automerge/persistence path is unchanged. The browser harness bounds shutdown and records any cleanup timeouts.', '',
 '## Sources', '']
for f in files:lines.append(f'- [{f.name}]({f.as_posix()})')
pathlib.Path('RESULTS.md').write_text('\n'.join(lines)+'\n')
