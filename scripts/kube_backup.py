#!/usr/bin/env python3
"""Offline application-state backup of explicitly selected Kubernetes namespaces."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid
from cold_backup import digest
from cold_restore import validate_archive


class Cluster:
    def __init__(self, kubeconfig, context, placement=None):
        self.placement = placement or {}
        self.command = ["kubectl", "--kubeconfig", str(kubeconfig.resolve()), "--context", context]

    def run(self, args, **kwargs):
        return subprocess.run(self.command + args, check=True, **kwargs)

    def output(self, args, **kwargs):
        return subprocess.check_output(self.command + args, **kwargs)

    def lines(self, args):
        # Stream stdout and spool stderr separately; no table-sized allocation
        # or pipe deadlock while computing backup verification fingerprints.
        with tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(self.command + args, stdout=subprocess.PIPE,
                                       stderr=errors, text=True)
            try:
                for line in process.stdout:
                    yield line.rstrip("\n")
                if process.wait() != 0:
                    raise RuntimeError("Database fingerprint command failed; backup is incomplete")
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()

    def get(self, namespace, kind, name=None):
        args = ["-n", namespace, "get", kind]
        if name:
            args.append(name)
        return json.loads(self.output(args + ["-o", "json"]))

    def assert_stopped(self, namespaces):
        for ns in namespaces:
            for kind in ("deployments", "statefulsets"):
                for item in self.get(ns, kind)["items"]:
                    if item["spec"].get("replicas", 1):
                        raise RuntimeError("Scale application workloads to zero before backup: " + ns + "/" + item["metadata"]["name"])
            for pod in self.get(ns, "pods")["items"]:
                if "cnpg.io/cluster" in pod["metadata"].get("labels", {}):
                    continue
                if pod["status"]["phase"] not in ("Succeeded", "Failed"):
                    raise RuntimeError("An application/Job/allocation pod is still active: " + ns + "/" + pod["metadata"]["name"])
            for job in self.get(ns, "cronjobs")["items"]:
                if not job["spec"].get("suspend", False):
                    raise RuntimeError("Suspend CronJobs before backup: " + ns)

    def primary(self, ns, cluster):
        obj = self.get(ns, "clusters.postgresql.cnpg.io", cluster)
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in obj.get("status", {}).get("conditions", [])):
            raise RuntimeError("Database is not Ready: " + ns + "/" + cluster)
        return obj["status"]["currentPrimary"]

    def databases(self, ns, pod):
        # CNPG's initdb is only the first database. Restored clusters may not
        # have initdb at all. Inventory the server, including postgres, so newly
        # added application databases are never silently omitted.
        query = "SELECT json_build_object('database', datname, 'owner', pg_get_userbyid(datdba), 'allow_connections', datallowconn) FROM pg_database WHERE NOT datistemplate ORDER BY datname"
        output = self.output(["-n", ns, "exec", pod,
            "-c", "postgres", "--", "psql", "-XAt", "--username=postgres",
            "--dbname=postgres", "-v", "ON_ERROR_STOP=1", "-c", query], text=True)
        records = [json.loads(line) for line in output.splitlines() if line]
        if not records:
            raise RuntimeError("Empty database inventory: " + ns + "/" + pod)
        seen = set()
        for record in records:
            database, owner = identifier(record["database"]), identifier(record["owner"])
            if database in seen or record["allow_connections"] is not True:
                raise RuntimeError("Database inventory requires review: " + ns + "/" + database)
            seen.add(database)
        return records

    def fingerprints(self, ns, pod, database):
        base = ["-n", ns, "exec", pod, "-c", "postgres", "--", "psql", "-XAt", "--username=postgres", "--dbname=" + identifier(database), "-v", "ON_ERROR_STOP=1", "-c"]
        query = "SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname NOT LIKE 'pg_%' AND n.nspname NOT IN ('information_schema','ag_catalog') ORDER BY 1"
        tables = self.output(base + [query], text=True).splitlines()
        result = {}
        for table in tables:
            query = "SELECT md5(row_to_json(t)::text) FROM " + table + " t ORDER BY 1"
            rows, digest = 0, hashlib.md5()
            for row in self.lines(base + [query]):
                if not re.fullmatch(r"[a-f0-9]{32}", row):
                    raise RuntimeError("Invalid row fingerprint; backup is incomplete")
                digest.update(row.encode("ascii"))
                rows += 1
            result[table] = {"rows": rows, "row_md5": digest.hexdigest()}
        return result

    def volume_pod(self, ns, claim, image, writable=False):
        name = "state-copy-" + uuid.uuid4().hex[:12]
        capabilities = ["DAC_READ_SEARCH"]
        if writable:
            capabilities += ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": name, "namespace": ns, "labels": {"platform.scitrera.io/operation": "state-copy"}},
            "spec": {"restartPolicy": "Never", "terminationGracePeriodSeconds": 1, "automountServiceAccountToken": False,
                "securityContext": {"runAsUser": 0, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "copy", "image": image,
                    "command": ["python", "-c", "import time; time.sleep(900)"],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"], "add": capabilities}},
                    "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
                    "volumeMounts": [{"name": "state", "mountPath": "/state", "readOnly": not writable}]}],
                "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": claim, "readOnly": not writable}}]}}
        pod["spec"].update(self.placement.get(ns, {}))
        self.run(["apply", "-f", "-"], input=json.dumps(pod), text=True, stdout=subprocess.DEVNULL)
        try:
            self.run(["-n", ns, "wait", "--for=condition=Ready", "pod/" + name, "--timeout=180s"], stdout=subprocess.DEVNULL)
        except BaseException:
            self.remove_copy(ns, name)
            raise
        return name

    def remove_copy(self, ns, name):
        self.run(["-n", ns, "delete", "pod", name, "--wait=true", "--timeout=90s"], stdout=subprocess.DEVNULL)


def placement_file(path):
    """Explicit namespace placement; no extra Pod fields can enter the helper."""
    if path is None:
        return {}
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Placement must map namespaces to nodeSelector/tolerations")
    for namespace, placement in value.items():
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", namespace) or len(namespace) > 63:
            raise ValueError("Invalid placement namespace")
        if not isinstance(placement, dict) or set(placement) - {"nodeSelector", "tolerations"}:
            raise ValueError("Placement only supports nodeSelector/tolerations")
        selector, tolerations = placement.get("nodeSelector", {}), placement.get("tolerations", [])
        if not isinstance(selector, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in selector.items()):
            raise ValueError("Invalid placement nodeSelector")
        if not isinstance(tolerations, list):
            raise ValueError("Invalid placement tolerations")
        for item in tolerations:
            if (not isinstance(item, dict) or set(item) - {"key", "operator", "value", "effect", "tolerationSeconds"}
                    or any(not isinstance(v, str) for k, v in item.items() if k != "tolerationSeconds")
                    or item.get("operator", "Equal") not in {"Equal", "Exists"}
                    or item.get("effect", "") not in {"", "NoSchedule", "PreferNoSchedule", "NoExecute"}
                    or ("tolerationSeconds" in item and (type(item["tolerationSeconds"]) is not int or item["tolerationSeconds"] < 0))):
                raise ValueError("Invalid placement toleration")
    return value


def backup_databases(cluster, ns, db, directory):
    name = db["metadata"]["name"]
    pod = cluster.primary(ns, name)
    inventory = cluster.databases(ns, pod)
    files = {}
    # Roles include ownership/membership/password hashes and are private backup
    # material. A restore drill must not replay them into a running server.
    roles_name = ns + "--" + name + "--roles.sql"
    with (directory / roles_name).open("wb") as out:
        cluster.run(["-n", ns, "exec", pod, "-c", "postgres", "--",
                     "pg_dumpall", "--roles-only", "--username=postgres"], stdout=out)
    files[roles_name] = {"kind": "roles", "namespace": ns, "cluster": name,
                        "sha256": digest(directory / roles_name)}
    for item in inventory:
        database, owner = item["database"], item["owner"]
        filename = ns + "--" + name + "--" + database + ".dump"
        with (directory / filename).open("wb") as out:
            cluster.run(["-n", ns, "exec", pod, "-c", "postgres", "--",
                         "pg_dump", "--format=custom", "--username=postgres", "--dbname=" + database], stdout=out)
        with (directory / filename).open("rb") as source:
            cluster.run(["-n", ns, "exec", "-i", pod, "-c", "postgres", "--",
                         "pg_restore", "--list"], stdin=source, stdout=subprocess.DEVNULL)
        files[filename] = {"kind": "database", "namespace": ns, "cluster": name,
            "database": database, "owner": owner, "image": db["spec"]["imageName"],
            "sha256": digest(directory / filename),
            "table_fingerprints": cluster.fingerprints(ns, pod, database)}
        print("Backed up database:", ns + "/" + name + "/" + database, flush=True)
    if inventory != cluster.databases(ns, pod):
        raise RuntimeError("Database inventory changed during backup: " + ns + "/" + name)
    return files


def identifier(value):
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise ValueError("Unsupported database identifier")
    return value


def verify(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("format") != "kubernetes-application-state-v1":
        raise ValueError("Unsupported backup format")
    for name, record in manifest["files"].items():
        if Path(name).name != name or digest(directory / name) != record["sha256"]:
            raise ValueError("Invalid archive/checksum: " + name)
        if record["kind"] == "pvc":
            validate_archive(directory / name)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--placement", type=Path, help="JSON namespace mapping to helper Pod nodeSelector/tolerations")
    parser.add_argument("--namespace", action="append", default=[])
    parser.add_argument("--helper-image", help="Recorded backend image containing Python; pulled/loaded before maintenance")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.action == "verify":
        manifest = verify(directory)
        print("Verified", len(manifest["files"]), "private backup files")
        return
    if not all((args.kubeconfig, args.context, args.namespace, args.helper_image)):
        parser.error("Backup requires explicit kubeconfig, context, namespaces and helper image")
    os.umask(0o077)
    cluster = Cluster(args.kubeconfig, args.context, placement_file(args.placement))
    cluster.assert_stopped(args.namespace)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = {"format": "kubernetes-application-state-v1", "context": args.context,
                "namespaces": args.namespace, "helper_image": args.helper_image, "files": {},
                "scope": "Quiesced application PVCs, logical CNPG application databases, namespace configuration. External S3 requires its own coordinated backup."}
    resources = {}
    for ns in args.namespace:
        resources[ns] = {kind: cluster.get(ns, kind)["items"] for kind in
                        ("deployments", "statefulsets", "secrets", "configmaps", "pvc", "clusters.postgresql.cnpg.io")}
        for db in resources[ns]["clusters.postgresql.cnpg.io"]:
            manifest["files"].update(backup_databases(cluster, ns, db, directory))
        for pvc in resources[ns]["pvc"]:
            if "cnpg.io/cluster" in pvc["metadata"].get("labels", {}):
                continue
            name = pvc["metadata"]["name"]
            filename = ns + "--" + name + ".tar.gz"
            pod = cluster.volume_pod(ns, name, args.helper_image)
            try:
                with (directory / filename).open("wb") as out:
                    cluster.run(["-n", ns, "exec", pod, "-c", "copy", "--", "python", "-c",
                        "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/state',arcname='.'); t.close()"], stdout=out)
            finally:
                cluster.remove_copy(ns, pod)
            validate_archive(directory / filename)
            manifest["files"][filename] = {"kind": "pvc", "namespace": ns, "claim": name,
                "storage": pvc["spec"]["resources"]["requests"]["storage"], "storageClass": pvc["spec"].get("storageClassName", ""),
                "sha256": digest(directory / filename)}
            print("Backed up PVC:", ns + "/" + name, flush=True)
    path = directory / "resources.json"
    path.write_text(json.dumps(resources, indent=2) + "\n")
    manifest["files"][path.name] = {"kind": "configuration", "sha256": digest(path)}
    cluster.assert_stopped(args.namespace)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify(directory)
    print("Backup verified. Keep namespaces quiesced until the coordinated object-store backup is complete.")


if __name__ == "__main__":
    main()
