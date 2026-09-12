#!/usr/bin/env python3
"""Temporarily shorten fixture auth session TTL, test expiry, and restore the workload."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import yaml
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from compose import command


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["compose", "kind"], required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--expect-project")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    args = parser.parse_args()
    if not (ROOT / ".local/fixtures.enabled").is_file():
        parser.error("This check only runs with the local fixture profile enabled")
    environment = dict(os.environ, PLATFORM_ORIGIN=args.origin, PLATFORM_TENANT="alpha",
                       FIXTURE_SESSION_EXPIRY="1")
    if args.profile == "compose":
        environment.pop("PLATFORM_PROFILE", None)
        config = json.loads(subprocess.check_output(command("config", "--format", "json"), cwd=ROOT))
        if not args.expect_project or config["name"] != args.expect_project:
            parser.error("Explicit Compose project does not match this installation")
        override = ROOT / ".local/session-expiry-override.yaml"
        override.write_text(yaml.safe_dump({"services": {"auth": {"environment": {"AUTH_PROXY_SESSION_TTL": "3s"}}}}))
        def install(short):
            run(command() + (["-f", str(override)] if short else []) + ["up", "-d", "--no-deps", "auth"])
            deadline = time.monotonic() + 90
            while True:
                raw = subprocess.check_output(command("ps", "--format", "json", "auth"), cwd=ROOT, text=True).strip()
                rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
                if rows and all(row["State"] == "running" and row.get("Health") in ("", None, "healthy") for row in rows):
                    try:
                        urlopen(args.origin + "/api/auth/checkz", timeout=2).close()
                    except HTTPError as error:
                        if error.code == 401:
                            return  # Real auth listener is ready and refuses anonymous callers.
                    except (URLError, TimeoutError):
                        pass
                if time.monotonic() > deadline:
                    raise RuntimeError("Fixture auth did not become healthy")
                time.sleep(1)
    else:
        environment["PLATFORM_PROFILE"] = "kind"
        if not args.kubeconfig or args.context != "kind-platform-integration":
            parser.error("Explicit disposable kubeconfig and kind-platform-integration context are required")
        kube = ["kubectl", "--kubeconfig", str(args.kubeconfig.resolve()), "--context", args.context, "-n", "platform-shared"]
        deployment = json.loads(subprocess.check_output(kube + ["get", "deployment/shared-auth", "-o", "json"]))
        containers = deployment["spec"]["template"]["spec"]["containers"]
        if len(containers) != 1 or any(entry["name"] == "AUTH_PROXY_SESSION_TTL" for entry in containers[0].get("env", [])):
            parser.error("Fixture TTL check requires the baseline auth container without a TTL override")
        def install(short):
            run(kube + ["set", "env", "deployment/shared-auth", "AUTH_PROXY_SESSION_TTL=3s" if short else "AUTH_PROXY_SESSION_TTL-"])
            run(kube + ["rollout", "status", "deployment/shared-auth", "--timeout=90s"])
    try:
        install(True)
        run(["npx", "playwright", "test", "--grep", "short-lived fixture session expires"], env=environment)
    finally:
        install(False)
    print("Real cookie session expiry passed; baseline auth workload restored")


if __name__ == "__main__":
    main()
