#!/usr/bin/env python3
"""Invoke paired clients; record sender-clock RTT, not unsynchronized one-way timestamps."""
import argparse,base64,concurrent.futures,hashlib,json,math,os,pathlib,random,statistics,subprocess,time,uuid
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parents[1];os.chdir(ROOT);os.umask(0o077)
p=argparse.ArgumentParser();p.add_argument('--transports',nargs='+',choices=['webtransport','websocket'],default=['webtransport','websocket']);p.add_argument('--regions',nargs='*');p.add_argument('--samples',type=int,default=10);p.add_argument('--warmup',type=int,default=2);p.add_argument('--repetitions',type=int,default=3);p.add_argument('--concurrency',type=int,default=4);p.add_argument('--payload-bytes',type=int,default=64);p.add_argument('--interval-ms',type=int,default=100);p.add_argument('--output',default='results/global-comparison/latency.json');p.add_argument('--resume',action='store_true');p.add_argument('--skip-restart',action='store_true');a=p.parse_args()
assert 1<=a.samples<=100 and 0<=a.warmup<=10 and 1<=a.repetitions<=3 and 1<=a.concurrency<=4 and 0<=a.payload_bytes<=16384 and 0<=a.interval_ms<=1000
outputs=json.loads(subprocess.check_output(['terraform','-chdir='+str(HERE),'output','-json'],text=True));functions={k:v for k,v in outputs.get('functions',{}).get('value',{}).items() if v};
if not functions:raise SystemExit('No regional clients are deployed. Run the Terraform bootstrap, publish, and functions commands first.')
regions=sorted(a.regions or functions)
assert len(set(a.transports)) == len(a.transports)
assert regions and all(r in functions for r in regions),'Requested region has no deployed client'
path=ROOT/a.output;path.parent.mkdir(parents=True,exist_ok=True);raw=path.parent/'raw';raw.mkdir(exist_ok=True)
metadata={'at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'regions':regions,'relay_region':'us-west-1','relay_instance':outputs['relay_instance']['value'],'relay_ip':outputs['relay_ip']['value'],'relay_instance_type':'t4g.small','relay_cpu_quota':'100% of one CPU','relay_memory_max_bytes':1073741824,'relay_queue_capacity':64,'function_memory_mb':256,'function_timeout_s':150,'handler_deadline_s':120,'samples':a.samples,'warmup':a.warmup,'repetitions':a.repetitions,'concurrency':a.concurrency,'payload_bytes':a.payload_bytes,'interval_ms':a.interval_ms,'schedule_seed':49217,'metric':'sender monotonic request/reply RTT through persistent Automerge relay; not direct Lambda-to-Lambda QUIC or one-way latency','transports':a.transports,'transport_order':'separate transport phases; order reversed each repetition; identical shuffled pair schedule','websocket_tcp_nodelay':True,'websocket_compression':False,'tls':'TLS 1.3 with identical pinned certificate','restart_policy':'relay restart with fresh data directory before each transport phase; fresh document per attempt','run_id':uuid.uuid4().hex[:8],'automerge':'0.12.0','architecture':'arm64','cargo_lock_sha256':hashlib.sha256((ROOT/'Cargo.lock').read_bytes()).hexdigest(),'lambda_image':subprocess.check_output(['docker','image','inspect','automerge-wtransport:lambda','--format','{{.Id}}'],text=True).strip()}
rows=[]
if a.resume and path.exists():
 prior=json.loads(path.read_text());assert all(prior['metadata'][k]==metadata[k] for k in ['regions','samples','warmup','repetitions','payload_bytes','interval_ms','lambda_image','cargo_lock_sha256','relay_instance','concurrency','transports']);rows=prior['rows'];metadata=prior['metadata']
def save():
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps({'metadata':metadata,'rows':rows},indent=2)+'\n');tmp.replace(path)
completed={(r['source_region'],r['destination_region'],r['repetition'],r['transport']) for r in rows}
save()
def invoke(region,role,document,transport):
 payload={'transport':transport,'role':role,'document':document,'samples':a.samples,'warmup':a.warmup,'payload_bytes':a.payload_bytes,'interval_ms':a.interval_ms}
 response=raw/(document+'-'+role+'.json');start=time.time()
 command=['aws','lambda','invoke','--region',region,'--function-name',functions[region],'--invocation-type','RequestResponse','--log-type','Tail','--cli-binary-format','raw-in-base64-out','--payload',json.dumps(payload),'--cli-connect-timeout','10','--cli-read-timeout','180',str(response),'--output','json']
 result=subprocess.run(command,text=True,capture_output=True,env=os.environ|{'AWS_MAX_ATTEMPTS':'1'},timeout=200)
 if result.returncode:raise RuntimeError(region+' '+role+' invoke failed: '+result.stderr[-2000:])
 info=json.loads(result.stdout);body=json.loads(response.read_text());return {'region':region,'role':role,'invoke_elapsed_s':time.time()-start,'function_error':info.get('FunctionError'),'log_tail':base64.b64decode(info.get('LogResult','')).decode('utf8',errors='replace'),'response':body}
