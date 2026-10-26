#!/usr/bin/env python3
"""Experiment-scoped AWS/SSM lifecycle; credentials stay in the normal AWS CLI chain."""
import argparse,json,os,pathlib,secrets,shlex,subprocess,sys,time,urllib.request,tarfile
ROOT=pathlib.Path(__file__).resolve().parent.parent
STATE=ROOT/'deploy/state.json';REGION='us-west-1';INSTANCE='i-0a4a44c4ac1e0b42b';SG='sg-066b774f8762ceacc';NAME='automerge-wtransport'
def call(args):return subprocess.check_output(args,text=True).strip()
def aws(*args):
 s=call(['aws',*args,'--region',REGION,'--output','json']);return json.loads(s) if s else {}
def save(s):STATE.write_text(json.dumps(s,indent=2));STATE.chmod(0o600)
def ssm(commands,timeout=300):
 r=aws('ssm','send-command','--instance-ids',INSTANCE,'--document-name','AWS-RunShellScript','--timeout-seconds',str(timeout),'--parameters',json.dumps({'commands':['set -eu',*commands],'executionTimeout':[str(timeout)]}))
 ident=r['Command']['CommandId'];deadline=time.time()+timeout+20
 while time.time()<deadline:
  time.sleep(1)
  try:r=aws('ssm','get-command-invocation','--command-id',ident,'--instance-id',INSTANCE)
  except subprocess.CalledProcessError:continue
  if r['Status'] in ('Pending','InProgress','Delayed'):continue
  if r['Status']!='Success':raise RuntimeError(r.get('StandardErrorContent','')+'\n'+r.get('StandardOutputContent',''))
  return r.get('StandardOutputContent','')
 raise TimeoutError('SSM command timeout '+ident)
def load():return json.loads(STATE.read_text())
def health():return ssm(['systemctl is-active xwing xwing-proxy','curl -fsS --max-time 5 http://127.0.0.1:8080/health','free -m','systemctl show xwing xwing-proxy -p MemoryCurrent -p CPUUsageNSec'])
def reset(s):
 suffix=time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)
 return ssm([f'systemctl stop {NAME}',f'install -d -o {NAME} -g {NAME} -m 700 /var/lib/{NAME}/{suffix}',f"sed -i '/^DATA_DIR=/d' /etc/{NAME}.env",f"printf '%s\\n' 'DATA_DIR=/var/lib/{NAME}/{suffix}' >> /etc/{NAME}.env",f'systemctl start {NAME} {NAME}-guard',f'systemctl is-active {NAME}','curl -kfsS --retry 10 --retry-delay 1 --retry-connrefused https://127.0.0.1:4433/healthz'])
