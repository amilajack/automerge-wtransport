#!/usr/bin/env python3
"""Matched browser runs through an isolated Linux netem router; no host impairment."""
import argparse
import json
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
CMD = ['docker', 'compose', '-f', str(ROOT/'bench/compose.yml')]
PROFILES = {
    'baseline': [],
    'loss1-rtt100-5mbit': ['delay','50ms','5ms','distribution','normal','loss','1%','rate','5mbit','limit','256'],
    # Gilbert-Elliott: 1% transition to bad, 25% transition to good,
    # 75% loss in bad and 0.1% in good => approximately 3% long-run loss.
    'bursty3-rtt150-1mbit': ['delay','75ms','15ms','distribution','normal',
                          'loss','gemodel','1%','25%','75%','0.1%','rate','1mbit','limit','256'],
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='results/resilience')
    parser.add_argument('--profiles', nargs='+', choices=list(PROFILES), default=list(PROFILES))
    parser.add_argument('--suites', nargs='+', choices=['edits','isolation','outage','churn'], default=['edits','isolation','outage','churn'])
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    assert 1 <= args.repetitions <= 3 and 0 <= args.warmup <= 10 and 10 <= args.duration <= 60
    os.chdir(ROOT)
    out = ROOT/args.output
    raw = out/'raw'
    raw.mkdir(parents=True, exist_ok=True)
    environment = os.environ | {'QUEUE_CAPACITY':'64','BULK_BYTES':'262144','BULK_DOCUMENTS':'4'}
    rows = []
    active_container=None
    metadata = dict(requested_profiles=args.profiles, requested_suites=args.suites, profiles=PROFILES, repetitions=args.repetitions, warmup_seconds=args.warmup,
                    duration_seconds=args.duration, queue_capacity=64, bulk_document_bytes=262144,
                    bulk_documents=4, bulk_total_bytes=1048576, edit_intervals_ms={'edits':200,'isolation':1000,'outage':1000,'churn':1000}, offloads='GRO/GSO/TSO disabled on all routed interfaces',
                    loss_realizations='Fresh stochastic netem realization per attempt; matched configured distributions, not identical lost packet sequences',
                    application='Reliable Automerge sync only; does not test unreliable datagrams',
                    outage=dict(blackout_seconds=3,offset_seconds=min(15,args.duration/3),recovery='same session or recorded failure'),
                    quick=not(args.repetitions==3 and args.warmup==10 and args.duration==60))

    def compose(*arguments, **kw):
        return subprocess.run(CMD+list(arguments), check=True, env=environment, **kw)

    def capture(*arguments):
        return subprocess.check_output(CMD+list(arguments), text=True, env=environment)

    def save():
        path=out/'results.json'
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(metadata=metadata, rows=rows), indent=2)+'\n')
        temporary.replace(path)

    def qdisc(devices, parameters):
        for dev in devices:
            if parameters:
                compose('exec','-T','router','tc','qdisc','replace','dev',dev,'root','netem',*parameters,
                        stdout=subprocess.DEVNULL)
            else:
                subprocess.run(CMD+['exec','-T','router','tc','qdisc','del','dev',dev,'root'],
                               capture_output=True, env=environment)

    if args.resume and (out/'results.json').exists():
        previous=json.loads((out/'results.json').read_text())
        assert previous['metadata']==metadata, 'Resume settings differ'
        rows=previous['rows']
    completed={(r['profile'],r['suite'],r['rep'],r['kind']) for r in rows}
    save()
    try:
        compose('up','-d','router',stdout=subprocess.DEVNULL)
        for _ in range(120):
            if subprocess.run(CMD+['exec','-T','router','test','-e','/ready'],capture_output=True).returncode==0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('Router readiness timed out')
        links=json.loads(capture('exec','-T','router','ip','-j','-4','address'))
        devices=[x['ifname'] for x in links if any(a['local'] in ('172.29.81.1','172.29.82.1') for a in x['addr_info'])]
        assert len(devices)==2
        for dev in devices:
            compose('exec','-T','router','ethtool','-K',dev,'gro','off','gso','off','tso','off',stdout=subprocess.DEVNULL)
        (out/'router-interfaces.json').write_text(json.dumps(links,indent=2))
        (out/'router-offloads.txt').write_text('\n'.join(capture('exec','-T','router','ethtool','-k',dev) for dev in devices))
        (out/'router-kernel.txt').write_text(capture('exec','-T','router','uname','-a'))
        for profile in args.profiles:
            for suite in args.suites:
                # Outage recovery is a separate suite, not a full profile Cartesian matrix.
                if suite in ('outage','churn') and profile!='loss1-rtt100-5mbit':
                    continue
                for rep in range(args.repetitions):
                    for kind in (['ws','wt'] if rep%2 else ['wt','ws']):
                        key=(profile,suite,rep,kind)
                        if key in completed:
                            continue
                        name=f'{profile}-{suite}-{rep}-{kind}'
                        result_path=raw/(name+'.json')
                        if result_path.exists():
                            raise RuntimeError('Uncheckpointed result exists; inspect before rerun: '+str(result_path))
                        qdisc(devices,PROFILES[profile])
                        compose('up','-d','--force-recreate','server',stdout=subprocess.DEVNULL)
                        # Docker 'started' does not mean the Rust TLS listener is bound.
                        # Probe locally before creating either transport's browser clients.
                        for _ in range(120):
                            ready=subprocess.run(CMD+['exec','-T','server','bash','-c','set -e; exec 3<>/dev/tcp/127.0.0.1/4433; exec 3<&-; exec 3>&-'],capture_output=True,env=environment)
                            if ready.returncode==0:break
                            time.sleep(.25)
                        else:raise RuntimeError('Server TCP listener readiness timed out')
                        before=capture('exec','-T','router','tc','-s','-j','qdisc','show')
                        variables=dict(EDIT_INTERVAL_MS='200' if suite=='edits' else '1000',SUITES=suite if suite!='outage' else 'edits',KINDS=kind,REPETITIONS='1',
                            EXPECTED_REPETITIONS=str(args.repetitions),REPETITION_INDEX=str(rep),
                            WARMUP_SECONDS=str(args.warmup),MEASURE_SECONDS=str(args.duration),
                            NETWORK_PROFILE=profile,OUTPUT='/app/'+str(result_path.relative_to(ROOT)))
                        extra=[v for k,value in variables.items() for v in ['-e',k+'='+value]]
                        events=[]
                        start=time.monotonic()
                        active_container='amwt-resilience-'+name
                        with (raw/(name+'.log')).open('w') as log:
                            process=subprocess.Popen(CMD+['run','--rm','--name',active_container,*extra,'clients'],env=environment,stdout=log,stderr=subprocess.STDOUT)
                            outage_start=None
                            measurement_seen=None
                            while process.poll() is None:
                                if time.monotonic()-start>600:
                                    process.terminate()
                                    subprocess.run(['docker','rm','-f',active_container],capture_output=True)
                                    process.wait(timeout=30)
                                    raise TimeoutError('Attempt exceeded ten minutes: '+name)
                                if suite=='outage' and result_path.exists():
                                    try:
                                        state=json.loads(result_path.read_text())['rows'][0]
                                    except (ValueError,IndexError):
                                        state={}
                                    measurement=state.get('measurementStart')
                                    offset=min(15,args.duration/3)
                                    if measurement and measurement_seen is None:measurement_seen=time.monotonic()
                                    if measurement_seen is not None and outage_start is None and time.monotonic()-measurement_seen>=offset:
                                        prior=json.loads(capture('exec','-T','router','tc','-s','-j','qdisc','show'))
                                        qdisc(devices,['loss','100%','limit','256'])
                                        outage_start=time.monotonic()
                                        events.append(dict(event='blackout-start',epoch_ms=int(capture('exec','-T','router','date','+%s%3N').strip()),qdisc_before=prior))
                                    if outage_start and len(events)==1 and time.monotonic()-outage_start>=3:
                                        prior=json.loads(capture('exec','-T','router','tc','-s','-j','qdisc','show'))
                                        qdisc(devices,PROFILES[profile])
                                        events.append(dict(event='blackout-end',epoch_ms=int(capture('exec','-T','router','date','+%s%3N').strip()),qdisc_before=prior))
                                time.sleep(.2)
                        (raw/(name+'-server.log')).write_text(capture('logs','--no-color','server'))
                        after=capture('exec','-T','router','tc','-s','-j','qdisc','show')
                        if not result_path.exists():
                            raise RuntimeError('Container exited without checkpoint: '+name)
                        result=json.loads(result_path.read_text())
                        assert len(result['rows'])==1
                        row=result['rows'][0]
                        row.update(profile=profile,suite=suite,rep=rep,kind=kind,networkEvents=events,
                                   netemBefore=json.loads(before),netemAfter=json.loads(after),externalExitCode=process.returncode)
                        if process.returncode:
                            row.update(status='failed',error=row.get('error',f'Container exited {process.returncode}'))
                        if suite=='outage' and len(events)==2:
                            restored=events[1]['epoch_ms']
                            recovered=[min([d['received']-restored for d in row.get('deliveries',[]) if d['receiver']==i and d['sent']>=restored] or [float('inf')]) for i in range(row['clients'])]
                            row['freshRecoveryMs']=max(recovered) if all(x!=float('inf') for x in recovered) else None
                        if suite=='outage' and len(events)!=2:
                            row.update(status='failed',error='Outage schedule did not complete')
                        row['runtimeMetadata']=result['metadata']
                        rows.append(row)
                        save()
                        print(json.dumps({k:row.get(k) for k in ['profile','suite','rep','kind','status','p50','p95','bulkP95','bulkTransferMs','error']}),flush=True)
                        # Memory/resource failures stop the suite instead of increasing offered load.
                        if process.returncode or row.get('resourcePressure'):
                            raise RuntimeError('Stopped after container/resource failure; results retained')
    finally:
        if active_container:subprocess.run(['docker','rm','-f',active_container],capture_output=True)
        compose('down','--remove-orphans',stdout=subprocess.DEVNULL)


if __name__=='__main__':
    main()
