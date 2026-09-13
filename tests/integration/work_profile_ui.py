#!/usr/bin/env python3
"""Configure the profile chooser only in an explicitly named fixture installation."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from compose import command

PROGRAM = """
import asyncio, os
from x_acl_seed import _build_ti2
async def main():
    client, ti = await _build_ti2(os.environ["AETHER_GATEWAY"], "alpha")
    try:
        result = await ti.set_ui_config_variables(enableWorkProfileSelection=ENABLED)
        assert result.get("enableWorkProfileSelection") is ENABLED
    finally:
        await client.close()
asyncio.run(asyncio.wait_for(main(), 30))
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--enabled", choices=["true", "false"], required=True)
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (ROOT / ".local/compose.env").read_text().splitlines() if "=" in line)
    if not (ROOT / ".local/fixtures.enabled").is_file() or not args.project.startswith("platform-smoke-") or env.get("COMPOSE_PROJECT_NAME") != args.project:
        parser.error("Requires the exact disposable fixture Compose project")
    program = "ENABLED = " + repr(args.enabled == "true") + "\n" + PROGRAM
    result = subprocess.run(command("run", "--rm", "--no-deps", "-T",
        "-v", str(ROOT / ".local/alpha/tls/management") + ":/run/management:ro",
        "-e", "AETHER_MGMT_TLS_CA_CERT=/run/management/ca.crt",
        "-e", "AETHER_MGMT_TLS_CLIENT_CERT=/run/management/tls.crt",
        "-e", "AETHER_MGMT_TLS_CLIENT_KEY=/run/management/tls.key",
        "--entrypoint", "python", "acl-alpha", "-c", program), capture_output=True, text=True, timeout=60)
    if result.returncode:
        log = ROOT / ".local/work-profile-ui.log"
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
        raise RuntimeError("Fixture UI configuration failed; inspect .local/work-profile-ui.log")
    print("Tenant alpha enableWorkProfileSelection =", args.enabled)

if __name__ == "__main__":
    main()
