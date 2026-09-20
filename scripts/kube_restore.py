#!/usr/bin/env python3
"""Restore verified Kubernetes backups into NEW databases/PVCs and compare their contents."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tarfile
from kube_backup import Cluster, identifier, verify, placement_file


def file_inventory(path):
    result = {}
    with tarfile.open(path) as archive:
        for member in archive:
            if member.isdir():
                result[member.name] = {"mode": member.mode, "uid": member.uid, "gid": member.gid, "directory": True}
            else:
                source = archive.extractfile(member)
                digest = hashlib.sha256()
                for part in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(part)
                result[member.name] = {"mode": member.mode, "uid": member.uid, "gid": member.gid,
                                       "size": member.size, "sha256": digest.hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--receipt-directory", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--placement", type=Path, help="JSON namespace mapping to helper Pod nodeSelector/tolerations")
    parser.add_argument("--suffix", required=True, help="New database/PVC suffix; existing destinations are refused")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9]{0,15}", args.suffix):
        parser.error("Use an alphanumeric suffix starting with a letter, at most 16 characters")
    suffix = args.suffix
    backup = args.directory.resolve()
    manifest = verify(backup)  # Verify ALL checksums and archive paths before writes.
    cluster = Cluster(args.kubeconfig, args.context, placement_file(args.placement))
    targets = {}
    for filename, record in manifest["files"].items():
        ns = record.get("namespace")
        if record["kind"] == "database":
            database = identifier(record["database"] + "_" + args.suffix)
            pod = cluster.primary(ns, record["cluster"])
            current = cluster.get(ns, "clusters.postgresql.cnpg.io", record["cluster"])
            if current["spec"]["imageName"] != record["image"]:
                parser.error("Restore requires the recorded database image: " + ns + "/" + record["cluster"])
            query = "SELECT count(*) FROM pg_database WHERE datname='" + database + "'"
            found = subprocess.check_output(cluster.command + ["-n", ns, "exec", pod, "-c", "postgres", "--",
                "psql", "-XAt", "--username=postgres", "--dbname=postgres", "-v", "ON_ERROR_STOP=1", "-c", query], text=True).strip()
            if found != "0":
                parser.error("Refusing existing destination database: " + ns + "/" + database)
            owner = identifier(record["owner"])
            owner_query = "SELECT count(*) FROM pg_roles WHERE rolname='" + owner + "'"
            owner_found = subprocess.check_output(cluster.command + ["-n", ns, "exec", pod,
                "-c", "postgres", "--", "psql", "-XAt", "--username=postgres", "--dbname=postgres",
                "-v", "ON_ERROR_STOP=1", "-c", owner_query], text=True).strip()
            if owner_found != "1":
                parser.error("Restore owner is missing; provision reviewed roles first: " + ns + "/" + owner)
            if "table_fingerprints" not in record:
                parser.error("Database verification inventory missing from backup")
            targets[filename] = {"namespace": ns, "database": database, "pod": pod}
        elif record["kind"] == "pvc":
            name = record["claim"] + "-" + suffix
            if len(name) > 63:
                parser.error("Destination PVC name is too long")
            found = subprocess.check_output(cluster.command + ["-n", ns, "get", "pvc", name, "--ignore-not-found", "-o", "name"], text=True).strip()
            if found:
                parser.error("Refusing existing destination PVC: " + ns + "/" + name)
            targets[filename] = {"namespace": ns, "claim": name}
    os.umask(0o077)
    receipt = args.receipt_directory.resolve()
    receipt.mkdir(parents=True, exist_ok=False, mode=0o700)
    results = {}
    for filename, target in targets.items():
        record = manifest["files"][filename]
        ns = target["namespace"]
        if record["kind"] == "database":
            pod, database = target["pod"], target["database"]
            base = ["-n", ns, "exec", pod, "-c", "postgres", "--"]
            cluster.run(base + ["createdb", "--username=postgres", "--template=template0",
                               "--owner=" + identifier(record["owner"]), database])
            with (backup / filename).open("rb") as source:
                cluster.run(["-n", ns, "exec", "-i", pod, "-c", "postgres", "--",
                    "pg_restore", "--username=postgres", "--exit-on-error", "--single-transaction", "--dbname=" + database], stdin=source)
            actual = cluster.fingerprints(ns, pod, database)
            if actual != record["table_fingerprints"]:
                (receipt / (filename + ".mismatch.json")).write_text(json.dumps({"expected": record["table_fingerprints"], "actual": actual}, indent=2))
                raise RuntimeError("Restored database row fingerprints differ: " + ns + "/" + database)
            results[filename] = {**target, "tables_verified": len(actual)}
            print("Verified restored database:", ns + "/" + database, flush=True)
        else:
            claim = target["claim"]
            pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {
                "name": claim, "namespace": ns,
                "labels": {"platform.scitrera.io/operation": "restore"},
                "annotations": {"helm.sh/resource-policy": "keep"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": record["storageClass"],
                         "resources": {"requests": {"storage": record["storage"]}}}}
            cluster.run(["apply", "-f", "-"], input=json.dumps(pvc), text=True, stdout=subprocess.DEVNULL)
            pod = cluster.volume_pod(ns, claim, manifest["helper_image"], writable=True)
            try:
                with (backup / filename).open("rb") as source:
                    cluster.run(["-n", ns, "exec", "-i", pod, "-c", "copy", "--", "python", "-c",
                        "import os,sys,tarfile; assert not os.listdir('/state'), 'Restore target is not empty'; "
                        "t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|gz'); t.extractall('/state',numeric_owner=True,filter='fully_trusted')"], stdin=source)
                readback = receipt / filename
                with readback.open("wb") as out:
                    cluster.run(["-n", ns, "exec", pod, "-c", "copy", "--", "python", "-c",
                        "import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|gz'); t.add('/state',arcname='.'); t.close()"], stdout=out)
                expected, actual = file_inventory(backup / filename), file_inventory(readback)
                if actual != expected:
                    raise RuntimeError("Restored PVC file/ownership inventory differs: " + ns + "/" + claim)
                results[filename] = {**target, "entries_verified": len(actual)}
                print("Verified restored PVC:", ns + "/" + claim, flush=True)
            finally:
                cluster.remove_copy(ns, pod)
        (receipt / "restored.json").write_text(json.dumps(results, indent=2) + "\n")
    print("Restored and verified all databases and PVCs into unused destinations. Original resources are unchanged.")
    print("Role snapshots are retained for recovery but never replayed into the live server by this drill.")
    print("Recovery promotion requires operator-selected database URLs, PVC bindings and the backed-up matching Secrets.")


if __name__ == "__main__":
    main()
