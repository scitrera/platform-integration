#!/usr/bin/env python3
"""Enable local synthetic identity/model inputs. Never use this on production clusters."""
import json
from pathlib import Path
import secrets
from configure import create

ROOT=Path(__file__).resolve().parents[1]
env=dict(line.split('=',1) for line in (ROOT/'.local/compose.env').read_text().splitlines() if line and not line.startswith('#'))
# These settings are confined to the explicitly selected local fixture profile.
for key,value in {"MODEL_PROVIDER_HOSTS":"inference","MODEL_PROVIDER_ALLOW_HTTP":"true",
                  "EMBED_ENDPOINT":"http://inference:8080"}.items():
    if key in env and env[key] != value:
        raise SystemExit("Existing "+key+" differs from fixture policy; choose a separate installation directory")
    env[key]=value
path=ROOT/'.local/compose.env'
path.write_text(''.join(k+'='+v+'\n' for k,v in env.items()))
path.chmod(0o600)
secret=secrets.token_hex(24)
web='http://127.0.0.1:'+env['WEB_PORT']
create(ROOT/'.local/fixture.env','FIXTURE_CLIENT_SECRET='+secret+'\n')
secret=dict(line.split('=',1) for line in (ROOT/'.local/fixture.env').read_text().splitlines())['FIXTURE_CLIENT_SECRET']
create(ROOT/'.local/oauth.env','\n'.join([
    'AUTH_PROXY_LOGIN_PROVIDERS=fixture','AUTH_PROXY_LOGIN_FIXTURE_ISSUER=http://idp:8080',
    'AUTH_PROXY_LOGIN_FIXTURE_CLIENT_ID=platform-integration-fixture','AUTH_PROXY_LOGIN_FIXTURE_CLIENT_SECRET='+secret,
    'AUTH_PROXY_LOGIN_FIXTURE_REDIRECT_URL='+web+'/api/auth/auth/callback/fixture',
    'AUTH_PROXY_SESSION_COOKIE_SECURE=false','AUTH_PROXY_SESSION_COOKIE_NAME=platform_dev_session',
    'AUTH_PROXY_SESSION_COOKIE_DOMAIN=','AUTH_PROXY_SESSION_TTL=1h'])+'\n')
create(ROOT/'.local/fixtures.enabled','Disposable fixtures explicitly selected.\n')
print('Enabled signed OIDC fixture; external OAuth acceptance remains pending.')
