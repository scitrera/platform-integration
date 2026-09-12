#!/usr/bin/env python3
"""Verify one acceptance catalog value through the public Aether SDK."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
from scitrera_aether_client.client_async import AsyncServiceClient

async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model",required=True)
    parser.add_argument("--expected",type=Path,required=True)
    parser.add_argument("--remove",action="store_true")
    args=parser.parse_args()
    if not re.fullmatch(r"fixture-acceptance-[a-f0-9]{32}",args.model):
        parser.error("Only a unique acceptance model may be read or removed")
    expected=args.expected.read_bytes()
    assert json.loads(expected)["model"]==args.model
    client=AsyncServiceClient(implementation="scitrera-management-plane",specifier="acceptance-read-"+args.model,
        auto_reconnect=False,tls_enabled=True,tls_root_cert_path="/run/tls/ca.crt",
        tls_client_cert_path="/run/tls/tls.crt",tls_client_key_path="/run/tls/tls.key")
    await client.connect(target=os.environ["AETHER_GATEWAY"])
    try:
        key="sparkroute/modelcatalog/v1/models/"+hashlib.sha256(args.model.encode()).hexdigest()
        result=await client.kv_get(key,timeout=10)
        assert result is not None and result.success and result.value==expected,"Catalog differs from the successful publisher's exact bytes"
        if args.remove:
            result=await client.kv_delete(key,timeout=10)
            assert result is not None and result.success
        print("Verified exact catalog bytes"+(" and removed acceptance record" if args.remove else ""))
    finally:
        await client.close()

if __name__=="__main__":
    asyncio.run(asyncio.wait_for(main(),45))
