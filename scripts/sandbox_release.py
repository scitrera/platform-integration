#!/usr/bin/env python3
"""Release one sandbox through the owning public SDK after checking the expected owner on a tenant Aether connection."""
import argparse
import asyncio
import os
from scitrera_aether_client.client_async import AsyncServiceClient
from scitrera_sandbox_sdk import SandboxClient

async def release(sandbox_id,expected_owner):
    tenant=os.environ['TENANT_ID']
    client=AsyncServiceClient(implementation='scitrera-management-plane',specifier='integration-release',
        auto_reconnect=False,tls_enabled=True,
        tls_root_cert_path=os.environ['AETHER_MGMT_TLS_CA_CERT'],
        tls_client_cert_path=os.environ['AETHER_MGMT_TLS_CLIENT_CERT'],
        tls_client_key_path=os.environ['AETHER_MGMT_TLS_CLIENT_KEY'])
    await client.connect(target=os.environ['AETHER_GATEWAY'])
    try:
        sdk=SandboxClient(aether_client=client)
        state=await sdk.get(sandbox_id)
        if state is None:
            print('Sandbox already absent')
            return
        if state.owner_key != expected_owner:
            raise RuntimeError('Refusing release: sandbox owner does not match')
        await sdk.destroy(sandbox_id)
        assert await sdk.get(sandbox_id) is None, 'Sandbox still registered after release'
        print('Released sandbox through public lifecycle API:',sandbox_id)
    finally:
        await client.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sandbox',required=True)
    parser.add_argument('--owner',required=True)
    args=parser.parse_args()
    asyncio.run(asyncio.wait_for(release(args.sandbox,args.owner),timeout=120))
