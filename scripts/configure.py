#!/usr/bin/env python3
"""Generate disposable development configuration. Existing secrets are never replaced."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from compose_tenants import select_tenants, write_compose
from compose_network import network_settings

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'


def create(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return
    with os.fdopen(fd, 'w') as output:
        output.write(content)


def openssl(*args):
    subprocess.run(['openssl', *map(str, args)], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)


def certificate(directory, cn, ca=None, hosts=(), *, days=30):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError("Certificate validity must be between 1 and 3650 days")
    directory.mkdir(parents=True, exist_ok=True)
    key, crt = directory / 'tls.key', directory / 'tls.crt'
    if key.exists() or crt.exists():
        if not key.exists() or not crt.exists():
            raise RuntimeError(f'Incomplete keypair in {directory}; repair it explicitly')
        return
    openssl('genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', key)
    key.chmod(0o600)
    if ca is None:
        openssl('req', '-new', '-x509', '-key', key, '-out', crt, '-days', str(days),
                '-subj', '/CN='+cn, '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        return
    csr, ext = directory / 'request.csr', directory / 'extensions.cnf'
    ext.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                   'extendedKeyUsage='+('serverAuth' if hosts else 'clientAuth')+'\n'+
                   ('subjectAltName='+','.join('DNS:'+h for h in hosts)+'\n' if hosts else ''))
    openssl('req', '-new', '-key', key, '-out', csr, '-subj', '/CN='+cn)
    openssl('x509', '-req', '-in', csr, '-CA', ca/'tls.crt', '-CAkey', ca/'tls.key',
            '-set_serial', str(secrets.randbits(128)), '-days', str(days), '-extfile', ext, '-out', crt)
    create(directory/'ca.crt', (ca/'tls.crt').read_text(), 0o644)
    csr.unlink()
    ext.unlink()


def nginx(tenants, *, tools_wss_enabled=True):
    # Explicit route allowlist. No request header or query parameter chooses upstream authority.
    text = '''worker_processes auto;
events { worker_connections 1024; }
http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  resolver 127.0.0.11 valid=5s ipv6=off;
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  map $http_x_blob_capability $blob_capability_authorization {
    default "Bearer $http_x_blob_capability";
    '' '';
  }
  access_log off;
  server {
    listen 8080;
    server_name _;
    root /usr/share/nginx/html;
    location = /healthz { return 200 'ok'; }
    location ^~ /api/auth/ {
      proxy_pass http://auth:8081/;
      proxy_set_header Host $http_host;
      proxy_set_header Origin $http_origin;
    }
    location ^~ /auth/login/ { set $args "next=/api/auth/"; proxy_pass http://auth:8081; proxy_set_header Host $http_host; }
    location ^~ /auth/callback/ { proxy_pass http://auth:8081; proxy_set_header Host $http_host; }
    location ^~ /auth/ { return 404; }
    location = /source.tar.gz { try_files $uri =404; }
    location ^~ /assets/ { try_files $uri =404; }
'''
    # Use the original escaped request target, including its signed query.
    # NGINX rewrite operates on decoded $uri and corrupts presigned object keys.
    upload_maps = "".join(
        f'  map $request_uri $upload_uri_{tenant["slug"].replace("-", "_")} {{\n'
        f'    default "";\n    ~^/storage/{tenant["slug"]}/uploads(/.*)$ $1;\n  }}\n'
        for tenant in tenants
    )
    text = text.replace("  server {", upload_maps + "  server {", 1)
    for tenant in tenants:
        slug = tenant['slug']
        variable = slug.replace('-', '_')
        if tools_wss_enabled:
            text += f'''    location = /{slug}/tools/v1/connect {{
      proxy_pass_request_headers off;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header Origin $http_origin;
      proxy_set_header Authorization $http_authorization;
      proxy_set_header X-API-Key $http_x_api_key;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Sec-WebSocket-Key $http_sec_websocket_key;
      proxy_set_header Sec-WebSocket-Version $http_sec_websocket_version;
      proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_buffering off;
      set $tools_{variable} http://tools-{slug}:8090;
      rewrite ^/{slug}/tools/(.*)$ /$1 break;
      proxy_pass $tools_{variable};
    }}
'''
        else:
            text += f'    location = /{slug}/tools/v1/connect {{ return 404; }}\n'
        text += f'''    location = /_verify_{slug} {{
      internal;
      # Upload routes enforce their own limit; auth never forwards a body.
      client_max_body_size 0;
      proxy_pass http://auth:8080/auth/verify?workspace_id=auth-app&tenant_id={slug};
      proxy_pass_request_body off;
      proxy_pass_request_headers off;
      proxy_set_header Content-Length "";
      proxy_set_header Cookie $http_cookie;
      proxy_set_header Authorization $http_authorization;
    }}
    location ~ ^/{slug}/(rfe1-ws(?:/v2)?/?)$ {{
      auth_request /_verify_{slug};
      auth_request_set $user_{variable} $upstream_http_x_scitrera_user;
      auth_request_set $name_{variable} $upstream_http_x_scitrera_name;
      auth_request_set $tenants_{variable} $upstream_http_x_scitrera_tenants;
      auth_request_set $default_{variable} $upstream_http_x_scitrera_default_tenant;
      auth_request_set $type_{variable} $upstream_http_x_auth_principal_type;
      proxy_pass_request_headers off;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header Origin $http_origin;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Sec-WebSocket-Key $http_sec_websocket_key;
      proxy_set_header Sec-WebSocket-Version $http_sec_websocket_version;
      proxy_set_header Sec-WebSocket-Protocol $http_sec_websocket_protocol;
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header X-Scitrera-User $user_{variable};
      proxy_set_header X-Scitrera-Name $name_{variable};
      proxy_set_header X-Scitrera-Tenants $tenants_{variable};
      proxy_set_header X-Scitrera-Default-Tenant $default_{variable};
      proxy_set_header X-Auth-User-ID $user_{variable};
      proxy_set_header X-Auth-Tenant-ID {slug};
      proxy_set_header X-Auth-Principal-Type $type_{variable};
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_buffering off;
      set $platform_{variable} http://platform-{slug}:8000;
      rewrite ^/{slug}/(.*)$ /$1 break;
      proxy_pass $platform_{variable};
    }}
'''
        text += f'''    location ^~ /storage/{slug}/uploads/ {{
      auth_request /_verify_{slug};
      limit_except PUT {{ deny all; }}
      proxy_pass_request_headers off;
      proxy_set_header Host objects:9000;
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header Content-Length $http_content_length;
      client_max_body_size 512m;
      proxy_request_buffering off;
      set $upload_{variable} http://objects:9000;
      if ($upload_uri_{variable} = "") {{ return 400; }}
      proxy_pass $upload_{variable}$upload_uri_{variable};
    }}
'''
        text += f'''    location ~ ^/storage/{slug}/(blob|staged|finalize)/ {{
      auth_request /_verify_{slug};
      auth_request_set $storage_user_{variable} $upstream_http_x_scitrera_user;
      proxy_pass_request_headers off;
      proxy_set_header X-Auth-Tenant-ID {slug};
      proxy_set_header X-Scitrera-User $storage_user_{variable};
      proxy_set_header Authorization $blob_capability_authorization;
      proxy_set_header Content-Type $http_content_type;
      proxy_set_header Range $http_range;
      proxy_set_header If-Range $http_if_range;
      proxy_set_header If-None-Match $http_if_none_match;
      proxy_set_header Content-Length $http_content_length;
      client_max_body_size 512m;
      proxy_request_buffering off;
      proxy_buffering off;
      set $storage_{variable} http://storage-edge:8090;
      rewrite ^/storage/{slug}/(.*)$ /$1 break;
      proxy_pass $storage_{variable};
    }}
'''
    text += '''    location ~ /tools/ { return 404; }
    location ~ /rfe1-ws { return 404; }
    location /storage/ { return 404; }
    location ~* \\.(js|css|map|png|jpg|svg|ico|woff2?|gz)$ { try_files $uri =404; }
    location ^~ /api/ { return 404; }
    location / { try_files $uri $uri/ /index.html; }
  }
}
'''
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='platform-integration')
    parser.add_argument('--tenants', type=Path, help='Tenant JSON definitions for a fresh installation')
    parser.add_argument('--web-port', type=int, default=18080)
    parser.add_argument('--bind-address', help='Browser service bind IPv4 address; default 127.0.0.1')
    parser.add_argument('--public-origin', help='Browser-facing web origin, e.g. http://dev-host:18080')
    parser.add_argument('--fixture-public-origin', help='Browser-facing fixture identity provider origin')
    parser.add_argument('--admin-port', type=int, default=18082)
    parser.add_argument('--fixture-idp-port', type=int, default=18090)
    args = parser.parse_args()
    if not re.fullmatch('[a-z][a-z0-9-]{2,40}', args.project):
        parser.error('project must contain 3–41 lowercase letters, digits or hyphens')
    if not all(1024 <= p <= 65535 for p in (args.web_port,args.admin_port,args.fixture_idp_port)) or len({args.web_port,args.admin_port,args.fixture_idp_port})!=3:
        parser.error('choose distinct unprivileged ports')
    os.umask(0o077)
    LOCAL.mkdir(exist_ok=True)
    tenants=select_tenants(ROOT, args.tenants)
    write_compose(ROOT, tenants)
    env={'COMPOSE_PROJECT_NAME':args.project,'WEB_PORT':args.web_port,'AUTH_ADMIN_PORT':args.admin_port,'FIXTURE_IDP_PORT':args.fixture_idp_port,
         'LOCAL_UID':os.getuid(),'LOCAL_GID':os.getgid(),'MT_PASSWORD':secrets.token_hex(24),
         'AUTH_HMAC':secrets.token_hex(32),'SANDBOX_HOST_ROOT':str(LOCAL/'sandbox-state')}
    if (LOCAL/'compose.env').exists():
        env.update(dict(line.split('=',1) for line in (LOCAL/'compose.env').read_text().splitlines()
                        if line and not line.startswith('#')))
    env.update(network_settings(env, initialized=(LOCAL/'compose.env').exists(),
        bind_address=args.bind_address, public_origin=args.public_origin,
        fixture_public_origin=args.fixture_public_origin))
    for key in ('STORAGE_PASSWORD','OBJECT_PASSWORD','SPARKROUTE_PASSWORD'):
        env.setdefault(key,secrets.token_hex(24))
    env.setdefault('EDGE_SIGNING_SEED',secrets.token_hex(32))
    for tenant in tenants:
        slug=tenant['slug']
        suffix=slug.upper().replace('-', '_')
        env.setdefault('ML_PASSWORD_'+suffix,secrets.token_hex(24))
        env.setdefault('DC_PASSWORD_'+suffix,secrets.token_hex(24))
        if not re.fullmatch('[a-z][a-z0-9-]{0,30}',slug):
            raise ValueError('Invalid synthetic tenant slug')
        env.setdefault('AETHER_HMAC_'+suffix,secrets.token_hex(32))
        env.setdefault('AETHER_ADMIN_'+suffix,secrets.token_hex(24))
        tls=LOCAL/slug/'tls'
        ca=tls/'ca'
        certificate(ca, f'{args.project}-{slug}-development-ca')
        certificate(tls/'server',f'aether-{slug}',ca,[f'aether-{slug}','aether-gateway',
                    f'aether-gateway.tenant-{slug}.svc.cluster.local','localhost'])
        for name,cn in {'metrics-bridge':'metrics::shard0','orchestrator':'orc::default::bootstrap','management':'sv::scitrera-management-plane::bootstrap',
                        'anonymous':'_anonymous','platform-server':f'sv::platform-server::{slug}',
                        'platform-bridge':f'sv::platform-bridge::{slug}','memorylayer':f'sv::memorylayer::{slug}',
                        'data-connectors':f'sv::data-connectors::{slug}','tool-catalog':f'sv::tool-catalog::{slug}',
                        'sandbox-provider':f'sv::sandbox-provider::{slug}',
                        'model-catalog':'sv::sparkroute-modelcatalog::gateway',
                        'tools-wss-client':f'sv::tools-wss-client::{slug}'}.items():
            certificate(tls/name,cn,ca)
        create(LOCAL/slug/'aether.yaml',json.dumps({
            'mode':'lite','gateway':{'port':50051,'ops_port':9090,'gateway_id':f'aether-{slug}',
              'tls':{'cert_file':'/etc/aether/tls/tls.crt','key_file':'/etc/aether/tls/tls.key',
                     'ca_file':'/etc/aether/tls/ca.crt','client_auth':'require'}},
            'admin':{'enabled':True,'port':31880,'cors_origin':'',
                     'tls_cert_file':'/etc/aether/tls/tls.crt','tls_key_file':'/etc/aether/tls/tls.key'},
            'auth':{'modes':['mtls','api_key'],'mtls':{'required':True,'mode':'semi-strict'},
                    'api_key':{},'oauth':{'verify_signature':True,'providers':[]}},
            'acl':{'required':False},'lite':{'data_dir':'/data'},'log_level':'info'
        },indent=2)+'\n',0o644)
    env_path=LOCAL/'compose.env'
    env_path.write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
    env_path.chmod(0o600)
    create(LOCAL/'storage-tenants.json',json.dumps({'tenants':{
        t['slug']:{'endpoint':'http://objects:9000','region':'us-east-1',
                   'bucket':'tenant-'+t['slug'],'forcePathStyle':True,'prefix':'packs','credentialRef':t['slug']}
        for t in tenants}},indent=2)+'\n',0o644)
    create(LOCAL/'storage-credentials.json',json.dumps({t['slug']:{'accessKey':'platform-storage',
        'secretKey':env['OBJECT_PASSWORD']} for t in tenants})+'\n')
    create(LOCAL/'nginx.conf',nginx(tenants),0o644)
    print('Prepared development configuration in .local; existing values retained.')
    print('Run auth bootstrap using the component CLI before starting auth.')


if __name__=='__main__':
    main()
