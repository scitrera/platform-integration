"""Disposable OIDC issuer. Loaded only by the explicit fixture Compose overlay."""
import base64
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

KEY=rsa.generate_private_key(public_exponent=65537,key_size=2048)
ISSUER=os.environ['FIXTURE_ISSUER']
PUBLIC=os.environ['FIXTURE_PUBLIC_ORIGIN']
CALLBACK=os.environ['FIXTURE_CALLBACK']
CLIENT=os.environ['FIXTURE_CLIENT_ID']
SECRET=os.environ['FIXTURE_CLIENT_SECRET']
CODES={}
LOCK=threading.Lock()
IDENTITIES={'alice@example.test':'Alice Example','bob@example.test':'Bob Example','denied@example.test':'Denied Example'}

def b64(data): return base64.urlsafe_b64encode(data).rstrip(b'=').decode()
def integer(value): return b64(value.to_bytes((value.bit_length()+7)//8,'big'))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass  # Never log codes, tokens or cookies.

    def reply(self,status,data,headers=None):
        body=json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path=urlsplit(self.path)
        query={k:v[0] for k,v in parse_qs(path.query).items()}
        if path.path=='/.well-known/openid-configuration':
            return self.reply(200,{'issuer':ISSUER,'authorization_endpoint':PUBLIC+'/authorize',
                'token_endpoint':ISSUER+'/token','jwks_uri':ISSUER+'/jwks','response_types_supported':['code'],
                'subject_types_supported':['public'],'id_token_signing_alg_values_supported':['RS256'],
                'token_endpoint_auth_methods_supported':['client_secret_basic','client_secret_post']})
        if path.path=='/jwks':
            pub=KEY.public_key().public_numbers()
            return self.reply(200,{'keys':[{'kty':'RSA','kid':'disposable','alg':'RS256','use':'sig',
                                          'n':integer(pub.n),'e':integer(pub.e)}]})
        if path.path=='/select':
            identity=query.get('email')
            if identity not in IDENTITIES:return self.reply(400,{'error':'unknown_fixture_identity'})
            return self.reply(200,{'selected':identity},{'Set-Cookie':f'fixture_identity={identity}; Path=/; HttpOnly; SameSite=Lax'})
        if path.path=='/authorize':
            if query.get('client_id')!=CLIENT or query.get('redirect_uri')!=CALLBACK or query.get('response_type')!='code':
                return self.reply(400,{'error':'invalid_request'})
            cookie=cookies.SimpleCookie(self.headers.get('Cookie',''))
            identity=cookie['fixture_identity'].value if 'fixture_identity' in cookie else 'alice@example.test'
            if identity not in IDENTITIES:return self.reply(400,{'error':'unknown_fixture_identity'})
            code=secrets.token_urlsafe(32)
            with LOCK:
                for old in list(CODES):
                    if CODES[old]['expires']<time.time():CODES.pop(old)
                CODES[code]={'email':identity,'nonce':query.get('nonce'),'challenge':query.get('code_challenge'),
                             'method':query.get('code_challenge_method'),'expires':time.time()+60}
            return self.reply(302,{}, {'Location':CALLBACK+'?'+urlencode({'code':code,'state':query.get('state','')})})
        if path.path=='/healthz':return self.reply(200,{'fixture':True})
        self.reply(404,{'error':'not_found'})

    def do_POST(self):
        if self.path!='/token':return self.reply(404,{'error':'not_found'})
        length=int(self.headers.get('Content-Length','0'))
        if length>8192:return self.reply(413,{'error':'too_large'})
        query={k:v[0] for k,v in parse_qs(self.rfile.read(length).decode()).items()}
        client,secret=query.get('client_id'),query.get('client_secret')
        if self.headers.get('Authorization','').startswith('Basic '):
            try:client,secret=base64.b64decode(self.headers['Authorization'][6:]).decode().split(':',1)
            except (ValueError,UnicodeError):return self.reply(401,{'error':'invalid_client'})
        if client!=CLIENT or not secrets.compare_digest(secret or '',SECRET):
            return self.reply(401,{'error':'invalid_client'})
        with LOCK:code=CODES.pop(query.get('code'),None)
        if not code or code['expires']<time.time() or query.get('redirect_uri')!=CALLBACK:
            return self.reply(400,{'error':'invalid_grant'})
        if code['challenge']:
            verifier=query.get('code_verifier','')
            expected=b64(hashlib.sha256(verifier.encode()).digest()) if code['method']=='S256' else verifier
            if not secrets.compare_digest(expected,code['challenge']):return self.reply(400,{'error':'invalid_grant'})
        now=int(time.time())
        claims={'iss':ISSUER,'aud':CLIENT,'sub':code['email'],'email':code['email'],'email_verified':True,
                'name':IDENTITIES[code['email']],'iat':now,'exp':now+300}
        if code['nonce']:claims['nonce']=code['nonce']
        signing=(b64(json.dumps({'alg':'RS256','kid':'disposable','typ':'JWT'}).encode())+'.'+b64(json.dumps(claims).encode())).encode()
        token=signing.decode()+'.'+b64(KEY.sign(signing,padding.PKCS1v15(),hashes.SHA256()))
        self.reply(200,{'access_token':secrets.token_urlsafe(24),'token_type':'Bearer','expires_in':300,'id_token':token})

ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
