"""Mint a disposable tool-host fixture token through the public Aether admin API."""
import asyncio
import json
import os
from pathlib import Path
from x_acl_seed import _build_admin_client

async def run():
    connection, admin = await _build_admin_client(os.environ["AETHER_GATEWAY"])
    try:
        target=Path(os.environ.get("TOOL_HOST_TOKEN_FILE","/fixture/tool-host-token.json"))
        if target.exists():
            previous=json.loads(target.read_text())
            result=await admin.revoke_token(previous["id"])
            if result is None or not result.success:
                raise RuntimeError("Could not revoke prior fixture token")
        result=await admin.create_token(name="integration-fixture-tool-host-"+os.environ["TENANT_ID"],
            principal_type="User",created_by=os.environ.get("FIXTURE_USER","alice@example.test"),
            workspace_patterns=["default"],expires_in_hours=1)
        if result is None or not result.success or not result.plaintext_token:
            raise RuntimeError("Fixture token mint failed")
        fd=os.open(target,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w") as output:
            json.dump({"id":result.created_token.id,"token":result.plaintext_token},output)
        if os.environ.get("FIXTURE_OWNER_UID"):
            os.chown(target,int(os.environ["FIXTURE_OWNER_UID"]),int(os.environ["FIXTURE_OWNER_GID"]))
        print("Disposable user token created; secret value withheld.")
    finally:
        await connection.close()

if __name__=="__main__":asyncio.run(asyncio.wait_for(run(),30))
