"""Validated, runtime-independent installation configuration.

This module never reads credential files or contacts a running service. Runtime
renderers consume its resolved description instead of interpreting customer
configuration independently.
"""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import copy
import ipaddress
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml

ROOT_FIELDS = {
    "schemaVersion", "tenant", "inputs", "services", "database", "auth", "public",
    "resources", "backup", "billing", "profiles", "secretFiles", "customer",
    "objectStorage", "development",
}
PROFILE_FIELDS = ROOT_FIELDS - {"schemaVersion", "tenant", "inputs", "profiles", "customer", "secretFiles"}
INPUT_NAMES = {"tenants", "auth", "models", "documentServices", "memorylayer", "workProfiles", "review"}
BINDING_FIELDS = {
    "schemaVersion", "kubernetes", "images", "secrets", "objectStorage", "backup",
    "auth", "metering", "compose",
}
TOKEN = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
ENV_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}\Z")
SENSITIVE_KEYS = {"password", "api_key", "apikey", "client_secret", "clientsecret",
                  "secret_key", "secretkey", "access_token", "accesstoken"}
MEMORY = re.compile(r"([1-9][0-9]*)(Ki|Mi|Gi)\Z")


class ConfigError(ValueError):
    """Errors identify a field; credential values are never included."""


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ConfigError("Configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read(path):
    try:
        return yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as error:
        # YAML parser diagnostics can contain source lines with secrets.
        raise ConfigError("Unable to read configuration file: " + Path(path).name) from None


def _fields(value, allowed, where, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ConfigError("Invalid or missing fields in " + where)


def _string(value, where):
    if not isinstance(value, str) or not value:
        raise ConfigError("Expected a nonempty string in " + where)
    return value


def _choice(value, choices, where):
    if not isinstance(value, str) or value not in choices:
        raise ConfigError("Unsupported value in " + where)


def _boolean(value, where):
    if type(value) is not bool:
        raise ConfigError("Expected a boolean in " + where)


def _origin(value, where, secure=False):
    _string(value, where)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfigError("Invalid origin in " + where) from None
    if (parsed.scheme not in ({"https"} if secure else {"http", "https"})
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or (port is not None and not 0 < port < 65536)):
        raise ConfigError("Invalid origin in " + where)
    return value.rstrip("/")


def _merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _no_secrets(value, where):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE_KEYS and item not in (None, ""):
                if not isinstance(item, str) or not ENV_REF.fullmatch(item):
                    raise ConfigError("Use a credential reference in " + where + "." + key)
            _no_secrets(item, where + "." + key)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item, where)


def _relative(root, value, where, exists=True):
    value = _string(value, where)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError("Expected a relative path within the config directory in " + where)
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()) or (exists and not target.is_file()):
        raise ConfigError("Invalid or missing configuration input in " + where)
    return target


