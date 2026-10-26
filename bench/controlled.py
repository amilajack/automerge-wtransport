#!/usr/bin/env python3
"""Run each transport against a fresh server, with symmetric netem on a dedicated router."""
import json,os,pathlib,subprocess,time
ROOT=pathlib.Path(__file__).resolve().parent.parent;os.chdir(ROOT)
CMD=['docker','compose','-f','bench/compose.yml']
def call(args,**kw):return subprocess.run(args,check=True,**kw)
def compose(*args,**kw):return call(CMD+list(args),**kw)
profiles={'baseline':(0,0,0),'rtt50-loss01-20mbit':(25,.1,20),'rtt150-loss1-5mbit':(75,1,5)}
rows=[];metadata=None
try:
 compose('build')
 compose('up','-d','router')
 # Wait for tools inside the router; no host network impairment is applied.
 for _ in range(120):
  r=subprocess.run(CMD+['exec','-T','router','test','-e','/ready'],capture_output=True)
  if r.returncode==0:break
  time.sleep(1)
 else:raise RuntimeError('Router readiness timeout')
 for profile in os.getenv('PROFILES',','.join(profiles)).split(','):
  delay,loss,rate=profiles[profile];os.environ['NETWORK_PROFILE']=profile
  # Resolve devices by actual assigned subnet rather than relying on eth ordering.
  output=subprocess.check_output(CMD+['exec','-T','router','ip','-j','-4','address'],text=True)
  links=json.loads(output);devs=[x['ifname'] for x in links if any(a['local'] in ('172.29.81.1','172.29.82.1') for a in x['addr_info'])];assert len(devs)==2
  for dev in devs:
   subprocess.run(CMD+['exec','-T','router','tc','qdisc','del','dev',dev,'root'],capture_output=True)
   if delay:compose('exec','-T','router','tc','qdisc','add','dev',dev,'root','netem','delay',f'{delay}ms','loss',f'{loss}%','rate',f'{rate}mbit','limit','10000')
  compose('exec','-T','router','tc','-s','qdisc','show')
  suites=os.getenv('SUITES','initial,edits,isolation,stress,churn,stalled,oversized').split(',')
  for suite in suites:
   cases=[(int(s),2) for s in os.getenv('SIZES','65536,1048576,8388608').split(',')] if suite=='initial' else [(65536,int(n)) for n in os.getenv('CLIENTS','2,8,32,128').split(',')] if suite=='stress' else [(65536,2)]
   stop=False
   for size,clients in cases:
    if stop:break
    case_failed=False
    for rep in range(int(os.getenv('REPETITIONS','3'))):
     for kind in (['ws','wt'] if rep%2 else ['wt','ws']):
      compose('up','-d','--force-recreate','server') # /data is ephemeral in this container.
      name=f'controlled-{profile}-{suite}-{size}-{clients}-{rep}-{kind}.json'
      args=[]
      for key,value in {'SUITES':suite,'SIZES':str(size),'CLIENTS':str(clients),'KINDS':kind,'REPETITIONS':'1','EXPECTED_REPETITIONS':os.getenv('REPETITIONS','3'),'REPETITION_INDEX':str(rep),'OUTPUT':'/app/results/'+name}.items():args+=['-e',f'{key}={value}']
      completed=subprocess.run(CMD+['run','--rm',*args,'clients'])
      if not (ROOT/'results'/name).exists():raise RuntimeError(f'Client container exited {completed.returncode} without a results file')
      result=json.loads((ROOT/'results'/name).read_text());metadata=result['metadata'];metadata['netem']={'oneWayDelayMs':delay,'directionalLossPercent':loss,'directionalRateMbps':rate,'routerDevices':devs};rows+=result['rows']
      for row in result['rows']:
       row['networkProfile']=profile
       if completed.returncode:
        row['status']='failed';row['externalExitCode']=completed.returncode;row.setdefault('error',f'Client container exited {completed.returncode}; possible resource exhaustion')
      (ROOT/'results/controlled-combined.json').write_text(json.dumps({'metadata':metadata|{'networkProfile':'multiple; see row profiles'},'rows':rows},indent=2))
      if completed.returncode:raise RuntimeError(f'Client container resource/error exit {completed.returncode}; recorded results and stopping load')
      if any(r['status']!='passed' for r in result['rows']):case_failed=True
    stop=case_failed
finally:compose('down','--remove-orphans')
