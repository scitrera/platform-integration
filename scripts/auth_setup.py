#!/usr/bin/env python3
"""Idempotent synthetic enrollment through auth-go's supported operator API."""
import argparse
import http.cookiejar
import json
from pathlib import Path
from compose_tenants import load_tenants
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, Request

ROOT=Path(__file__).resolve().parents[1]

class Operator:
    def __init__(self,origin,token):
        self.origin=origin
        self.base=origin+'/api/auth-admin/v1'
        self.opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf=''
        deadline=time.monotonic()+60
        while True:
            try:
                result=self.call('POST','/session',{'operator':'operator','token':token})
                self.csrf=result['csrf_token']
                break
            except HTTPError:
                raise
            except (URLError, ConnectionError, TimeoutError):
                if time.monotonic()>deadline:raise RuntimeError('Auth operator plane was not ready within 60 seconds') from None
                time.sleep(0.5)

    def call(self,method,path,data=None,revision=None):
        headers={'Origin':self.origin,'Content-Type':'application/json'}
        if self.csrf:headers['X-CSRF-Token']=self.csrf
        if revision is not None:headers['If-Match']='"'+str(revision)+'"'
        request=Request(self.base+path,method=method,headers=headers,
                        data=json.dumps(data).encode() if data is not None else None)
        with self.opener.open(request,timeout=15) as response:
            return json.load(response)

    def read(self,path):return self.call('GET',path)

    def write(self,method,path,data):
        # Optimistic concurrency: conflicts fail rather than overwriting another operator.
        revision=self.read('/status')['revision']
        return self.call(method,path,data,revision)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',help='Explicit operator origin, for example a loopback Kubernetes port-forward')
    args=parser.parse_args()
    env=dict(line.split('=',1) for line in (ROOT/'.local/compose.env').read_text().splitlines() if line and not line.startswith('#'))
    token=json.loads((ROOT/'.local/operators.json').read_text())['operators']['operator']
    operator=Operator(args.origin or 'http://127.0.0.1:'+env['AUTH_ADMIN_PORT'],token)
    for tenant in load_tenants(ROOT):
        slug=tenant['slug']
        try:operator.read('/tenants/'+slug)
        except HTTPError as error:
            if error.code!=404:raise
            operator.write('POST','/tenants',{'slug':slug,'name':tenant['name'],'enabled':True,
                                             'metadata':{'default_workspace':tenant['workspace']}})
            if (ROOT/'.local/fixtures.enabled').exists():
                operator.write('PUT','/tenants/'+slug+'/auth',{'auto_add':False,'providers':['fixture'],'checks':{}})
        users=operator.read('/users?q='+tenant['email'])['data']
        if isinstance(users,dict):users=users.get('users',users.get('items',[]))
        matches=[user for user in users if user['email']==tenant['email']]
        if not matches:
            operator.write('POST','/users',{'email':tenant['email'],'name':tenant['name']+' administrator','enabled':True})
            users=operator.read('/users?q='+tenant['email'])['data']
            if isinstance(users,dict):users=users.get('users',users.get('items',[]))
            matches=[user for user in users if user['email']==tenant['email']]
        user=matches[0]
        memberships=operator.read('/users/'+user['id']+'/memberships')['data']
        if slug not in memberships:
            operator.write('POST','/users/'+user['id']+'/memberships',{'tenant_slug':slug})
        print('Verified synthetic auth enrollment:',slug)
    operator.call('DELETE','/session')

if __name__=='__main__':main()
