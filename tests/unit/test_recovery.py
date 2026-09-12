# SPDX-License-Identifier: AGPL-3.0-only
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from cold_restore import validate_archive
from kube_backup import verify
from kube_restore import file_inventory


class RecoveryChecks(unittest.TestCase):
    def test_restore_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.tar"
            with tarfile.open(path, "w") as archive:
                entry = tarfile.TarInfo("../outside")
                archive.addfile(entry, io.BytesIO())
            with self.assertRaises(SystemExit):
                validate_archive(path)

    def test_restore_refuses_symlink_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.tar"
            with tarfile.open(path, "w") as archive:
                entry = tarfile.TarInfo("link")
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/etc"
                archive.addfile(entry)
            with self.assertRaises(SystemExit):
                validate_archive(path)

    def test_changed_backup_cannot_pass_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "database.dump").write_bytes(b"modified")
            (root / "manifest.json").write_text(json.dumps({
                "format": "kubernetes-application-state-v1",
                "files": {"database.dump": {"kind": "database", "sha256": "0" * 64}}
            }))
            with self.assertRaises(ValueError):
                verify(root)

    def test_byte_identical_restore_must_preserve_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / "source.tar", Path(folder) / "destination.tar"]
            for path, uid in zip(paths, [1000, 0]):
                with tarfile.open(path, "w") as archive:
                    entry = tarfile.TarInfo("private-state")
                    entry.size = 4
                    entry.mode = 0o600
                    entry.uid = uid
                    archive.addfile(entry, io.BytesIO(b"data"))
            self.assertNotEqual(file_inventory(paths[0]), file_inventory(paths[1]))


if __name__ == "__main__":
    unittest.main()
