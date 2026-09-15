#!/usr/bin/env python3
"""Ensure auth tenants, and enroll example users only in fixture mode."""
import argparse
import json
from pathlib import Path
from compose_tenants import load_tenants
from urllib.error import HTTPError
from auth_operator import Operator

ROOT=Path(__file__).resolve().parents[1]

def reconcile_default_workspace(operator, tenant):
    """Explicit defaults (including null) update existing tenant landing behavior."""
    if "default_workspace" not in tenant:
        return
    current = operator.read('/tenants/' + tenant['slug'])['data']
    metadata = dict(current.get('metadata') or {})
    desired = tenant['default_workspace']
    if metadata.get('default_workspace') == desired:
        return
    metadata['default_workspace'] = desired
    operator.write('PUT', '/tenants/' + tenant['slug'],
                   {'name': current['name'], 'enabled': current['enabled'], 'metadata': metadata})


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
                                             'metadata':{'default_workspace':tenant.get('default_workspace', tenant['workspace'])}})
            if (ROOT/'.local/fixtures.enabled').exists():
                operator.write('PUT','/tenants/'+slug+'/auth',{'auto_add':False,'providers':['fixture'],'checks':{}})
        reconcile_default_workspace(operator, tenant)
        if not (ROOT/'.local/fixtures.enabled').exists():
            print('Verified auth tenant:', slug)
            continue
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
