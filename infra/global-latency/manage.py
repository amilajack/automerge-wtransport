#!/usr/bin/env python3
"""Terraform lifecycle + artifact publishing. All infrastructure resources live in this state."""
import argparse,concurrent.futures,json,os,pathlib,re,shutil,ssl,subprocess,sys,time,urllib.request
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parents[1];os.chdir(ROOT);os.umask(0o077)
VARS=HERE/'experiment.auto.tfvars.json'
def aws(*args,region='us-west-1',**kw):
 return json.loads(subprocess.check_output(['aws',*args,'--region',region,'--output','json'],text=True,**kw))
def tf(*args,capture=False):
 command=['terraform','-chdir='+str(HERE),*args]
 if capture:return json.loads(subprocess.check_output(command,text=True))
 subprocess.run(command,check=True)
def outputs():
 d=tf('output','-json',capture=True);(HERE/'outputs.json').write_text(json.dumps(d,indent=2));return {k:v['value'] for k,v in d.items()}
def apply(enable,regions,replace_relay=False):
 v=json.loads(VARS.read_text());v['enable_functions']=enable;v['regions_to_enable']=regions;VARS.write_text(json.dumps(v,indent=2)+'\n')
 stage='functions' if enable else 'bootstrap';log=ROOT/'results'/f'global-terraform-{stage}.log'
 with log.open('w') as f:
  subprocess.run(['terraform','-chdir='+str(HERE),'plan','-input=false','-parallelism=4','-out=deploy.tfplan'],check=True,stdout=f,stderr=subprocess.STDOUT)
 plan=tf('show','-json','deploy.tfplan',capture=True);changes=[r for r in plan.get('resource_changes',[]) if r['change']['actions']!=['no-op'] and r['mode']=='managed']
 if any('delete' in r['change']['actions'] and not (replace_relay and r['address']=='aws_instance.relay') for r in changes):raise RuntimeError('Deploy plan contains deletion/replacement; inspect deploy.tfplan before changing existing experiment configuration')
 print('Terraform plan:',len(changes),'managed resource changes; relay replacement allowed' if replace_relay else 'managed resource changes; no deletion/replacement',flush=True)
 with log.open('a') as f:subprocess.run(['terraform','-chdir='+str(HERE),'apply','-input=false','-parallelism=4','deploy.tfplan'],check=True,stdout=f,stderr=subprocess.STDOUT)
 return outputs()
