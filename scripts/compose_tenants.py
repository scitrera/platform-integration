"""Tenant definitions and Compose rendering for an installation."""
# SPDX-License-Identifier: AGPL-3.0-only
import copy
import json
from pathlib import Path
import re

import yaml


TENANT_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:alpha|ALPHA)(?![A-Za-z0-9])")
OTHER_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:beta|BETA)(?![A-Za-z0-9])")


def validate_tenants(tenants):
    if not isinstance(tenants, list) or not tenants:
        raise ValueError("Supply a nonempty list of tenant definitions")
    seen = set()
    for tenant in tenants:
        if not isinstance(tenant, dict):
            raise ValueError("Each tenant must be an object")
        slug = tenant.get("slug", "")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", slug):
            raise ValueError("Invalid tenant slug")
        if slug in seen or slug in {"gateway", "sandbox-state"}:
            raise ValueError("Duplicate or reserved tenant slug: " + slug)
        seen.add(slug)
        for key in ("name", "email", "workspace"):
            if not isinstance(tenant.get(key), str) or not tenant[key].strip():
                raise ValueError("Tenant requires " + key)
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", tenant["email"]):
            raise ValueError("Invalid tenant administrator email")
    return tenants


def load_tenants(root):
    path = root / ".local/tenants.json"
    if not path.exists():
        path = root / "examples/compose/tenants.json"
    return validate_tenants(json.loads(path.read_text()))


def select_tenants(root, source=None):
    """Persist the installation identity; changing it requires a fresh directory."""
    tenants = validate_tenants(json.loads(Path(source).read_text())) if source else load_tenants(root)
    existing = load_tenants(root)
    if (root / ".local/compose.env").exists() or (root / ".local/tenants.json").exists():
        if tenants != existing:
            raise ValueError("Tenant definitions differ from this installation; use a fresh integration directory")
    local = root / ".local"
    local.mkdir(exist_ok=True)
    (local / "tenants.json").write_text(json.dumps(tenants, indent=2) + "\n")
    return tenants


def rename_template(value, tenant):
    if isinstance(value, str):
        value = TENANT_TOKEN.sub(lambda match: tenant["slug"].upper().replace("-", "_")
                                 if match[0] == "ALPHA" else tenant["slug"], value)
        return value.replace("alice@example.test", tenant["email"])
    if isinstance(value, list):
        return [rename_template(item, tenant) for item in value]
    if isinstance(value, dict):
        return {rename_template(key, tenant): rename_template(item, tenant) for key, item in value.items()}
    return value


def render_compose(document, tenants):
    """Expand the maintained alpha service template and shared tenant references.

    The checked-in alpha/beta Compose example remains the canonical template.
    Contract tests require rendering those definitions to reproduce it exactly.
    """
    validate_tenants(tenants)
    document = copy.deepcopy(document)
    # This shared shell command contains all tenant buckets in one string.
    # Render it explicitly rather than treating it as a per-tenant list item.
    object_init = document.get("services", {}).get("objects-init")
    bucket_command = object_init.pop("command") if object_init else None

    def expand(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if OTHER_TOKEN.search(key):
                    continue
                if TENANT_TOKEN.search(key):
                    for tenant in tenants:
                        rendered_key = rename_template(key, tenant)
                        if rendered_key in result:
                            raise ValueError("Tenant service or resource collision: " + rendered_key)
                        result[rendered_key] = rename_template(item, tenant)
                else:
                    if key in result:
                        raise ValueError("Tenant service or resource collision: " + key)
                    result[key] = expand(item)
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, str) and OTHER_TOKEN.search(item):
                    continue
                if isinstance(item, str) and TENANT_TOKEN.search(item):
                    result.extend(rename_template(item, tenant) for tenant in tenants)
                else:
                    result.append(expand(item))
            return result
        if isinstance(value, str) and (TENANT_TOKEN.search(value) or OTHER_TOKEN.search(value)):
            raise ValueError("Unrecognized shared tenant reference in Compose template")
        return value

    result = expand(document)
    if bucket_command is not None:
        marker = "local/tenant-alpha local/tenant-beta"
        if len(bucket_command) != 1 or bucket_command[0].count(marker) != 1:
            raise ValueError("Unrecognized object bucket initialization template")
        buckets = " ".join("local/tenant-" + tenant["slug"] for tenant in tenants)
        result["services"]["objects-init"]["command"] = [bucket_command[0].replace(marker, buckets)]
    return result


def write_compose(root, tenants):
    for template, name in [("compose/compose.yaml", "compose.yaml"),
                           ("compose/profiles/fixtures.yaml", "fixtures.yaml")]:
        document = yaml.safe_load((root / template).read_text())
        (root / ".local" / name).write_text(yaml.safe_dump(render_compose(document, tenants), sort_keys=False))
