#!/usr/bin/env python3
"""Prepare synthetic Helm inputs only for the named disposable Kind cluster."""
import argparse
import base64
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import yaml
from configure import certificate,create

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.local'
def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--kubeconfig',type=Path,required=True)
 parser.add_argument('--context',required=True)
 parser.add_argument('--check',action='store_true',help='Validate generated inputs against existing fixture Secrets without applying resources')
 args=parser.parse_args()
 if args.context!='kind-platform-integration':parser.error('This fixture helper is restricted to kind-platform-integration')
 kube=['kubectl','--kubeconfig',str(args.kubeconfig.resolve()),'--context',args.context]
 os.umask(0o077)
 local=LOCAL/'helm';local.mkdir(exist_ok=True)
 env=dict(line.split('=',1) for line in (LOCAL/'compose.env').read_text().splitlines() if line and not line.startswith('#'))
 images=json.loads((LOCAL/'images.json').read_text())
 if not (LOCAL/'operators.json').exists():
  if args.check:raise RuntimeError('Operator fixture input is missing; prepare it before --check')
  run(['docker','run','--rm','--network','none','--user',str(os.getuid())+':'+str(os.getgid()),
       '-v',str(LOCAL)+':/out',images['AUTH_IMAGE']['tag'],'bootstrap',
       '--token-file','/out/operators.json','--operator','operator'],stdout=subprocess.DEVNULL)
 pending=[]
 def apply(obj):
  pending.append(obj)
 def current(kind,ns,name):
  result=subprocess.run(kube+['-n',ns,'get',kind,name,'--ignore-not-found','-o','json'],capture_output=True,text=True,check=True)
  return json.loads(result.stdout) if result.stdout.strip() else None
 def save_values(name,defaults):
  path=local/(name+'.yaml')
  existing=yaml.safe_load(path.read_text()) if path.exists() else {}
  def missing(target,source):
   for key,value in source.items():
    if key not in target:target[key]=copy.deepcopy(value)
    elif isinstance(value,dict) and isinstance(target[key],dict):missing(target[key],value)
  missing(existing,defaults)
  ns='platform-'+name if name in ['shared','storage'] else 'tenant-'+name
  release=subprocess.run(['helm','get','values',name,'--kubeconfig',str(args.kubeconfig.resolve()),
      '--kube-context',args.context,'-n',ns,'-o','json'],capture_output=True,text=True)
  if release.returncode==0:
   live=json.loads(release.stdout) or {}
   existing['phase']=max(existing.get('phase',0),live.get('phase',0))
  elif 'release: not found' not in release.stderr:
   raise RuntimeError('Unable to inspect existing Helm release '+name+': '+release.stderr.strip())
  if not args.check:path.write_text(yaml.safe_dump(existing,sort_keys=False))
  return existing

 def secret(ns,name,data,kind='Opaque'):
  previous=current('secret',ns,name)
  if previous:
   live={key:base64.b64decode(value).decode() for key,value in previous.get('data',{}).items()}
   differences=[]
   for key,value in data.items():
    if key not in live:continue
    same=live[key]==value
    if not same and key.endswith('.json'):
     try:same=json.loads(live[key])==json.loads(value)
     except (ValueError,TypeError):pass
    if not same:differences.append(key)
   if differences:raise RuntimeError('Existing fixture Secret differs; explicitly reconcile '+ns+'/'+name+' keys: '+','.join(differences))
   if all(key in live for key in data):return
   data={**live,**data}
   metadata={'name':name,'namespace':ns,'resourceVersion':previous['metadata']['resourceVersion']}
  else:metadata={'name':name,'namespace':ns}
  apply({'apiVersion':'v1','kind':'Secret','metadata':metadata,'type':kind,'stringData':data})

 def namespace(ns,tenant=False):
  labels={'platform.scitrera.io/test-project':'platform-integration'}
  if tenant:labels['platform.scitrera.io/tenant']='true'
  previous=current('namespace',ns,ns)
  if previous and previous['metadata'].get('labels',{}).get('platform.scitrera.io/test-project')!='platform-integration':
   raise RuntimeError('Refusing to adopt unlabelled namespace '+ns)
  apply({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'labels':labels}})
 def url(user,pw,host,db=None):return 'postgresql://'+user+':'+pw+'@'+host+':5432/'+(db or user)+'?sslmode=disable'
 namespace('platform-shared');namespace('platform-storage')
 def envfile(path):return dict(line.split('=',1) for line in path.read_text().splitlines() if line and not line.startswith('#'))
 postgres=(LOCAL/'cluster/postgres-image.txt').read_text().strip()
 fixture_policy=yaml.safe_load((ROOT/'compose/profiles/fixtures.yaml').read_text())['services']

 for t in ['alpha','beta']:
  ns='tenant-'+t;namespace(ns,True)
  tls=LOCAL/t/'tls'
  certificate(local/t/'server',t+'-aether',tls/'ca',[t+'-aether',t+'-aether.'+ns+'.svc',t+'-aether.'+ns+'.svc.cluster.local'])
  for role in ['server','orchestrator','management','anonymous','platform-server','platform-bridge','memorylayer','data-connectors','sandbox-provider','tool-catalog','tools-wss-client']:
   source=local/t/'server' if role=='server' else tls/role
   secret(ns,'aether-client-'+role,{file:source.joinpath(file).read_text() for file in ['tls.crt','tls.key','ca.crt']})
  secret('platform-shared','aether-sparkroute-creds-'+t,
         {file:(tls/'model-catalog'/file).read_text() for file in ['tls.crt','tls.key','ca.crt']})
  secret(ns,'memorylayer-db',{'username':'memorylayer','password':env['ML_PASSWORD_'+t.upper()]},'kubernetes.io/basic-auth')
  secret(ns,'connectors-db',{'username':'dataconnectors','password':env['DC_PASSWORD_'+t.upper()]},'kubernetes.io/basic-auth')
  secret(ns,'aether-config',{'AETHER_TOKEN_HMAC_KEY':env['AETHER_HMAC_'+t.upper()],
         'AETHER_ADMIN_API_KEY':env['AETHER_ADMIN_'+t.upper()],'TOOL_CATALOG_CURSOR_KEY':env['AETHER_HMAC_'+t.upper()]})
  secret(ns,'memorylayer-env',{'MEMORYLAYER_POSTGRESQL_URL':url('memorylayer',env['ML_PASSWORD_'+t.upper()],t+'-ml-db-rw').split('?')[0],
      'MEMORYLAYER_EMBEDDING_SERVICE':'default','MEMORYLAYER_EMBED_SERVER_URL':'http://inference.platform-shared.svc:8080',
      'MEMORYLAYER_RATE_LIMIT_REQUESTS':'10000',
      **envfile(LOCAL/t/'memorylayer.env'),'MEMORYLAYER_TENANT_ID':t,
      'MEMORYLAYER_LLM_PROFILE_DEFAULT_PROVIDER':'openai',
      'MEMORYLAYER_LLM_PROFILE_DEFAULT_BASE_URL':'http://shared-gateway.platform-shared.svc:8080/v1',
      'MEMORYLAYER_LLM_PROFILE_DEFAULT_MODEL':'memorylayer-default',
      'MEMORYLAYER_LLM_IDENTITY_HEADER_HOSTS':'shared-gateway.platform-shared.svc',
      'MEMORYLAYER_DOCUMENT_TRANSCRIBE_ENABLED':'true',
      'MEMORYLAYER_TRANSCRIPTION_SERVICE':'direct','MEMORYLAYER_TRANSCRIBE_CASCADE':'fixture',
      'MEMORYLAYER_TRANSCRIBE_PROFILE_FIXTURE_URL':'http://shared-gateway.platform-shared.svc:8080/v1',
      'MEMORYLAYER_TRANSCRIBE_PROFILE_FIXTURE_MODEL':'memorylayer-default',
      'MEMORYLAYER_TRANSCRIBE_PROFILE_FIXTURE_CONTRACT':'generic_markdown',
      'MEMORYLAYER_TRANSCRIBE_PROFILE_FIXTURE_AUTH':'bearer','MEMORYLAYER_TRANSCRIBE_FIGURE_CAPTIONS':'false'})
  secret(ns,'connectors-env',{'DC_POSTGRESQL_URL':url('dataconnectors',env['DC_PASSWORD_'+t.upper()],t+'-dc-db-rw').split('?')[0],
      'DC_BLOBGW_UPLOAD_PUBLIC_URL':'https://platform.example.test:18443/storage/'+t+'/uploads',
      'DC_BLOBGW_EDGE_PUBLIC_URL':'https://platform.example.test:18443/storage/'+t})
  secret(ns,'platform-env',{'INTEGRATION_PROFILE':'fixtures'})
  secret(ns,'provider-env',{'SANDBOX_SIDECAR_TLS_CA_CERT':'/run/anonymous/ca.crt',
      'SANDBOX_SIDECAR_TLS_CLIENT_CERT':'/run/anonymous/tls.crt','SANDBOX_SIDECAR_TLS_CLIENT_KEY':'/run/anonymous/tls.key'})
  secret(ns,'gateway-token',{'token':(LOCAL/t/'gateway-token').read_text()})
  keys={'backend':'BACKEND_IMAGE','aether':'AETHER_IMAGE','memorylayer':'MEMORYLAYER_IMAGE','connectors':'CONNECTORS_IMAGE',
      'tools':'TOOLS_IMAGE','sparkroute':'SPARKROUTE_IMAGE','skills':'SKILLS_IMAGE','provider':'PROVIDER_IMAGE','sahara':'SAHARA_IMAGE','sidecar':'SIDECAR_IMAGE','code':'CODE_IMAGE','mlPostgres':'ML_CNPG_IMAGE'}
  values={'development':True,'tenant':t,'phase':0,'seedDevelopmentAdmin':True,
      'adminEmail':('alice' if t=='alpha' else 'bob')+'@example.test','publicOrigin':'https://platform.example.test:18443',
      'images':{key:images[name]['tag'] for key,name in keys.items()},'storage':{'className':'standard','size':'1Gi'}}
  values['images']['postgres']=postgres
  values['documentTranscription']=True
  values['blobUploadBaseURL']='http://storage-download.platform-storage.svc.cluster.local:8080/uploads'
  values['serviceGrants']=json.loads(fixture_policy['acl-'+t]['environment']['ACL_SERVICE_GRANTS'])
  records={}
  for path in sorted((LOCAL/t/'model-catalog').glob('*.json')):
   record=json.loads(path.read_text())
   for provider in record['providers']:
    provider['base_url']='http://inference.platform-shared.svc.cluster.local:8080/v1'
   records[path.stem]=record
  values['modelCatalog']={'records':records}
  save_values(t,values)
 endpoints=json.loads(subprocess.check_output(kube+['-n','default','get','endpointslices','-l','kubernetes.io/service-name=kubernetes','-o','json']))
 api_egress=[]
 for subset in endpoints['items']:
  for endpoint in subset['endpoints']:
   for address in endpoint['addresses']:
    ip=ipaddress.ip_address(address)
    for port in subset['ports']:
     if port['protocol']=='TCP':api_egress.append({'cidr':str(ip)+('/32' if ip.version==4 else '/128'),'port':port['port']})
 if not api_egress:raise RuntimeError('The disposable API EndpointSlice has no usable addresses')
 shared_values={'development':True,'phase':0,'publicOrigin':'https://platform.example.test:18443',
  'adminOrigin':'http://127.0.0.1:18083','storage':{'className':'standard','size':'1Gi'},
  'images':{key:images[name]['tag'] for key,name in {'auth':'AUTH_IMAGE','web':'WEB_IMAGE','sparkroute':'SPARKROUTE_IMAGE'}.items()},
  'tenants':[{'id':t,'namespace':'tenant-'+t,'release':t} for t in ['alpha','beta']],
  'uploadProxy':{'enabled':True,'endpoint':'http://objects.platform-storage.svc.cluster.local:9000','signedHost':'objects:9000'},
  'modelCatalog':{'allowedProviderHosts':['inference.platform-shared.svc.cluster.local'],'allowHTTP':True,
   'credentialSecretNames':['aether-sparkroute-creds-'+t for t in ['alpha','beta']],'apiEgress':api_egress}}
 shared_values['images'].update(postgres=postgres,valkey=yaml.safe_load((ROOT/'compose/compose.yaml').read_text())['services']['sessions']['image'])
 save_values('shared',shared_values)
 save_values('storage',{'development':True,'phase':0,'storage':{'className':'standard','size':'1Gi'},
  'uploadProxy':{'enabled':True,'endpoint':'http://objects.platform-storage.svc.cluster.local:9000','signedHost':'objects:9000'},
  'images':{'postgres':postgres,**{key:images[name]['tag'] for key,name in {'web':'WEB_IMAGE','edge':'EDGE_IMAGE','blobgw':'BLOBGW_IMAGE'}.items()}}})
 ns='platform-shared'
 secret(ns,'auth-db',{'username':'auth','password':env['MT_PASSWORD']},'kubernetes.io/basic-auth')
 secret(ns,'gateway-db',{'username':'sparkroute','password':env['SPARKROUTE_PASSWORD']},'kubernetes.io/basic-auth')
 secret(ns,'auth-env',{'SCITRERA_MT_DB_URL':url('auth',env['MT_PASSWORD'],'shared-auth-db-rw'),
     'AUTH_PROXY_TOKEN_HMAC_KEY':env['AUTH_HMAC']})
 secret(ns,'auth-operators',{'operators.json':(LOCAL/'operators.json').read_text()})
 fixture=dict(line.split('=',1) for line in (LOCAL/'fixture.env').read_text().splitlines())
 origin='https://platform.example.test:18443'
 secret(ns,'oauth-env',{'AUTH_PROXY_LOGIN_PROVIDERS':'fixture','AUTH_PROXY_LOGIN_FIXTURE_ISSUER':'http://idp:8080',
     'AUTH_PROXY_LOGIN_FIXTURE_CLIENT_ID':'platform-integration-fixture','AUTH_PROXY_LOGIN_FIXTURE_CLIENT_SECRET':fixture['FIXTURE_CLIENT_SECRET'],
     'AUTH_PROXY_LOGIN_FIXTURE_REDIRECT_URL':origin+'/api/auth/auth/callback/fixture',
     'AUTH_PROXY_SESSION_COOKIE_NAME':'platform_kind_session','AUTH_PROXY_SESSION_COOKIE_DOMAIN':'','AUTH_PROXY_SESSION_TTL':'1h'})
 secret(ns,'idp-env',{**fixture,'FIXTURE_ISSUER':'http://idp:8080','FIXTURE_PUBLIC_ORIGIN':'http://127.0.0.1:18091',
     'FIXTURE_CALLBACK':origin+'/api/auth/auth/callback/fixture','FIXTURE_CLIENT_ID':'platform-integration-fixture'})
 gw=url('sparkroute',env['SPARKROUTE_PASSWORD'],'shared-gateway-db-rw')
 secret(ns,'gateway-env',{'SPARKROUTE_POSTGRES_URL':gw,'SPARKROUTE_RUNTIME_POSTGRES_URL':gw})
 gateway_config=json.loads((LOCAL/'gateway/config.json').read_text())
 secret(ns,'gateway-files',{'config.json':json.dumps(gateway_config,indent=2)+'\n',
     'callers.json':(LOCAL/'gateway/callers.json').read_text(),'caller-headers.json':(ROOT/'examples/gateway/caller-headers.json').read_text()})
 certificate(local/'public-ca','platform-integration-kind-ca')
 certificate(local/'public-tls','platform.example.test',local/'public-ca',['platform.example.test'])
 secret(ns,'platform-public-tls',{key:(local/'public-tls'/key).read_text() for key in ['tls.crt','tls.key']},'kubernetes.io/tls')
 ns='platform-storage'
 secret(ns,'storage-db',{'username':'storage','password':env['STORAGE_PASSWORD']},'kubernetes.io/basic-auth')
 secret(ns,'storage-env',{'BLOBGW_DATABASE_URL':url('storage',env['STORAGE_PASSWORD'],'storage-db-rw'),
     'BLOBGW_EDGE_DATABASE_URL':url('storage',env['STORAGE_PASSWORD'],'storage-db-rw'),
     'EDGE_SIGNING_SEED':env['EDGE_SIGNING_SEED'],'S3_ENDPOINT':'http://objects:9000',
     'AWS_ACCESS_KEY_ID':'platform-storage','AWS_SECRET_ACCESS_KEY':env['OBJECT_PASSWORD'],'AWS_REGION':'us-east-1'})
 secret(ns,'storage-files',{'tenants.json':(LOCAL/'storage-tenants.json').read_text(),
                          'credentials.json':(LOCAL/'storage-credentials.json').read_text()})
 secret(ns,'objects-env',{'MINIO_ROOT_USER':'platform-storage','MINIO_ROOT_PASSWORD':env['OBJECT_PASSWORD']})
 # Fixtures are explicitly separate from production chart resources.
 fixture_docs=[]
 for role,script,namespace_,secret_ in [('idp','oidc.py','platform-shared','idp-env'),('inference','inference.py','platform-shared',None)]:
  apply({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':role+'-fixture','namespace':namespace_},
      'data':{script:(ROOT/'tests/fixtures'/script).read_text()}})
  c={'name':role,'image':images['BACKEND_IMAGE']['tag'],'command':['python','/fixtures/'+script],
    'volumeMounts':[{'name':'fixture','mountPath':'/fixtures','readOnly':True}],
    'readinessProbe':{'httpGet':{'path':'/healthz','port':8080},'periodSeconds':3},
    'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
    'resources':{'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'cpu':'1','memory':'512Mi'}}}
  if secret_:c['envFrom']=[{'secretRef':{'name':secret_}}]
  label={'app.kubernetes.io/component':role,'platform.scitrera.io/trust':'service'}
  apply({'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':role,'namespace':namespace_},
    'spec':{'replicas':1,'selector':{'matchLabels':label},'template':{'metadata':{'labels':label},
     'spec':{'automountServiceAccountToken':False,'securityContext':{'runAsNonRoot':True,'runAsUser':1000,'runAsGroup':1000,'seccompProfile':{'type':'RuntimeDefault'}},'containers':[c],'volumes':[{'name':'fixture','configMap':{'name':role+'-fixture'}}]}}}})
  apply({'apiVersion':'v1','kind':'Service','metadata':{'name':role,'namespace':namespace_},
    'spec':{'selector':label,'ports':[{'port':8080,'targetPort':8080}]}})
 for file in ['kubernetes-objects.yaml','kubernetes-network.yaml']:
  pending.extend(yaml.safe_load_all((ROOT/'tests/fixtures'/file).read_text()))
 if args.check:
  print('Fixture inputs validated; existing Secret values and release phases preserved. No cluster resources applied.')
 else:
  for obj in pending:
   if obj:run(kube+['apply','-f','-'],input=json.dumps(obj),text=True,stdout=subprocess.DEVNULL)
  print('Prepared fixture resources; existing Secret values and release phases preserved.')


if __name__=='__main__':main()
