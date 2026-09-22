#!/usr/bin/env python3
"""Prepare local bearer principals and a fixture-only SparkRoute routing document."""
import argparse
import hashlib
import json
from pathlib import Path
from compose_tenants import load_tenants
import secrets
from configure import create, certificate
from models import managed

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures',action='store_true',required=True,
        help='Acknowledge deterministic inference; external providers need operator configuration')
    parser.add_argument('--extend-existing',action='store_true',help='Add missing fixture principals and aliases; existing records must match')
    args=parser.parse_args()
    tenants=load_tenants(ROOT)
    clients=[]
    for tenant in tenants:
        slug=tenant['slug']
        path=ROOT/'.local'/slug/'gateway-token'
        create(path,secrets.token_urlsafe(32)+'\n')
        clients.append({'id':'platform-'+slug,'type':'service','tenant':slug,
            'subject':'platform-server','token_sha256':hashlib.sha256(path.read_text().strip().encode()).hexdigest(),
            'allowed_attribution':['user','source','workspace','thread_id','task_id','agent'],
            'fixed_attribution':{'source':'sahara'}})
        ml_path=ROOT/'.local'/slug/'memorylayer-gateway-token'
        create(ml_path,secrets.token_urlsafe(32)+'\n')
        clients.append({'id':'memorylayer-'+slug,'type':'service','tenant':slug,
            'subject':'memorylayer','token_sha256':hashlib.sha256(ml_path.read_text().strip().encode()).hexdigest(),
            'allowed_attribution':['source','task_id'],'fixed_attribution':{'source':'memorylayer'}})
        create(ROOT/'.local'/slug/'memorylayer.env',
            'MEMORYLAYER_LLM_PROFILE_DEFAULT_API_KEY='+ml_path.read_text().strip()+'\n'+
            'MEMORYLAYER_TRANSCRIBE_PROFILE_FIXTURE_AUTH_TOKEN='+ml_path.read_text().strip()+'\n')
    callers_path=ROOT/'.local/gateway/callers.json'
    if callers_path.exists() and args.extend_existing:
        existing=json.loads(callers_path.read_text())
        by_id={entry['id']:entry for entry in existing['clients']}
        for client in clients:
            if client['id'] in by_id and by_id[client['id']] != client:
                raise SystemExit('Existing caller policy differs: '+client['id'])
            if client['id'] not in by_id:existing['clients'].append(client)
        callers_path.write_text(json.dumps(existing,indent=2)+'\n')
    else:
        create(callers_path,json.dumps({'version':1,'clients':clients},indent=2)+'\n')
    aliases=['sahara-default','sahara-vision-advanced','sahara-text-advanced','memorylayer-default']
    config={'providers':[{'name':'fixture','type':'openai_compatible','base_url':'http://inference:8080/v1'}],
      'deployments':[{'name':'fixture','provider':'fixture','model':'fixture-model','max_concurrency':4,
                      'capabilities':['tools','vision','developer_messages','stream_usage']},
                     {'name':'fixture-memorylayer','provider':'fixture','model':'fixture-memorylayer','max_concurrency':4}],
      'virtual_models':[{'name':alias,'response_model':'virtual','selection':{'mode':'weighted_random'},
         'pools':[{'priority':0,'targets':[{'deployment':('fixture-memorylayer' if alias=='memorylayer-default' else 'fixture'),'weight':100}]}]} for alias in aliases]}
    catalog_models=[model for model in config['virtual_models'] if model['name'].startswith('sahara-')]
    config['virtual_models']=[model for model in config['virtual_models'] if not model['name'].startswith('sahara-')]
    (ROOT/'.local/gateway').chmod(0o700)
    for tenant in tenants:
        slug=tenant['slug']
        tls=ROOT/'.local'/slug/'tls'
        certificate(tls/'model-catalog','sv::sparkroute-modelcatalog::gateway',tls/'ca')
        create(ROOT/'.local/gateway'/('catalog-'+slug+'.json'),json.dumps({
            key:(tls/'model-catalog'/key).read_text() for key in ['ca.crt','tls.crt','tls.key']})+'\n')
        for model in catalog_models:
            record={'schema_version':1,'model':model['name'],'virtual_model':model,
                'providers':config['providers'],
                'deployments':[entry for entry in config['deployments'] if entry['name']=='fixture']}
            create(ROOT/'.local'/slug/'model-catalog'/(model['name']+'.json'),json.dumps(record,indent=2)+'\n',0o644)
    config_path=ROOT/'.local/gateway/config.json'
    if config_path.exists() and args.extend_existing and managed(ROOT):
        print("Prepared gateway principals; YAML-managed routing retained.")
        return
    if config_path.exists() and args.extend_existing:
        existing=json.loads(config_path.read_text())
        if existing['providers']!=config['providers'] or existing['deployments']!=config['deployments']:
            raise SystemExit('Existing provider policy differs from fixture; refusing changes')
        models={entry['name']:entry for entry in existing['virtual_models']}
        for model in config['virtual_models']:
            if model['name'] in models and models[model['name']]!=model:
                raise SystemExit('Existing model policy differs: '+model['name'])
            if model['name'] not in models:existing['virtual_models'].append(model)
        for model in catalog_models:
            if model['name'] in models and models[model['name']]!=model:
                raise SystemExit('Existing central catalog alias differs: '+model['name'])
        existing['virtual_models']=[m for m in existing['virtual_models'] if m['name'] not in {v['name'] for v in catalog_models}]
        config_path.write_text(json.dumps(existing,indent=2)+'\n')
    else:
        create(config_path,json.dumps(config,indent=2)+'\n',0o644)
    print('Prepared fixture gateway inputs; existing credentials and policies retained.')

if __name__=='__main__':main()
