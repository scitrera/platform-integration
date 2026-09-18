#!/usr/bin/env python3
"""Stage or apply a tenant's YAML model routes to a Compose installation."""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ModelConfigError(ValueError):
    """An operator input failed validation; messages must never include secrets."""


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ModelConfigError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def fields(value, allowed, where):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ModelConfigError("Invalid or unknown fields in " + where)


def token(value, where):
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ModelConfigError("Invalid name in " + where)
    return value


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


@contextmanager
def installation_lock(root):
    local = root / ".local"
    local.mkdir(exist_ok=True, mode=0o700)
    fd = os.open(local / "models.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ModelConfigError("Another model configuration operation is running") from None
        yield


def plan_path(root, tenant):
    token(tenant, "tenant")
    return root / ".local/models" / (tenant + ".json")


def managed(root):
    """Fixture setup may initialize missing routes, but never reset YAML policies."""
    return (any(read_json(path).get("records") for path in (root / ".local/models").glob("*.json"))
            or any((root / ".local/gateway").glob("models-*-secrets.json")))


def compile_config(path, tenant, environ=None, previous=None):
    token(tenant, "tenant")
    environ = os.environ if environ is None else environ
    previous = previous or {}
    try:
        doc = yaml.load(path.read_text(), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ModelConfigError("Invalid model YAML; check indentation and syntax") from None
    fields(doc, {"version", "models", "routes"}, "model YAML")
    if type(doc.get("version")) is not int or doc["version"] != 1:
        raise ModelConfigError("Model YAML requires version: 1")
    models, routes = doc.get("models"), doc.get("routes")
    models = {} if models is None else models
    routes = {} if routes is None else routes
    if not isinstance(models, dict) or not isinstance(routes, dict):
        raise ModelConfigError("models and routes must be mappings")
    credentials = copy.deepcopy(previous.get("credentials", {}))
    sources = copy.deepcopy(previous.get("credential_sources", {}))
    definitions, records = {}, {}
    credential_ref = "file:///run/gateway/__MODEL_SECRETS__#"

    def secret(model_name, entry, field):
        name = entry[field]
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            raise ModelConfigError("Credential fields must name environment variables")
        key = model_name + "-" + field
        value = environ.get(name)
        if value is None and previous.get("credential_sources", {}).get(key) == name:
            value = previous.get("credentials", {}).get(key)
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ModelConfigError("Set the environment variable named by " + model_name + "." + field)
        sources[key], credentials[key] = name, value
        return credential_ref + key

    for name, entry in models.items():
        token(name, "models")
        fields(entry, {"provider", "base_url", "model", "api_key_env", "modal_key_env",
                       "modal_secret_env", "max_concurrency", "capabilities", "allow_http"}, "model " + name)
        kind = entry.get("provider", "openai_compatible")
        if kind not in {"openai_compatible", "gemini"}:
            raise ModelConfigError("provider must be openai_compatible or gemini")
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
            raise ModelConfigError("Model " + name + " needs an absolute base_url")
        try:
            url = urlsplit(base_url)
            valid_port = url.port is None or 1 <= url.port <= 65535
        except ValueError:
            raise ModelConfigError("Invalid provider URL for " + name) from None
        allow_http = entry.get("allow_http", False)
        if type(allow_http) is not bool:
            raise ModelConfigError("allow_http must be boolean")
        if (url.scheme not in ({"https", "http"} if allow_http else {"https"}) or not url.hostname
                or url.username is not None or url.password is not None or url.query or url.fragment
                or not valid_port or not re.fullmatch(r"[A-Za-z0-9.-]+", url.hostname)):
            raise ModelConfigError("Use an HTTPS base_url without userinfo, query or fragment; HTTP requires allow_http: true")
        served = entry.get("model")
        if not isinstance(served, str) or not served.strip() or any(c in served for c in "\r\n"):
            raise ModelConfigError("Model " + name + " needs the exact served model ID")
        concurrency = entry.get("max_concurrency", 2)
        if type(concurrency) is not int or concurrency < 1:
            raise ModelConfigError("max_concurrency must be a positive integer")
        capabilities = entry.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities):
            raise ModelConfigError("capabilities must be a list of strings")
        ident = "yaml-" + tenant + "-" + name
        provider = {"name": ident, "type": kind, "base_url": base_url.rstrip("/")}
        if "api_key_env" in entry:
            provider["auth"] = {"type": "header" if kind == "gemini" else "bearer",
                                "credential": secret(name, entry, "api_key_env")}
            if kind == "gemini":
                provider["auth"]["header"] = "x-goog-api-key"
        modal = {"modal_key_env", "modal_secret_env"} & entry.keys()
        if modal:
            if len(modal) != 2:
                raise ModelConfigError("Modal proxy authentication needs both modal_key_env and modal_secret_env")
            provider["default_headers"] = {
                header: {"value_from": secret(name, entry, field)}
                for header, field in [("Modal-Key", "modal_key_env"), ("Modal-Secret", "modal_secret_env")]}
        deployment = {"name": ident, "provider": ident, "model": served, "max_concurrency": concurrency}
        if capabilities:
            deployment["capabilities"] = capabilities
        definitions[name] = (provider, deployment)
    for alias, target in routes.items():
        token(alias, "routes")
        if not isinstance(target, str) or target not in definitions:
            raise ModelConfigError("Every route must name a defined model")
        provider, deployment = definitions[target]
        vm = {"name": alias, "response_model": "virtual", "selection": {"mode": "weighted_random"},
              "pools": [{"priority": 0, "targets": [{"deployment": deployment["name"], "weight": 100}]}]}
        records[alias] = {"schema_version": 1, "model": alias, "virtual_model": vm,
                          "providers": [copy.deepcopy(provider)], "deployments": [copy.deepcopy(deployment)]}
    filename = previous.get("credential_file") if credentials == previous.get("credentials") else None
    filename = filename or f"models-{tenant}-{uuid4().hex}-secrets.json"
    for record in records.values():
        for provider in record["providers"]:
            if "auth" in provider:
                provider["auth"]["credential"] = provider["auth"]["credential"].replace("__MODEL_SECRETS__", filename)
            for header in provider.get("default_headers", {}).values():
                header["value_from"] = header["value_from"].replace("__MODEL_SECRETS__", filename)
    return {"tenant": tenant, "records": records, "credentials": credentials,
            "credential_sources": sources, "credential_file": filename}


def stage(root, path, tenant):
    with installation_lock(root):
        plan = compile_config(path, tenant, previous=read_json(plan_path(root, tenant)))
        write_json(plan_path(root, tenant), plan)
    return plan


def central_config(current, records):
    result = copy.deepcopy(current)
    central_names = {m["name"] for m in result["virtual_models"]}
    if (set(records) - {"memorylayer-default"}) & central_names:
        raise ModelConfigError("A requested tenant alias already exists centrally; resolve its central override first")
    if "memorylayer-default" in records:
        record = records["memorylayer-default"]
        for field in ("providers", "deployments"):
            incoming = {x["name"]: x for x in record[field]}
            for item in result[field]:
                if item["name"] in incoming and item != incoming[item["name"]]:
                    # Never overwrite an object that an unmanaged route might share.
                    raise ModelConfigError("Central provider/deployment name is already used with different settings")
            existing_names = {x["name"] for x in result[field]}
            result[field].extend(x for x in incoming.values() if x["name"] not in existing_names)
        result["virtual_models"] = [m for m in result["virtual_models"] if m["name"] != "memorylayer-default"]
        result["virtual_models"].append(record["virtual_model"])
    return result


def version_central_model(plan):
    """Immutable central objects preserve unrelated routes when endpoint settings change."""
    import hashlib
    plan = copy.deepcopy(plan)
    record = plan["records"].get("memorylayer-default")
    if record:
        suffix = "-" + hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:16]
        for provider in record["providers"]:
            provider["name"] += suffix
        for deployment in record["deployments"]:
            deployment["name"] += suffix
            deployment["provider"] += suffix
        for pool in record["virtual_model"]["pools"]:
            for target in pool["targets"]:
                target["deployment"] += suffix
    return plan


