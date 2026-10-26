#!/usr/bin/env python3
"""Keep failures visible beside successful latency and whole-window deadline coverage."""
import argparse
import csv
import json
import math
import pathlib
import statistics

ROOT=pathlib.Path(__file__).resolve().parents[1]
LABELS={'baseline':'Baseline','loss1-rtt100-5mbit':'1% random loss · 100 ms RTT · 5 Mbps',
        'bursty3-rtt150-1mbit':'≈3% burst loss · 150 ms RTT · 1 Mbps'}
WORKLOADS={'edits':'Edits · 5/s per client','isolation':'Control 1/s + 4 bulk documents','outage':'Three-second blackout · control 1/s','churn':'Repeated reconnects · control 1/s'}


def median(values):
    return statistics.median(values) if values else None


def write_csv(path,rows):
    if not rows:return
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def generate(directory,plots=False):
    data=json.loads((directory/'results.json').read_text())
    meta,rows=data['metadata'],data['rows']
    groups={}
    for r in rows:groups.setdefault((r['profile'],r['suite'],r['kind']),[]).append(r)
    diagnostics=[]
    for row in rows:
        if row['status']=='passed':continue
        name=f"{row['profile']}-{row['suite']}-{row['rep']}-{row['kind']}-server.log"
        log=(directory/'raw'/name).read_text() if (directory/'raw'/name).exists() else ''
        category='document_queue_full' if 'document inbound queue full' in log else 'stream_cancelled_or_overloaded' if 'cancelled or overloaded' in log else 'other_failure'
        diagnostics.append(dict(profile=row['profile'],workload=row['suite'],transport=row['kind'],repetition=row['rep'],category=category,server_log='raw/'+name))
    summary=[]
    for (profile,suite,kind),attempts in groups.items():
        good=[r for r in attempts if r['status']=='passed']
        target=sum(r.get('targetDeliveries',0) for r in attempts)
        delivered500=sum(r.get('deadlineCoverage',{}).get('500',{}).get('delivered',0) for r in attempts)
        rss=[s.get('process',{}).get('rssBytes') or 0 for r in attempts for s in r.get('samples',[])]
        summary.append(dict(profile=profile,workload=suite,transport=kind,attempts=len(attempts),passed=len(good),
            failed=len(attempts)-len(good),edit_interval_ms=meta['edit_intervals_ms'][suite],
            median_successful_run_p50_ms=median([r['p50'] for r in good if r.get('p50') is not None]),
            median_successful_run_p95_ms=median([r['p95'] for r in good if r.get('p95') is not None]),
            successful_bulk_observations=sum(len(r.get('bulkLatencyMs',[])) for r in good),
            median_successful_bulk_p95_ms=median([r['bulkP95'] for r in good if r.get('bulkP95') is not None]),
            median_successful_bulk_transfer_ms=median([r['bulkTransferMs'] for r in good if r.get('bulkTransferMs') is not None]),
            median_successful_fresh_recovery_ms=median([r['freshRecoveryMs'] for r in good if r.get('freshRecoveryMs') is not None]),
            target_deliveries=target,actual_generated_edits=sum(r.get('edits',0) for r in attempts),
            observed_deliveries=sum(len(r.get('latencyMs',[])) for r in attempts),
            coverage_500ms=min(1,delivered500/target) if target else None,
            completed_reconnects=sum(r.get('churns',0) for r in attempts),
            disconnects=sum(r.get('disconnects',0) for r in attempts),
            overloads=sum(r.get('metricsAfter',{}).get('overloads',0) for r in attempts),
            peak_sampled_server_rss_bytes=max(rss) if rss else None))
    write_csv(directory/'comparison.csv',summary)
    messages=[dict(profile=r['profile'],workload=r['suite'],transport=r['kind'],repetition=r['rep'],
                   attempt_status=r['status'],**event) for r in rows for event in r.get('deliveries',[])]
    write_csv(directory/'deliveries.csv',messages)
    trials=[{k:r.get(k) for k in ['profile','suite','kind','rep','status','editIntervalMs','p50','p95','p99',
        'targetDeliveries','edits','observedDeliveries','deliveryRatio','bulkP95','bulkTransferMs','freshRecoveryMs',
        'disconnects','convergenceFailures','error']} for r in rows]
    write_csv(directory/'attempts.csv',trials)
    resources=[dict(profile=r['profile'],workload=r['suite'],transport=r['kind'],repetition=r['rep'],epoch_ms=s['at'],rss_bytes=(s.get('process') or {}).get('rssBytes'),cpu_ticks=(s.get('process') or {}).get('cpuTicks'),inbound_bytes=s.get('inboundBytes'),outbound_bytes=s.get('outboundBytes'),overloads=s.get('overloads'),queue_high_water=s.get('queueHighWater'),outbound_queue_high_water=s.get('outboundQueueHighWater')) for r in rows for s in r.get('samples',[])]
    write_csv(directory/'resources.csv',resources)
    expected=sum(1 for p in meta['requested_profiles'] for s in meta['requested_suites'] if s not in ('outage','churn') or p=='loss1-rtt100-5mbit')*meta['repetitions']*2
    result=dict(metric='browser edit propagation: sender edit to peer observation through relay',metadata=meta,summary=summary,expected_attempts=expected,complete=len(rows)==expected,profile_labels=LABELS,workload_labels=WORKLOADS,
                attempts=len(rows),passed=sum(r['status']=='passed' for r in rows),
                failed=sum(r['status']!='passed' for r in rows),failure_diagnostics=diagnostics,verification=json.loads((directory/'verification.json').read_text()) if (directory/'verification.json').exists() else None,cleanup=json.loads((directory/'cleanup.json').read_text()) if (directory/'cleanup.json').exists() else None)
    (directory/'comparison.json').write_text(json.dumps(result,indent=2)+'\n')
    fmt=lambda x:'—' if x is None else f'{x:.1f}'
    lines=['# WebTransport vs WebSocket on unreliable links','',
        f"{result['attempts']}/{expected} planned attempts completed: **{result['passed']} converged, {result['failed']} failed**. "
        +('These are quick functional checks, not full timing runs.' if meta['quick'] else
          f"{meta['repetitions']} repetitions, configured {meta['warmup_seconds']} seconds warmup and {meta['duration_seconds']} seconds measurement. Failed attempts stop early."),'',
        '| Profile | Workload | Transport | Converged | Successful-run p95 ms | ≤500 ms target coverage | Bulk p95 ms | Bulk duration ms | Fresh recovery ms |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for r in summary:
        lines.append(f"| {LABELS[r['profile']]} | {WORKLOADS[r['workload']]} | {r['transport']} | {r['passed']}/{r['attempts']} | {fmt(r['median_successful_run_p95_ms'])} | {fmt(100*r['coverage_500ms']) if r['coverage_500ms'] is not None else '—'}% | {fmt(r['median_successful_bulk_p95_ms'])} | {fmt(r['median_successful_bulk_transfer_ms'])} | {fmt(r['median_successful_fresh_recovery_ms'])} |")
    if diagnostics:
        counts={c:sum(d['category']==c for d in diagnostics) for c in sorted({d['category'] for d in diagnostics})}
        lines += ['', 'Failure diagnostics (attempts): '+', '.join(c+': '+str(n) for c,n in counts.items())+'. The precise cause of stream cancellation is not distinguished by the retained logs. Raw logs remain linked in comparison.json.', '']
    lines += ['', '## What this tests','',
        'Two real Chromium clients exchange concurrent Automerge edits through the Rust durable sync server. '
        'Latency measures a sender edit until observation by the other browser, with both clocks on the same browser host. '
        'The worldwide Lambda suite measures request/reply RTT; compare transports within each suite. '
        'The isolated Linux router impairs TCP and UDP equally in both directions; the Mac host network is unchanged. '
        'GRO/GSO/TSO are disabled on routed interfaces to avoid comparing large offloaded TCP packets against individual UDP packets. '
        'Server quota is one CPU / 1 GiB; browser clients have four CPUs / 5 GiB. Both transports use identical TLS certificates, '
        '64-message queue limits, snapshots, edit schedules and data. WSS has TCP_NODELAY and no compression. '
        'The server and its data directory are recreated for every attempt; transport order reverses each repetition.', '',
        'The edits workload retains five edits per client per second. The isolation workload uses one edit per client per second '
        'while four independent 256 KiB documents (1 MiB total seeded text) download on the same transport connection. '
        'WT uses independent document streams; WSS multiplexes them into one ordered connection. '
        'Low-rate control probes help distinguish network blocking from saturation of application queues. '
        'Bulk latency includes only control edits generated while the bulk transfers are active. '
        'Bulk-window sample counts are exported; at one probe per second, their tail percentiles can be coarse. '
        'All successful attempts check equal heads, equal contents, acknowledgements and the expected delivery count.', '',
        'The outage workload adds a target three-second bidirectional packet blackout to the 1% loss profile. '
        'Actual start/end times and router counters are recorded, including command scheduling overhead. '
        'Fresh recovery is the time from the recorded restore point until both clients observe a newly generated post-restore edit; '
        'It includes waiting until the next scheduled one-second probe. Connections survive and recover or the attempt fails; no automatic reconnect is added to the blackout workload. '
        'The separate reconnect workload deliberately disconnects and reconnects one client approximately every two seconds while both clients keep editing; local documents remain in memory and sync state resets on reconnect.', '',
        '## Read the metrics together','',
        'Latency summaries are medians of successful per-repetition percentiles; failed attempts are excluded from those percentiles '
        'and remain visible in completion counts and raw data. A low successful latency does not imply an available service. '
        'Deadline coverage includes every attempt and divides timely deliveries by the nominal full-window edit schedule. '
        'Generation stalls, disconnects, missing deliveries and late observations count against coverage. '
        'Generated edit counts and delivery fractions among generated edits are separately retained. '
        'An attempt that fails before measurement has zero target coverage.', '',
        'Independent streams can avoid ordered-delivery blocking across streams, but still share congestion control and application resources '
        '([QUIC RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html), '
        '[WebTransport specification](https://www.w3.org/TR/webtransport/)). '
        'Reliable Automerge sync still requires delivery and retransmission; this experiment does not send CRDT sync over unreliable datagrams. '
        'Datagram freshness and IP migration are outside this application suite.', '',
        'These are application-level observations, including browser CRDT processing, full snapshot fsync and configured write timeouts. '
        'Queue exhaustion or a stream reset is an implementation limit, not proof of an intrinsic transport limit. '
        'The severe-loss preflight failures remain in separate diagnostic results. Netem uses fresh random loss realizations: '
        'configured distributions match, but the lost packet sequences cannot be identical across protocols. '
        'Three repetitions are descriptive evidence rather than a statistical significance test. '
        'The burst model has 1% transition to bad state, 25% transition to good, 75% loss in bad and 0.1% in good '
        '(approximately 2.98% long-run directional loss). Each impaired interface has a 256-packet queue limit; '
        'bandwidth shaping can also cause queue drops. Jitter is independently applied in each direction.', '',
        '## Artifacts','', '- [Interactive comparison](report.html)', '- [All attempt outcomes](attempts.csv)',
        '- [All edit observations, including partial failures](deliveries.csv)', '- [Server resource and queue samples](resources.csv)', '- [Comparison CSV](comparison.csv)',
        '- [Full results, events, netem counters and resource samples](results.json)',
        '- [Raw case files and client/server logs](raw/)', '- [Outcome and measured-source verification](verification.json)', '- [Measured images and component source provenance](deployment-manifest.json)', '- [Container/network cleanup](cleanup.json)', '- [Router offload settings](router-offloads.txt)',
        '- [Worldwide AWS comparison on unmodified networks](../global-comparison/REPORT.md)', '',
        'Reproduce with `docker compose -f bench/compose.yml build` then `python3 bench/resilience.py` '
        'and `python3 bench/resilience-report.py --plots`. See [benchmark instructions](../../bench/RESILIENCE.md).','']
    (directory/'REPORT.md').write_text('\n'.join(lines))
    template=(ROOT/'bench/resilience-template.html').read_text()
    (directory/'report.html').write_text(template.replace('__RESULTS__',json.dumps(result).replace('<','\\u003c')))
    if plots:make_plots(directory,summary)
    print(json.dumps({k:result[k] for k in ['attempts','passed','failed']},indent=2))


def make_plots(out,summary):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    keys=list(dict.fromkeys((r['profile'],r['workload']) for r in summary))
    lookup={(r['profile'],r['workload'],r['transport']):r for r in summary}
    short={'baseline':'Baseline','loss1-rtt100-5mbit':'1% / 100 ms / 5 Mbps','bursty3-rtt150-1mbit':'GE3% / 150 ms / 1 Mbps'}
    labels=[short[p]+'\n'+w+'\n'+', '.join(t.upper()+' '+str(lookup.get((p,w,t),{}).get('passed',0))+'/'+str(lookup.get((p,w,t),{}).get('attempts',0)) for t in ['wt','ws']) for p,w in keys]
    trials=json.loads((out/'results.json').read_text())['rows']
    fig,axes=plt.subplots(2,1,figsize=(13,10),layout='constrained')
    for index,(kind,label) in enumerate([('wt','WebTransport'),('ws','WebSocket')]):
        x=np.arange(len(keys))+(index-.5)*.34
        values=[lookup.get((p,w,kind),{}).get('median_successful_run_p95_ms') for p,w in keys]
        axes[0].bar(x,[v if v is not None else float('nan') for v in values],width=.32,label=label)
        coverage=[lookup.get((p,w,kind),{}).get('coverage_500ms') for p,w in keys]
        axes[1].bar(x,[100*v if v is not None else float('nan') for v in coverage],width=.32,label=label)
        for j,(profile,workload) in enumerate(keys):
            cases=[r for r in trials if r['profile']==profile and r['suite']==workload and r['kind']==kind]
            observed=[r['p95'] for r in cases if r['status']=='passed' and r.get('p95') is not None]
            axes[0].scatter(x[j]+np.linspace(-.06,.06,len(observed)),observed,s=18,color='black',alpha=.55,zorder=3)
            points=[100*r.get('deadlineCoverage',{}).get('500',{}).get('fraction',0) for r in cases]
            axes[1].scatter(x[j]+np.linspace(-.06,.06,len(points)),points,s=18,color='black',alpha=.55,zorder=3)
    axes[0].set_yscale('log');axes[0].set_ylabel('Median successful-run edit p95 (ms; log scale)')
    axes[0].set_title('Successful-run p95: median bars, individual repetition dots; check completion counts')
    axes[1].set_ylabel('Full-window targets delivered within 500 ms (%)');axes[1].set_ylim(0,105)
    axes[1].set_title('Deadline coverage includes failed attempts and generation stalls')
    for ax in axes:
        ax.set_xticks(range(len(keys)),labels,rotation=20,ha='right');ax.grid(axis='y',alpha=.2);ax.legend()
    for ext in ['png','svg']:fig.savefig(out/('latency-deadlines.'+ext),dpi=180)
    plt.close(fig)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',nargs='?',default='results/resilience');p.add_argument('--plots',action='store_true')
    a=p.parse_args();generate(pathlib.Path(a.directory),a.plots)
