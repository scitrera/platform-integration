"""Customer-code injection adapters; application policy stays in customer config."""
# SPDX-License-Identifier: AGPL-3.0-only
import copy
from deployment_compose import env, INSTALL, DEPLOY


def artifacts(resolved, models):
    if not resolved["deployment"].get("customer"):
        return {}
    return {
        "review.yaml": resolved["inputs"]["review"],
        "customer-source.manifest.json": resolved["inputs"]["customerSource"],
        **{"model-catalog/" + name + ".json": record for name, record in models["records"].items()},
    }


def compose(resolved, document):
    config = resolved["deployment"]
    customer = config.get("customer")
    if not customer:
        return
    tenant = config["tenant"]
    source = env("PLATFORM_CUSTOMER_ROOT") + "/" + customer["sourcePath"] + ":/opt/customer/src:ro"
    mounts = [source, DEPLOY + "/work-profiles.json:/run/work-profiles.json:ro",
              DEPLOY + "/review.yaml:/run/customer-config/review.yaml:ro",
              DEPLOY + "/model-catalog:/run/customer-config/model-catalog:ro"]
    services = document["services"]
    common = {"PYTHONPATH": "/opt/customer/src:/opt/platform/backend",
              "SCITRERA_WORK_PROFILES_FILE": "/run/work-profiles.json",
              **customer.get("environment", {})}
    for role in ("platform", "bridge", "catalog"):
        service = services[role + "-" + tenant]
        service.setdefault("environment", {}).update(common)
        service.setdefault("volumes", []).extend(mounts)
    # The provider only needs the compiled shared work-profile registry.
    provider = services["provider-" + tenant]
    provider.setdefault("environment", {})["SCITRERA_WORK_PROFILES_FILE"] = "/run/work-profiles.json"
    provider.setdefault("volumes", []).append(mounts[1])
    if customer.get("pythonModule"):
        state = "customer-state-" + tenant
        init = "customer-state-init-" + tenant
        image = services["platform-" + tenant]["image"]
        document.setdefault("volumes", {})[state] = {}
        services[init] = {
            "image": image, "user": "0:0", "restart": "no", "network_mode": "none",
            "read_only": True, "cap_drop": ["ALL"], "cap_add": ["CHOWN"],
            "security_opt": ["no-new-privileges:true"], "mem_limit": "128m", "cpus": "0.5",
            "labels": {"platform.scitrera.io/lifecycle": "job"},
            "command": ["python", "-c", "import os; os.chown('/state', 1000, 1000)"],
            "volumes": [state + ":/state"],
        }
        services["customer-worker-" + tenant] = {
            "image": image, "command": ["python", "-m", customer["pythonModule"]],
            "user": "1000:1000", "read_only": True, "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "tmpfs": ["/tmp:size=128m,uid=1000,gid=1000"],
            "restart": "unless-stopped", "stop_grace_period": "60s",
            "networks": ["tenant-" + tenant],
            "environment": {**common, "SCITRERA_TENANT": tenant,
                "AETHER_GATEWAY": "aether-" + tenant + ":50051", "AETHER_TLS_ENABLED": "true",
                "AETHER_TLS_CA_CERT": "/run/tls/ca.crt", "AETHER_TLS_CLIENT_CERT": "/run/tls/tls.crt",
                "AETHER_TLS_CLIENT_KEY": "/run/tls/tls.key", "AETHER_MAX_RECONNECT_ATTEMPTS": "0",
                "LLM_GATEWAY_BASE_URL": "http://llm-gateway.mt:8080/v1/",
                "LLM_GATEWAY_BEARER_TOKEN_FILE": "/run/gateway/token"},
            "volumes": [*mounts, state + ":" + customer.get("statePath", "/var/lib/customer"),
                        INSTALL + "/.local/" + tenant + "/tls/customer-worker:/run/tls:ro",
                        INSTALL + "/.local/" + tenant + "/gateway-token:/run/gateway/token:ro"],
            "depends_on": {init: {"condition": "service_completed_successfully"},
                           "bridge-" + tenant: {"condition": "service_started"}},
        }
        if customer.get("readinessFile"):
            services["customer-worker-" + tenant]["healthcheck"] = {
                "test": ["CMD", "python", "-c", "from pathlib import Path; assert Path(" + repr(customer["readinessFile"]) + ").exists()"],
                "interval": "5s", "timeout": "3s", "retries": 12, "start_period": "60s"}
    if customer.get("provisionModule"):
        provision = copy.deepcopy(services["catalog-" + tenant])
        provision["command"] = ["python", "-m", customer["provisionModule"]]
        provision["environment"].update(customer.get("provisionEnvironment", {}))
        provision["restart"] = "no"
        provision["labels"] = {"platform.scitrera.io/lifecycle": "job"}
        provision["depends_on"] = {"catalog-" + tenant: {"condition": "service_completed_successfully"}}
        services["customer-provision-" + tenant] = provision


def helm(resolved, models):
    customer = resolved["deployment"].get("customer")
    if not customer:
        return {}
    tenant = resolved["deployment"]["tenant"]
    return {
        "enabled": True, "claim": tenant + "-customer-code",
        "revision": resolved["inputDigests"]["customerSource"], "environment": customer.get("environment", {}),
        "workerModule": customer.get("pythonModule", ""), "workerTLSSecret": "customer-worker-tls",
        "statePath": customer.get("statePath", "/var/lib/customer"),
        "readinessFile": customer.get("readinessFile", ""), "stateSize": "10Gi",
        "provisionModule": customer.get("provisionModule", ""),
        "provisionEnvironment": customer.get("provisionEnvironment", {}),
        "config": {"review": resolved["inputs"]["review"], "models": models["records"]},
    }
