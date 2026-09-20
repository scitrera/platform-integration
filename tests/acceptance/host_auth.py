#!/usr/bin/env python3
"""Exercise the rendered NGINX host gate using isolated synthetic containers.

No tenant credentials, model calls or customer documents. Requires existing
local web and auth-gate images. The test publishes one random loopback port.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests/unit')]
from test_deployment_render import DeploymentRenderTests
from deployment_render import compose

FIXTURE='''from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading,time
class H(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  cookie=self.headers.get('Cookie','')
  if self.path.startswith('/auth/verify'):
   from urllib.parse import urlsplit,parse_qs
   q=parse_qs(urlsplit(self.path).query)
   if q!={'tenant_id':['example'],'workspace_id':['auth-app']}:
    self.send_response(403)
   elif cookie=='synthetic=allowed':
    self.send_response(200);self.send_header('X-Scitrera-User','synthetic-user')
   elif cookie=='synthetic=wrong-tenant':self.send_response(403)
   elif cookie=='synthetic=outage':self.send_response(503)
   else:self.send_response(401)
   self.end_headers();return
  self.send_response(200);self.end_headers();self.wfile.write(b'synthetic permitted resource')
 do_PUT=do_POST=do_GET
for port in [8080,8081,8090,8000]:
 server=ThreadingHTTPServer(('0.0.0.0',port),H)
 threading.Thread(target=server.serve_forever,daemon=True).start()
while True:time.sleep(60)
'''

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


def run(args,**kwargs):
    return subprocess.run(args,check=True,text=True,**kwargs)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-image',required=True)
    parser.add_argument('--auth-gate-image',required=True)
    parser.add_argument('--python-image',default='python:3.13-alpine@sha256:1a63a53928ce53d2b0baf08092a703f4840ac5dfbd61fd48802dbf48e08c801e')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    project='platform-host-auth-qa-'+uuid.uuid4().hex[:10]
    containers=[];results=[]
    fixture=DeploymentRenderTests();fixture.setUp()
    network=False; public_network=False
    try:
        fixture.bindings['auth']={'verifyURL':'http://authstub:8080/auth/verify','statusURL':'http://authstub:8081'}
        resolved=fixture.resolved()
        root=fixture.root
        (root/'nginx.conf').write_text(compose(resolved)['nginx.conf'])
        (root/'fixture.py').write_text(FIXTURE)
        root.chmod(0o755)
        run(['docker','network','create','--internal',project],stdout=subprocess.DEVNULL);network=True
        run(['docker','network','create',project+'-public'],stdout=subprocess.DEVNULL);public_network=True
        def container(name,image,flags,command):
            name=project+'-'+name;containers.append(name)
            run(['docker','run','-d','--name',name,'--network',project,'--memory','128m','--cpus','0.5',
                 '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                 *flags,image,*command],stdout=subprocess.DEVNULL)
            return name
        container('fixture',args.python_image,
                  ['--network-alias','authstub','--network-alias','storage-edge','--network-alias','platform-example',
                   '--network-alias','tools-example','-v',str(root/'fixture.py')+':/fixture.py:ro'],
                  ['python','-B','/fixture.py'])
        container('gate',args.auth_gate_image,['--network-alias','auth-gate',
             '-e','AUTH_GATE_VERIFY_URL=http://authstub:8080/auth/verify',
             '-e','AUTH_GATE_PUBLIC_ORIGIN='+resolved['deployment']['public']['origin'],
             '-e','AUTH_GATE_LOGIN_ORIGIN='+resolved['deployment']['auth']['origin'],
             '-e','AUTH_GATE_TENANT=example'],[])
        web=container('web',args.web_image,['--network',project+'-public','--user','1000:1000','-p','127.0.0.1::8080','--tmpfs','/tmp:size=16m,mode=1777',
                       '--tmpfs','/var/cache/nginx:size=32m,uid=1000,gid=1000',
                       '--tmpfs','/var/run:size=4m,uid=1000,gid=1000',
                       '-v',str(root/'nginx.conf')+':/etc/nginx/nginx.conf:ro','--entrypoint','nginx'],['-g','daemon off;'])
        port=subprocess.check_output(['docker','port',web,'8080/tcp'],text=True).strip().rsplit(':',1)[1]
        opener=urllib.request.build_opener(NoRedirect())
        def request(path,headers=None,method='GET'):
            req=urllib.request.Request('http://127.0.0.1:'+port+path,headers=headers or {},method=method)
            try:response=opener.open(req,timeout=5)
            except urllib.error.HTTPError as e:response=e
            with response:return response.status,dict(response.headers),response.read()
        deadline=time.monotonic()+20
        while True:
            try:
                if request('/healthz')[0]==200 and request('/storage/example/blob/probe')[0]==401:break
            except OSError:pass
            if time.monotonic()>deadline:raise RuntimeError('Synthetic proxy failed startup')
            time.sleep(.2)
        def expect(label,path,expected,headers=None,method='GET'):
            status,response_headers,body=request(path,headers,method)
            if status not in expected:raise AssertionError(label+': unexpected HTTP '+str(status))
            results.append({'case':label,'status':status})
            return response_headers,body
        headers,_=expect('navigation redirects preserving deep link','/example/bids?tab=analysis',[302],{'Accept':'text/html'})
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)
        assert query['tenant']==['example']
        assert query['rd']==[resolved['deployment']['public']['origin']+'/example/bids?tab=analysis']
        for path in ['/assets/private.js','/source.tar.gz','/storage/example/blob/page-image','/storage/example/uploads/object','/example/rfe1-ws']:
            expect('unauthenticated '+path,path,[401,403] if '/uploads/' in path else [401],{'Accept':'*/*'})
        expect('upload cannot bypass host auth','/storage/example/uploads/object',[401],{},'PUT')
        expect('forged identity headers ignored','/storage/example/blob/page-image',[401],{'X-Scitrera-User':'forged','X-Auth-Tenant-ID':'example'})
        expect('wrong tenant denied','/storage/example/blob/page-image',[403],{'Cookie':'synthetic=wrong-tenant'})
        expect('auth outage fails closed','/storage/example/blob/page-image',[500,503],{'Cookie':'synthetic=outage'})
        _,body=expect('authorized page uses same session','/storage/example/blob/page-image',[200],{'Cookie':'synthetic=allowed'})
        assert body==b'synthetic permitted resource'
        expect('authorized initial application','/example',[200],{'Cookie':'synthetic=allowed'})
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps({'cases':results,'scope':'Real generated NGINX and auth-gate; synthetic auth/storage; no Envoy or document ACL qualification'},indent=2)+'\n')
        print('Host authorization acceptance passed: '+str(len(results))+' cases')
    except Exception:
        for name in containers:
            status=subprocess.run(['docker','inspect',name,'--format','{{json .State}} {{json .HostConfig.PortBindings}} {{json .NetworkSettings.Ports}}'],text=True,capture_output=True)
            print(status.stdout,file=sys.stderr)
            logs=subprocess.run(['docker','logs',name],text=True,capture_output=True)
            print(name+': '+(logs.stdout+logs.stderr)[-2500:],file=sys.stderr)
        raise
    finally:
        if containers:subprocess.run(['docker','rm','-f',*containers],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if network:subprocess.run(['docker','network','rm',project],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if public_network:subprocess.run(['docker','network','rm',project+'-public'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        fixture.doCleanups()


if __name__=='__main__':main()