def publish(regions):
 v=json.loads(VARS.read_text());v['image_tag']=subprocess.check_output(['docker','image','inspect','automerge-wtransport:lambda','--format','{{.Id}}'],text=True).strip().split(':')[1][:20];VARS.write_text(json.dumps(v,indent=2)+'\n');o=outputs();regions=regions or list(o['repositories'])
 host=subprocess.check_output(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip();config=HERE/'artifacts/docker-auth';config.mkdir(exist_ok=True);config.chmod(0o700)
 docker_env=os.environ|{'DOCKER_CONFIG':str(config),'DOCKER_HOST':host}
 def one(region):
  repo=o['repositories'][region]
  images=aws('ecr','list-images','--repository-name',v['name'],region=region)['imageIds']
  if any(x.get('imageTag')==v['image_tag'] for x in images):return region+' already published'
  password=subprocess.check_output(['aws','ecr','get-login-password','--region',region],text=True)
  subprocess.run(['docker','login','--username','AWS','--password-stdin',repo.split('/')[0]],input=password,text=True,env=docker_env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
  target=repo+':'+v['image_tag'];subprocess.run(['docker','tag','automerge-wtransport:lambda',target],check=True,env=docker_env)
  subprocess.run(['docker','push',target],check=True,env=docker_env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=300)
  return region+' published'
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
  for result in pool.map(one,regions):print(result,flush=True)
def ssm(commands,timeout=300):
 o=outputs();request=aws('ssm','send-command','--instance-ids',o['relay_instance'],'--document-name','AWS-RunShellScript','--parameters',json.dumps({'commands':['set -eu',*commands],'executionTimeout':[str(timeout)]}))
 ident=request['Command']['CommandId'];end=time.time()+timeout+30
 while time.time()<end:
  time.sleep(1)
  try:r=aws('ssm','get-command-invocation','--command-id',ident,'--instance-id',o['relay_instance'])
  except subprocess.CalledProcessError:continue
  if r['Status'] in ('Pending','InProgress','Delayed'):continue
  if r['Status']!='Success':raise RuntimeError(r.get('StandardErrorContent','')+'\n'+r.get('StandardOutputContent',''))
  return r.get('StandardOutputContent','')
 raise TimeoutError('SSM timed out '+ident)
def wait():
 o=outputs();end=time.time()+600
 while time.time()<end:
  try:
   response=urllib.request.urlopen('https://'+o['relay_ip']+':4433/healthz',context=ssl._create_unverified_context(),timeout=5)
   if response.read()==b'ok':print('Relay ready:',o['relay_ip']);return
  except Exception:time.sleep(3)
 raise TimeoutError('Relay startup timed out; inspect SSM/cloud-init logs')
def archive():
 o=outputs();key='snapshots/'+time.strftime('%Y%m%d-%H%M%S')+'.tgz'
 print(ssm(['systemctl stop amwt-global','tar czf /tmp/amwt-snapshots.tgz -C /var/lib/amwt-global .','aws s3 cp /tmp/amwt-snapshots.tgz s3://'+o['artifact_bucket']+'/'+key+' --region us-west-1','rm /tmp/amwt-snapshots.tgz']))
 dest=ROOT/os.environ.get('AMWT_RESULTS_DIR','results/global-comparison')/'snapshots.tgz';dest.parent.mkdir(parents=True,exist_ok=True)
 subprocess.run(['aws','s3','cp','s3://'+o['artifact_bucket']+'/'+key,str(dest),'--region','us-west-1'],check=True);print('Snapshots preserved at',dest)
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('command',choices=['bootstrap','publish','functions','wait','status','logs','restart','phase','relay-code','archive','destroy']);parser.add_argument('--regions',nargs='*',default=[]);parser.add_argument('--phase');parser.add_argument('--replace-relay',action='store_true');a=parser.parse_args()
 if a.command=='bootstrap':apply(False,[],a.replace_relay)
 elif a.command=='publish':publish(a.regions)
 elif a.command=='functions':apply(True,a.regions)
 elif a.command=='wait':wait()
 elif a.command=='status':print(ssm(['systemctl status amwt-global --no-pager || true','systemctl show amwt-global -p CPUQuotaPerSecUSec -p MemoryMax -p MemoryPeak','uname -a','free -m']))
 elif a.command=='logs':print(ssm(['tail -80 /var/log/cloud-init-output.log','journalctl -u amwt-global -n 80 --no-pager']))
 elif a.command=='restart':print(ssm(['systemctl restart amwt-global']));wait()
 elif a.command=='relay-code':
  v=json.loads(VARS.read_text());o=apply(True,v.get('regions_to_enable',[]))
  print(ssm(['systemctl stop amwt-global', 'aws s3 cp s3://'+o['artifact_bucket']+'/relay.tgz /tmp/amwt-relay-update.tgz --region us-west-1',
    'tar xzf /tmp/amwt-relay-update.tgz -C /opt/amwt-global server', 'chmod 755 /opt/amwt-global/server',
    'rm /tmp/amwt-relay-update.tgz', 'systemctl start amwt-global', 'sha256sum /opt/amwt-global/server']))
  wait()
 elif a.command=='phase':
  if not a.phase or not re.fullmatch(r'(webtransport|websocket)-[0-2]-[0-9a-f]{8}',a.phase):raise ValueError('Invalid benchmark phase')
  directory='/var/lib/amwt-global/'+a.phase
  print(ssm(['systemctl stop amwt-global', 'install -d -m 700 -o amwt-global -g amwt-global '+directory,
    "sed -i 's#^DATA_DIR=.*#DATA_DIR="+directory+"#' /opt/amwt-global/server.env", 'systemctl start amwt-global']))
  wait()
 elif a.command=='archive':archive()
 elif a.command=='destroy':
  state=tf('show','-json',capture=True)
  managed=[r for r in state.get('values',{}).get('root_module',{}).get('resources',[]) if r.get('mode')=='managed']
  if not managed:print('No Terraform-owned resources remain')
  else:
   archive();tf('plan','-destroy','-input=false','-parallelism=4','-out=destroy.tfplan');tf('apply','-input=false','-parallelism=4','destroy.tfplan')
  shutil.rmtree(HERE/'artifacts/docker-auth',ignore_errors=True)
