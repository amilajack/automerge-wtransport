#!/usr/bin/env python3
"""Report a fresh, alternating WT/WSS run; never mix historical WT observations."""
import argparse
import csv
import json
import math
import pathlib
import re
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[2]


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [a['lat'], a['lon'], b['lat'], b['lon']])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, h)))


def csv_write(path, rows):
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def report(path, plots=False):
    data = json.loads(path.read_text())
    meta, attempts = data['metadata'], data['rows']
    out = path.parent
    regions = meta['regions']
    coordinate_file = out/'region-city-proxies.json'
    if not coordinate_file.exists():
        coordinate_file.write_bytes((ROOT/'results/global/region-city-proxies.json').read_bytes())
    coords = json.loads(coordinate_file.read_text())['regions']
    groups = {}
    observations, connections = [], []
    for row in attempts:
        key = row['source_region'], row['destination_region'], row['transport']
        groups.setdefault(key, []).append(row)
        for i, value in enumerate(row.get('rtt_ms', [])):
            observations.append(dict(transport=row['transport'], source_region=key[0], destination_region=key[1],
                                     repetition=row['repetition'], document=row['document'], sample=i, rtt_ms=value))
        for role in ['source', 'responder']:
            item = row.get(role, {})
            result = item.get('response', {})
            log = item.get('log_tail', '')
            init = re.search(r'Init Duration: ([\d.]+) ms', log)
            peak_memory = re.search(r'Max Memory Used: (\d+) MB', log)
            if 'request_id' in result:
                connections.append(dict(transport=row['transport'], region=result['region'], role=role,
                    request_id=result['request_id'], document=row['document'], repetition=row['repetition'],
                    connection_ready_ms=result['connection_ready_ms'], initial_sync_ms=result['initial_sync_ms'],
                    invoke_elapsed_s=item.get('invoke_elapsed_s'),
                    cold_init_ms=float(init.group(1)) if init else None,
                    max_memory_mb=int(peak_memory.group(1)) if peak_memory else None))
    summary = []
    for source in regions:
        for dest in regions:
            r = dict(source_region=source, source_label=coords[source]['label'],
                     destination_region=dest, destination_label=coords[dest]['label'],
                     direct_great_circle_km=round(distance(coords[source], coords[dest]), 1),
                     via_relay_great_circle_km=round(distance(coords[source], coords['us-west-1'])+
                                                   distance(coords['us-west-1'], coords[dest]), 1))
            for transport in meta['transports']:
                cases = groups.get((source, dest, transport), [])
                passed = [c for c in cases if c['status'] == 'passed']
                values = [v for c in passed for v in c['rtt_ms']]
                prefix = transport + '_'
                r.update({prefix+'attempts': len(cases), prefix+'passed': len(passed), prefix+'samples': len(values),
                          prefix+'p50_ms': statistics.median(values) if values else None,
                          prefix+'p95_ms': percentile(values, .95), prefix+'p99_ms': percentile(values, .99)})
            wt, ws = r.get('webtransport_p50_ms'), r.get('websocket_p50_ms')
            r['wt_median_reduction_percent'] = 100*(1-wt/ws) if wt is not None and ws else None
            r['wss_over_wt_ratio'] = ws/wt if wt and ws is not None else None
            r['complete'] = all(r.get(t+'_passed') == meta['repetitions'] for t in ['webtransport', 'websocket'])
            summary.append(r)
    resource_path = out/'relay-metrics.json'
    resource_summary = {}
    if resource_path.exists():
        resources = json.loads(resource_path.read_text())
        previous = None
        phase_bounds = {}
        for attempt in attempts:
            phase = attempt.get('phase', '')
            if phase not in phase_bounds:
                phase_bounds[phase] = dict(start=attempt['started_at'], end=attempt['started_at']+attempt['elapsed_s'], transport=attempt['transport'], repetition=attempt['repetition'])
            else:
                phase_bounds[phase]['start'] = min(phase_bounds[phase]['start'], attempt['started_at'])
                phase_bounds[phase]['end'] = max(phase_bounds[phase]['end'], attempt['started_at']+attempt['elapsed_s'])
        resource_rows = []
        for sample in resources:
            metrics = sample.get('metrics', {})
            process = metrics.get('process') or {}
            ticks = process.get('cpuTicks')
            phase, phase_info = next(((k,v) for k,v in phase_bounds.items() if v['start'] <= sample['at'] <= v['end']), (None, {}))
            cpu = None
            if previous and previous[2] == phase and phase is not None and ticks is not None and ticks >= previous[1] and sample['at'] > previous[0]:
                cpu = (ticks-previous[1])/process.get('ticksPerSecond',100)/(sample['at']-previous[0])*100
            previous = (sample['at'], ticks, phase) if ticks is not None else None
            resource_rows.append(dict(epoch_s=sample['at'], phase=phase, transport=phase_info.get('transport'), repetition=phase_info.get('repetition'), rss_bytes=process.get('rssBytes'),
                cpu_percent=cpu, active_subscriptions=metrics.get('activeSubscriptions'),
                inbound_bytes=metrics.get('inboundBytes'), outbound_bytes=metrics.get('outboundBytes'),
                overloads=metrics.get('overloads'), queue_high_water=metrics.get('queueHighWater'),
                outbound_high_water=metrics.get('outboundQueueHighWater'), error=sample.get('error')))
        if resource_rows:
            csv_write(out/'relay-resources.csv', resource_rows)
            resource_summary = dict(samples=len(resource_rows),
                peak_sampled_rss_bytes=max([r['rss_bytes'] or 0 for r in resource_rows]),
                peak_sampled_cpu_percent=max([r['cpu_percent'] or 0 for r in resource_rows]),
                maximum_phase_overloads=max([r['overloads'] or 0 for r in resource_rows]),
                by_transport={t:dict(peak_sampled_rss_bytes=max([r['rss_bytes'] or 0 for r in resource_rows if r['transport']==t] or [0]),
                    mean_sampled_cpu_percent=statistics.mean([r['cpu_percent'] for r in resource_rows if r['transport']==t and r['cpu_percent'] is not None]) if any(r['transport']==t and r['cpu_percent'] is not None for r in resource_rows) else None) for t in meta['transports']})
    totals = {}
    for t in meta['transports']:
        cases = [r for r in attempts if r['transport'] == t]
        passed = [r for r in cases if r['status'] == 'passed']
        values = [v for r in passed for v in r['rtt_ms']]
        setup = [r['connection_ready_ms'] for r in connections if r['transport'] == t]
        totals[t] = dict(attempts=len(cases), passed=len(passed), failed=len(cases)-len(passed), samples=len(values),
                         pooled_p50_ms=statistics.median(values) if values else None,
                         pooled_p95_ms=percentile(values, .95),
                         connection_p50_ms=statistics.median(setup) if setup else None)
    complete = [r for r in summary if r['complete']]
    reductions = [r['wt_median_reduction_percent'] for r in complete]
    overview = dict(resources=resource_summary, region_pairs=len(summary), complete_pairs=len(complete), totals=totals,
                    median_pair_reduction_percent=statistics.median(reductions) if reductions else None,
                    wt_lower_by_over_5_percent=sum(x > 5 for x in reductions),
                    wss_lower_by_over_5_percent=sum(x < -5 for x in reductions),
                    within_5_percent=sum(-5 <= x <= 5 for x in reductions))
    verification = {name:json.loads((out/(name+'.json')).read_text()) for name in ['durability','snapshot-verification'] if (out/(name+'.json')).exists()}
    cleanup = out/'terraform-state-after-cleanup.txt'
    verification['terraform_state_empty'] = cleanup.exists() and not cleanup.read_text().strip()
    result = dict(metadata=meta, overview=overview, verification=verification, regions=coords, rows=summary)
    (out/'comparison.json').write_text(json.dumps(result, indent=2)+'\n')
    csv_write(out/'comparison.csv', summary)
    if observations: csv_write(out/'observations.csv', observations)
    if connections: csv_write(out/'connections.csv', connections)
    csv_write(out/'attempts.csv', [dict(transport=r['transport'], source_region=r['source_region'],
        destination_region=r['destination_region'], repetition=r['repetition'], status=r['status'],
        document=r['document'], p50_ms=r.get('p50_ms'), p95_ms=r.get('p95_ms'), error=r.get('error')) for r in attempts])
    lines = ['# Worldwide WebTransport vs WebSocket', '',
        f"{len(regions)} enabled AWS regions, {len(summary)} directed region pairs, {meta['repetitions']} repetitions per transport. "
        f"**{len(complete)} pairs have all repetitions converged on both transports.**", '',
        '| Transport | Passed attempts | Failed | RTT samples | Pooled median ms | Pooled p95 ms | Median connection ms |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for t, s in totals.items():
        fmt = lambda v: f'{v:.2f}' if v is not None else '—'
        lines.append(f"| {t} | {s['passed']}/{s['attempts']} | {s['failed']} | {s['samples']} | {fmt(s['pooled_p50_ms'])} | {fmt(s['pooled_p95_ms'])} | {fmt(s['connection_p50_ms'])} |")
    lines += ['', 'Pooled percentiles combine different geographic paths; use the region-pair table for a specific path. '
        'Connection readiness excludes Lambda invocation startup and is not part of message RTT.', '',
        f"Among complete pairs: WebTransport median is >5% lower in {overview['wt_lower_by_over_5_percent']}, "
        f"WebSocket median is >5% lower in {overview['wss_lower_by_over_5_percent']}, "
        f"and {overview['within_5_percent']} are within ±5%. These are descriptive thresholds, not statistical significance tests.", '',
        '## Method', '',
        f"Fresh measurements for both transports on the same {meta['relay_instance_type']} relay in us-west-1; "
        f"{meta['function_memory_mb']} MiB ARM64 Lambda clients. Each attempt has {meta['warmup']} warmups, "
        f"{meta['samples']} measured request/reply exchanges, {meta['payload_bytes']}-byte bodies plus marker/envelope metadata, "
        f"and {meta['interval_ms']} ms between exchanges. At most {meta['concurrency']} region-pair groups run concurrently. "
        'Transports run in separate phases, reversing phase order each repetition, with the same seeded pair schedule. '
        f'Before each phase the relay restarts with a fresh data directory, retaining previous snapshots. Each phase has {len(regions)**2} documents, below the resident worker cap.', '',
        'Both transports share the exact Rust Automerge sync engine, schema, edit sequence, bounded queues, TLS 1.3 '
        'certificate verifier, and server durable snapshot persistence. Each attempt has a fresh document UUID and independent actors. '
        'WebSocket uses TCP_NODELAY on client and server, immediate writes, and no negotiated compression. '
        'WebTransport uses one reliable bidirectional stream; this suite opens one document per endpoint. '
        'Both endpoints must finish with equal document heads, acknowledged changes, and every expected message received.', '',
        'RTT is measured with the sender monotonic clock and includes Automerge processing and relay snapshot fsync. '
        'Traffic travels source → Northern California relay → responder and back. It is not direct Lambda-to-Lambda transport, '
        'one-way latency, browser performance, or pure network RTT. This small-message workload does not test stream isolation or bulk transfer. '
        'The earlier diagnostic attempts with server Nagle enabled are stored separately and excluded. '
        'Cloud scheduling, shared resources and time-varying routes remain sources of variation. '
        f"With {meta['samples']*meta['repetitions']} observations per complete pair, p95/p99 estimates are coarse. Failed attempts remain recorded and are excluded from latency percentiles.", '',
        'Distances use analyst-chosen representative city coordinates from OpenStreetMap. Direct and via-relay distances are '
        'one-way great-circle estimates, not AWS facility positions, cable lengths, or observed routes; RTT includes the return exchange. '
        '[Coordinate provenance](region-city-proxies.json) · [OpenStreetMap attribution](https://www.openstreetmap.org/copyright).', '',
        '## Results', '',
        '- [Interactive region and distance comparison](comparison.html)',
        '- [All region pairs CSV](comparison.csv)', '- [Full comparison JSON](comparison.json)',
        '- [Every measured RTT](observations.csv)', '- [Every attempt, including failures](attempts.csv)',
        '- [Connection setup samples](connections.csv)', '- [Raw responses and log tails](raw/)',
        '- [Complete run checkpoint](latency.json)', '- [Sampled relay resources by phase](relay-resources.csv)', '- [Immutable deployed client images and source checksums](deployment-manifest.json)', '',
        'Reproduce with `python3 infra/global-latency/run.py` then '
        '`python3 infra/global-latency/compare.py --plots`. See [deployment and cleanup](../../infra/global-latency/README.md).', '']
    if verification.get('snapshot-verification',{}).get('passed'):
        lines += ['Independent JavaScript Automerge verification passed for all '+str(verification['snapshot-verification']['documents'])+' measured successful snapshots. [Evidence](snapshot-verification.json).', '']
    if verification.get('durability',{}).get('passed'):
        lines += ['After relay restart, a fresh native client downloaded the final-phase document with the same heads and acknowledgement. [Evidence](durability.json).', '']
    if verification['terraform_state_empty']:
        lines += ['Cleanup verified: the Terraform state is empty. Dedicated benchmark cloud resources were destroyed; snapshots and raw results remain local. [State check](terraform-state-after-cleanup.txt).', '']
    (out/'REPORT.md').write_text('\n'.join(lines))
    template = (ROOT/'infra/global-latency/comparison-template.html').read_text()
    (out/'comparison.html').write_text(template.replace('__BENCHMARK_DATA__', json.dumps(result).replace('<', '\\u003c')))
    if plots:
        make_plots(out, regions, summary)
    print(json.dumps(overview, indent=2))


def make_plots(out, regions, rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    lookup = {(r['source_region'], r['destination_region']): r for r in rows}
    matrices = [np.array([[lookup[s,d].get(t+'_p50_ms') or float('nan') for d in regions] for s in regions])
                for t in ['webtransport', 'websocket']]
    limit = max(float(np.nanmax(m)) for m in matrices)
    fig, axes = plt.subplots(1, 3, figsize=(25, 9), layout='constrained')
    for ax, matrix, title in zip(axes[:2], matrices, ['WebTransport median RTT (ms)', 'WebSocket median RTT (ms)']):
        im = ax.imshow(matrix, vmin=0, vmax=limit, cmap='viridis')
        ax.set_title(title)
        fig.colorbar(im, ax=ax, shrink=.65)
    reductions = np.array([[lookup[s,d]['wt_median_reduction_percent']
                           if lookup[s,d]['complete'] else float('nan') for d in regions] for s in regions])
    delta_limit = max(5, float(np.nanmax(np.abs(reductions)))) if np.any(np.isfinite(reductions)) else 5
    im = axes[2].imshow(reductions, vmin=-delta_limit, vmax=delta_limit, cmap='RdBu')
    axes[2].set_title('WT median reduction vs WSS (%)\nPositive = lower WT latency')
    fig.colorbar(im, ax=axes[2], shrink=.65)
    for ax in axes:
        ax.set_xticks(range(len(regions)), regions, rotation=65, ha='right', fontsize=8)
        ax.set_yticks(range(len(regions)), regions, fontsize=8)
        ax.set_xlabel('Responder region'); ax.set_ylabel('Sender region')
    fig.suptitle('Worldwide application request/reply RTT through us-west-1 relay', fontsize=17)
    for ext in ['svg', 'png']: fig.savefig(out/('comparison-matrices.'+ext), dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 6), layout='constrained')
    for t, label, marker in [('webtransport', 'WebTransport', 'o'), ('websocket', 'WebSocket', 'x')]:
        valid = [r for r in rows if r.get(t+'_p50_ms') is not None]
        ax.scatter([r['via_relay_great_circle_km']/1000 for r in valid],
                   [r[t+'_p50_ms'] for r in valid], label=label, s=18, alpha=.55, marker=marker)
    ax.set(xlabel='One-way great-circle distance via relay (thousands of km; city proxies)',
           ylabel='Median application RTT (ms)', title='Region-pair latency versus geographic relay distance')
    ax.legend(); ax.grid(alpha=.2)
    for ext in ['svg', 'png']: fig.savefig(out/('distance-latency.'+ext), dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('path', nargs='?', default='results/global-comparison/latency.json')
    parser.add_argument('--plots', action='store_true')
    args = parser.parse_args()
    report(pathlib.Path(args.path), args.plots)
