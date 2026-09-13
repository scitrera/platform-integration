#!/usr/bin/env python3
"""Invoke public setup primitives and verify the resulting workspace projection."""
import asyncio
import os
from scitrera_aether_client.client_async import AsyncServiceClient
from scitrera_app_server.tenant_api.ml_client import MemoryLayerClient
from scitrera_app_server.tenant_api.tenant_interface2 import TenantInterface2
from scitrera_app_server.provisioning.ti2_setup import setup_ti2

async def setup():
    tenant=os.environ['TENANT_ID']
    client=AsyncServiceClient(implementation='scitrera-management-plane',specifier='bootstrap',
        auto_reconnect=False,tls_enabled=True,
        tls_root_cert_path=os.environ['AETHER_MGMT_TLS_CA_CERT'],
        tls_client_cert_path=os.environ['AETHER_MGMT_TLS_CLIENT_CERT'],
        tls_client_key_path=os.environ['AETHER_MGMT_TLS_CLIENT_KEY'])
    await client.connect(target=os.environ['AETHER_GATEWAY'])
    try:
        ml=MemoryLayerClient(aether_client=client,tenant_id=tenant,aether_target='sv::memorylayer')
        ti=TenantInterface2(tenant=tenant,aether_client=client,ml_client=ml)
        await setup_ti2(ti,client,os.environ.get('ADMIN_EMAIL',''),
                        minimal=os.environ.get('TENANT_SETUP_MINIMAL')=='true',
                        seed_dev_admin=os.environ.get('SEED_DEV_ADMIN')=='true')
        workspaces=await ml.list_workspaces()
        actual={entry['id'] for entry in workspaces}
        required={'default','_tenant','_global','_global_user'}
        if not required<=actual:
            raise RuntimeError('Bootstrap incomplete; missing workspaces: '+', '.join(sorted(required-actual)))
        result=await ti.reconcile_all_workspace_access_projections(None,agent=True)
        if result['workspaces_reconciled']<len(required):
            raise RuntimeError('Workspace projection was incomplete')
        print('Verified public workspace bootstrap and strict access projection:',tenant)
    finally:
        await client.close()

if __name__=='__main__':
    asyncio.run(asyncio.wait_for(setup(),timeout=240))
