#!/usr/bin/env python3
"""Prepare tenant Kubernetes credentials in a private operator directory.

Does not contact a cluster. Existing installation identity must be imported for a
migration; --fresh is only for a new empty tenant. No generated value is printed.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from urllib.parse import quote

from configure import certificate, create
from deployment import write_artifacts
from deployment_config import ConfigError, resolve
from deployment_render import components, helm


def read_env(path):
    """Literal assignments only: never source, expand, or execute an env file."""
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, separator, value = line.partition('=')
        key = key.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or key in values:
            raise ConfigError('Invalid or duplicate env assignment at line ' + str(number))
        try:
            parsed = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ConfigError('Invalid env quoting at line ' + str(number)) from None
        if len(parsed) > 1:
            raise ConfigError('Quote values containing whitespace at line ' + str(number))
        values[key] = parsed[0] if parsed else ''
    return values


def database_url(user, password, host, database=None):
    return 'postgresql://' + quote(user, safe='') + ':' + quote(password, safe='') + '@' + host + ':5432/' + quote(database or user, safe='') + '?sslmode=require'


def sqlalchemy_async_url(url):
    """SQLAlchemy's asyncpg dialect accepts ssl, not libpq's sslmode keyword."""
    return url.replace('postgresql://', 'postgresql+asyncpg://', 1).replace('?sslmode=require', '?ssl=require')


def initial_identity(tenant, source=None):
    """Import identity, never rotate it as an incidental deployment side effect."""
    if source:
        source = Path(source)
        env = read_env(source / 'compose.env')
        suffix = tenant.upper().replace('-', '_')
        return {'databases': {name: env[key] for name, key in {
            'memorylayer': 'ML_PASSWORD_' + suffix, 'dataconnectors': 'DC_PASSWORD_' + suffix,
            'sparkroute': 'SPARKROUTE_PASSWORD', 'storage': 'STORAGE_PASSWORD'}.items()},
            'aetherHMAC': env['AETHER_HMAC_' + suffix], 'aetherAdmin': env['AETHER_ADMIN_' + suffix],
            'edgeSigningSeed': env['EDGE_SIGNING_SEED'],
            'gatewayToken': (source / tenant / 'gateway-token').read_text().strip(),
            'memorylayerGatewayToken': (source / tenant / 'memorylayer-gateway-token').read_text().strip(),
            'usageProducerToken': secrets.token_urlsafe(32)}
    return {'databases': {name: secrets.token_hex(24) for name in ('memorylayer', 'dataconnectors', 'sparkroute', 'storage')},
            **{name: secrets.token_hex(32) for name in ('aetherHMAC', 'aetherAdmin', 'edgeSigningSeed')},
            **{name: secrets.token_urlsafe(32) for name in ('gatewayToken', 'memorylayerGatewayToken', 'usageProducerToken')}}


def tls_material(state, tenant, namespace, customer=None, source=None):
    import shutil
    tls = state / 'tls'
    ca = tls / 'ca'
    if source and not ca.exists():
        original = Path(source) / tenant / 'tls' / 'ca'
        shutil.copytree(original, ca)
    certificate(ca, tenant + '-deployment-ca', days=365)
    # Reject an expired/near-expiry imported CA rather than manufacture a new trust root.
    from configure import openssl
    openssl('x509', '-checkend', str(7*86400), '-noout', '-in', ca/'tls.crt')
    certificate(tls/'server', tenant+'-aether', ca,
        [tenant+'-aether', tenant+'-aether.'+namespace+'.svc', tenant+'-aether.'+namespace+'.svc.cluster.local'], days=90)
    principals = {'metrics-bridge': 'metrics::shard0', 'orchestrator': 'orc::default::bootstrap', 'management': 'sv::scitrera-management-plane::bootstrap',
        'anonymous': '_anonymous', 'model-catalog': 'sv::sparkroute-modelcatalog::gateway',
        **{role: 'sv::'+role+'::'+tenant for role in ('platform-server', 'platform-bridge', 'memorylayer',
            'data-connectors', 'sandbox-provider', 'tool-catalog', 'tools-wss-client')}}
    if customer:
        principals['customer-worker'] = customer['principal']
    for role, principal in principals.items():
        certificate(tls/role, principal, ca, days=90)
    result = {}
    for role in ['server', *principals]:
        openssl('x509', '-checkend', str(7*86400), '-noout', '-in', tls/role/'tls.crt')
        result[role] = {file: (tls/role/file).read_text() for file in ('tls.crt', 'tls.key', 'ca.crt')}
    return result


def tenant_secrets(resolved, identity, tls, env):
    config, bindings, inputs = resolved['deployment'], resolved['bindings'], resolved['inputs']
    if config['database']['mode'] != 'consolidated' or config['objectStorage']['mode'] != 's3':
        raise ConfigError('Kubernetes credential adapter requires consolidated PostgreSQL and external S3')
    tenant, ns = config['tenant'], bindings['kubernetes']['namespace']
    models, _, _, _ = components(resolved)
    result = {}
    def secret(name, data, kind='Opaque'):
        document = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': ns,
                    'labels': {'platform.scitrera.io/tenant-id': tenant}}, 'type': kind, 'stringData': data}
        if name in result and result[name] != document:
            raise ConfigError('Conflicting Secret definition: '+name)
        result[name] = document
    def required(name):
        value = env.get(name)
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value:
            raise ConfigError('Configure environment variable '+name)
        return value
    host = tenant + '-postgres-db-rw'
    urls = {name: database_url(name, password, host) for name, password in identity['databases'].items()}
    db_secrets = {'memorylayer': 'memorylayer-db', 'dataconnectors': 'connectors-db',
                  'sparkroute': 'gateway-db', 'storage': 'storage-db'}
    for name, secret_name in db_secrets.items():
        secret_name = bindings.get('secrets', {}).get(name+'Database', secret_name)
        secret(secret_name, {'username': name, 'password': identity['databases'][name]}, 'kubernetes.io/basic-auth')
    for role, data in tls.items():
        name = ('aether-sparkroute-creds-'+tenant if role == 'model-catalog' else
                'customer-worker-tls' if role == 'customer-worker' else 'aether-client-'+role)
        secret(name, data)
    secret('aether-config', {'AETHER_TOKEN_HMAC_KEY': identity['aetherHMAC'],
        'AETHER_ADMIN_API_KEY': identity['aetherAdmin'], 'TOOL_CATALOG_CURSOR_KEY': identity['aetherHMAC']})
    gateway = 'http://'+tenant+'-serving-gateway.'+ns+'.svc.cluster.local:8080/v1'
    secret('memorylayer-env', {'MEMORYLAYER_POSTGRESQL_URL': sqlalchemy_async_url(urls['memorylayer']),
        'MEMORYLAYER_RATE_LIMIT_REQUESTS': '10000', 'MEMORYLAYER_TENANT_ID': tenant,
        'MEMORYLAYER_LLM_PROFILE_DEFAULT_PROVIDER': 'openai', 'MEMORYLAYER_LLM_PROFILE_DEFAULT_MODEL': 'memorylayer-default',
        'MEMORYLAYER_LLM_PROFILE_DEFAULT_BASE_URL': gateway,
        'MEMORYLAYER_LLM_PROFILE_DEFAULT_API_KEY': identity['memorylayerGatewayToken'],
        'MEMORYLAYER_LLM_IDENTITY_HEADER_HOSTS': tenant+'-serving-gateway.'+ns+'.svc.cluster.local'})
    secret('connectors-env', {'DC_POSTGRESQL_URL': sqlalchemy_async_url(urls['dataconnectors']),
        'DC_BLOBGW_UPLOAD_PUBLIC_URL': config['public']['origin']+'/storage/'+tenant+'/uploads',
        'DC_BLOBGW_EDGE_PUBLIC_URL': config['public']['origin']+'/storage/'+tenant})
    secret('platform-env', {})
    secret('provider-env', {'SANDBOX_SIDECAR_TLS_CA_CERT': '/run/anonymous/ca.crt',
        'SANDBOX_SIDECAR_TLS_CLIENT_CERT': '/run/anonymous/tls.crt',
        'SANDBOX_SIDECAR_TLS_CLIENT_KEY': '/run/anonymous/tls.key'})
    secret('gateway-token', {'token': identity['gatewayToken']})
    secret('gateway-env', {'SPARKROUTE_POSTGRES_URL': urls['sparkroute'], 'SPARKROUTE_RUNTIME_POSTGRES_URL': urls['sparkroute']})
    clients = []
    for subject, token_key, allowed in [('platform-server','gatewayToken',['user','source','workspace','thread_id','task_id','agent']),
                                       ('memorylayer','memorylayerGatewayToken',['source','task_id'])]:
        clients.append({'id': subject+'-'+tenant, 'type': 'service', 'tenant': tenant, 'subject': subject,
            'token_sha256': hashlib.sha256(identity[token_key].encode()).hexdigest(), 'allowed_attribution': allowed,
            'fixed_attribution': {'source': 'sahara' if subject=='platform-server' else 'memorylayer'}})
    secret('gateway-files', {'config.json': json.dumps({'providers':[], 'deployments':[], 'virtual_models':[]}),
        'callers.json': json.dumps({'version':1,'clients':clients}),
        models['credential_file']: json.dumps({key:required(name) for key,name in models['credential_sources'].items()})})
    object_store = bindings['objectStorage']
    mode = object_store.get('credentialMode','static')
    storage_env = {'BLOBGW_DATABASE_URL':urls['storage'],'BLOBGW_EDGE_DATABASE_URL':urls['storage'],
        'EDGE_SIGNING_SEED': identity['edgeSigningSeed'], 'S3_ENDPOINT':object_store['endpoint'],
        'AWS_REGION':object_store['region'], 'AWS_DEFAULT_REGION':object_store['region']}
    credentials = {}
    if mode == 'static':
        key, value = required('STORAGE_AWS_ACCESS_KEY_ID'), required('STORAGE_AWS_SECRET_ACCESS_KEY')
        credentials[tenant] = {'accessKey': key, 'secretKey': value}
        storage_env.update(AWS_ACCESS_KEY_ID=key,AWS_SECRET_ACCESS_KEY=value)
    secret('storage-env',storage_env)
    secret('storage-files',{'tenants.json':json.dumps({'tenants':{tenant:{
        'endpoint':object_store['endpoint'],'region':object_store['region'],'bucket':object_store['bucket'],
        'prefix':object_store.get('prefix','packs'),'forcePathStyle':True,
        'credentialRef':object_store['roleARN'] if mode=='aws-sts' else tenant}}}),
        'credentials.json':json.dumps(credentials)})
    docs = inputs['documentServices']
    auths = ([service['auth'] for service in docs['services'].values() if service.get('transport')=='modal']
             if docs['version']==2 else [docs['proxy']] if docs['transport']=='modal' else [])
    for auth in auths:
        secret(auth['secret_name'],{'MODAL_KEY':required(auth['key_env']),'MODAL_SECRET':required(auth['secret_env'])})
    if config['billing']['mode'] != 'off':
        secret(bindings['metering']['credentialSecret'], {'SPARKROUTE_REPORT_DATABASE_URL':urls['sparkroute'],
            'BILLING_PRODUCER_TOKEN':identity['usageProducerToken']})
    if config['backup']['enabled'] and not bindings.get('backup',{}).get('serviceAccountAnnotations'):
        secret(bindings['backup']['credentialsSecret'], {'AWS_ACCESS_KEY_ID':required('BACKUP_AWS_ACCESS_KEY_ID'),
            'AWS_SECRET_ACCESS_KEY':required('BACKUP_AWS_SECRET_ACCESS_KEY')})
    return list(result.values())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--profile',required=True)
    parser.add_argument('--bindings',type=Path,required=True)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--fresh',action='store_true')
    parser.add_argument('--from-compose',type=Path,help='Existing installation .local directory; imports passwords, signing keys, callers and CA')
    args=parser.parse_args()
    if args.fresh and args.from_compose:parser.error('Choose fresh installation OR import, not both')
    resolved=resolve(args.config,profile=args.profile,bindings_path=args.bindings)
    helm(resolved) # All image, endpoint and policy validation before generating identity.
    os.umask(0o077)
    args.state.mkdir(parents=True,exist_ok=True,mode=0o700)
    identity_path=args.state/'identity.json'
    if not identity_path.exists():
        if not args.fresh and not args.from_compose:parser.error('New credential state requires --fresh or --from-compose')
        create(identity_path,json.dumps(initial_identity(resolved['deployment']['tenant'],args.from_compose)))
    identity=json.loads(identity_path.read_text())
    env={}
    for file in resolved['deployment'].get('secretFiles',{}).values():
        path=args.config.resolve().parent/file
        if path.exists():
            for key,value in read_env(path).items():
                if key in env and env[key] != value:raise ConfigError('Conflicting env value: '+key)
                env[key]=value
    env.update(os.environ)
    tls=tls_material(args.state,resolved['deployment']['tenant'],resolved['bindings']['kubernetes']['namespace'],
                     resolved['deployment'].get('customer'),args.from_compose)
    documents=tenant_secrets(resolved,identity,tls,env)
    import yaml
    write_artifacts(args.state,{'tenant-secrets.yaml':yaml.safe_dump_all(documents,sort_keys=False)},mode=0o600)
    print('Prepared '+str(len(documents))+' private Secret definitions; no cluster changes')


if __name__=='__main__':
    main()
