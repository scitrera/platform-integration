#!/usr/bin/env python3
"""Exercise the Kubernetes backup database path on isolated Docker PostgreSQL.

This qualifies multi-database dump/restore and bounded fingerprints, not CNPG,
Kubernetes volumes, object storage, or off-node disaster recovery.
"""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from kube_backup import Cluster, backup_databases


class DockerDatabase(Cluster):
    def __init__(self, name):
        self.name = name

    def translate(self, args):
        return ["docker", "exec", "-i", self.name] + args[args.index("--") + 1:]

    def run(self, args, **kwargs):
        return subprocess.run(self.translate(args), check=True, **kwargs)

    def output(self, args, **kwargs):
        return subprocess.check_output(self.translate(args), **kwargs)

    def lines(self, args):
        # Exercise the actual streaming implementation with the Docker command.
        proxy = Cluster(Path("/unused"), "synthetic")
        proxy.command = []
        yield from proxy.lines(self.translate(args))

    def primary(self, namespace, name):
        return self.name

    def sql(self, text, db="postgres"):
        return subprocess.check_output(["docker", "exec", "-i", self.name, "psql", "-XAt",
            "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", text], text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="postgres:17.11-alpine")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    name = "consolidated-backup-qa-" + uuid.uuid4().hex[:10]
    start = time.monotonic()
    database = DockerDatabase(name)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none", "--memory", "512m",
        "--cpus", "1", "-e", "POSTGRES_PASSWORD=synthetic-qa-only", args.image], check=True, stdout=subprocess.DEVNULL)
    try:
        for attempt in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0: break
            time.sleep(1)
        else: raise RuntimeError("Postgres did not start")
        for db in ("memorylayer", "dataconnectors", "sparkroute", "storage"):
            database.sql("CREATE ROLE " + db + " LOGIN")
            database.sql("CREATE DATABASE " + db + " OWNER " + db)
            database.sql("SET ROLE " + db + "; CREATE TABLE evidence(id integer PRIMARY KEY, body text); "
                         "INSERT INTO evidence SELECT n, 'Synthetic café ' || n FROM generate_series(1,25000) n", db)
        files = backup_databases(database, "synthetic", {"metadata": {"name": "consolidated"},
                                 "spec": {"imageName": args.image}}, args.output)
        checked = {}
        for filename, record in files.items():
            if record["kind"] != "database": continue
            db = record["database"]
            target = db + "_restored"
            database.sql("CREATE DATABASE " + target + " OWNER " + record["owner"])
            with (args.output / filename).open("rb") as source:
                subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", "postgres", "--exit-on-error",
                                "--single-transaction", "-d", target], stdin=source, check=True)
            actual = database.fingerprints("synthetic", name, target)
            if actual != record["table_fingerprints"]: raise RuntimeError("Restore mismatch: " + db)
            if db != "postgres":
                legacy = database.sql("SELECT md5(string_agg(md5(row_to_json(t)::text),'' ORDER BY md5(row_to_json(t)::text))) FROM evidence t", target).strip()
                if actual['public.evidence']['row_md5'] != legacy: raise RuntimeError("Fingerprint compatibility mismatch")
            checked[db] = actual
        evidence = {"databases": checked, "roles_saved": any(r["kind"] == "roles" for r in files.values()),
                    "elapsed_seconds": round(time.monotonic() - start, 3), "image": args.image,
                    "scope": "synthetic Docker database dump/restore; no Kubernetes or off-node backup acceptance"}
        (args.output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-fv", name], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__": main()
