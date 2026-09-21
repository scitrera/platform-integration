"""Runtime adapters for the validated deployment description.

Rendering is offline and non-secret. Provisioning credentials and application
state are separate operations and cannot be hidden inside a rendering command.
"""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import yaml

from deployment_config import ConfigError, public_summary, _merge
from models import compile_document
from document_services import render as render_documents

ROOT = Path(__file__).resolve().parents[1]
MEMORYLAYER_FLAGS = {
    "post_store_enrichment": "MEMORYLAYER_POST_STORE_ENRICHMENT_ENABLED",
    "fact_decomposition": "MEMORYLAYER_FACT_DECOMPOSITION_ENABLED",
    "semantic_tiering": "MEMORYLAYER_SEMANTIC_TIERING_ENABLED",
    "relationship_classification": "MEMORYLAYER_ASSOCIATION_LLM_CLASSIFY_ENABLED",
}


def components(resolved):
    config, inputs = resolved["deployment"], resolved["inputs"]
    models = compile_document(inputs["models"], config["tenant"], references_only=True)
    document_config = copy.deepcopy(inputs["documentServices"])
    image = resolved["bindings"].get("images", {}).get("documentProxy")
    if image:
        document_config.setdefault("proxy", {})["image"] = image
    # The document profile owns the embedding dimension; a live database change
    # remains an explicit migration, never a side effect of config rendering.
    documents = render_documents(document_config, config["tenant"], 1920,
                                 allow_local_image=config["development"])
    flags = inputs["memorylayer"]
    if not isinstance(flags, dict) or set(flags) != set(MEMORYLAYER_FLAGS) or any(type(v) is not bool for v in flags.values()):
        raise ConfigError("Invalid MemoryLayer enrichment settings")
    memory = {MEMORYLAYER_FLAGS[k]: str(v).lower() for k, v in flags.items()}
    profiles = copy.deepcopy(inputs["workProfiles"])
    profiles.setdefault("tenants", {})[config["tenant"]] = {
        "allowed_profiles": list(profiles.get("profiles", {}))}
    return models, documents, memory, profiles


def resource_overlay(resolved):
    tenant = resolved["deployment"]["tenant"]
    services = {}
    names = {"worker": f"customer-worker-{tenant}", "gateway": "gateway", "web": "web",
             "blobgw": "blobgw", "edge": "storage-edge", "document-proxy": f"embed-proxy-{tenant}",
             "database": f"tenant-postgres-{tenant}"}
    for role, resources in resolved["deployment"]["resources"].items():
        name = names.get(role, role + "-" + tenant)
        cpu = str(resources["limits"]["cpu"])
        cpu = str(float(cpu[:-1]) / 1000) if cpu.endswith("m") else cpu
        memory = str(resources["limits"]["memory"]).replace("Gi", "g").replace("Mi", "m").replace("Ki", "k")
        reserve = str(resources["requests"]["memory"]).replace("Gi", "g").replace("Mi", "m").replace("Ki", "k")
        services[name] = {"mem_limit": memory, "mem_reservation": reserve, "cpus": cpu}
    return {"services": services}


