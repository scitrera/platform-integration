#!/usr/bin/env python3
"""Explicitly edit claim overrides on an existing auth-go user–tenant membership."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from email.utils import parseaddr
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from auth_config import (ROOT, LABEL, domain, fields, local_origin, unique_object,
                         validate_provider_checks)
from auth_operator import Operator, validate_origin


def load_config(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("Membership configuration exceeds 64 KiB")
    config = json.loads(raw, object_pairs_hook=unique_object)
    fields(config, ("version", "tenant", "email", "checks"))
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Unsupported membership configuration version")
    if not isinstance(config["tenant"], str) or not LABEL.fullmatch(config["tenant"]):
        raise ValueError("Tenant must be a lowercase DNS label")
    address = config["email"]
    if not isinstance(address, str):
        raise ValueError("Email must be an exact address")
    address = address.strip().lower()
    name, parsed = parseaddr(address)
    if (name or parsed != address or address.count("@") != 1 or len(address) > 254
            or any(c.isspace() or c in "*?[]" for c in address)):
        raise ValueError("Email must be an exact address")
    local, host = address.rsplit("@", 1)
    config["email"] = local + "@" + domain(host)
    config["checks"] = validate_provider_checks(config["checks"])
    if any(not checks for checks in config["checks"].values()):
        raise ValueError("Provider overrides require claims; omit the provider to inherit its rules")
    return config


def read_snapshot(operator, config):
    revision = operator.read("/status")["revision"]
    users = operator.read("/users?q=" + quote(config["email"], safe=""))
    if users["revision"] != revision:
        raise ValueError("Registry changed while reading; inspect and rerun")
    matches = [user for user in users["data"] if user["email"] == config["email"]]
    if len(matches) != 1:
        raise ValueError("Create the exact user and membership explicitly before setting overrides")
    user = matches[0]
    if config["tenant"] not in user["memberships"]:
        raise ValueError("Existing membership required; this operation never enrolls users")
    path = "/users/" + user["id"] + "/memberships/" + config["tenant"] + "/auth"
    response = operator.read(path)
    if response["revision"] != revision or operator.read("/status")["revision"] != revision:
        raise ValueError("Registry changed while reading; inspect and rerun")
    return {"revision": revision, "path": path, "checks": response["data"]["checks"]}


def apply_config(operator, config):
    snapshot = read_snapshot(operator, config)
    if snapshot["checks"] == config["checks"]:
        return False
    written = operator.call("PUT", snapshot["path"], {"checks": config["checks"]}, revision=snapshot["revision"])
    final = read_snapshot(operator, config)
    if final["revision"] != written["revision"] or final["checks"] != config["checks"]:
        raise ValueError("Membership changed during verification; inspect and rerun")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "plan", "apply"])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--origin")
    parser.add_argument("--connect-to")
    parser.add_argument("--operators", type=Path)
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.file)
        if args.action == "check":
            print("Valid explicit membership configuration; no connections or changes")
            return
        origin = validate_origin(args.origin or local_origin(ROOT))
        credentials = json.loads((args.operators or ROOT / ".local/operators.json").read_text())
        operator = Operator(origin, credentials["operators"][args.operator], operator=args.operator, connect_to=args.connect_to)
        try:
            if args.action == "plan":
                current = read_snapshot(operator, config)
                print(json.dumps({"tenant": config["tenant"], "email": config["email"],
                    "revision": current["revision"], "changed": current["checks"] != config["checks"],
                    "checks": config["checks"]}, indent=2))
            else:
                changed = apply_config(operator, config)
                print("Verified explicit membership claim overrides; " + ("updated" if changed else "unchanged"))
        finally:
            operator.call("DELETE", "/session")
    except HTTPError as error:
        parser.exit(1, f"Auth-go returned HTTP {error.code}; inspect the membership and auth-go version before retrying.\n")
    except (ValueError, OSError, KeyError, URLError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
