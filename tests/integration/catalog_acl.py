"""Probe actual Aether reader permissions using only supplied fixture credentials."""
import asyncio
import hashlib
import json
import os
import uuid
import sys
from scitrera_aether_client.client_async import AsyncServiceClient
from scitrera_aether_client.admin_async import AsyncAdminClient

async def denied(operation):
    try:
        result = await operation
    except Exception as error:
        text = str(error).lower()
        assert any(word in text for word in ("denied", "permission", "not authorized", "require an admin_acl grant")), type(error).__name__
        return
    assert result is not None and not result.success, "Operation was permitted or timed out"
    if result.DESCRIPTOR.name == "KVResponse":
        assert not result.value, "Denied KV operation leaked a value"
    else:
        assert any(word in result.error.lower() for word in ("denied", "permission", "not authorized", "require an admin_acl grant")), result.error

async def run():
    if "--seed" in sys.argv:
        client = AsyncServiceClient(implementation="scitrera-management-plane", specifier="catalog-probe-seed",
          auto_reconnect=False, tls_enabled=True, tls_root_cert_path="/run/tls/ca.crt",
          tls_client_cert_path="/run/tls/tls.crt", tls_client_key_path="/run/tls/tls.key")
        await client.connect(target=os.environ["AETHER_GATEWAY"])
        try:
            result = await client.kv_put("integration/noncatalog-private-value", b"synthetic private fixture")
            assert result is not None and result.success
            result = await client.kv_get("integration/noncatalog-private-value")
            assert result is not None and result.success and result.value == b"synthetic private fixture"
        finally:
            await client.close()
        print("Non-catalog synthetic value seeded and read back.")
        return
    results = {}
    for specifier in ("catalog-probe", "different-replica-"+uuid.uuid4().hex):
        client = AsyncServiceClient(implementation="sparkroute-modelcatalog", specifier=specifier,
          auto_reconnect=False, tls_enabled=True, tls_root_cert_path="/run/tls/ca.crt",
          tls_client_cert_path="/run/tls/tls.crt", tls_client_key_path="/run/tls/tls.key")
        await client.connect(target=os.environ["AETHER_GATEWAY"])
        try:
            key = "sparkroute/modelcatalog/v1/models/"+hashlib.sha256(b"sahara-default").hexdigest()
            record = await client.kv_get(key, timeout=10)
            assert record is not None and record.success and json.loads(record.value)["model"] == "sahara-default"
            # Reusing exactly the same bytes makes a failed negative check non-destructive.
            await denied(client.kv_put(key, record.value))
            await denied(client.kv_get("integration/noncatalog-private-value"))
            await denied(AsyncAdminClient(client).list_acl_rules())
            results["stable" if specifier == "catalog-probe" else "changed_replica"] = {
              "catalog_read": True, "catalog_write_denied": True,
              "other_kv_denied": True, "admin_api_denied": True}
        finally:
            await client.close()
    print(json.dumps(results, sort_keys=True))

if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(run(), 75))