def compose(resolved):
    """Return policy overlays and referenced provisioning inputs.

    The base installer owns certificate/credential generation and persistent
    volume identity. A consolidated database is only supported on a new
    installation or after explicit migration.
    """
    config, bindings = resolved["deployment"], resolved["bindings"]
    tenant = config["tenant"]
    models, (doc_compose, _, doc_manifest), memory, profiles = components(resolved)
    result = copy.deepcopy(doc_compose)
    memory.update({"MEMORYLAYER_BLOB_STORAGE_SERVICE": "blobgw", "MEMORYLAYER_BLOBGW_URL": "http://blobgw:8080",
                   "MEMORYLAYER_BLOBGW_DOMAIN": tenant, "MEMORYLAYER_BLOBGW_EDGE_URL": "http://storage-edge:8090"})
    result = _merge(result, {"services": {f"memorylayer-{tenant}": {"environment": memory}}})
    if config["auth"]["mode"] == "local":
        result = _merge(result, {"services": {"auth": {"environment": {
            "SCITRERA_AUTH_LOGIN_DEFAULT_TENANT": tenant,
            "AUTH_PROXY_SESSION_COOKIE_DOMAIN": config["auth"].get("cookieDomain", ""),
            "AUTH_PROXY_SESSION_TTL": config["auth"].get("sessionTTL", "24h"),
            "AUTH_PROXY_SESSION_COOKIE_SECURE": str(not config["development"]).lower(),
            "SCITRERA_AUTH_ALLOWED_REDIRECT_ORIGINS": config["public"]["origin"],
        }}}})
    if config["auth"]["protectAllPaths"]:
        verify = bindings.get("auth", {}).get("verifyURL")
        image = bindings.get("images", {}).get("authGate")
        if not verify or not image:
            raise ConfigError("Host-wide auth requires bindings.auth.verifyURL and images.authGate")
        result["services"]["auth-gate"] = {
            "image": image, "networks": ["gateway"], "restart": "unless-stopped",
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "mem_limit": "128m", "cpus": "1",
            "environment": {"AUTH_GATE_VERIFY_URL": verify, "AUTH_GATE_PUBLIC_ORIGIN": config["public"]["origin"],
                            "AUTH_GATE_LOGIN_ORIGIN": config["auth"]["origin"], "AUTH_GATE_TENANT": tenant,
                            "AUTH_GATE_WORKSPACE": config["auth"]["workspace"]},
        }
    from deployment_compose import build
    artifacts = build(resolved, result, resource_overlay(resolved))
    from deployment_customer import artifacts as customer_artifacts
    return {
        **artifacts,
        **customer_artifacts(resolved, models),
        "compose.policy.yaml": result,
        "compose.resources.yaml": resource_overlay(resolved),
        "models.catalog.json": models["records"],
        "credential-references.json": {"models": {"file": models["credential_file"], "sources": models["credential_sources"]}},
        "work-profiles.json": profiles,
        "document-services.manifest.json": doc_manifest,
        "deployment.manifest.json": public_summary(resolved),
    }


def _images(bindings, names, production):
    images = {}
    for name in names:
        image = bindings.get("images", {}).get(name)
        if not image:
            raise ConfigError("Missing image binding: " + name)
        if production and not re.fullmatch(r"\S+@sha256:[a-f0-9]{64}", image):
            raise ConfigError("Production image binding requires a registry digest: " + name)
        images[name] = image
    return images


