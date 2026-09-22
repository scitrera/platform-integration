"""Small reporting-only OpenMeter deployment with explicit process budgets."""
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import copy
import re
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
# Dependencies from the selected upstream quickstart, not the retired SaaS chart.
DEFAULT_IMAGES = {
    "openmeter": "ghcr.io/openmeterio/openmeter:v1.0.0-beta.229@sha256:2bbcd397dba722dcb190e13542f93ec16bb4b6c0a115fb742266c34f153a7e98",
    "clickhouse": "clickhouse/clickhouse-server:25.12.3-alpine@sha256:74da41cd61db84f652c6364fd30d59e19b7276d34f7c82515f5f0e70d6f325da",
    "kafka": "confluentinc/cp-kafka:8.0.3@sha256:db5eac24a1d15a1689fa89642ef97a7e3bc4f55ad5159a07b213c4dc7d0114e3",
    "redis": "redis:7.4-alpine@sha256:520775a41a63e77e06c73e35d2fd9cc15921a609516818796b4ecbb813078bc7",
    "postgres": "postgres:17.11-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73",
}
BUDGETS = {
    "clickhouse": ("256m", "2g", "2"),
    "kafka": ("512m", "1536m", "1"),
    "redis": ("32m", "256m", "0.5"),
    "postgres": ("128m", "512m", "1"),
    "api": ("64m", "512m", "1"),
    "sink": ("64m", "512m", "1"),
}
METERS = ("tokens_in", "tokens_out", "cached_tokens_in", "cache_write_tokens",
          "ocr_pages", "embedding_inputs", "time_seconds", "cpu_time", "gpu_time",
          "ram_time", "storage_gb_seconds", "usage_unknown", "startups")


def openmeter_config():
    return {
        "ingest": {"kafka": {"broker": "metering-kafka:9092", "brokerAddressFamily": "v4",
                             "socketKeepAliveEnable": True, "topicMetadataRefreshInterval": "10s"}},
        "aggregation": {"clickhouse": {"address": "metering-clickhouse:9000", "database": "openmeter",
            "username": "default", "password": "", "maxOpenConns": 4, "maxIdleConns": 2, "blockBufferSize": 2}},
        # PostgreSQL connection fields and passwords use Viper env overrides.
        "postgres": {"url": "", "autoMigrate": "false"},
        "sink": {"minCommitCount": 100, "maxCommitWait": "2s", "namespaceRefetch": "15s",
                 "namespaceRefetchTimeout": "15s",
                 "kafka": {"brokers": "metering-kafka:9092", "brokerAddressFamily": "v4",
                            "socketKeepAliveEnable": True, "topicMetadataRefreshInterval": "10s"},
                 "dedupe": {"enabled": True, "driver": "redis", "config": {
                     "address": "metering-redis:6379", "password": "", "database": 0, "expiration": "768h"}}},
        "portal": {"enabled": False},
        # Omitting Svix yields the upstream no-op webhook handler. No notification,
        # balance or billing worker is started by the reporting installation.
        "meters": [{"slug": name, "eventType": name, "aggregation": "SUM", "valueProperty": "$.qty",
                    "groupBy": {key: "$." + key for key in ("model", "provider", "workspace", "user", "agent", "kind")}}
                   for name in METERS] + [
            {"slug": "active_users", "eventType": "active_user_ping", "aggregation": "UNIQUE_COUNT",
             "valueProperty": "$.user", "groupBy": {"workspace": "$.workspace"}},
            {"slug": "licensed_users", "eventType": "licensed_users", "aggregation": "SUM",
             "valueProperty": "$.qty"}],
    }


def reference(name):
    return "$" + "{" + name + ":?Configure " + name + "}"


