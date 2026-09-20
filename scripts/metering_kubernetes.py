"""Kubernetes manifests from the same bounded metering configuration as Compose."""
# SPDX-License-Identifier: AGPL-3.0-only
import copy
import re
import yaml
from metering_config import BUDGETS, artifacts, compose_config, openmeter_config


def quantity(value):
    return value[:-1] + {"m": "Mi", "g": "Gi"}[value[-1]]


def render(*, namespace, storage_class, postgres_host, postgres_secret,
           credentials_secret, node_selector=None, tolerations=None, images=None,
           production=True, storage_sizes=None):
    document = compose_config(images=images, production=production)
    node_selector, tolerations = node_selector or {}, tolerations or []
    labels = {"app.kubernetes.io/instance": "metering", "platform.scitrera.io/trust": "service"}
    settings = artifacts(images=images, production=production)
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "metering-config", "namespace": namespace},
          "data": {"openmeter.yaml": yaml.safe_dump(openmeter_config(), sort_keys=False),
                   "clickhouse-limits.xml": settings["metering/clickhouse-limits.xml"],
                   "clickhouse-users.xml": settings["metering/clickhouse-users.xml"]}}
    result = [cm]
    storage_sizes = {**{"clickhouse": "20Gi", "kafka": "10Gi", "redis": "2Gi"}, **(storage_sizes or {})}
    for role, size in storage_sizes.items():
        if role not in {"clickhouse", "kafka", "redis"} or not re.fullmatch(r"[1-9][0-9]*(Gi|Mi)", size):
            raise ValueError("Invalid metering persistent storage")
        result.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {
            "name": "metering-" + role, "namespace": namespace,
            "annotations": {"helm.sh/resource-policy": "keep"}}, "spec": {
                "accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
                "resources": {"requests": {"storage": size}}}})
    secret_keys = {
        "METERING_CLICKHOUSE_PASSWORD": (credentials_secret, "clickhousePassword"),
        "METERING_REDIS_PASSWORD": (credentials_secret, "redisPassword"),
        "METERING_KAFKA_CLUSTER_ID": (credentials_secret, "kafkaClusterID"),
        "METERING_POSTGRES_PASSWORD": (postgres_secret, "password"),
    }
    ports = {"clickhouse": 9000, "kafka": 9092, "redis": 6379, "api": 8888}
    for name, service in document["services"].items():
        role = name.removeprefix("metering-")
        if role == "postgres":
            continue  # Independent CNPG instance, supplied by infrastructure bindings.
        component_labels = {**labels, "app.kubernetes.io/component": name}
        env = []
        for key, value in service.get("environment", {}).items():
            match = re.fullmatch(r"\$\{([A-Z0-9_]+):\?[^}]+\}", value)
            if match:
                secret, field = secret_keys[match[1]]
                env.append({"name": key, "valueFrom": {"secretKeyRef": {"name": secret, "key": field}}})
            else:
                env.append({"name": key, "value": postgres_host if key == "POSTGRES_HOST" else value})
        reservation, limit, cpu = BUDGETS.get(role, BUDGETS["api"])
        container = {"name": role, "image": service["image"], "env": env,
                     "resources": {"requests": {"cpu": "100m", "memory": quantity(reservation)},
                                   "limits": {"cpu": cpu, "memory": quantity(limit)}},
                     "securityContext": {"allowPrivilegeEscalation": False,
                                         "capabilities": {"drop": ["ALL"]}}}
        # These upstream entrypoints initialize owned files and then drop root.
        # No privileged containers, host mounts, Docker socket or host networking.
        if role in {"clickhouse", "redis"}:
            container["securityContext"]["capabilities"]["add"] = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"]
        if "command" in service:
            container["args"] = [arg.replace("$$", "$") for arg in service["command"]]
        volumes, mounts = [], []
        for index, volume in enumerate(service.get("volumes", [])):
            source, target, *flags = volume.split(":")
            volume_name = "volume-" + str(index)
            if source.startswith("./metering/"):
                key = source.rsplit("/", 1)[1]
                volumes.append({"name": volume_name, "configMap": {"name": "metering-config"}})
                mounts.append({"name": volume_name, "mountPath": target, "subPath": key, "readOnly": True})
            else:
                volumes.append({"name": volume_name, "persistentVolumeClaim": {"claimName": source}})
                mounts.append({"name": volume_name, "mountPath": target})
        container["volumeMounts"] = mounts
        if role != "migrate":
            check = service["healthcheck"]["test"]
            probe = {"exec": {"command": (["sh", "-ec", check[1].replace("$$", "$")]
                                         if check[0] == "CMD-SHELL" else check[1:])},
                     "periodSeconds": 10, "timeoutSeconds": 10}
            container["readinessProbe"] = probe
            container["startupProbe"] = {**copy.deepcopy(probe), "failureThreshold": 60}
        template = {"metadata": {"labels": component_labels,
                                "annotations": service.get("labels", {})},
                    "spec": {"nodeSelector": node_selector, "tolerations": tolerations,
                             "automountServiceAccountToken": False,
                             "terminationGracePeriodSeconds": 60,
                             "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                             "containers": [container], "volumes": volumes}}
        if role == "kafka":
            template["spec"]["securityContext"]["fsGroup"] = 1000
        if role == "migrate":
            template["spec"]["restartPolicy"] = "Never"
            result.append({"apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": name + "-" + service["labels"]["platform.scitrera.io/config-digest"][:12], "namespace": namespace},
                "spec": {"backoffLimit": 3, "activeDeadlineSeconds": 600, "template": template}})
        else:
            result.append({"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                         "selector": {"matchLabels": component_labels}, "template": template}})
        if role in ports:
            result.append({"apiVersion": "v1", "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"selector": component_labels,
                         "ports": [{"name": "tcp", "port": ports[role], "targetPort": ports[role]}]
                            + ([{"name": "controller", "port": 9093, "targetPort": 9093}] if role == "kafka" else [])}})
    result.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "metering-isolation", "namespace": namespace},
        "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "metering"}},
                 "policyTypes": ["Ingress", "Egress"],
                 "ingress": [{"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "metering"}}}]},
                             {"from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "usage-reporting"}}}],
                              "ports": [{"protocol": "TCP", "port": 8888}]}],
                 "egress": [{"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "metering"}}}]},
                            {"to": [{"podSelector": {"matchLabels": {"cnpg.io/cluster": postgres_host.removesuffix("-rw")}}}],
                             "ports": [{"protocol": "TCP", "port": 5432}]},
                            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                      "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                             "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}})
    return result


def reporting(*, namespace, backend_image, environment_secret, producer_secret,
              journal_cluster, node_selector=None, tolerations=None):
    if not re.fullmatch(r"\S+@sha256:[a-f0-9]{64}", backend_image):
        raise ValueError("Reporting backend image requires a registry digest")
    labels = {"app.kubernetes.io/component": "usage-reporting", "platform.scitrera.io/trust": "service"}
    container = {
        "name": "reporting", "image": backend_image, "command": ["python", "-m", "x_usage_reporting"],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "512Mi"}},
        "env": [{"name": "BILLING_PRODUCERS_FILE", "value": "/run/usage/producers.json"},
                {"name": "OPENMETER_API_URL", "value": "http://metering-api:8888"}],
        "envFrom": [{"secretRef": {"name": environment_secret}}],
        "volumeMounts": [{"name": "producers", "mountPath": "/run/usage", "readOnly": True},
                         {"name": "tmp", "mountPath": "/tmp"}],
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10},
        "startupProbe": {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 5, "failureThreshold": 60},
    }
    return [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "usage-reporting", "namespace": namespace},
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                  "template": {"metadata": {"labels": labels}, "spec": {
                      "nodeSelector": node_selector or {}, "tolerations": tolerations or [],
                      "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 60,
                      "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                                          "seccompProfile": {"type": "RuntimeDefault"}},
                      "containers": [container], "volumes": [
                          {"name": "producers", "secret": {"secretName": producer_secret}},
                          {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "usage-reporting", "namespace": namespace},
         "spec": {"selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": 8080}]}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
         "metadata": {"name": "usage-reporting-isolation", "namespace": namespace},
         "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress", "Egress"],
                  "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"platform.scitrera.io/tenant": "true"}},
                                          "podSelector": {"matchLabels": {"app.kubernetes.io/component": "usage-collector"}}}],
                               "ports": [{"protocol": "TCP", "port": 8080}]}],
                  "egress": [{"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "metering-api"}}}],
                              "ports": [{"protocol": "TCP", "port": 8888}]},
                             {"to": [{"podSelector": {"matchLabels": {"cnpg.io/cluster": journal_cluster}}}],
                              "ports": [{"protocol": "TCP", "port": 5432}]},
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                       "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                              "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}}
    ]
