#!/usr/bin/env python3
"""Bounded fixture enrollment and session revocation through auth-go's operator API."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from auth_setup import Operator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["grant-member", "remove-member", "revoke-sessions"])
    parser.add_argument("--origin", required=True)
    parser.add_argument("--operators", type=Path, required=True)
    parser.add_argument("--tenant", choices=["alpha", "beta"], required=True)
    parser.add_argument("--email", choices=["alice@example.test", "bob@example.test", "denied@example.test"], required=True)
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != "http" or origin.hostname not in ("localhost", "127.0.0.1") or origin.path or origin.query or origin.fragment:
        parser.error("Fixture operator actions require an explicit loopback operator origin")
    if not (ROOT / ".local/fixtures.enabled").is_file():
        parser.error("Fixture profile is not enabled in this installation")
    if args.action != "revoke-sessions" and args.email != "denied@example.test":
        parser.error("Membership scenarios only own denied@example.test")
    token = json.loads(args.operators.read_text())["operators"]["operator"]
    operator = Operator(args.origin, token)
    try:
        def find_user():
            users = operator.read("/users?q=" + quote(args.email))["data"]
            if isinstance(users, dict):
                users = users.get("users", users.get("items", []))
            return next((user for user in users if user["email"] == args.email), None)
        user = find_user()
        if user is None and args.action == "grant-member":
            operator.write("POST", "/users", {"email": args.email, "name": "Fixture member", "enabled": True})
            user = find_user()
        if user is None:
            raise RuntimeError("Expected synthetic fixture user is missing")
        path = "/users/" + user["id"]
        if args.action == "grant-member":
            memberships = operator.read(path + "/memberships")["data"]
            if args.tenant not in memberships:
                operator.write("POST", path + "/memberships", {"tenant_slug": args.tenant})
        elif args.action == "remove-member":
            operator.write("DELETE", path + "/memberships/" + args.tenant, None)
            operator.call("DELETE", path + "/sessions")
        else:
            operator.call("DELETE", path + "/sessions")
        print("Completed synthetic fixture operator action:", args.action)
    finally:
        operator.call("DELETE", "/session")


if __name__ == "__main__":
    main()
