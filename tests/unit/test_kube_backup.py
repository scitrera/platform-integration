# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kube_backup import Cluster, backup_databases, placement_file


class BackupTests(unittest.TestCase):
    def test_server_inventory_includes_all_databases_without_initdb(self):
        cluster = Cluster(Path("/unused"), "synthetic")
        inventory = [{"database": name, "owner": name, "allow_connections": True}
                     for name in ("dataconnectors", "memorylayer", "postgres", "sparkroute", "storage")]
        cluster.primary = lambda *args: "primary"
        cluster.output = lambda *args, **kwargs: "\n".join(json.dumps(item) for item in inventory)
        cluster.fingerprints = lambda ns, pod, db: {"public.example": {"rows": 2, "row_md5": "synthetic"}}
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            out = kwargs.get("stdout")
            if hasattr(out, "write"):
                out.write(b"synthetic private backup")
        cluster.run = run
        with tempfile.TemporaryDirectory() as temp:
            files = backup_databases(cluster, "tenant-example", {
                "metadata": {"name": "one-db"}, "spec": {"imageName": "synthetic"}}, Path(temp))
        self.assertEqual({r["database"] for r in files.values() if r["kind"] == "database"},
                         {r["database"] for r in inventory})
        self.assertEqual(sum(r["kind"] == "roles" for r in files.values()), 1)
        self.assertEqual(sum("pg_restore" in call for call in calls), 5)

    def test_closed_database_does_not_silently_disappear(self):
        cluster = Cluster(Path("/unused"), "synthetic")
        cluster.output = lambda *args, **kwargs: json.dumps({"database": "retired", "owner": "owner", "allow_connections": False})
        with self.assertRaisesRegex(RuntimeError, "inventory requires review"):
            cluster.databases("tenant-example", "primary")

    def test_inventory_change_invalidates_backup(self):
        cluster = Cluster(Path("/unused"), "synthetic")
        cluster.primary = lambda *args: "primary"
        inventories = iter([[{"database": "one", "owner": "one"}], [{"database": "two", "owner": "two"}]])
        cluster.databases = lambda *args: next(inventories)
        cluster.fingerprints = lambda *args: {}
        def run(args, **kwargs):
            out = kwargs.get("stdout")
            if hasattr(out, "write"): out.write(b"synthetic")
        cluster.run = run
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(RuntimeError, "changed during backup"):
            backup_databases(cluster, "tenant-example", {"metadata": {"name": "db"},
                "spec": {"imageName": "synthetic"}}, Path(temp))

    def test_helper_placement_is_restricted_and_applied(self):
        placement = {"tenant-example": {"nodeSelector": {"pool": "example"},
            "tolerations": [{"key": "tenant", "operator": "Equal", "value": "example", "effect": "NoSchedule"}]}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "placement.json"
            path.write_text(json.dumps(placement))
            cluster = Cluster(Path("/unused"), "synthetic", placement_file(path))
            calls = []
            cluster.run = lambda args, **kwargs: calls.append((args, kwargs))
            cluster.volume_pod("tenant-example", "state", "synthetic")
            spec = json.loads(calls[0][1]["input"])["spec"]
            self.assertEqual(spec["nodeSelector"], {"pool": "example"})
            self.assertEqual(spec["tolerations"], placement["tenant-example"]["tolerations"])
            placement["tenant-example"]["hostNetwork"] = True
            path.write_text(json.dumps(placement))
            with self.assertRaises(ValueError): placement_file(path)
