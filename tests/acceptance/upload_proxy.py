#!/usr/bin/env python3
"""Verify upload request targets through actual rendered NGINX containers.

Uses only synthetic auth, payloads, and query strings. Checks byte preservation,
not AWS credentials or S3 signature validation. No customer data or cloud calls.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests/unit')]
from configure import nginx
from deployment_render import compose, helm
from test_deployment_render import DeploymentRenderTests

FIXTURE = '''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib,json,threading,time
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  if self.path.startswith('/auth/verify'):
   cookie=self.headers.get('Cookie','')
   status=200 if cookie=='synthetic=allowed' else (403 if cookie=='synthetic=wrong-tenant' else 401)
   self.send_response(status);self.end_headers();return
  self.send_response(404);self.end_headers()
 def do_PUT(self):
  data=self.rfile.read(int(self.headers.get('Content-Length','0')))
  result={'target':self.path,'host':self.headers.get('Host'),
          'content_type':self.headers.get('Content-Type'),'sha256':hashlib.sha256(data).hexdigest(),
          'cookie':self.headers.get('Cookie'),'authorization':self.headers.get('Authorization'),
          'identity':self.headers.get('X-Scitrera-User')}
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps(result).encode())
for port in (8080,8081,9000):
 server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
 threading.Thread(target=server.serve_forever,daemon=True).start()
while True: time.sleep(60)
'''


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def configs(fixture):
    fixture.bindings['auth'] = {'verifyURL': 'http://auth:8080/auth/verify', 'statusURL': 'http://auth:8081'}
    fixture.bindings['objectStorage']['endpoint'] = 'http://objects:9000'
    fixture.fixture.config['development'] = True
    fixture.fixture.config['auth']['protectAllPaths'] = False
    resolved = fixture.resolved()
    yield 'compose-basic', nginx([{'slug': 'example'}])
    yield 'compose-deployment', compose(resolved)['nginx.conf']
    values = helm(resolved)['helm/serving.yaml']
    values['dnsResolver'] = '127.0.0.11'
    path = fixture.root / 'values.yaml'
    path.write_text(yaml.safe_dump(values))
    objects = yaml.safe_load_all(run('helm', 'template', 'example', str(ROOT / 'charts/platform-shared'),
        '-f', str(path), '--kube-version', '1.36.0'))
    config = next(obj['data']['nginx.conf'] for obj in objects if obj and
                  obj['kind'] == 'ConfigMap' and 'nginx.conf' in obj.get('data', {}))
    yield 'helm', config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-image', required=True)
    parser.add_argument('--python-image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--internal-subnet', help='Optional unused CIDR on hosts with exhausted Docker pools')
    parser.add_argument('--public-subnet', help='Optional unused CIDR for the loopback test listener')
    args = parser.parse_args()
    fixture = DeploymentRenderTests(); fixture.setUp()
    fixture.root.chmod(0o755)
    (fixture.root / 'fixture.py').write_text(FIXTURE)
    project = 'upload-proxy-qa-' + uuid.uuid4().hex[:10]
    containers, networks, results = [], [], []
    try:
        for network, extra in ((project, ['--internal']), (project + '-public', [])):
            subnet = args.internal_subnet if network == project else args.public_subnet
            run('docker', 'network', 'create', *extra, *(['--subnet', subnet] if subnet else []), network)
            networks.append(network)
        def container(name, image, flags, command):
            name = project + '-' + name; containers.append(name)
            run('docker', 'run', '-d', '--name', name, '--network', project,
                '--memory', '128m', '--cpus', '0.5', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges:true', *flags, image, *command)
            return name
        container('fixture', args.python_image, ['--network-alias', 'auth', '--network-alias', 'objects',
            '--entrypoint', 'python', '-v', str(fixture.root / 'fixture.py') + ':/fixture.py:ro'], ['-B', '/fixture.py'])
        payload = b'Synthetic upload transport verification.\n'
        digest = hashlib.sha256(payload).hexdigest()
        prefix = '/storage/example/uploads'
        query = '?X-Amz-Credential=EXAMPLE%2Ftest&X-Amz-Security-Token=a%2Bb%2Fc%3D&X-Amz-Signature=synthetic'
        names = ['plain.pdf', 'Synthetic A+B proposal.pdf', 'Cost 25% & Café.pdf', 'literal%2Ffile?#.pdf']
        for profile, config in configs(fixture):
            conf = fixture.root / (profile + '.conf'); conf.write_text(config)
            web = container(profile, args.web_image, ['--network', project + '-public', '--user', '1000:1000',
                '-p', '127.0.0.1::8080', '--tmpfs', '/tmp:size=16m,mode=1777',
                '--tmpfs', '/var/cache/nginx:size=32m,uid=1000,gid=1000',
                '--tmpfs', '/var/run:size=4m,uid=1000,gid=1000', '--entrypoint', 'nginx',
                '-v', str(conf) + ':/etc/nginx/nginx.conf:ro'], ['-g', 'daemon off;'])
            port = run('docker', 'port', web, '8080/tcp').rsplit(':', 1)[1]
            def request(path, cookie='synthetic=allowed', method='PUT'):
                req = urllib.request.Request('http://127.0.0.1:' + port + path, method=method,
                    data=payload if method=='PUT' else None,
                    headers={'Cookie': cookie, 'Content-Type': 'application/pdf',
                             'Authorization': 'Bearer synthetic', 'X-Scitrera-User': 'forged'})
                try: response = urllib.request.urlopen(req, timeout=5)
                except urllib.error.HTTPError as error: response = error
                with response: return response.status, response.read()
            deadline = time.monotonic() + 20
            while True:
                try:
                    if request('/healthz', method='GET')[0] == 200: break
                except OSError: pass
                if time.monotonic() > deadline: raise RuntimeError('NGINX startup failed: ' + profile)
                time.sleep(.2)
            for name in names:
                target = '/synthetic-bucket/staging/' + urllib.parse.quote(name, safe='') + '/up-01%3A02%3A03' + query
                status, body = request(prefix + target)
                assert status == 200, (profile, name, status)
                echoed = json.loads(body)
                assert echoed['target'] == target, (profile, 'request target changed', target, echoed['target'])
                assert echoed['host'] == 'objects:9000', echoed
                assert echoed['content_type'] == 'application/pdf' and echoed['sha256'] == digest, echoed
                assert all(echoed[key] is None for key in ('cookie', 'authorization', 'identity')), echoed
                results.append({'profile': profile, 'case': name, 'status': status})
            for label, cookie, method, expected in [('anonymous', '', 'PUT', 401),
                    ('wrong-tenant', 'synthetic=wrong-tenant', 'PUT', 403),
                    ('wrong-method', 'synthetic=allowed', 'GET', 403)]:
                status, _ = request(prefix + '/synthetic-bucket/probe' + query, cookie, method)
                assert status == expected, (profile, label, status)
                results.append({'profile': profile, 'case': label, 'status': status})
            run('docker', 'rm', '-f', web); containers.remove(web)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'cases': results, 'scope': 'Real rendered NGINX; synthetic auth and upstream'}, indent=2) + '\n')
        print('Upload proxy acceptance passed:', len(results), 'cases')
    finally:
        for name in containers:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for network in networks:
            subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        fixture.doCleanups()


if __name__ == '__main__':
    main()
