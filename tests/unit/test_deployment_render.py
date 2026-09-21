# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import subprocess
import unittest
import yaml
import test_deployment_config
import test_document_services
from deployment_config import resolve
from deployment_render import compose, helm, ROOT, MEMORYLAYER_FLAGS
from deployment import write_artifacts


class DeploymentRenderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_deployment_config.DeploymentConfigTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.config["backup"]["walArchive"] = False
        inputs = {
            "documentServices": test_document_services.DocumentServicesTests().config(),
            "models": {"version": 1, "models": {"review": {"base_url": "https://models.example.test/v1",
                       "model": "served-model", "api_key_env": "EXAMPLE_API_KEY", "max_concurrency": 8}},
                       "routes": {"sahara-default": "review", "memorylayer-default": "review"}},
            "memorylayer": {key: False for key in MEMORYLAYER_FLAGS},
            "workProfiles": {"profiles": {"document-review": {"instructions": "Review permitted documents.", "max_concurrent_turns": 8}}},
        }
        for name, document in inputs.items():
            (self.root / (name + ".json")).write_text(json.dumps(document))
        names = ["auth", "web", "sparkroute", "postgres", "valkey", "backend", "aether", "memorylayer",
                 "connectors", "provider", "sahara", "sidecar", "code", "mlPostgres", "skills", "tools",
                 "edge", "blobgw", "authGate", "postgresBackup"]
        self.bindings = {
            "kubernetes": {"namespace": "tenant-example", "sharedNamespace": "platform-shared",
                "dnsResolver": "10.100.0.10", "apiEgress": [{"cidr": "10.100.0.1/32", "port": 443}],
                "storageClass": "synthetic", "gatewayName": "public", "gatewayNamespace": "envoy-gateway-system",
                "nodeSelector": {"platform.scitrera.io/pool": "example"}},
            "images": {key: "registry.example/" + key.lower() + "@sha256:" + "1"*64 for key in names},
            "metering": {"endpoint": "http://usage-reporting.platform-shared:8080", "credentialSecret": "usage-producer"},
            "auth": {"verifyURL": "http://shared-auth.platform-shared:8080/auth/verify",
                     "statusURL": "http://shared-auth.platform-shared:8081"},
            "objectStorage": {"endpoint": "https://objects.example.test", "region": "example", "bucket": "example-objects"},
            "backup": {"bucket": "example-backups", "prefix": "example", "credentialsSecret": "backup-credentials"},
        }

    def resolved(self):
        return self.fixture.run_config(self.bindings)

    def test_browser_session_ttl_reaches_compose_and_helm(self):
        self.fixture.config["auth"].update(mode="local", sessionTTL="8h")
        resolved = self.resolved()
        self.assertEqual(compose(resolved)["compose.yaml"]["services"]["auth"]
                         ["environment"]["AUTH_PROXY_SESSION_TTL"], "8h")
        self.assertEqual(helm(resolved)["helm/serving.yaml"]["authSession"]["ttl"], "8h")
        for bad in ("0h", "-1h", "1d", True, "8761h"):
            self.fixture.config["auth"]["sessionTTL"] = bad
            with self.subTest(ttl=bad), self.assertRaisesRegex(ValueError, "auth.sessionTTL"):
                self.resolved()

    def test_same_policy_and_no_secret_resolution(self):
        resolved = self.resolved()
        docker, kube = compose(resolved), helm(resolved)
        for name in ("deployment.manifest.json", "models.catalog.json", "credential-references.json", "work-profiles.json"):
            self.assertEqual(docker[name], kube[name])
        text = json.dumps(kube)
        self.assertIn("EXAMPLE_API_KEY", text)
        self.assertNotIn('"credentials": {', text)
        self.assertEqual(docker["compose.yaml"]["services"]["memorylayer-example"]["environment"]["MEMORYLAYER_EMBEDDING_DIMENSIONS"], "1920")
        self.assertEqual(kube["helm/tenant.yaml"]["embeddingDimensions"], 1920)
        self.assertEqual(kube["helm/tenant.yaml"]["documentServices"]["environment"]["MEMORYLAYER_FACT_DECOMPOSITION_ENABLED"], "false")

    def test_compose_consolidation_and_auth(self):
        outputs = compose(self.resolved())
        services = outputs["compose.yaml"]["services"]
        databases = [name for name, service in services.items() if "POSTGRES_DB" in service.get("environment", {})]
        self.assertEqual(databases, ["tenant-postgres-example"])
        self.assertNotIn("auth", services)
        self.assertNotIn("objects", services)
        self.assertNotIn("ports", services["auth-gate"])
        self.assertIn("auth_request /_host_auth;", outputs["nginx.conf"])
        self.assertIn("location @auth_denied", outputs["nginx.conf"])
        self.assertIn("tenant-postgres-example", services["gateway"]["environment"]["SPARKROUTE_POSTGRES_URL"])
        self.assertEqual(services["backup-tenant-postgres-example"]["environment"]["BACKUP_DATABASES"],
                         "memorylayer,dataconnectors,sparkroute,storage")
        self.assertNotIn("fixture-idp", services)

    def test_chart_composition_has_one_database_and_host_wide_gate(self):
        outputs = helm(self.resolved())
        outputs["helm/tenant.yaml"]["modelCatalog"]["records"]["model-v4.1-flash"] = {}
        objects = []
        for role, chart in (("serving", "shared"), ("tenant", "tenant"), ("storage", "storage"), ("postgres", "postgres")):
            path = self.root / (role + ".yaml")
            path.write_text(yaml.safe_dump(outputs["helm/" + role + ".yaml"]))
            output = subprocess.check_output(["helm", "template", "example-" + role,
                str(ROOT / "charts" / ("platform-" + chart)), "-n", "tenant-example",
                "-f", str(path), "--kube-version", "1.34.0"], text=True, stderr=subprocess.PIPE)
            objects.extend(obj for obj in yaml.safe_load_all(output) if obj)
        bootstrap = next(obj for obj in objects if obj["kind"] == "ConfigMap" and "memorylayer_migrate.py" in obj.get("data", {}))
        for script in ("memorylayer_migrate.py", "tenant_setup.py"):
            self.assertEqual(bootstrap["data"][script], (ROOT / "scripts" / script).read_text())
        self.assertEqual(len([obj for obj in objects if obj["kind"] == "Cluster"]), 1)
        self.assertEqual(len([obj for obj in objects if obj["kind"] == "Database"]), 3)
        self.assertFalse(any(obj["kind"] == "Secret" for obj in objects))
        self.assertFalse(any(obj["kind"] == "Deployment" and obj["metadata"]["name"].endswith("-auth") for obj in objects))
        route = next(obj for obj in objects if obj["kind"] == "HTTPRoute")
        self.assertEqual(route["spec"]["rules"][0]["matches"][0]["path"]["value"], "/")
        policy = next(obj for obj in objects if obj["kind"] == "SecurityPolicy")
        self.assertFalse(policy["spec"]["extAuth"]["failOpen"])
        for obj in objects:
            if obj["kind"] == "Deployment":
                self.assertEqual(obj["spec"]["template"]["spec"]["nodeSelector"], {"platform.scitrera.io/pool": "example"})
        provider = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"].endswith("-provider"))
        env = {v["name"]: v.get("value") for v in provider["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertIn("SANDBOX_BLOB_FETCH_BASE_URL", env)
        self.assertIn("SANDBOX_K8S_NODE_SELECTOR", env)

    def test_repeat_render_does_not_rewrite_files(self):
        output = self.root / "output"
        artifacts = compose(self.resolved())
        self.assertTrue(write_artifacts(output, artifacts))
        mtimes = {p: p.stat().st_mtime_ns for p in output.rglob("*") if p.is_file()}
        self.assertEqual(write_artifacts(output, compose(self.resolved())), [])
        self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in mtimes})

    def test_cluster_network_and_staged_ingress(self):
        self.bindings["kubernetes"]["ingressEnabled"] = False
        serving = helm(self.resolved())["helm/serving.yaml"]
        self.assertEqual(serving["dnsResolver"], "10.100.0.10")
        self.assertEqual(serving["modelCatalog"]["addressTemplate"].count("{tenant}"), 1)
        self.assertEqual(serving["modelCatalog"]["serverNameTemplate"], "{tenant}-aether")
        self.assertEqual(serving["modelCatalog"]["apiEgress"], [{"cidr": "10.100.0.1/32", "port": 443}])
        self.assertFalse(serving["ingress"]["enabled"])
        self.assertTrue(serving["ingress"]["hostAuth"]["enabled"])
        del self.bindings["kubernetes"]["dnsResolver"]
        with self.assertRaisesRegex(ValueError, "DNS resolver"):
            helm(self.resolved())