def _resources(resources):
    if not isinstance(resources, dict):
        raise ConfigError("resources must be a mapping")
    for name, resource in resources.items():
        if not isinstance(name, str) or not TOKEN.fullmatch(name):
            raise ConfigError("Invalid resource role")
        _fields(resource, {"requests", "limits"}, "resources." + name, {"requests", "limits"})
        numbers = {}
        for kind in ("requests", "limits"):
            _fields(resource[kind], {"cpu", "memory"}, "resources." + name + "." + kind,
                    {"cpu", "memory"})
            cpu, memory = str(resource[kind]["cpu"]), str(resource[kind]["memory"])
            if not re.fullmatch(r"(?:[1-9][0-9]*m|[0-9]+(?:\.[0-9]+)?)", cpu):
                raise ConfigError("Invalid CPU resource for " + name)
            mcpu = float(cpu[:-1]) if cpu.endswith("m") else float(cpu) * 1000
            match = MEMORY.fullmatch(memory)
            if mcpu <= 0 or not match:
                raise ConfigError("Invalid resource quantity for " + name)
            nbytes = int(match[1]) * {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[match[2]]
            numbers[kind] = (mcpu, nbytes)
        if any(a > b for a, b in zip(numbers["requests"], numbers["limits"])):
            raise ConfigError("Resource request exceeds limit for " + name)


def _validate(config):
    _fields(config, ROOT_FIELDS, "deployment", {
        "schemaVersion", "tenant", "inputs", "services", "database", "auth", "public",
        "resources", "backup", "billing", "objectStorage", "development",
    })
    if type(config["schemaVersion"]) is not int or config["schemaVersion"] != 1:
        raise ConfigError("Unsupported deployment schemaVersion")
    if not isinstance(config["tenant"], str) or not TOKEN.fullmatch(config["tenant"]):
        raise ConfigError("Invalid tenant")
    _boolean(config["development"], "development")
    production = not config["development"]
    _fields(config["inputs"], INPUT_NAMES, "inputs", INPUT_NAMES)
    _fields(config["services"], {"frontend", "storage", "sparkroute"}, "services",
            {"frontend", "storage", "sparkroute"})
    for name, scope in config["services"].items():
        _choice(scope, {"tenant", "shared", "external"}, "services." + name)
    _fields(config["database"], {"mode", "databases", "storage", "maxConnections"}, "database",
            {"mode", "databases", "storage", "maxConnections"})
    _choice(config["database"]["mode"], {"dedicated", "consolidated", "external"}, "database.mode")
    databases = config["database"]["databases"]
    if (not isinstance(databases, list) or not databases
            or any(not isinstance(n, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", n) for n in databases)
            or len(set(databases)) != len(databases)):
        raise ConfigError("Invalid database names")
    for service, db in (("sparkroute", "sparkroute"), ("storage", "storage")):
        if config["services"][service] == "tenant" and db not in databases:
            raise ConfigError("Tenant-owned service requires database: " + service)
    if not {"memorylayer", "dataconnectors"}.issubset(databases):
        raise ConfigError("Tenant requires MemoryLayer and connector databases")
    if not MEMORY.fullmatch(str(config["database"]["storage"])):
        raise ConfigError("Invalid database storage size")
    if type(config["database"]["maxConnections"]) is not int or config["database"]["maxConnections"] < 10:
        raise ConfigError("Invalid database maxConnections")
    _fields(config["public"], {"origin", "applicationPath"}, "public", {"origin", "applicationPath"})
    config["public"]["origin"] = _origin(config["public"]["origin"], "public.origin", production)
    path = config["public"]["applicationPath"]
    if not isinstance(path, str) or not re.fullmatch(r"/(?:[a-z0-9-]+)?", path):
        raise ConfigError("Invalid public.applicationPath")
    _fields(config["auth"], {"mode", "origin", "tenant", "workspace", "protectAllPaths", "cookieDomain"},
            "auth", {"mode", "origin", "tenant", "workspace", "protectAllPaths"})
    _choice(config["auth"]["mode"], {"local", "shared"}, "auth.mode")
    config["auth"]["origin"] = _origin(config["auth"]["origin"], "auth.origin", production)
    if config["auth"]["tenant"] != config["tenant"] or not isinstance(config["auth"]["workspace"], str) or not TOKEN.fullmatch(config["auth"]["workspace"]):
        raise ConfigError("Auth scope must match the tenant and a valid workspace")
    _boolean(config["auth"]["protectAllPaths"], "auth.protectAllPaths")
    if production and config["auth"]["protectAllPaths"] is not True:
        raise ConfigError("Production requires host-wide authentication")
    domain = config["auth"].get("cookieDomain", "")
    if not isinstance(domain, str):
        raise ConfigError("Invalid auth.cookieDomain")
    domain = domain.lstrip(".")
    if domain:
        if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", domain):
            raise ConfigError("Invalid auth.cookieDomain")
        for name in ("auth", "public"):
            host = urlsplit(config[name]["origin"]).hostname
            if host != domain and not host.endswith("." + domain):
                raise ConfigError("Cookie domain does not cover configured origins")
    elif urlsplit(config["auth"]["origin"]).hostname != urlsplit(config["public"]["origin"]).hostname:
        raise ConfigError("Cross-host auth requires an explicit shared cookie domain")
    _resources(config["resources"])
    _fields(config["backup"], {"enabled", "schedule", "retentionDays", "walArchive"}, "backup",
            {"enabled", "schedule", "retentionDays", "walArchive"})
    for name in ("enabled", "walArchive"):
        _boolean(config["backup"][name], "backup." + name)
    if type(config["backup"]["retentionDays"]) is not int or config["backup"]["retentionDays"] < 1:
        raise ConfigError("Invalid backup retention")
    if not isinstance(config["backup"]["schedule"], str) or len(config["backup"]["schedule"].split()) != 5:
        raise ConfigError("Backup schedule must use five-field cron; renderers adapt runtime syntax")
    _fields(config["billing"], {"mode", "rateCard"}, "billing", {"mode"})
    _choice(config["billing"]["mode"], {"off", "reporting", "pricing"}, "billing.mode")
    if config["billing"]["mode"] == "pricing" and not config["billing"].get("rateCard"):
        raise ConfigError("Pricing requires a rateCard reference")
    _fields(config["objectStorage"], {"mode"}, "objectStorage", {"mode"})
    _choice(config["objectStorage"]["mode"], {"local", "s3"}, "objectStorage.mode")
    if production and config["objectStorage"]["mode"] != "s3":
        raise ConfigError("Production requires an external object store")
    _fields(config.get("customer", {}), {"pythonModule", "sourcePath", "workProfileSkills", "principal", "statePath", "readinessFile", "environment", "provisionModule", "provisionEnvironment"}, "customer")
    if "pythonModule" in config.get("customer", {}) and (not isinstance(config["customer"]["pythonModule"], str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", config["customer"]["pythonModule"])):
        raise ConfigError("Invalid customer.pythonModule")


def _bindings(value):
    _fields(value, BINDING_FIELDS, "bindings")
    if value.get("schemaVersion", 1) != 1:
        raise ConfigError("Unsupported bindings schemaVersion")
    fields = {
        "kubernetes": {"context", "namespace", "sharedNamespace", "storageClass", "nodeSelector",
                       "tolerations", "gatewayName", "gatewayNamespace", "gatewayListener",
                       "tlsSecret", "serviceAccountAnnotations", "kubeVersion", "dnsResolver", "apiEgress", "ingressEnabled"},
        "compose": {"project", "bindAddress", "webPort", "adminPort", "idpPort", "networkCIDR"},
        "auth": {"verifyURL", "statusURL", "operatorSecret"},
        "metering": {"endpoint", "credentialSecret", "networkName"},
        "objectStorage": {"endpoint", "region", "bucket", "prefix", "credentialsSecret", "credentialMode", "roleARN", "serviceAccountAnnotations"},
        "backup": {"endpoint", "region", "bucket", "prefix", "credentialsSecret", "serviceAccountAnnotations"},
    }
    for name, keys in fields.items():
        if name in value:
            _fields(value[name], keys, "bindings." + name)
    kubernetes = value.get("kubernetes", {})
    if "ingressEnabled" in kubernetes:
        _boolean(kubernetes["ingressEnabled"], "bindings.kubernetes.ingressEnabled")
    if "dnsResolver" in kubernetes:
        try:
            ipaddress.ip_address(kubernetes["dnsResolver"])
        except (ValueError, TypeError):
            raise ConfigError("Kubernetes DNS resolver must be a cluster DNS IP address") from None
    if "apiEgress" in kubernetes:
        rules = kubernetes["apiEgress"]
        if not isinstance(rules, list):
            raise ConfigError("Kubernetes API egress must be a list of CIDR/port rules")
        for rule in rules:
            _fields(rule, {"cidr", "port"}, "API egress rule", {"cidr", "port"})
            try:
                network = ipaddress.ip_network(rule["cidr"], strict=True)
            except (ValueError, TypeError):
                raise ConfigError("Invalid Kubernetes API egress CIDR") from None
            if network.prefixlen == 0 or type(rule["port"]) is not int or not 0 < rule["port"] < 65536:
                raise ConfigError("Kubernetes API egress requires scoped CIDRs and valid ports")
    for name in ("images", "secrets"):
        if name in value and (not isinstance(value[name], dict)
                              or any(not isinstance(v, str) or not v for v in value[name].values())):
            raise ConfigError("Expected named references in bindings." + name)
    for section, fields in (("auth", ("verifyURL", "statusURL")), ("metering", ("endpoint",)),
                            ("objectStorage", ("endpoint",)), ("backup", ("endpoint",))):
        for field in fields:
            if field not in value.get(section, {}):
                continue
            url = value[section][field]
            _string(url, "bindings." + section + "." + field)
            try:
                parsed = urlsplit(url)
                port = parsed.port
            except ValueError:
                raise ConfigError("Invalid service endpoint") from None
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.fragment or parsed.query
                    or (port is not None and not 0 < port < 65536)):
                raise ConfigError("Service endpoints cannot contain credentials or query strings")
    _no_secrets(value, "bindings")
    return copy.deepcopy(value)


def resolve(path, *, profile, bindings_path=None):
    """Resolve once for every runtime; no credential expansion or deployment."""
    path = Path(path).resolve()
    source = _read(path)
    _fields(source, ROOT_FIELDS, "deployment")
    profiles = source.get("profiles", {})
    if not isinstance(profiles, dict) or profile not in profiles:
        raise ConfigError("Unknown deployment profile")
    for name, override in profiles.items():
        if not isinstance(name, str) or not TOKEN.fullmatch(name):
            raise ConfigError("Invalid profile name")
        _fields(override, PROFILE_FIELDS, "profiles." + name)
    config = _merge({k: v for k, v in source.items() if k != "profiles"}, profiles[profile])
    _no_secrets(config, "deployment")
    _validate(config)
    documents, digests = {}, {}
    for name, filename in config["inputs"].items():
        file = _relative(path.parent, filename, "inputs." + name)
        if file.suffix not in {".json", ".yaml", ".yml"}:
            raise ConfigError("Component inputs must be JSON or YAML")
        document = _read(file)
        if not isinstance(document, (dict, list)):
            raise ConfigError("Component input must be a document: " + name)
        _no_secrets(document, "inputs." + name)
        documents[name] = document
        digests[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    tenants = documents["tenants"]
    if not isinstance(tenants, list) or sum(t.get("slug") == config["tenant"] for t in tenants if isinstance(t, dict)) != 1:
        raise ConfigError("Tenant definition does not match deployment")
    auth_document = documents["auth"]
    if (not isinstance(auth_document, dict)
            or not isinstance(auth_document.get("tenant"), dict)
            or auth_document["tenant"].get("slug") != config["tenant"]):
        raise ConfigError("Auth tenant definition does not match deployment")
    work_profiles = documents["workProfiles"]
    if not isinstance(work_profiles, dict) or not isinstance(work_profiles.get("profiles", {}), dict) or not isinstance(work_profiles.get("tenants", {}), dict):
        raise ConfigError("Invalid work-profile registry")
    work_profiles.setdefault("profiles", {})
    for name, tenant in work_profiles.get("tenants", {}).items():
        if name != config["tenant"]:
            raise ConfigError("Work profiles contain another tenant")
    customer = config.get("customer", {})
    project = path.parent.parent.resolve()
    if customer:
        for field in ("environment", "provisionEnvironment"):
            env = customer.get(field, {})
            if not isinstance(env, dict) or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str) for key, value in env.items()):
                raise ConfigError("Invalid customer environment")
        for field in ("pythonModule", "provisionModule"):
            value = customer.get(field)
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*", value)):
                raise ConfigError("Invalid customer module")
        for field in ("statePath", "readinessFile"):
            value = customer.get(field)
            if value is not None and (not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts):
                raise ConfigError("Invalid customer runtime path")
        source_name = customer.get("sourcePath", "src")
        source = _relative(project, source_name, "customer.sourcePath", exists=False)
        if not source.is_dir():
            raise ConfigError("Missing customer source directory")
        manifest = {}
        for file in sorted(source.rglob("*")):
            if any(part.startswith(".") or part == "__pycache__" for part in file.relative_to(source).parts) or file.suffix in {".pyc", ".pyo"}:
                continue
            if not file.resolve().is_relative_to(source):
                raise ConfigError("Customer source cannot contain escaping symlinks")
            if file.is_file():
                if file.suffix in {".env", ".pem", ".key", ".p12", ".sqlite", ".db"}:
                    raise ConfigError("Credential or runtime file found in customer source")
                manifest[str(file.relative_to(source))] = hashlib.sha256(file.read_bytes()).hexdigest()
        if not manifest:
            raise ConfigError("Empty customer source directory")
        documents["customerSource"] = manifest
        digests["customerSource"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        skills = customer.get("workProfileSkills", {})
        if not isinstance(skills, dict):
            raise ConfigError("workProfileSkills must map profile IDs to lists of Markdown files")
        for profile_name, filenames in skills.items():
            if profile_name not in work_profiles["profiles"] or not isinstance(filenames, list):
                raise ConfigError("Invalid work-profile skill selection")
            profile_doc = work_profiles["profiles"][profile_name]
            if not isinstance(profile_doc, dict) or not isinstance(profile_doc.get("instructions"), str):
                raise ConfigError("Work profile requires instruction text")
            for index, filename in enumerate(filenames):
                skill = _relative(project, filename, "customer.workProfileSkills")
                if skill.suffix != ".md":
                    raise ConfigError("Work-profile skills must be Markdown")
                content = skill.read_text()
                digests[f"skill:{profile_name}:{index}"] = hashlib.sha256(content.encode()).hexdigest()
                if content.startswith("---\n"):
                    parts = content.split("---", 2)
                    if len(parts) != 3:
                        raise ConfigError("Malformed skill front matter")
                    content = parts[2]
                profile_doc["instructions"] += "\n\n" + content.strip()
    if config["billing"].get("rateCard"):
        rate_path = _relative(path.parent, config["billing"]["rateCard"], "billing.rateCard")
        documents["rateCard"] = _read(rate_path)
        _no_secrets(documents["rateCard"], "billing.rateCard")
        digests["rateCard"] = hashlib.sha256(rate_path.read_bytes()).hexdigest()
    secret_files = config.get("secretFiles", {})
    if not isinstance(secret_files, dict):
        raise ConfigError("secretFiles must map names to ignored relative paths")
    for name, filename in secret_files.items():
        if not isinstance(name, str) or not TOKEN.fullmatch(name):
            raise ConfigError("Invalid secret file name")
        _relative(path.parent, filename, "secretFiles." + name, exists=False)
        if not str(filename).endswith(".env"):
            raise ConfigError("Secret file references must end in .env")
    bindings = _bindings(_read(bindings_path)) if bindings_path else {}
    resolved = {"schemaVersion": 1, "profile": profile, "deployment": config,
                "inputs": documents, "bindings": bindings, "inputDigests": digests}
    try:
        canonical = json.dumps(resolved, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise ConfigError("Configuration values must be JSON-compatible scalars, lists and mappings") from None
    resolved["configDigest"] = hashlib.sha256(canonical).hexdigest()
    return resolved


def public_summary(resolved):
    config = resolved["deployment"]
    return {"tenant": config["tenant"], "profile": resolved["profile"],
            "configDigest": resolved["configDigest"], "services": config["services"],
            "database": config["database"], "public": config["public"], "auth": config["auth"],
            "resources": config["resources"], "backup": config["backup"],
            "billing": config["billing"], "inputDigests": resolved["inputDigests"]}
