#!/usr/bin/env python3
"""Grant and remove bounded worker-fixture permissions in disposable Compose."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from compose import command

PROGRAM = """
import asyncio, os
from x_acl_seed import _build_admin_client
async def main():
    connection, admin = await _build_admin_client(os.environ["AETHER_GATEWAY"])
    try:
        for resource_type, resource_id in [("workspace", "default"), ("service_impl", "platform-bridge"), ("service_impl", "tool-catalog")]:
            key = dict(principal_type="user", principal_id="denied@example.test", resource_type=resource_type, resource_id=resource_id)
            response = await admin.list_acl_rules(**key)
            assert response and response.success
            for rule in response.rules:
                assert rule.reason == "Disposable work-profile acceptance", "Refusing to change an existing operator grant"
            if ACTION == "grant":
                response = await admin.create_acl_rule(**key, access_level=20, granted_by="work-profile-fixture", reason="Disposable work-profile acceptance")
                assert response and response.success
            else:
                for rule in response.rules:
                    response = await admin.delete_acl_rule(rule.rule_id)
                    assert response and response.success
    finally:
        await connection.close()
asyncio.run(asyncio.wait_for(main(), 30))
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["grant", "revoke"])
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (ROOT / ".local/compose.env").read_text().splitlines() if "=" in line)
    if not (ROOT / ".local/fixtures.enabled").is_file() or not args.project.startswith("platform-smoke-") or env.get("COMPOSE_PROJECT_NAME") != args.project:
        parser.error("Requires the exact disposable fixture Compose project")
    program = "ACTION = " + repr(args.action) + "\n" + PROGRAM
    result = subprocess.run(command("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "acl-alpha", "-c", program), capture_output=True, text=True, timeout=60)
    if result.returncode:
        log = ROOT / ".local/work-profile-acl.log"
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        raise RuntimeError("Fixture workspace grant failed; inspect .local/work-profile-acl.log")
    print("Completed bounded work-profile fixture permissions:", args.action)

if __name__ == "__main__":
    main()