def helm(resolved):
    config, bindings, inputs = resolved["deployment"], resolved["bindings"], resolved["inputs"]
    if any(v != "tenant" for v in config["services"].values()):
        raise ConfigError("This renderer currently requires tenant-owned frontend, storage and SparkRoute")
    models, (_, docs, doc_manifest), memory, profiles = components(resolved)
    if docs is None:
        raise ConfigError("Kubernetes requires a published documentProxy image binding")
    tenant = config["tenant"]
    k = bindings.get("kubernetes", {})
    required = {"namespace", "sharedNamespace", "storageClass", "gatewayName", "gatewayNamespace"}
    if not required.issubset(k):
        raise ConfigError("Kubernetes namespace, shared namespace, storage class and Gateway bindings required")
    if not config["development"] and (not k.get("dnsResolver") or not k.get("apiEgress")):
        raise ConfigError("Production requires explicit Kubernetes DNS resolver and API egress bindings")
    ns, shared = k["namespace"], k["sharedNamespace"]
    serving, storage = tenant + "-serving", tenant + "-storage"
    origin = config["public"]["origin"]
    resources = config["resources"]
    common = {"development": config["development"], "bootstrapRevision": resolved["configDigest"][:20],
              "storage": {"className": k["storageClass"], "size": config["database"]["storage"]},
              "nodeSelector": k.get("nodeSelector", {}), "tolerations": k.get("tolerations", []),
              "roleResources": resources, "databaseResources": resources.get("database", {})}
    consolidated = config["database"]["mode"] == "consolidated"
    if config["database"]["mode"] == "external":
        raise ConfigError("External databases require explicit connection validation before rendering")
    verify = bindings.get("auth", {}).get("verifyURL")
    external = bindings.get("auth", {}).get("statusURL")
    if not verify or not external:
        raise ConfigError("Kubernetes requires private auth verifyURL and statusURL bindings")
    hosts = sorted({urlsplit(m["base_url"]).hostname for m in inputs["models"].get("models", {}).values()})
    sv = _merge(common, {
        "tenant": tenant, "phase": 4, "components": {"auth": config["auth"]["mode"] == "local", "gateway": True, "web": True},
        "managedDatabases": {"auth": True, "gateway": not consolidated},
        "publicOrigin": origin, "adminOrigin": config["auth"]["origin"], "hostname": urlsplit(origin).hostname,
        "dnsResolver": ("[" + k["dnsResolver"] + "]" if ":" in k.get("dnsResolver", "") else k.get("dnsResolver", "10.96.0.10")),
        "tlsSecret": k.get("tlsSecret", ""), "storageNamespace": ns,
        "tenants": [{"id": tenant, "release": tenant, "namespace": ns, "toolsWssEnabled": config["toolsWssEnabled"]}],
        "storageEdgeEndpoint": f"http://{storage}-edge.{ns}.svc.cluster.local:8090",
        "authEndpoint": {"verify": verify, "external": external},
        "externalAuthNamespace": shared,
        "authSession": {"cookieDomain": config["auth"].get("cookieDomain", ""), "defaultTenant": tenant,
                        "ttl": config["auth"].get("sessionTTL", "24h"),
                        "allowedOrigins": [origin]},
        "images": _images(bindings, ["auth", "web", "sparkroute", "postgres", "valkey"], not config["development"]),
        "ingress": {"enabled": k.get("ingressEnabled", True), "createGateway": False, "parentName": k["gatewayName"], "parentNamespace": k["gatewayNamespace"],
                    "sectionName": k.get("gatewayListener", "https"),
                    "hostAuth": {"enabled": config["auth"]["protectAllPaths"], "tenant": tenant,
                                 "workspace": config["auth"]["workspace"], "loginOrigin": config["auth"]["origin"],
                                 "verifyURL": verify, "image": bindings.get("images", {}).get("authGate", "")}},
        "modelCatalog": {"allowedProviderHosts": hosts, "allowHTTP": False, "apiEgress": k.get("apiEgress", []),
                         "addressTemplate": f"{{tenant}}-aether.{ns}.svc.cluster.local:50051",
                         "serverNameTemplate": "{tenant}-aether",
                         "credentialSecretNames": ["aether-sparkroute-creds-" + tenant]},
    })
    tv = _merge(common, {"tenant": tenant, "phase": 4, "managedDatabases": not consolidated,
                        "toolsWssEnabled": config["toolsWssEnabled"],
                        "sharedNamespace": ns, "storageNamespace": ns, "storageRelease": storage, "publicOrigin": origin,
                        "seedDevelopmentAdmin": False, "adminEmail": next(t["email"] for t in inputs["tenants"] if t["slug"] == tenant),
                        "images": _images(bindings, ["backend", "aether", "memorylayer", "connectors", "provider",
                                                    "sahara", "sidecar", "code", "mlPostgres", "postgres", "skills", "sparkroute"]
                                                   + (["tools"] if config["toolsWssEnabled"] else [])
                                                   + (["platform"] if "platform" in bindings["images"] else []),
                                          not config["development"]),
                        "workProfiles": {"profiles": profiles["profiles"], "allowedProfiles": list(profiles["profiles"])},
                        "modelCatalog": {"records": models["records"]},
                        "documentBlobStorage": {"provider": "blobgw"},
                        "blobFetchBaseURL": f"http://{storage}-download.{ns}.svc.cluster.local:8080",
                        "quota": {"enabled": False}})
    from deployment_customer import helm as customer_helm
    tv["extension"] = customer_helm(resolved, models) or {"enabled": False}
    tv["gatewayEndpoint"] = f"http://{serving}-gateway.{ns}.svc.cluster.local:8080/v1/"
    tv = _merge(tv, docs)
    tv["documentServices"]["environment"].update(memory)
    st = _merge(common, {"phase": 2, "managedDatabases": not consolidated, "sharedNamespace": ns, "tenantNamespaces": [ns],
                        "images": _images(bindings, ["postgres", "edge", "blobgw", "web"], not config["development"]),
                        "sourceDocumentEndpoints": {tenant: f"http://{tenant}-memorylayer.{ns}.svc.cluster.local:8000"},
                        "stagingBucket": bindings.get("objectStorage", {}).get("bucket", "staging")})
    s3 = bindings.get("objectStorage", {})
    if config["objectStorage"]["mode"] == "s3":
        if not s3.get("endpoint") or not s3.get("bucket"):
            raise ConfigError("External storage requires endpoint and bucket bindings")
        mode = s3.get("credentialMode", "static")
        if mode not in {"static", "aws-sts"}:
            raise ConfigError("Unsupported object storage credentialMode")
        if mode == "aws-sts" and (not s3.get("roleARN") or not s3.get("serviceAccountAnnotations")):
            raise ConfigError("AWS storage requires roleARN and serviceAccountAnnotations")
        st["credentialMode"] = mode
        st["serviceAccountAnnotations"] = s3.get("serviceAccountAnnotations", {})
        upstream = urlsplit(s3["endpoint"])
        sv["uploadProxy"] = {"enabled": True, "endpoint": s3["endpoint"], "signedHost": upstream.netloc}
        st["uploadProxy"] = copy.deepcopy(sv["uploadProxy"])
        tv["blobUploadBaseURL"] = origin + "/storage/" + tenant + "/uploads/"
    tv["roleEnvironment"] = {"bridge": {"SANDBOX_BLOB_FETCH_BASE_URL": tv["blobFetchBaseURL"]}}
    if config["billing"]["mode"] != "off":
        metering = bindings.get("metering", {})
        if not metering.get("endpoint") or not metering.get("credentialSecret"):
            raise ConfigError("Usage reporting requires private endpoint and credentialSecret bindings")
        from deployment_metrics import environment as metrics_environment
        tv["metricsBridge"] = {"enabled": True, "namespace": shared,
            "image": bindings["images"].get("metricsBridge", bindings["images"]["backend"]),
            "environment": metrics_environment(tenant, metering.get("openmeterEndpoint",
                f"http://metering-api.{shared}.svc.cluster.local:8888"))}
        tv["usageReporting"] = {"enabled": True, "endpoint": metering["endpoint"],
            "credentialSecret": metering["credentialSecret"], "namespace": shared,
            "databaseCluster": tenant + "-postgres-db" if consolidated else serving + "-gateway-db"}
    results = {"helm/serving.yaml": sv, "helm/tenant.yaml": tv, "helm/storage.yaml": st,
               "models.catalog.json": models["records"], "work-profiles.json": profiles,
               "credential-references.json": {"models": {"file": models["credential_file"], "sources": models["credential_sources"]}},
               "document-services.manifest.json": doc_manifest, "deployment.manifest.json": public_summary(resolved)}
    if consolidated:
        backup = bindings.get("backup", {})
        enabled = config["backup"]["enabled"]
        if enabled and not backup.get("bucket"):
            raise ConfigError("Enabled backups require an off-node bucket binding")
        secrets = bindings.get("secrets", {})
        results["helm/postgres.yaml"] = {
            "development": config["development"], "image": tv["images"]["mlPostgres"], "instances": 1,
            "storage": common["storage"], "resources": resources.get("database", {}),
            "nodeSelector": common["nodeSelector"], "tolerations": common["tolerations"],
            "serviceAccountAnnotations": backup.get("serviceAccountAnnotations", {}),
            "maxConnections": config["database"]["maxConnections"], "memorylayerExtensions": True,
            "databases": [{"name": db, "secret": secrets.get(db + "Database", {"memorylayer": "memorylayer-db", "dataconnectors": "connectors-db", "sparkroute": "gateway-db", "storage": "storage-db"}[db])}
                          for db in config["database"]["databases"]],
            "backup": {"enabled": enabled, "schedule": "0 " + config["backup"]["schedule"],
                       "retentionDays": config["backup"]["retentionDays"],
                       "destinationPath": "s3://" + backup.get("bucket", "") + "/" + backup.get("prefix", tenant),
                       "endpointURL": backup.get("endpoint", ""), "region": backup.get("region", ""),
                       "credentialsSecret": backup.get("credentialsSecret", ""),
                       "inheritFromIAMRole": bool(backup.get("serviceAccountAnnotations"))},
        }
    from deployment_customer import artifacts as customer_artifacts
    results.update(customer_artifacts(resolved, models))
    return results