def deploy():
 if STATE.exists():raise RuntimeError('Experiment state already exists; use status/stop/cleanup before a new deploy')
 print('Existing-service baseline:',health(),flush=True)
 info=aws('ec2','describe-instances','--instance-ids',INSTANCE)['Reservations'][0]['Instances'][0]
 if info['Architecture']!='arm64' or info['PublicIpAddress']!='52.9.189.226':raise RuntimeError('Target identity changed')
 ip=urllib.request.urlopen('https://checkip.amazonaws.com',timeout=10).read().decode().strip();import ipaddress;ipaddress.IPv4Address(ip)
 state={'instance':INSTANCE,'region':REGION,'securityGroup':SG,'publicIP':info['PublicIpAddress'],'clientIP':ip,'ruleIDs':[],'instanceType':info['InstanceType'],'architecture':info['Architecture'],'imageId':info['ImageId'],'createdAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())};save(state)
 token=secrets.token_urlsafe(32);(ROOT/'certs/token').write_text(token);(ROOT/'certs/token').chmod(0o600)
 stage=ROOT/'deploy/staging';stage.mkdir(exist_ok=True)
 container=call(['docker','create','automerge-wtransport:server'])
 try:subprocess.run(['docker','cp',container+':/app/server',str(stage/'server')],check=True)
 finally:subprocess.run(['docker','rm',container],check=True,stdout=subprocess.DEVNULL)
 artifact=ROOT/'deploy/artifact.tgz'
 with tarfile.open(artifact,'w:gz') as t:
  for p,name in [(stage/'server','server'),(ROOT/'dist','dist'),(ROOT/'certs/cert.pem','cert.pem'),(ROOT/'certs/key.pem','key.pem'),(ROOT/'certs/hash.hex','hash.hex'),(ROOT/'deploy/guard.sh','guard.sh')]:t.add(p,arcname=name)
 artifact.chmod(0o600)
 bucket=f'automerge-wt-{aws("sts","get-caller-identity")["Account"]}-{secrets.token_hex(5)}';state['bucket']=bucket;save(state)
 aws('s3api','create-bucket','--bucket',bucket,'--create-bucket-configuration',json.dumps({'LocationConstraint':REGION}))
 aws('s3api','put-public-access-block','--bucket',bucket,'--public-access-block-configuration',json.dumps(dict.fromkeys(['BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets'],True)))
 aws('s3api','put-object','--bucket',bucket,'--key','artifact.tgz','--body',str(artifact),'--server-side-encryption','AES256')
 signed=call(['aws','s3','presign',f's3://{bucket}/artifact.tgz','--expires-in','900','--region',REGION])
 unit=f'''[Unit]
Description=Automerge transport experiment
After=network-online.target
[Service]
Type=simple
User={NAME}
Group={NAME}
WorkingDirectory=/opt/{NAME}
EnvironmentFile=/etc/{NAME}.env
ExecStart=/opt/{NAME}/server
MemoryMax=256M
MemoryHigh=220M
CPUQuota=25%
TasksMax=64
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/{NAME}
Restart=no
[Install]
WantedBy=multi-user.target
'''
 guard=f'''[Unit]
Description=Guard existing game services during Automerge experiment
[Service]
Type=simple
ExecStart=/opt/{NAME}/guard.sh
Restart=no
'''
 suffix=time.strftime('%Y%m%d-%H%M%S')
 commands=[f'test ! -e /opt/{NAME}',f'curl -fsS {shlex.quote(signed)} -o /tmp/{NAME}.tgz',f'id {NAME} >/dev/null 2>&1 || useradd --system --home /var/lib/{NAME} --shell /usr/sbin/nologin {NAME}',f'install -d -m 755 /opt/{NAME}',f'tar xzf /tmp/{NAME}.tgz -C /opt/{NAME}',f'rm /tmp/{NAME}.tgz',f'chmod 755 /opt/{NAME}/server /opt/{NAME}/guard.sh',f'chown root:{NAME} /opt/{NAME}/key.pem',f'chmod 640 /opt/{NAME}/key.pem',f'install -d -o {NAME} -g {NAME} -m 700 /var/lib/{NAME}/{suffix}',f"cat > /etc/{NAME}.env <<'ENV'\nEXPERIMENT_TOKEN={token}\nDATA_DIR=/var/lib/{NAME}/{suffix}\nCERT=cert.pem\nKEY=key.pem\nCERT_HASH=hash.hex\nMAX_SESSIONS=16\nQUEUE_CAPACITY=4\nENV",f'chmod 600 /etc/{NAME}.env',f"cat > /etc/systemd/system/{NAME}.service <<'UNIT'\n{unit}UNIT",f"cat > /etc/systemd/system/{NAME}-guard.service <<'UNIT'\n{guard}UNIT",'systemctl daemon-reload',f'systemctl start {NAME} {NAME}-guard',f'systemctl is-active {NAME}','curl -kfsS --retry 10 --retry-delay 1 --retry-connrefused https://127.0.0.1:4433/healthz']
 print(ssm(commands),flush=True)
 for proto in ['tcp','udp']:
  r=aws('ec2','authorize-security-group-ingress','--group-id',SG,'--ip-permissions',json.dumps([{'IpProtocol':proto,'FromPort':4433,'ToPort':4433,'IpRanges':[{'CidrIp':ip+'/32','Description':'automerge-wtransport experiment '+state['createdAt']}]}]))
  state['ruleIDs'] += [x['SecurityGroupRuleId'] for x in r['SecurityGroupRules']];save(state)
 aws('s3api','delete-object','--bucket',bucket,'--key','artifact.tgz');aws('s3api','delete-bucket','--bucket',bucket);state.pop('bucket');save(state)
 print('Deployed https://'+state['publicIP']+':4433 ; token stored in certs/token');print(health())
def cleanup(s,purge):
 commands=[f'systemctl stop {NAME} {NAME}-guard || true',f'rm -f /etc/systemd/system/{NAME}.service /etc/systemd/system/{NAME}-guard.service /etc/{NAME}.env',f'rm -rf /opt/{NAME}','systemctl daemon-reload']
 if purge:commands += [f'rm -rf /var/lib/{NAME}',f'userdel {NAME} || true']
 print(ssm(commands))
 for rid in s['ruleIDs']:aws('ec2','revoke-security-group-ingress','--group-id',SG,'--security-group-rule-ids',rid)
 if s.get('bucket'):
  aws('s3api','delete-object','--bucket',s['bucket'],'--key','artifact.tgz');aws('s3api','delete-bucket','--bucket',s['bucket'])
 STATE.unlink();print('Cleaned experiment resources; snapshots '+('removed' if purge else 'preserved'))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('command',choices=['deploy','status','logs','stop','reset','cleanup','health']);p.add_argument('--purge-snapshots',action='store_true');args=p.parse_args();os.chdir(ROOT)
 if args.command=='deploy':deploy()
 elif args.command=='health':print(health())
 else:
  s=load()
  if args.command=='reset':print(health());print(reset(s))
  elif args.command=='status':print(ssm([f'systemctl status {NAME} {NAME}-guard --no-pager || true',f'systemctl show {NAME} -p MemoryCurrent -p MemoryPeak -p CPUUsageNSec -p CPUQuotaPerSecUSec -p MemoryMax',f'ss -lntup | grep 4433 || true','uname -a','lscpu','cat /etc/os-release']));print(health())
  elif args.command=='logs':print(ssm([f'journalctl -u {NAME} -u {NAME}-guard -n 100 --no-pager']))
  elif args.command=='stop':print(ssm([f'systemctl stop {NAME} {NAME}-guard',f'systemctl show {NAME} {NAME}-guard -p Id -p ActiveState -p SubState']));print(health())
  elif args.command=='cleanup':cleanup(s,args.purge_snapshots)
