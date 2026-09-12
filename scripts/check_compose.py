#!/usr/bin/env python3
"""Validate a fresh synthetic Compose configuration without starting services."""
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import shutil
import subprocess
import tempfile
from build import RECIPES

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="platform-compose-check-") as directory:
    root = Path(directory)
    for name in ("scripts", "compose", "examples", "tests"):
        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    for command in (["configure.py", "--project", "platform-static"], ["fixtures.py"], ["gateway_configure.py", "--fixtures"]):
        subprocess.run(["python3", str(root / "scripts" / command[0]), *command[1:]], cwd=root, check=True, stdout=subprocess.DEVNULL)
    # These are intentionally invalid registry hosts, used only for config parsing.
    (root / ".local/images.env").write_text("".join(key + "=registry.example.invalid/static:render-only\n"
        for key in sorted(set(RECIPES) | {"WEB_IMAGE", "SPARKROUTE_IMAGE"})))
    subprocess.run(["python3", str(root / "scripts/compose.py"), "config", "--quiet"], cwd=root, check=True)
print("Fresh Compose configuration parsed; no containers started")
