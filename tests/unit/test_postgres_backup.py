# SPDX-License-Identifier: AGPL-3.0-only
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from postgres_backup import backup, due


class BackupTests(unittest.TestCase):
    def test_schedule(self):
        self.assertTrue(due("15 3 * * *", datetime(2026, 9, 19, 3, 15, tzinfo=timezone.utc)))
        self.assertFalse(due("15 3 * * *", datetime(2026, 9, 19, 3, 16, tzinfo=timezone.utc)))
        self.assertTrue(due("*/15 3-5 * * *", datetime(2026, 9, 19, 4, 30, tzinfo=timezone.utc)))
        with self.assertRaises(ValueError):
            due("99 3 * * *", datetime.now(timezone.utc))

    def test_failure_preserves_last_good_backup_and_does_not_expire(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "last-success.json").write_text('{"snapshot":"previous"}')
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                if command[0] == "pg_dump":
                    raise RuntimeError("database unavailable")
                return b""
            with self.assertRaises(RuntimeError):
                backup(["memorylayer"], server="example", digest="a"*64, state=state, runner=runner)
            self.assertEqual(json.loads((state / "last-success.json").read_text())["snapshot"], "previous")
            self.assertFalse(any("forget" in call for call in calls))

    def test_success_requires_remote_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            captured = {}
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                if "--file" in command:
                    Path(command[command.index("--file")+1]).write_bytes(b"synthetic fixture")
                if command[:2] == ["restic", "backup"]:
                    captured["manifest"] = (Path(kwargs["cwd"]) / "manifest.json").read_bytes()
                    return b'{"message_type":"summary","snapshot_id":"synthetic"}\n'
                if command[:2] == ["restic", "dump"]:
                    return captured["manifest"]
                return b""
            receipt = backup(["memorylayer", "storage"], server="example", digest="a"*64, state=state, runner=runner)
            self.assertEqual(receipt["snapshot"], "synthetic")
            self.assertEqual(set(receipt["files"]), {"roles.sql", "memorylayer.dump", "storage.dump"})
            self.assertIn("forget", calls[-1])
