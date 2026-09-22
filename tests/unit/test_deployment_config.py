# SPDX-License-Identifier: AGPL-3.0-only
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from deployment_config import ConfigError, resolve


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        inputs = {
            "tenants": [{"slug": "example", "name": "Example", "email": "admin@example.test", "workspace": "default"}],
            "auth": {"tenant": {"slug": "example"}},
            "models": {"version": 1, "models": {}, "routes": {}},
            "documentServices": {}, "memorylayer": {}, "workProfiles": {}, "review": {},
        }
        for name, value in inputs.items():
            (self.root / (name + ".json")).write_text(json.dumps(value))
        self.config = {
            "schemaVersion": 1, "tenant": "example", "inputs": {k: k + ".json" for k in inputs},
            "services": {k: "tenant" for k in ("frontend", "storage", "sparkroute")},
            "database": {"mode": "consolidated", "databases": ["memorylayer", "dataconnectors", "sparkroute", "storage"], "storage": "10Gi", "maxConnections": 100},
            "auth": {"mode": "shared", "origin": "https://auth.example.test", "tenant": "example",
                     "workspace": "auth-app", "protectAllPaths": True, "cookieDomain": "example.test"},
            "public": {"origin": "https://app.example.test", "applicationPath": "/example"},
            "resources": {"database": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}},
            "backup": {"enabled": True, "schedule": "0 3 * * *", "retentionDays": 30, "walArchive": True},
            "billing": {"mode": "reporting"}, "development": False,
            "objectStorage": {"mode": "s3"}, "profiles": {"production": {}},
            "secretFiles": {"models": "models.env"},
        }

    def run_config(self, bindings=None):
        path = self.root / "deployment.yaml"
        path.write_text(yaml.safe_dump(self.config))
        binding_path = None
        if bindings is not None:
            binding_path = self.root / "bindings.yaml"
            binding_path.write_text(yaml.safe_dump(bindings))
        return resolve(path, profile="production", bindings_path=binding_path)

    def test_usage_attribution_boundary_requires_a_stable_zoned_timestamp(self):
        for bad in ("", "2026-09-20", "tomorrow", True, 42):
            self.config["billing"]["attributionFrom"] = bad
            with self.subTest(value=bad), self.assertRaisesRegex(ConfigError, "attributionFrom"):
                self.run_config()
        self.config["billing"]["attributionFrom"] = "2026-09-20T00:00:00Z"
        self.assertEqual(self.run_config(), self.run_config())

    def test_deterministic_without_reading_secrets(self):
        first = self.run_config()
        (self.root / "models.env").write_text("PRIVATE=never-render-this")
        self.assertEqual(first, self.run_config())
        self.assertNotIn("never-render-this", json.dumps(first))

    def test_component_change_changes_digest(self):
        first = self.run_config()
        (self.root / "models.json").write_text('{"routes":{"agent":"new-model"}}')
        second = self.run_config()
        self.assertNotEqual(first["configDigest"], second["configDigest"])
        self.assertNotEqual(first["inputDigests"]["models"], second["inputDigests"]["models"])

    def test_profile_cannot_change_identity(self):
        self.config["profiles"]["production"]["tenant"] = "other"
        with self.assertRaises(ConfigError):
            self.run_config()

    def test_infrastructure_cannot_override_customer_policy(self):
        for value in ({"tenant": "other"}, {"models": {}}, {"auth": {"tenant": "other"}}, {"public": {"origin": "http://attacker.test"}}):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.run_config(value)

    def test_no_literal_secrets_or_credentials_in_url(self):
        for value in ({"auth": {"verifyURL": "http://user:private@auth:8080/verify"}},
                      {"backup": {"endpoint": "https://storage.test?token=private"}},
                      {"secrets": {"password": "private"}}):
            with self.subTest(value=value), self.assertRaises(ConfigError) as error:
                self.run_config(value)
            self.assertNotIn("private", str(error.exception))

    def test_component_tenant_mismatch(self):
        (self.root / "auth.json").write_text('{"tenant":{"slug":"other"}}')
        with self.assertRaises(ConfigError):
            self.run_config()

    def test_production_is_fail_closed(self):
        original = copy.deepcopy(self.config)
        for field, key, value in (("auth", "protectAllPaths", False), ("auth", "cookieDomain", "other.test"),
                                  ("public", "origin", "http://app.example.test"), ("objectStorage", "mode", "local")):
            self.config = copy.deepcopy(original)
            self.config[field][key] = value
            with self.subTest(field=field, key=key), self.assertRaises(ConfigError):
                self.run_config()

    def test_rejects_invalid_quantities_and_types(self):
        for value in ({"cpu": "0", "memory": "1Gi"}, {"cpu": "1", "memory": "bad"}, {"cpu": "99", "memory": "1Gi"}):
            self.config["resources"]["database"]["requests"] = value
            with self.assertRaises(ConfigError):
                self.run_config()
        self.config["services"]["frontend"] = {}
        with self.assertRaises(ConfigError):
            self.run_config()

    def test_symlink_and_parent_escape(self):
        self.config["inputs"]["models"] = "../private.yaml"
        with self.assertRaises(ConfigError):
            self.run_config()
        outside = self.root.parent / (self.root.name + "-private.yaml")
        outside.write_text("{}")
        self.addCleanup(outside.unlink)
        (self.root / "escape.yaml").symlink_to(outside)
        self.config["inputs"]["models"] = "escape.yaml"
        with self.assertRaises(ConfigError):
            self.run_config()

    def test_duplicate_keys_and_parser_errors_are_sanitized(self):
        self.run_config()
        path = self.root / "deployment.yaml"
        for text in ("tenant: example\ntenant: other\n", "password: [private-secret"):
            path.write_text(text)
            with self.assertRaises(ConfigError) as error:
                resolve(path, profile="production")
            self.assertNotIn("private-secret", str(error.exception))

    def test_cluster_network_bindings_reject_ambiguous_or_unscoped_rules(self):
        for value in ({"dnsResolver": "10.100.0.10; arbitrary-directive"},
                      {"apiEgress": [{"cidr": "0.0.0.0/0", "port": 443}]},
                      {"apiEgress": [{"cidr": "10.0.0.1/32", "port": True}]},
                      {"ingressEnabled": "false"}):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self.run_config({"kubernetes": value})
