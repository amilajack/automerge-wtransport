#!/usr/bin/env python3
"""Pinned-TLS resource samples, stopped when the checkpoint contains the full matrix."""
import argparse,hashlib,http.client,json,pathlib,ssl,time
parser=argparse.ArgumentParser();parser.add_argument('--directory',default='results/global-comparison');a=parser.parse_args()
HERE=pathlib.Path(__file__).resolve().parent;ROOT=HERE.parents[1];variables=json.loads((HERE/'experiment.auto.tfvars.json').read_text());outputs=json.loads((HERE/'outputs.json').read_text());ip=outputs['relay_ip']['value'];directory=ROOT/a.directory;directory.mkdir(parents=True,exist_ok=True);out=directory/'relay-metrics.json';samples=[];deadline=time.time()+7200
while time.time()<deadline:
 row={'at':time.time()}
 try:
  connection=http.client.HTTPSConnection(ip,4433,timeout=10,context=ssl._create_unverified_context());connection.connect()
  if hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()!=variables['certificate_hash']:raise RuntimeError('Relay certificate pin mismatch')
  connection.request('GET','/metrics?token='+variables['experiment_token']);response=connection.getresponse()
  if response.status!=200:raise RuntimeError('metrics HTTP '+str(response.status))
  row['metrics']=json.loads(response.read());connection.close()
 except Exception as e:row['error']=str(e)
 samples.append(row);tmp=out.with_suffix('.tmp');tmp.write_text(json.dumps(samples,indent=2));tmp.replace(out)
 try:
  d=json.loads((directory/'latency.json').read_text());m=d['metadata'];expected=len(m['regions'])**2*m['repetitions']*len(m.get('transports',['webtransport']))
  if len(d['rows'])>=expected:break
 except (FileNotFoundError,json.JSONDecodeError):pass
 time.sleep(5)
print('Saved',len(samples),'pinned-TLS relay metric samples')
