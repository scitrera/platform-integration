"""Probe enforced network boundaries from an actual allocated code container."""
import json
import socket
import urllib.request
import urllib.error

checks = [
    ("own-aether", "alpha-aether.tenant-alpha.svc.cluster.local", 50051, True),
    ("shared-gateway", "shared-gateway.platform-shared.svc.cluster.local", 8080, True),
    ("download-only", "storage-download.platform-storage.svc.cluster.local", 8080, True),
    ("private-capability-api", "storage-edge.platform-storage.svc.cluster.local", 8090, False),
    ("own-memorylayer", "alpha-memorylayer.tenant-alpha.svc.cluster.local", 8000, False),
    ("other-aether", "beta-aether.tenant-beta.svc.cluster.local", 50051, False),
    ("other-memorylayer", "beta-memorylayer.tenant-beta.svc.cluster.local", 8000, False),
    ("kubernetes-api", "kubernetes.default.svc.cluster.local", 443, False),
    ("metadata", "169.254.169.254", 80, False),
]
results = []
for name, host, port, expected in checks:
    try:
        connection = socket.create_connection((host, port), timeout=2)
        connection.close()
        connected = True
    except OSError:
        connected = False
    results.append(dict(target=name, connected=connected, expected=expected))
print(json.dumps(results))
assert all(row["connected"] == row["expected"] for row in results)

# Bypass ambient HTTP_PROXY to prove the Kubernetes service itself filters these.
http=urllib.request.build_opener(urllib.request.ProxyHandler({}))
for path,method,expected in [("/capabilities","POST",404),("/blob/synthetic","PUT",403)]:
    request=urllib.request.Request("http://storage-download.platform-storage.svc.cluster.local:8080"+path,method=method)
    try:
        with http.open(request,timeout=5) as response:status=response.status
    except urllib.error.HTTPError as error:
        status=error.code
    assert status==expected,(path,status,expected)
    print(json.dumps({"route":path,"method":method,"status":status,"expected":expected}))
