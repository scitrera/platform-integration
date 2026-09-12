"""Check Docker sandbox separation using observed container IPs and its real proxy."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
import subprocess
from urllib.parse import urlparse


def inspect(names):
    return json.loads(subprocess.check_output(["docker", "inspect", *names]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--sandbox", required=True, help="Full allocation UUID")
    parser.add_argument("--tenant", choices=["alpha", "beta"], default="alpha")
    args = parser.parse_args()
    ids = subprocess.check_output(["docker", "ps", "-q", "--filter", "label=scitrera.sandbox_id=" + args.sandbox], text=True).split()
    if not ids:
        parser.error("No running containers for this allocation")
    allocated = inspect(ids)
    for container in allocated:
        labels = container["Config"]["Labels"]
        if labels.get("scitrera.instance_id") != args.project or labels.get("scitrera.tenant_id") != args.tenant:
            parser.error("Allocation ownership differs from the explicit project/tenant")
    code = next(c for c in allocated if c["Config"]["Labels"].get("scitrera.role") == "code-sidecar")
    ids = subprocess.check_output(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=" + args.project], text=True).split()
    services = {c["Config"]["Labels"]["com.docker.compose.service"]: c for c in inspect(ids)}
    checks = []
    other = "beta" if args.tenant == "alpha" else "alpha"
    for service, port in [
        ("aether-" + args.tenant, 50051), ("memorylayer-" + args.tenant, 8000),
        ("aether-" + other, 50051), ("memorylayer-" + other, 8000),
        ("storage-edge", 8090), ("objects", 9000), ("mt-postgres", 5432),
        ("gateway", 8080),
    ]:
        networks = services[service]["NetworkSettings"]["Networks"]
        for index, network in enumerate(networks.values()):
            checks.append((service + "-direct-" + str(index), network["IPAddress"], port, False))
    environment = dict(value.split("=", 1) for value in code["Config"]["Env"])
    proxy = urlparse(environment["HTTP_PROXY"])
    checks.append(("sidecar-proxy", proxy.hostname, proxy.port, True))
    checks.append(("metadata", "169.254.169.254", 80, False))
    script = """
import json,socket,urllib.request,urllib.error,os
checks=INPUT_CHECKS
for name,host,port,expected in checks:
    try:
        with socket.create_connection((host,port),timeout=1): connected=True
    except OSError:connected=False
    print(json.dumps(dict(target=name,connected=connected,expected=expected)),flush=True)
    assert connected==expected,name
http=urllib.request.build_opener(urllib.request.ProxyHandler({'http':os.environ['HTTP_PROXY']}))
for path,expected in [('http://llm.local/v1/models',200),('http://data.local/capabilities',403)]:
    try:
        with http.open(path,timeout=10) as response:status=response.status
    except urllib.error.HTTPError as error:status=error.code
    print(json.dumps(dict(proxy_path=path,status=status,expected=expected)),flush=True)
    assert status==expected,(path,status)
""".replace("INPUT_CHECKS", repr(checks))
    subprocess.run(["docker", "exec", "-i", code["Id"], "python3", "-"], input=script, text=True, check=True)


if __name__ == "__main__":
    main()