def provider_policy(root, tenant, records, central):
    providers = list(central["providers"])
    for path in (root / ".local").glob("*/model-catalog/*.json"):
        if path.name.startswith("next-"):
            continue
        if path.parent.parent.name == tenant and path.stem in records:
            continue
        providers.extend(read_json(path)["providers"])
    for record in records.values():
        providers.extend(record["providers"])
    urls = [urlsplit(provider["base_url"]) for provider in providers]
    return {"MODEL_PROVIDER_HOSTS": ",".join(sorted({url.hostname for url in urls})),
            "MODEL_PROVIDER_ALLOW_HTTP": str(any(url.scheme == "http" for url in urls)).lower()}


def run_private(args, root, label):
    # Owner diagnostics may include configuration or credentials. Keep them local.
    log = root / ".local/models/last-command.log"
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        result = subprocess.run(args, cwd=root, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ModelConfigError(label + " failed; inspect " + str(log) + " privately, correct the cause and retry")


def validate_documents(root, plan, central):
    image = read_json(root / ".local/images.json")["SPARKROUTE_IMAGE"]["tag"]
    documents = [central] + [{"providers": r["providers"], "deployments": r["deployments"],
                              "virtual_models": [r["virtual_model"]]} for r in plan["records"].values()]
    with tempfile.TemporaryDirectory(prefix="model-check-", dir=root / ".local") as temp:
        folder = Path(temp)
        write_json(folder / plan["credential_file"], plan["credentials"])
        for document in documents:
            write_json(folder / "config.json", document)
            run_private(["docker", "run", "--rm", "--network", "none", "--read-only",
                         "--user", f"{os.getuid()}:{os.getgid()}", "-v", str(folder) + ":/run/gateway:ro",
                         "-e", "SPARKROUTE_CONFIG=/run/gateway/config.json",
                         "-e", "SPARKROUTE_CREDENTIAL_FILE_ROOTS=/run/gateway", image, "-config-check",
                         # Validate the managed auth shape without opening a DB.
                         # Network remains disabled and this invocation never serves.
                         "-caller-auth-mode", "managed",
                         "-client-credentials-postgres-url", "postgres://unreachable.invalid:1/config-check"],
                        root, "Offline model validation")


def apply_plan(root, tenant, command, *, restart=True, validate=validate_documents, run=run_private, expected_plan=None):
    with installation_lock(root):
        staged = read_json(plan_path(root, tenant))
        if expected_plan is not None and staged != expected_plan:
            raise ModelConfigError("Staged model configuration changed; retry the requested update")
        plan = version_central_model(staged)
        records = plan["records"]
        if not records:
            return 0
        central_path = root / ".local/gateway/config.json"
        old_central_text = central_path.read_text()
        central = central_config(json.loads(old_central_text), records)
        validate(root, plan, central)
        policy = provider_policy(root, tenant, records, central)
        catalog = root / ".local" / tenant / "model-catalog"
        write_json(root / ".local/gateway" / plan["credential_file"], plan["credentials"])
        for alias, record in records.items():
            if alias == "memorylayer-default":
                continue
            canonical = catalog / (alias + ".json")
            # The existing canonical file is the expected CAS value. Keep it until
            # success; an identical retry also recovers a crash after publication.
            next_path = catalog / ("next-" + alias + ".json")
            write_json(next_path, record)
            args = ["run", "--rm", f"models-{tenant}-sahara-default", "catalog", "publish",
                    "--address", f"aether-{tenant}:50051", "--server-name", f"aether-{tenant}",
                    "--implementation", "scitrera-management-plane", "--specifier", "catalog-publisher-" + alias,
                    "--ca", "/run/tls/ca.crt", "--cert", "/run/tls/tls.crt", "--key", "/run/tls/tls.key",
                    "--record", "/catalog/" + next_path.name]
            if canonical.exists():
                args += ["--expected", "/catalog/" + canonical.name]
            run(command(*args), root, "Catalog publication for " + alias)
            os.replace(next_path, canonical)
        if central_path.read_text() != old_central_text:
            raise ModelConfigError("Central routing changed during publication; retry after reviewing that change")
        write_json(central_path, central)
        env_path = root / ".local/compose.env"
        lines = env_path.read_text().splitlines()
        # Retain manually allowlisted hosts: they may be used by catalog records
        # managed elsewhere. Never inject a wildcard or a URL into this policy.
        old = dict(line.split("=", 1) for line in lines if line and not line.startswith("#") and "=" in line)
        hosts = set(filter(None, old.get("MODEL_PROVIDER_HOSTS", "").split(",")))
        hosts.update(policy["MODEL_PROVIDER_HOSTS"].split(","))
        policy["MODEL_PROVIDER_HOSTS"] = ",".join(sorted(hosts))
        if old.get("MODEL_PROVIDER_ALLOW_HTTP") == "true":
            policy["MODEL_PROVIDER_ALLOW_HTTP"] = "true"
        lines = [line for line in lines if line.split("=", 1)[0] not in policy]
        atomic_write(env_path, "\n".join(lines + [k + "=" + v for k, v in policy.items()]) + "\n")
        if restart:
            run(command("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "gateway"), root, "Gateway recreation")
        return len(records)


def apply_staged(root, command):
    count = 0
    for path in sorted((root / ".local/models").glob("*.json")):
        count += apply_plan(root, path.stem, command, restart=False)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "stage", "apply"])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    args = parser.parse_args()
    from compose import command
    try:
        from compose_tenants import load_tenants
        if args.tenant not in {t["slug"] for t in load_tenants(ROOT)}:
            raise ModelConfigError("Select a tenant configured in this installation")
        if args.action == "check":
            plan = compile_config(args.file, args.tenant, previous=read_json(plan_path(ROOT, args.tenant)))
            print(f"Valid YAML: {len(plan['records'])} routes; no configuration changed or provider contacted.")
        else:
            plan = stage(ROOT, args.file, args.tenant)
            if args.action == "apply":
                count = apply_plan(ROOT, args.tenant, command, expected_plan=plan)
                print(f"Applied {count} model routes. No inference requests were made by this script.")
            else:
                print(f"Staged {len(plan['records'])} routes for the next stack up.")
    except (ModelConfigError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
