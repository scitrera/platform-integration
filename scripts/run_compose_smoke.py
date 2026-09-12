#!/usr/bin/env python3
"""Run fixture acceptance from a fresh directory with operator-supplied local images."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True, help="Private images.json created by the reviewed builder")
    parser.add_argument("--directory", type=Path, required=True, help="An unused directory for this installation")
    parser.add_argument("--base-port", type=int, default=18100)
    args = parser.parse_args()
    if not 1024 <= args.base_port <= 65525:
        parser.error("Base port and its +2/+10 offsets must be unprivileged valid ports")
    records = json.loads(args.images.read_text())
    for name, record in records.items():
        actual = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", record["tag"]], text=True).strip()
        if actual != record["id"]:
            parser.error("Local image bytes differ from the selected record: " + name)
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ("scripts", "compose", "examples", "tests", "charts", "docs", "patches", "LICENSES", "images"):
        shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
    for name in ("package.json", "package-lock.json", "playwright.config.ts", "requirements-dev.txt", "LICENSE", "NOTICE", "README.md", "versions.yaml", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, root / name)
    local = root / ".local"
    local.mkdir(mode=0o700)
    (local / "images.json").write_text(json.dumps(records, indent=2) + "\n")
    (local / "images.env").write_text("".join(
        key + "=" + (record["tag"] if key in {"SAHARA_IMAGE", "CODE_IMAGE", "SIDECAR_IMAGE"} else record["id"]) + "\n"
        for key, record in sorted(records.items())))
    project = "platform-smoke-" + secrets.token_hex(5)
    step = 0
    def run(command, **kwargs):
        nonlocal step
        step += 1
        log = local / ("smoke-step-%02d.log" % step)
        with log.open("w") as output:
            subprocess.run(command, cwd=root, check=True, stdout=output, stderr=subprocess.STDOUT, **kwargs)
        print("Completed smoke step", step, flush=True)
    run(["python3", "scripts/configure.py", "--project", project, "--web-port", str(args.base_port),
         "--admin-port", str(args.base_port + 2), "--fixture-idp-port", str(args.base_port + 10)])
    run(["python3", "scripts/dev.py", "up", "--fixtures"])
    run(["npm", "ci"])
    run(["npx", "playwright", "install", "chromium"])
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("PLATFORM_", "PERSISTENCE_", "FIXTURE_IDP_", "AUTH_OPERATOR", "WORKER_", "GATEWAY_USAGE_", "FIXTURE_SESSION_"))}
    environment["PLATFORM_ORIGIN"] = "http://127.0.0.1:" + str(args.base_port)
    environment["FIXTURE_IDP_ORIGIN"] = "http://127.0.0.1:" + str(args.base_port + 10)
    environment["AUTH_OPERATOR_ORIGIN"] = "http://127.0.0.1:" + str(args.base_port + 2)
    environment["AUTH_OPERATORS_FILE"] = str(local / "operators.json")
    environment["PERSISTENCE_RECORD"] = str(local / "persistence.json")
    environment["GATEWAY_USAGE_RECORD"] = str(local / "gateway-usage.json")
    run(["npm", "run", "test:browser"], env=environment)
    run(["python3", "scripts/check_gateway.py", "--profile", "compose", "--project", project,
         "--usage-only", "--usage-record", str(local / "gateway-usage.json")])
    run(["python3", "scripts/dev.py", "up", "--fixtures"])
    environment.pop("PERSISTENCE_RECORD")
    environment["PERSISTENCE_CHECK"] = str(local / "persistence.json")
    run(["npm", "run", "test:browser", "--", "--grep", "previously recorded"], env=environment)
    print("Fresh installation and repeated-bootstrap persistence passed:", root)
    print("Retained this explicitly named installation for review:", project)
    print("Release its allocations through the public SDK, then use dev.py down; see docs/operations.md.")


if __name__ == "__main__":
    main()
