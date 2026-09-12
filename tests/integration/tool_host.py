"""Exercise the real tool-host edge with a disposable user API token."""
import asyncio
import json
import os
import ssl
from pathlib import Path
import aiohttp

async def run():
    url=os.environ.get("TOOL_HOST_URL","http://tools-alpha:8090/v1/connect")
    token=json.loads(Path(os.environ.get("TOOL_HOST_TOKEN_FILE","/fixture/token.json")).read_text())["token"]
    tls = ssl.create_default_context(cafile=os.environ["TOOL_HOST_CA_FILE"]) if os.environ.get("TOOL_HOST_CA_FILE") else True
    ws_options={"ssl":tls}
    common={}
    if os.environ.get("TOOL_HOST_HTTP_HOST"):common["Host"]=os.environ["TOOL_HOST_HTTP_HOST"]
    if os.environ.get("TOOL_HOST_SERVER_NAME"):
        ws_options["server_hostname"]=os.environ["TOOL_HOST_SERVER_NAME"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        for headers in ({},{"X-API-Key":"invalid-fixture-token"}):
            try:
                async with session.ws_connect(url,headers={**common,**headers},**ws_options):
                    raise AssertionError("Unauthenticated tool host accepted")
            except aiohttp.WSServerHandshakeError as error:
                assert error.status==401, error.status
        print("Missing and invalid credentials refused.",flush=True)
        try:
            async with session.ws_connect(url,headers={**common,"X-API-Key":token,"Origin":"https://unapproved.example.test"},**ws_options):
                raise AssertionError("Unapproved browser origin accepted")
        except aiohttp.WSServerHandshakeError as error:
            assert error.status==403, error.status
        if os.environ.get("TOOL_HOST_OTHER_TENANT_URL"):
            try:
                async with session.ws_connect(os.environ["TOOL_HOST_OTHER_TENANT_URL"],headers={**common,"X-API-Key":token},**ws_options):
                    raise AssertionError("Other tenant accepted the token")
            except aiohttp.WSServerHandshakeError as error:
                assert error.status in (401,403),error.status
        print("Unapproved browser origin and configured tenant boundary refused.",flush=True)
        async with session.ws_connect(url,headers={**common,"X-API-Key":token,
            "X-Auth-User-ID":"forged@example.test","X-Auth-Tenant-ID":"beta"},**ws_options) as ws:
            await ws.send_json({"jsonrpc":"2.0","id":"register","method":"tools/register",
                "params":{"tools":[{"name":"integration.echo","description":"Echo a synthetic fixture marker",
                  "jsonSchema":{"type":"object","properties":{"value":{"type":"string"}},"required":["value"]}}]}})
            await ws.send_json({"jsonrpc":"2.0","id":"threads","method":"chat/list_threads","params":{"workspace":"default"}})
            await ws.send_json({"jsonrpc":"2.0","id":"out-of-workspace","method":"chat/list_threads","params":{"workspace":"_tenant"}})
            found=set()
            while len(found)<3:
                message=await asyncio.wait_for(ws.receive(),35)
                assert message.type==aiohttp.WSMsgType.TEXT, "Tool host closed before checked RPC completed"
                data=json.loads(message.data)
                if data.get("id")=="out-of-workspace":
                    assert data.get("error"),"RPC exceeded the configured workspace"
                    found.add(data["id"])
                if data.get("id") in {"register","threads"}:
                    assert "error" not in data, data.get("error")
                    found.add(data["id"])
                    if data["id"]=="register":assert data["result"]["accepted"]==["integration.echo"]
            print("Tool registration and checked thread RPC succeeded; other workspace refused.",flush=True)

if __name__=="__main__":asyncio.run(asyncio.wait_for(run(),80))
