#!/usr/bin/env python3
"""Install the pinned Linux-amd64 Helm binary into private CI tooling."""
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import json
import os
from pathlib import Path
import tarfile
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
artifact = json.loads((root / "tests/fixtures/ci-tools.lock.json").read_text())["helm"]
target = root / ".local/ci-tools"
target.mkdir(parents=True, exist_ok=True)
with urlopen(artifact["url"], timeout=120) as response:
    data = response.read()
if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
    raise SystemExit("Helm artifact checksum mismatch")
archive = target / "helm.tar.gz"
archive.write_bytes(data)
with tarfile.open(archive) as bundle:
    binary = bundle.extractfile("linux-amd64/helm").read()
(target / "helm").write_bytes(binary)
(target / "helm").chmod(0o755)
if os.environ.get("GITHUB_PATH"):
    with open(os.environ["GITHUB_PATH"], "a") as output:
        output.write(str(target) + "\n")
print("Installed pinned Helm in .local/ci-tools")
