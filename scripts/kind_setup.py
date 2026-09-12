#!/usr/bin/env python3
"""Create and prepare only the disposable platform-integration Kind cluster."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen
import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local/cluster"
NAME = "platform-integration"
CONTEXT = "kind-" + NAME


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "create", "prerequisites", "load-images"])
    args = parser.parse_args()
    lock = json.loads((ROOT / "tests/fixtures/prerequisites.lock.json").read_text())
    LOCAL.mkdir(parents=True, exist_ok=True)
    kubeconfig = LOCAL / "kubeconfig"
    kube = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT]
    if args.action in ("download", "prerequisites"):
        for name, artifact in lock["artifacts"].items():
            path = LOCAL / name
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                if "oci" in artifact:
                    run(["helm", "pull", artifact["oci"], "--version", artifact["version"],
                         "--destination", str(LOCAL)])
                    data = path.read_bytes()
                else:
                    with urlopen(artifact["url"], timeout=120) as response:
                        data = response.read()
                if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    raise RuntimeError("Checksum mismatch: " + name)
                path.write_bytes(data)
            print("Verified prerequisite artifact:", name, flush=True)
        if args.action == "download":
            return
    if args.action == "create":
        existing = subprocess.check_output(["kind", "get", "clusters"], text=True).splitlines()
        if NAME in existing:
            parser.error("Named cluster already exists; creation never replaces a cluster")
        run(["kind", "create", "cluster", "--name", NAME, "--image", lock["images"]["kind"],
             "--config", str(ROOT / "tests/fixtures/kind.yaml"), "--kubeconfig", str(kubeconfig),
             "--wait", "120s"])
        kubeconfig.chmod(0o600)
        return
    nodes = json.loads(subprocess.check_output(kube + ["get", "nodes", "-o", "json"]))
    expected = {NAME + "-control-plane", NAME + "-worker"}
    if {node["metadata"]["name"] for node in nodes["items"]} != expected:
        parser.error("Refusing changes outside the declared two-node disposable cluster")
    if args.action == "prerequisites":
        calico = (LOCAL / "calico.yaml").read_text()
        old = '# - name: CALICO_IPV4POOL_CIDR\n            #   value: "192.168.0.0/16"'
        new = '- name: CALICO_IPV4POOL_CIDR\n              value: "10.244.0.0/16"'
        if calico.count(old) != 1:
            raise RuntimeError("The pinned Calico manifest has an unexpected pool configuration")
        run(kube + ["apply", "--server-side", "-f", "-"], input=calico.replace(old, new), text=True)
        run(kube + ["-n", "kube-system", "rollout", "status", "daemonset/calico-node", "--timeout=300s"])
        run(kube + ["apply", "--server-side", "-f", str(LOCAL / "cnpg.yaml")])
        run(kube + ["-n", "cnpg-system", "rollout", "status", "deployment/cnpg-controller-manager", "--timeout=300s"])
        run(["helm", "upgrade", "--install", "envoy", str(LOCAL / "gateway-helm-v1.9.1.tgz"),
             "--kubeconfig", str(kubeconfig), "--kube-context", CONTEXT,
             "-n", "envoy-gateway-system", "--create-namespace", "--wait", "--timeout", "300s"])
        run(kube + ["apply", "-f", "-"], input=json.dumps({
            "apiVersion": "gateway.networking.k8s.io/v1", "kind": "GatewayClass",
            "metadata": {"name": "envoy"},
            "spec": {"controllerName": "gateway.envoyproxy.io/gatewayclass-controller"}}), text=True)
        (LOCAL / "postgres-image.txt").write_text(lock["images"]["postgres"] + "\n")
    else:
        images = json.loads((ROOT / ".local/images.json").read_text())
        exclude = {"CODE_BASE_IMAGE", "ML_POSTGRES_IMAGE", "ORCHESTRATOR_IMAGE"}
        selected = [record["tag"] for key, record in images.items() if key not in exclude]
        for key in ("postgres", "minio", "mc"):
            source = lock["images"][key]
            run(["docker", "pull", source])
            target = lock.get("localAliases", {}).get(key, source)
            if target != source:
                run(["docker", "tag", source, target])
            selected.append(target)
        # The session cache must also be available without anonymous cluster pulls.
        selected.append(yaml.safe_load((ROOT / "compose/compose.yaml").read_text())["services"]["sessions"]["image"])
        run(["docker", "pull", selected[-1]])
        run(["kind", "load", "docker-image", "--name", NAME, *selected])


if __name__ == "__main__":
    main()
