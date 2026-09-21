#!/usr/bin/env python3
"""Render bounded shared usage reporting for Compose or Kubernetes."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import copy
from pathlib import Path
import yaml
from deployment import write_artifacts
from deployment_config import _read, _fields, _no_secrets
from metering_config import artifacts, reference


def generate(config, runtime):
    _fields(config, {"schemaVersion", "production", "images", "kubernetes", "compose"}, "metering",
            {"schemaVersion", "production", "images"})
    if config["schemaVersion"] != 1 or type(config["production"]) is not bool:
        raise ValueError("Invalid metering schema or production flag")
    _no_secrets(config, "metering")
    images = dict(config["images"])
    backend = images.pop("backend", "")
    if not backend:
        raise ValueError("A reporting backend image is required")
    production = config["production"]
    import re
    if production and not re.fullmatch(r"\S+@sha256:[a-f0-9]{64}", backend):
        raise ValueError("Production reporting image requires a registry digest")
    if set(images) - {"openmeter", "clickhouse", "kafka", "redis", "postgres"}:
        raise ValueError("Unknown metering image role")
    if runtime == "compose":
        _fields(config.get("compose", {}), {"networkName"}, "metering.compose")
        result = artifacts(images=images, production=production)
        document = result["compose.metering.yaml"]
        services = document["services"]
        journal = copy.deepcopy(services["metering-postgres"])
        journal["environment"] = {"POSTGRES_USER": "usage", "POSTGRES_DB": "usage",
                                  "POSTGRES_PASSWORD": reference("METERING_JOURNAL_PASSWORD")}
        journal["volumes"] = ["usage-postgres:/var/lib/postgresql/data"]
        journal["healthcheck"]["test"] = ["CMD", "pg_isready", "-U", "usage"]
        services["usage-postgres"] = journal
        document["volumes"]["usage-postgres"] = {}
        services["usage-reporting"] = {
            "image": backend, "command": ["python", "-m", "x_usage_reporting"], "restart": "unless-stopped",
            "networks": ["metering"], "mem_limit": "512m", "cpus": "1", "user": "1000:1000",
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:size=32m,uid=1000,gid=1000"],
            "volumes": ["./secrets/usage-producers.json:/run/usage/producers.json:ro"],
            "environment": {"BILLING_DATABASE_HOST": "usage-postgres",
                            "BILLING_DATABASE_PASSWORD": reference("METERING_JOURNAL_PASSWORD"),
                            "BILLING_PRODUCERS_FILE": "/run/usage/producers.json",
                            "OPENMETER_API_URL": "http://metering-api:8888"},
            "depends_on": {"usage-postgres": {"condition": "service_healthy"}},
            "healthcheck": {"test": ["CMD", "python", "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=5).read()"],
                "interval": "10s", "timeout": "6s", "retries": 30},
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
        network = config.get("compose", {}).get("networkName")
        if network:
            document["networks"]["metering"]["name"] = network
        return result
    from metering_kubernetes import render, reporting
    k = config.get("kubernetes", {})
    _fields(k, {"namespace", "storageClass", "nodeSelector", "tolerations", "postgresHost", "postgresSecret",
                "credentialsSecret", "reportingEnvironmentSecret", "producerSecret", "journalCluster", "tenantNamespaces"},
            "metering.kubernetes", {"namespace", "storageClass", "postgresHost", "postgresSecret",
                                   "credentialsSecret", "reportingEnvironmentSecret", "producerSecret", "journalCluster"})
    objects = render(namespace=k["namespace"], storage_class=k["storageClass"],
        postgres_host=k["postgresHost"], postgres_secret=k["postgresSecret"], credentials_secret=k["credentialsSecret"],
        node_selector=k.get("nodeSelector", {}), tolerations=k.get("tolerations", []), images=images, production=production, tenant_namespaces=k.get("tenantNamespaces", []))
    objects += reporting(namespace=k["namespace"], backend_image=backend,
        environment_secret=k["reportingEnvironmentSecret"], producer_secret=k["producerSecret"],
        journal_cluster=k["journalCluster"], node_selector=k.get("nodeSelector", {}), tolerations=k.get("tolerations", []))
    return {"metering.kubernetes.yaml": yaml.safe_dump_all(objects, sort_keys=False)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime", choices=["compose", "kubernetes"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    changed = write_artifacts(args.output, generate(_read(args.config), args.runtime))
    print("Rendered " + str(len(changed)) + " changed metering artifacts; no services changed")


if __name__ == "__main__":
    main()
