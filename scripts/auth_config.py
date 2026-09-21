#!/usr/bin/env python3
"""Check, preview, or reconcile one tenant through auth-go's operator API."""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError

from auth_operator import Operator, validate_origin

ROOT = Path(__file__).resolve().parents[1]
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
CLAIM = re.compile(r"[a-zA-Z][a-zA-Z0-9_.:/-]{0,127}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key: " + key)
        result[key] = value
    return result


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Expected fields: " + ", ".join(required) + "; optional: " + ", ".join(optional))


def domain(value):
    if not isinstance(value, str):
        raise ValueError("Domains must be strings")
    value = value.strip().lower().removesuffix(".").encode("idna").decode("ascii")
    if len(value) > 253 or "." not in value or not all(LABEL.fullmatch(p) for p in value.split(".")):
        raise ValueError("Domains must be exact DNS names without wildcards, commas, or URL components")
    return value


def validate_config(config):
    config = deepcopy(config)
    fields(config, ("version", "tenant", "domains", "auth"))
    if type(config["version"]) is not int or config["version"] != 1:
        raise ValueError("Unsupported auth configuration version")
    tenant = config["tenant"]
    fields(tenant, ("slug", "name", "enabled"), ("metadata",))
    if not isinstance(tenant["slug"], str) or not LABEL.fullmatch(tenant["slug"]):
        raise ValueError("Tenant slug must be a lowercase DNS label")
    if (not isinstance(tenant["name"], str) or not tenant["name"].strip()
            or len(tenant["name"]) > 200 or any(ord(c) < 32 for c in tenant["name"])):
        raise ValueError("Tenant name must be a nonempty string of at most 200 characters")
    tenant["name"] = tenant["name"].strip()
    if type(tenant["enabled"]) is not bool:
        raise ValueError("Tenant enabled must be a boolean")
    metadata = tenant.get("metadata", {})
    fields(metadata, (), ("logo", "default_workspace"))
    for key, value in metadata.items():
        if value is not None and (not isinstance(value, str) or len(value) > 2048):
            raise ValueError("Tenant metadata must contain strings or null")
        if key == "logo" and value and not value.startswith("https://"):
            raise ValueError("Tenant logo must use HTTPS")
    domains = config["domains"]
    if not isinstance(domains, list):
        raise ValueError("domains must be a list")
    config["domains"] = sorted(domain(d) for d in domains)
    if len(set(config["domains"])) != len(domains):
        raise ValueError("Duplicate normalized domain")
    policy = config["auth"]
    fields(policy, ("auto_add", "providers", "checks"))
    if type(policy["auto_add"]) is not bool:
        raise ValueError("auto_add must be a boolean")
    providers = policy["providers"]
    if (not isinstance(providers, list) or not providers or len(providers) > 64
            or any(not isinstance(p, str) or not PROVIDER.fullmatch(p) for p in providers)
            or len(set(providers)) != len(providers)):
        raise ValueError("Use a nonempty, unique provider allowlist; empty would permit all providers")
    policy["providers"] = sorted(providers)
    if policy["auto_add"] and not domains:
        raise ValueError("Auto-add requires at least one associated email domain")
    if not isinstance(policy["checks"], dict):
        raise ValueError("checks must be a provider map")
    for provider, checks in policy["checks"].items():
        if not PROVIDER.fullmatch(provider) or (checks is not None and not isinstance(checks, dict)):
            raise ValueError("Invalid provider check map")
        for claim, value in (checks or {}).items():
            if not CLAIM.fullmatch(claim):
                raise ValueError("Invalid claim name")
            values = value if isinstance(value, list) else [value]
            if any(not isinstance(v, str) or (not v and (provider, claim) != ("google", "hd")) for v in values):
                raise ValueError("Claim checks must contain strings; only Google hd permits a blank option")
            if (provider, claim) == ("google", "hd"):
                normalized = [domain(v) if v.strip() else "" for v in values]
                checks[claim] = normalized if isinstance(value, list) else normalized[0]
    return config