def pair(case):
 source,dest,rep,transport=case;document=str(uuid.uuid4());row={'transport':transport,'source_region':source,'destination_region':dest,'repetition':rep,'document':document,'status':'failed','started_at':time.time()}
 try:
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   responder=pool.submit(invoke,dest,'responder',document,transport);sender=pool.submit(invoke,source,'source',document,transport)
   # Read both results even if one endpoint fails; each has bounded execution.
   errors=[]
   for label,future in [('source',sender),('responder',responder)]:
    try:row[label]=future.result()
    except Exception as e:errors.append(str(e))
  if errors:raise RuntimeError('; '.join(errors))
  for label in ['source','responder']:
   assert not row[label]['function_error'],label+': '+json.dumps(row[label]['response'])
  src=row['source']['response'];dst=row['responder']['response'];assert src['transport']==transport and dst['transport']==transport,'Transport mismatch';assert src['region']==source and dst['region']==dest,'Region mismatch';assert src['heads']==dst['heads'],'Endpoint heads failed to converge';assert src['acknowledged'] and dst['acknowledged'],'Missing acknowledgement';assert len(src['rtt_ms'])==a.samples,'Incomplete RTT samples';assert dst['messages_received']==a.samples+a.warmup,'Missing request/reply messages';assert all(x>=0 for x in src['rtt_ms']),'Invalid monotonic RTT'
  row['status']='passed';row['rtt_ms']=src['rtt_ms'];row['p50_ms']=statistics.median(src['rtt_ms']);row['p95_ms']=sorted(src['rtt_ms'])[math.ceil(.95*len(src['rtt_ms']))-1]
 except Exception as e:row['error']=str(e)
 row['elapsed_s']=time.time()-row['started_at'];return row
for rep in range(a.repetitions):
 order=list(a.transports)
 if rep%2:order.reverse()
 for transport in order:
  cases=[(s,d,rep,transport) for s in regions for d in regions if (s,d,rep,transport) not in completed]
  random.Random(49217+rep).shuffle(cases)
  if not cases:continue
  phase=f"{transport}-{rep}-{metadata['run_id']}"
  if not a.skip_restart:subprocess.run(['python3',str(HERE/'manage.py'),'phase','--phase',phase],check=True)
  with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as pool:
   futures=[pool.submit(pair,case) for case in cases]
   for future in concurrent.futures.as_completed(futures):
    row=future.result();row['transport_order']=order;row['phase']=phase
    rows.append(row);save();print(json.dumps({k:row.get(k) for k in ['transport','source_region','destination_region','repetition','status','p50_ms','p95_ms','error']}),flush=True)
  current=[r for r in rows if r['repetition']==rep and r['transport']==transport]
  if len(current)>=10 and sum(r['status']!='passed' for r in current)/len(current)>.05:
   raise SystemExit('Stopped after phase due to >5% failure rate; inspect retained failures')
print('Saved',len(rows),'paired attempts to',path)
if any(r['status']!='passed' for r in rows):raise SystemExit(1)