def compose_config(*, images=None, production=False, config_prefix="./metering"):
    images = {**DEFAULT_IMAGES, **(images or {})}
    if production and any(not re.search(r"@sha256:[a-f0-9]{64}$", image) for image in images.values()):
        raise ValueError("Production metering images must all use registry digests")
    # Bind-mounted files are replaced atomically. A content label ensures
    # Compose recreates consumers instead of retaining the old mounted inode.
    digest = hashlib.sha256(json.dumps(openmeter_config(), sort_keys=True).encode()
        + (ROOT / "services/metering/clickhouse-limits.xml").read_bytes()
        + (ROOT / "services/metering/clickhouse-users.xml").read_bytes()).hexdigest()
    services = {}
    for role, (reservation, memory, cpu) in BUDGETS.items():
        services["metering-" + role] = {
            "labels": {"platform.scitrera.io/config-digest": digest},
            "image": images["openmeter" if role in {"api", "sink"} else role],
            "restart": "unless-stopped", "networks": ["metering"],
            "mem_reservation": reservation, "mem_limit": memory, "cpus": cpu,
            "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
        }
    services["metering-clickhouse"].update({
        "environment": {"CLICKHOUSE_DB": "openmeter", "CLICKHOUSE_USER": "default",
                        "CLICKHOUSE_PASSWORD": reference("METERING_CLICKHOUSE_PASSWORD"),
                        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT": "1"},
        "volumes": ["metering-clickhouse:/var/lib/clickhouse",
                    config_prefix + "/clickhouse-limits.xml:/etc/clickhouse-server/config.d/limits.xml:ro",
                    config_prefix + "/clickhouse-users.xml:/etc/clickhouse-server/users.d/limits.xml:ro"],
        "ulimits": {"nofile": {"soft": 65536, "hard": 65536}},
        "healthcheck": {"test": ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:8123/ping"], "interval": "5s", "timeout": "3s", "retries": 30}})
    services["metering-kafka"].update({
        "environment": {
            "CLUSTER_ID": reference("METERING_KAFKA_CLUSTER_ID"), "KAFKA_NODE_ID": "1", "KAFKA_BROKER_ID": "1",
            "KAFKA_PROCESS_ROLES": "broker,controller", "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@metering-kafka:9093",
            "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER", "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
            "KAFKA_LISTENERS": "INTERNAL://0.0.0.0:9092,CONTROLLER://0.0.0.0:9093",
            "KAFKA_ADVERTISED_LISTENERS": "INTERNAL://metering-kafka:9092",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
            "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
            "KAFKA_NUM_PARTITIONS": "1", "KAFKA_LOG_RETENTION_HOURS": "168",
            "KAFKA_LOG_RETENTION_BYTES": "4294967296", "KAFKA_LOG_SEGMENT_BYTES": "134217728",
            "KAFKA_NUM_NETWORK_THREADS": "2", "KAFKA_NUM_IO_THREADS": "2",
            "KAFKA_HEAP_OPTS": "-Xms384m -Xmx512m", "KAFKA_JVM_PERFORMANCE_OPTS": "-XX:ActiveProcessorCount=2 -XX:MaxDirectMemorySize=128m",
            "KAFKA_LOG_DIRS": "/var/lib/kafka/data"},
        "volumes": ["metering-kafka:/var/lib/kafka/data"],
        "healthcheck": {"test": ["CMD-SHELL", "kafka-topics --bootstrap-server localhost:9092 --list >/dev/null"],
                        "interval": "10s", "timeout": "10s", "retries": 30}})
    services["metering-redis"].update({
        "environment": {"REDIS_PASSWORD": reference("METERING_REDIS_PASSWORD")},
        "command": ["sh", "-ec", 'exec redis-server --appendonly yes --maxmemory 128mb --maxmemory-policy noeviction --requirepass "$$REDIS_PASSWORD"'],
        "volumes": ["metering-redis:/data"],
        "healthcheck": {"test": ["CMD-SHELL", 'REDISCLI_AUTH="$$REDIS_PASSWORD" redis-cli ping | grep -q PONG'],
                        "interval": "5s", "timeout": "3s", "retries": 30}})
    services["metering-postgres"].update({
        "environment": {"POSTGRES_USER": "openmeter", "POSTGRES_DB": "openmeter",
                        "POSTGRES_PASSWORD": reference("METERING_POSTGRES_PASSWORD")},
        "command": ["postgres", "-c", "shared_buffers=128MB", "-c", "work_mem=4MB", "-c", "max_connections=50"],
        "volumes": ["metering-postgres:/var/lib/postgresql/data"],
        "healthcheck": {"test": ["CMD", "pg_isready", "-U", "openmeter"], "interval": "5s", "timeout": "3s", "retries": 30}})
    for role in ("api", "sink"):
        services["metering-" + role].update({
            "command": ["openmeter" if role == "api" else "openmeter-sink-worker",
                        "--config", "/etc/openmeter/config.yaml", "--telemetry-address", "0.0.0.0:10000"]
                       + (["--address", "0.0.0.0:8888"] if role == "api" else []),
            "environment": {
                "POSTGRES_HOST": "metering-postgres", "POSTGRES_PORT": "5432",
                "POSTGRES_USER": "openmeter", "POSTGRES_DATABASE": "openmeter",
                "POSTGRES_PASSWORD": reference("METERING_POSTGRES_PASSWORD"),
                "POSTGRES_OPTIONS_SSLVERIFY": "disable", "POSTGRES_OPTIONS_POOLMAXCONNS": "5",
                "AGGREGATION_CLICKHOUSE_PASSWORD": reference("METERING_CLICKHOUSE_PASSWORD"),
                "SINK_DEDUPE_CONFIG_PASSWORD": reference("METERING_REDIS_PASSWORD"),
                "GOMEMLIMIT": "384MiB", "GOMAXPROCS": "1"},
            "volumes": [config_prefix + "/openmeter.yaml:/etc/openmeter/config.yaml:ro"],
            "depends_on": {n: {"condition": "service_healthy"} for n in
                          ("metering-clickhouse", "metering-kafka", "metering-postgres", "metering-redis")},
            "healthcheck": {"test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:10000/healthz/ready"],
                            "interval": "5s", "timeout": "3s", "retries": 40}})
    # SQL migrations use lib/pq, which cannot accept pgx's pool_max_conns URL
    # option. Run them once with that option disabled, before the API/sink.
    services["metering-migrate"] = copy.deepcopy(services["metering-api"])
    migration = services["metering-migrate"]
    migration["restart"] = "no"
    migration.pop("healthcheck")
    migration["command"] = ["openmeter-jobs", "--config", "/etc/openmeter/config.yaml", "migrate", "--mode", "migration"]
    migration["environment"]["POSTGRES_OPTIONS_POOLMAXCONNS"] = "0"
    for role in ("api", "sink"):
        services["metering-" + role]["depends_on"]["metering-migrate"] = {"condition": "service_completed_successfully"}
    services["metering-sink"]["depends_on"]["metering-api"] = {"condition": "service_healthy"}
    return {"services": services, "networks": {"metering": {"internal": True}},
            "volumes": {name: {} for name in ("metering-clickhouse", "metering-kafka", "metering-redis", "metering-postgres")}}


def artifacts(**kwargs):
    return {"compose.metering.yaml": compose_config(**kwargs),
            "metering/openmeter.yaml": openmeter_config(),
            "metering/clickhouse-limits.xml": (ROOT / "services/metering/clickhouse-limits.xml").read_text(),
            "metering/clickhouse-users.xml": (ROOT / "services/metering/clickhouse-users.xml").read_text()}
