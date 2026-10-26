#!/usr/bin/env python3
"""Discover enabled regions and generate Terraform's required static provider aliases."""
import hashlib,json,os,pathlib,secrets,subprocess,tarfile,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[2];HERE=pathlib.Path(__file__).resolve().parent
os.chdir(ROOT)
def aws(*args):return json.loads(subprocess.check_output(['aws',*args,'--output','json'],text=True))
regions=sorted(x['RegionName'] for x in aws('ec2','describe-regions','--all-regions','--region','us-west-1')['Regions'] if x['OptInStatus'] in ('opted-in','opt-in-not-required'))
(HERE/'regions.json').write_text(json.dumps(regions,indent=2)+'\n')
varsfile=HERE/'experiment.auto.tfvars.json'
if varsfile.exists():v=json.loads(varsfile.read_text())
else:v={'name':'amwt-global-'+secrets.token_hex(4),'experiment_token':secrets.token_urlsafe(32),'enable_functions':False}
v['client_cidr']=urllib.request.urlopen('https://checkip.amazonaws.com',timeout=10).read().decode().strip()+'/32'
art=HERE/'artifacts';art.mkdir(exist_ok=True);art.chmod(0o700)
if not (art/'cert.pem').exists():
 subprocess.run(['openssl','ecparam','-name','prime256v1','-genkey','-noout','-out',str(art/'key.pem')],check=True)
 subprocess.run(['openssl','req','-new','-x509','-key',str(art/'key.pem'),'-out',str(art/'cert.pem'),'-days','10','-subj','/CN=automerge-global-relay'],check=True)
 (art/'key.pem').chmod(0o600)
der=subprocess.check_output(['openssl','x509','-in',str(art/'cert.pem'),'-outform','DER']);v['certificate_hash']=hashlib.sha256(der).hexdigest();(art/'hash.hex').write_text(v['certificate_hash']+'\n')
(art/'server.env').write_text('EXPERIMENT_TOKEN='+v['experiment_token']+'\nDATA_DIR=/var/lib/amwt-global\nCERT=cert.pem\nKEY=key.pem\nCERT_HASH=hash.hex\nQUEUE_CAPACITY=64\nMAX_SESSIONS=64\nWEB_DIR=dist\n')
(art/'server.env').chmod(0o600)
# Server image is compiled locally for ARM64; transfer through Terraform-owned private S3.
container=subprocess.check_output(['docker','create','automerge-wtransport:server'],text=True).strip()
try:subprocess.run(['docker','cp',container+':/app/server',str(art/'server')],check=True)
finally:subprocess.run(['docker','rm',container],check=True,stdout=subprocess.DEVNULL)
with tarfile.open(art/'relay.tgz','w:gz') as t:
 for name in ['server','server.env','cert.pem','key.pem','hash.hex']:t.add(art/name,arcname=name)
(art/'relay.tgz').chmod(0o600)
v['image_tag']=hashlib.sha256(b''.join(p.read_bytes() for p in [ROOT/'Cargo.lock',ROOT/'src/client.rs',ROOT/'src/bin/lambda-client.rs',ROOT/'lambda/Dockerfile'])).hexdigest()[:20]
varsfile.write_text(json.dumps(v,indent=2)+'\n');varsfile.chmod(0o600)
providers=[{'region':'us-west-1','default_tags':[{'tags':{'Project':'automerge-wtransport-global','Experiment':v['name']}}]}]
resources={}
def resource(kind,name,body):resources.setdefault(kind,{})[name]=body
for region in regions:
 alias=region.replace('-','_');providers.append({'alias':alias,'region':region,'default_tags':[{'tags':{'Project':'automerge-wtransport-global','Experiment':v['name']}}]})
 provider='aws.'+alias
 count='${var.enable_functions && (length(var.regions_to_enable) == 0 || contains(var.regions_to_enable, '+json.dumps(region)+')) ? 1 : 0}'
 resource('aws_ecr_repository',alias,{'provider':provider,'name':'${var.name}','force_delete':True,'image_tag_mutability':'IMMUTABLE','image_scanning_configuration':[{'scan_on_push':True}]})
 resource('aws_ecr_repository_policy',alias,{'provider':provider,'repository':'${aws_ecr_repository.'+alias+'.name}','policy':json.dumps({'Version':'2012-10-17','Statement':[{'Sid':'LambdaImageRetrieval','Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':['ecr:BatchGetImage','ecr:GetDownloadUrlForLayer']} ]})})
 resource('aws_cloudwatch_log_group',alias,{'provider':provider,'name':'/aws/lambda/${var.name}','retention_in_days':3})
 resource('aws_lambda_function',alias,{'provider':provider,'count':count,'function_name':'${var.name}','role':'${aws_iam_role.lambda.arn}','package_type':'Image','image_uri':'${aws_ecr_repository.'+alias+'.repository_url}@${data.aws_ecr_image.'+alias+'[0].image_digest}','architectures':['arm64'],'memory_size':256,'timeout':150,'environment':[{'variables':{'RELAY_URL':'https://${aws_instance.relay.public_ip}:4433','EXPERIMENT_TOKEN':'${var.experiment_token}','CERTIFICATE_HASH':'${var.certificate_hash}'}}],'depends_on':['aws_iam_role_policy_attachment.lambda_logs','aws_cloudwatch_log_group.'+alias,'aws_ecr_repository_policy.'+alias]})
doc={'terraform':{'required_version':'>= 1.5.7','required_providers':{'aws':{'source':'hashicorp/aws','version':'~> 6.0'}}},'provider':{'aws':providers},'resource':resources,'data':{'aws_ecr_image':{r.replace('-','_'):{'provider':'aws.'+r.replace('-','_'),'count':'${var.enable_functions && (length(var.regions_to_enable) == 0 || contains(var.regions_to_enable, '+json.dumps(r)+')) ? 1 : 0}','repository_name':'${aws_ecr_repository.'+r.replace('-','_')+'.name}','image_tag':'${var.image_tag}'} for r in regions}},'output':{
 'repositories':{'value':{r:'${aws_ecr_repository.'+r.replace('-','_')+'.repository_url}' for r in regions}},
 'functions':{'value':{r:'${try(aws_lambda_function.'+r.replace('-','_')+'[0].function_name, null)}' for r in regions}},
 'relay_ip':{'value':'${aws_instance.relay.public_ip}'},'relay_instance':{'value':'${aws_instance.relay.id}'}}}
(HERE/'regions.tf.json').write_text(json.dumps(doc,indent=2)+'\n')
print('Prepared Terraform for '+str(len(regions))+' enabled regions: '+', '.join(regions))
