#!/usr/bin/env python3
"""Plan/apply ordinary Helm phases using an explicit cluster and existing Secrets."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
LAST_PHASE = {"shared": 2, "storage": 1, "tenant": 4}


def phases(current, target):
    if current is not None and target < current:
        raise ValueError("Refusing to reduce the installed phase; that would remove running resources")
    return list(range(current if current is not None else 0, target + 1))


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, **kwargs)


def secret_references(value):
    """Collect explicit Pod secret references without reading/printing secret bytes."""
    names = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("secretRef", "secretKeyRef") and isinstance(child, dict):
                names.add(child["name"])
            elif key == "secret" and isinstance(child, dict):
                name = child.get("secretName") or child.get("name")
                if name:
                    names.add(name)
            names.update(secret_references(child))
    elif isinstance(value, list):
        for child in value:
            names.update(secret_references(child))
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply"])
    parser.add_argument("--chart", choices=list(LAST_PHASE), required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--through-phase", type=int)
    parser.add_argument("--timeout", type=int, default=600, help="Seconds per phase")
    args = parser.parse_args()
    target = LAST_PHASE[args.chart] if args.through_phase is None else args.through_phase
    if not 0 <= target <= LAST_PHASE[args.chart]:
        parser.error("Phase is outside this chart's lifecycle")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    chart = ROOT / "charts" / ("platform-" + args.chart)
    kube = ["kubectl", "--kubeconfig", str(args.kubeconfig.resolve()), "--context", args.context]
    helm = ["--kubeconfig", str(args.kubeconfig.resolve()), "--kube-context", args.context, "-n", args.namespace]
    run(kube + ["get", "namespace", args.namespace], stdout=subprocess.DEVNULL)
    previous = subprocess.run(["helm", "get", "values", args.release, *helm, "-o", "json"],
                              capture_output=True, text=True)
    if previous.returncode == 0:
        current = (json.loads(previous.stdout) or {}).get("phase", 0)
    elif "release: not found" in previous.stderr:
        current = None
    else:
        raise RuntimeError("Unable to inspect release: " + previous.stderr.strip())
    try:
        selected = phases(current, target)
    except ValueError as error:
        parser.error(str(error))
    run(kube + ["get", "crd", "clusters.postgresql.cnpg.io"], stdout=subprocess.DEVNULL)
    if args.chart == "shared":
        run(kube + ["get", "crd", "gateways.gateway.networking.k8s.io",
                    "httproutes.gateway.networking.k8s.io"], stdout=subprocess.DEVNULL)
    version = json.loads(subprocess.check_output(kube + ["version", "-o", "json"], text=True))["serverVersion"]["gitVersion"]
    rendered = {}
    for phase in selected:
        output = subprocess.check_output(["helm", "template", args.release, str(chart),
                    *helm, "--kube-version", version, "-f", str(args.values.resolve()), "--set", "phase=" + str(phase)], text=True)
        objects = [obj for obj in yaml.safe_load_all(output) if obj]
        rendered[phase] = objects
        for name in sorted(secret_references(objects)):
            run(kube + ["-n", args.namespace, "get", "secret", name, "-o", "name"],
                stdout=subprocess.DEVNULL)
        for obj in objects:
            if obj["kind"] == "Cluster":
                storage_class = obj["spec"]["storage"].get("storageClass")
                if storage_class:
                    run(kube + ["get", "storageclass", storage_class], stdout=subprocess.DEVNULL)
            if obj["kind"] == "Gateway":
                run(kube + ["get", "gatewayclass", obj["spec"]["gatewayClassName"]],
                    stdout=subprocess.DEVNULL)
    print(f"{args.context}: {args.namespace}/{args.release}, phases {selected}", flush=True)
    if args.action == "plan":
        print("Chart/schema, prerequisite references and existing Secret names checked; no resources changed.")
        return
    for phase in selected:
        run(["helm", "upgrade", "--install", args.release, str(chart), *helm,
             "-f", str(args.values.resolve()), "--set", "phase=" + str(phase),
             "--wait", "--wait-for-jobs", "--timeout", str(args.timeout) + "s"])
        # Helm waits for standard workloads/Jobs, but not CNPG custom-resource health.
        for obj in rendered[phase]:
            if obj["kind"] == "Cluster":
                run(kube + ["-n", args.namespace, "wait", "--for=condition=Ready",
                    "cluster.postgresql.cnpg.io/" + obj["metadata"]["name"],
                    "--timeout=" + str(args.timeout) + "s"])
        print(f"Completed phase {phase}; rerun this command to resume after a failure.", flush=True)


if __name__ == "__main__":
    main()