def load_config(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("Auth configuration exceeds 64 KiB")
    return validate_config(json.loads(raw, object_pairs_hook=unique_object))


def read_snapshot(operator, slug):
    status = operator.read("/status")
    revision = status["revision"]
    path = "/tenants/" + slug
    result = {"revision": revision, "tenant": None, "domains": [],
              "auth": {"auto_add": False, "providers": [], "checks": {}}}
    try:
        for key, endpoint in (("tenant", path), ("domains", path + "/domains"), ("auth", path + "/auth")):
            response = operator.read(endpoint)
            if response["revision"] != revision:
                raise ValueError("Auth configuration changed while reading; inspect and rerun")
            result[key] = response["data"]
    except HTTPError as error:
        if error.code != 404 or result["tenant"] is not None:
            raise
    if operator.read("/status")["revision"] != revision:
        raise ValueError("Auth configuration changed while reading; inspect and rerun")
    if type(result["auth"].get("auto_add")) is not bool:
        raise ValueError("Selected auth-go must support explicit auto_add policies")
    return result


def plan_changes(config, snapshot):
    """Plan exact domain membership and the explicitly managed tenant/policy fields."""
    desired = config["tenant"]
    path = "/tenants/" + desired["slug"]
    current = deepcopy(snapshot["tenant"])
    policy = deepcopy(snapshot["auth"])
    policy["providers"] = sorted(policy["providers"])
    domains = set(snapshot["domains"])
    wanted_domains = set(config["domains"])
    target_policy = deepcopy(policy)
    target_policy.update(auto_add=config["auth"]["auto_add"], providers=config["auth"]["providers"])
    for provider, checks in config["auth"]["checks"].items():
        if checks:
            target_policy["checks"][provider] = checks
        else:
            target_policy["checks"].pop(provider, None)
    # Explicit nulls remove provider checks under the native API's patch semantics.
    policy_body = deepcopy(target_policy)
    for provider in policy["checks"].keys() - target_policy["checks"].keys():
        policy_body["checks"][provider] = None
    changes = []

    def add(method, endpoint, data):
        changes.append({"method": method, "path": endpoint, "data": deepcopy(data)})

    edit = {k: v for k, v in desired.items() if k != "slug"}
    if current is None:
        # A partially created tenant stays disabled until all configuration succeeds.
        add("POST", "/tenants", dict(desired, enabled=False))
        current = dict(desired, enabled=False)
    elif current["enabled"] and not desired["enabled"]:
        add("PUT", path, edit)
        current = dict(current, **edit)

    if domains != wanted_domains or policy != target_policy:
        interim = dict(policy_body, auto_add=False)
        if policy != dict(target_policy, auto_add=False):
            add("PUT", path + "/auth", interim)
        for old in sorted(domains - wanted_domains):
            add("DELETE", path + "/domains/" + old, None)
        for new in sorted(wanted_domains - domains):
            add("POST", path + "/domains", {"domain": new})
        if target_policy["auto_add"]:
            add("PUT", path + "/auth", policy_body)

    metadata = current.get("metadata") or {}
    if (current["name"] != desired["name"] or current["enabled"] != desired["enabled"]
            or any(metadata.get(k) != v for k, v in desired.get("metadata", {}).items())):
        add("PUT", path, edit)
    return changes


def apply_config(operator, config):
    snapshot = read_snapshot(operator, config["tenant"]["slug"])
    changes = plan_changes(config, snapshot)
    revision = snapshot["revision"]
    for change in changes:
        # Carry the reviewed revision forward; never refresh it to bypass a conflict.
        response = operator.call(change["method"], change["path"], change["data"], revision=revision)
        revision = response["revision"]
    final = read_snapshot(operator, config["tenant"]["slug"])
    if final["revision"] != revision or plan_changes(config, final):
        raise ValueError("Auth configuration changed during verification; inspect and rerun")
    return changes


def local_origin(root):
    env = dict(line.split("=", 1) for line in (root / ".local/compose.env").read_text().splitlines()
               if line and not line.startswith("#"))
    return "http://127.0.0.1:" + env["AUTH_ADMIN_PORT"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "plan", "apply"])
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--origin", help="Private operator origin; defaults to this installation's loopback listener")
    parser.add_argument("--connect-to", help="Optional HTTP loopback tunnel; --origin remains the configured HTTPS admin origin")
    parser.add_argument("--operators", type=Path, help="Auth-go bootstrap token file; defaults to .local/operators.json")
    parser.add_argument("--operator", default="operator")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.file)
        slug = config["tenant"]["slug"]
        if args.action == "check":
            print("Valid auth-go configuration for tenant " + slug + "; no connections or changes")
            return
        origin = validate_origin(args.origin or local_origin(ROOT))
        if args.action == "apply" and (ROOT / ".local/fixtures.enabled").exists():
            if origin == validate_origin(local_origin(ROOT)):
                raise ValueError("This installation uses fixture login; apply to the deployed auth-go origin instead")
        tokens = args.operators or ROOT / ".local/operators.json"
        token = json.loads(tokens.read_text())["operators"][args.operator]
        operator = Operator(origin, token, operator=args.operator, connect_to=args.connect_to)
        try:
            if args.action == "plan":
                snapshot = read_snapshot(operator, slug)
                print(json.dumps({"tenant": slug, "revision": snapshot["revision"],
                                  "changes": plan_changes(config, snapshot)}, indent=2))
            else:
                changes = apply_config(operator, config)
                print(f"Verified auth-go configuration for tenant {slug}; {len(changes)} changes")
        finally:
            operator.call("DELETE", "/session")
    except HTTPError as error:
        parser.exit(1, f"Auth-go returned HTTP {error.code}; inspect the current tenant configuration before rerunning.\n")
    except (ValueError, OSError, KeyError, URLError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
