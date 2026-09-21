"""Full Compose topology adapter. Persistent data is never migrated by rendering."""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations
import copy
from pathlib import Path
from urllib.parse import urlsplit
import yaml
from compose_tenants import render_compose
from configure import nginx
from deployment_config import ConfigError, _merge

ROOT = Path(__file__).resolve().parents[1]
def env(name):
    return "$" + "{" + name + ":?Configure " + name + "}"
INSTALL = env("PLATFORM_INSTALLATION_ROOT")
DEPLOY = env("PLATFORM_DEPLOYMENT_ROOT")


def walk(value, fn):
    if isinstance(value, dict):
        return {fn(k): walk(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [walk(v, fn) for v in value]
    return fn(value) if isinstance(value, str) else value


def _mount(value):
    if value.startswith("../.local/"):
        return INSTALL + "/.local/" + value[len("../.local/"):]
    if value.startswith("./config/"):
        return INSTALL + "/compose/config/" + value[len("./config/"):]
    if value.startswith("../"):
        return INSTALL + "/" + value[3:]
    return value


INIT_DATABASES = r"""#!/bin/bash
set -euo pipefail
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\getenv ml_password APP_ML_PASSWORD
\getenv dc_password APP_DC_PASSWORD
\getenv gateway_password APP_GATEWAY_PASSWORD
\getenv storage_password APP_STORAGE_PASSWORD
CREATE ROLE memorylayer LOGIN PASSWORD :'ml_password';
CREATE ROLE dataconnectors LOGIN PASSWORD :'dc_password';
CREATE ROLE sparkroute LOGIN PASSWORD :'gateway_password';
CREATE ROLE storage LOGIN PASSWORD :'storage_password';
CREATE DATABASE memorylayer OWNER memorylayer;
CREATE DATABASE dataconnectors OWNER dataconnectors;
CREATE DATABASE sparkroute OWNER sparkroute;
CREATE DATABASE storage OWNER storage;
\connect memorylayer
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
ALTER ROLE memorylayer SET search_path = public, ag_catalog;
SQL
"""


def build(resolved, policy, resources):
    config, bindings = resolved["deployment"], resolved["bindings"]
    tenant = config["tenant"]
    tenants = [t for t in resolved["inputs"]["tenants"] if t["slug"] == tenant]
    document = render_compose(yaml.safe_load((ROOT / "compose/compose.yaml").read_text()), tenants)
    document = walk(document, _mount)
    services, artifacts = document["services"], {}
    if config["database"]["mode"] == "consolidated":
        name = "tenant-postgres-" + tenant
        old_names = ["ml-postgres-" + tenant, "dc-postgres-" + tenant, "sparkroute-postgres", "storage-postgres"]
        database = copy.deepcopy(services[old_names[0]])
        suffix = tenant.upper().replace("-", "_")
        database["environment"] = {
            "POSTGRES_USER": "postgres", "POSTGRES_DB": "postgres",
            "POSTGRES_PASSWORD": env("TENANT_POSTGRES_PASSWORD"),
            "APP_ML_PASSWORD": env("ML_PASSWORD_" + suffix), "APP_DC_PASSWORD": env("DC_PASSWORD_" + suffix),
            "APP_GATEWAY_PASSWORD": env("SPARKROUTE_PASSWORD"), "APP_STORAGE_PASSWORD": env("STORAGE_PASSWORD")}
        database["command"] = ["postgres", "-c", "shared_preload_libraries=pg_textsearch",
                               "-c", "max_connections=" + str(config["database"]["maxConnections"]),
                               "-c", "shared_buffers=256MB", "-c", "work_mem=4MB",
                               "-c", "maintenance_work_mem=64MB", "-c", "max_parallel_workers_per_gather=1"]
        database["volumes"] = [name + ":/var/lib/postgresql/data",
                              DEPLOY + "/postgres-init.sh:/docker-entrypoint-initdb.d/10-apps.sh:ro"]
        database["networks"] = ["tenant-" + tenant, "model-state", "storage"]
        database["healthcheck"]["test"] = ["CMD-SHELL", "pg_isready -U postgres -d postgres"]
        for old in old_names:
            del services[old]
            document["volumes"].pop(old, None)
        def rewrite(value):
            for old in old_names:
                value = value.replace(old, name)
            return value
        document = walk(document, rewrite)
        services = document["services"]
        services[name] = database
        document["volumes"][name] = {}
        artifacts["postgres-init.sh"] = INIT_DATABASES
    elif config["database"]["mode"] != "dedicated":
        raise ConfigError("Compose external databases are not configured")
    if config["auth"]["mode"] == "shared":
        for name in ("auth", "auth-migrate", "mt-postgres", "sessions", "fixture-idp"):
            services.pop(name, None)
        for service in services.values():
            for name in list(service.get("depends_on", {})):
                if name not in services:
                    del service["depends_on"][name]
    if not config["development"]:
        services.pop("fixture-idp", None)
    if not config["toolsWssEnabled"]:
        services.pop("tools-" + tenant, None)
    conf = nginx(tenants, tools_wss_enabled=config["toolsWssEnabled"])
    if config["auth"]["mode"] == "shared":
        auth = bindings.get("auth", {})
        if not auth.get("verifyURL") or not auth.get("statusURL"):
            raise ConfigError("Shared Compose auth requires private verifyURL and statusURL")
        conf = conf.replace("http://auth:8080/auth/verify", auth["verifyURL"])
        conf = conf.replace("http://auth:8081", auth["statusURL"].rstrip("/"))
    if config["auth"]["protectAllPaths"]:
        guard = """
    auth_request /_host_auth;
    error_page 401 = @auth_denied;
    location = /_host_auth {
      internal;
      auth_request off;
      proxy_pass http://auth-gate:8080/check;
      proxy_pass_request_body off;
      proxy_pass_request_headers off;
      proxy_set_header Content-Length "";
      proxy_set_header Cookie $http_cookie;
      proxy_set_header Authorization $http_authorization;
    }
    location @auth_denied {
      auth_request off;
      set $auth_denied http://auth-gate:8080/denied;
      proxy_pass $auth_denied;
      proxy_pass_request_body off;
      proxy_pass_request_headers off;
      proxy_set_header Content-Length "";
      proxy_set_header X-Original-URI $request_uri;
      proxy_set_header X-Original-Method $request_method;
      proxy_set_header Accept $http_accept;
      proxy_set_header Sec-Fetch-Dest $http_sec_fetch_dest;
      proxy_set_header Upgrade $http_upgrade;
    }
"""
        conf = conf.replace("    root /usr/share/nginx/html;", "    root /usr/share/nginx/html;\n" + guard)
        conf = conf.replace("location = /healthz {", "location = /healthz { auth_request off;")
        conf = conf.replace("      internal;", "      internal;\n      auth_request off;")
        conf = conf.replace("      auth_request off;\n      auth_request off;", "      auth_request off;")
    artifacts["nginx.conf"] = conf
    services["web"]["volumes"] = [DEPLOY + "/nginx.conf:/etc/nginx/nginx.conf:ro"]
    if config["objectStorage"]["mode"] == "s3":
        s3 = bindings.get("objectStorage", {})
        if not all(s3.get(k) for k in ("endpoint", "region", "bucket")):
            raise ConfigError("External storage requires endpoint, region and bucket bindings")
        for name in ("objects", "objects-init"):
            services.pop(name, None)
        for service in services.values():
            service.get("depends_on", {}).pop("objects-init", None)
        for name in ("blobgw", "storage-edge"):
            service = services[name]
            command = service["command"]
            command[command.index("-s3-endpoint")+1] = s3["endpoint"]
            for flag in ("-s3-bucket", "-staging-bucket"):
                if flag in command:
                    command[command.index(flag)+1] = s3["bucket"]
            service["environment"].update({
                "AWS_ACCESS_KEY_ID": env("STORAGE_ACCESS_KEY_ID"),
                "AWS_SECRET_ACCESS_KEY": env("STORAGE_SECRET_ACCESS_KEY"), "AWS_REGION": s3["region"]})
            service["volumes"] = [v.replace(INSTALL + "/.local/storage-tenants.json",
                                            DEPLOY + "/storage-tenants.json") for v in service["volumes"]]
        artifacts["storage-tenants.json"] = {"tenants": {tenant: {
            "endpoint": s3["endpoint"], "region": s3["region"], "bucket": s3["bucket"],
            "prefix": s3.get("prefix", "packs"), "forcePathStyle": True, "credentialRef": tenant}}}
        upstream = urlsplit(s3["endpoint"])
        conf = conf.replace("http://objects:9000", s3["endpoint"]).replace("Host objects:9000", "Host " + upstream.netloc)
        conf = conf.replace("proxy_set_header Host " + upstream.netloc + ";",
                            "proxy_set_header Host " + upstream.netloc + ";\n      proxy_ssl_server_name on;\n      proxy_ssl_name " + upstream.hostname + ";\n      proxy_ssl_verify on;\n      proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;")
        artifacts["nginx.conf"] = conf
        download = (ROOT / "compose/config/download-nginx.conf").read_text()
        address = upstream.netloc + (":443" if upstream.port is None and upstream.scheme == "https" else "")
        download = download.replace("upstream artifact_objects { server objects:9000; }",
                                    "upstream artifact_objects { server " + address + "; }")
        download = download.replace("http://artifact_objects", upstream.scheme + "://artifact_objects")
        download = download.replace("proxy_set_header Host objects:9000;",
                                    "proxy_set_header Host " + upstream.netloc + ";\n   proxy_ssl_server_name on;\n   proxy_ssl_name " + upstream.hostname + ";\n      proxy_ssl_verify on;\n      proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;")
        artifacts["download-nginx.conf"] = download
        services["storage-download"]["volumes"] = [DEPLOY + "/download-nginx.conf:/etc/nginx/nginx.conf:ro"]
    if config["backup"]["enabled"]:
        if config["backup"]["walArchive"]:
            raise ConfigError("This Compose backup adapter provides nightly logical backups, not WAL/PITR")
        image = bindings.get("images", {}).get("postgresBackup")
        if not image:
            raise ConfigError("Nightly backups require images.postgresBackup")
        pg_services = {name: service for name, service in services.items()
                       if "POSTGRES_DB" in service.get("environment", {})}
        for name, service in pg_services.items():
            pg = service["environment"]
            dbs = config["database"]["databases"] if name.startswith("tenant-postgres-") else [pg["POSTGRES_DB"]]
            backup_name = "backup-" + name
            services[backup_name] = {
                "image": image, "restart": "unless-stopped", "read_only": True,
                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                "mem_limit": "512m", "cpus": "0.5", "tmpfs": ["/tmp:size=32m"],
                "volumes": [backup_name + ":/state"], "networks": service["networks"],
                "env_file": [{"path": env("PLATFORM_CONFIG_ROOT") + "/" + config.get("secretFiles", {}).get("backup", "backup.env"), "required": True}],
                "environment": {"PGHOST": name, "PGPORT": "5432", "PGUSER": pg["POSTGRES_USER"],
                                "PGPASSWORD": pg["POSTGRES_PASSWORD"], "BACKUP_SERVER": name,
                                "BACKUP_DATABASES": ",".join(dbs), "BACKUP_RETENTION_DAYS": str(config["backup"]["retentionDays"]),
                                "BACKUP_SCHEDULE": config["backup"]["schedule"],
                                "DEPLOYMENT_CONFIG_DIGEST": resolved["configDigest"], "GOMAXPROCS": "1"},
                "depends_on": {name: {"condition": "service_healthy"}},
            }
            document["volumes"][backup_name] = {}
    if config["billing"]["mode"] != "off":
        metering = bindings.get("metering", {})
        if not metering.get("endpoint"):
            raise ConfigError("Usage reporting requires bindings.metering.endpoint")
        services["usage-collector-" + tenant] = {
            "image": services["platform-" + tenant]["image"],
            "command": ["python", "-m", "scitrera_app_server.billing.collector"],
            "user": "1000:1000", "restart": "unless-stopped", "read_only": True,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:size=16m,uid=1000,gid=1000"], "mem_limit": "256m", "cpus": "0.5",
            "networks": ["tenant-" + tenant, "model-state"],
            "environment": {"BILLING_TENANT": tenant, "BILLING_REPORTING_URL": metering["endpoint"]},
            "env_file": [{"path": env("PLATFORM_CONFIG_ROOT") + "/" + config.get("secretFiles", {}).get("billing", "billing.env"), "required": True}],
            "depends_on": {"gateway": {"condition": "service_started"}},
        }
        if metering.get("networkName"):
            document["networks"]["billing"] = {"external": True, "name": metering["networkName"]}
            services["usage-collector-" + tenant]["networks"].append("billing")
        from deployment_metrics import environment as metrics_environment
        bridge = copy.deepcopy(services["usage-collector-" + tenant])
        bridge["image"] = bindings.get("images", {}).get("metricsBridge", bridge["image"])
        bridge.pop("env_file", None)
        bridge.pop("healthcheck", None)
        bridge["command"] = ["python", "-m", "x_metrics_bridge"]
        bridge["environment"] = {**metrics_environment(tenant, metering.get("openmeterEndpoint", "http://metering-api:8888")),
            "AETHER_GATEWAY": "aether-" + tenant + ":50051", "AETHER_TLS_ENABLED": "true",
            "AETHER_TLS_CA_CERT": "/run/tls/ca.crt", "AETHER_TLS_CLIENT_CERT": "/run/tls/tls.crt",
            "AETHER_TLS_CLIENT_KEY": "/run/tls/tls.key", "SCITRERA_TENANT": tenant,
            "STATEFUL_ROOT": "/tmp/state", "XDG_CONFIG_HOME": "/tmp/config", "BRIDGE_SERVER_PORT": "53002"}
        bridge["volumes"] = [INSTALL + "/.local/" + tenant + "/tls/metrics-bridge:/run/tls:ro"]
        bridge["healthcheck"] = {"test": ["CMD", "python", "-c",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:53002/health',timeout=5).read()"],
            "interval": "30s", "timeout": "6s", "retries": 5}
        bridge["depends_on"] = {"aether-" + tenant: {"condition": "service_healthy"}}
        services["metrics-bridge-" + tenant] = bridge
    document = _merge(document, policy)
    from deployment_customer import compose as customer_compose
    customer_compose(resolved, document)
    for name, limits in resources["services"].items():
        if name in document["services"]:
            document["services"][name].update(limits)
    for service in document["services"].values():
        service.setdefault("labels", {})["platform.scitrera.io/config-digest"] = resolved["configDigest"]
        if service.get("labels", {}).get("platform.scitrera.io/lifecycle") != "job":
            service.setdefault("restart", "unless-stopped")
    artifacts["compose.yaml"] = document
    return artifacts
