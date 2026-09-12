# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("helm_apply", Path(__file__).resolve().parents[2] / "scripts/helm_apply.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LifecycleTests(unittest.TestCase):
    def test_fresh_install_orders_all_phases(self):
        self.assertEqual(module.phases(None, 4), [0, 1, 2, 3, 4])

    def test_retry_keeps_completed_resources(self):
        self.assertEqual(module.phases(2, 4), [2, 3, 4])
        self.assertEqual(module.phases(4, 4), [4])

    def test_reducing_phase_is_destructive_and_refused(self):
        with self.assertRaises(ValueError):
            module.phases(4, 1)

    def test_preflight_includes_database_and_pod_secrets(self):
        self.assertEqual(module.secret_references([
            {"spec": {"bootstrap": {"initdb": {"secret": {"name": "database"}}}}},
            {"spec": {"volumes": [{"secret": {"secretName": "tls"}}],
                      "envFrom": [{"secretRef": {"name": "environment"}}],
                      "env": [{"valueFrom": {"secretKeyRef": {"name": "token", "key": "value"}}}]}}
        ]), {"database", "tls", "environment", "token"})


if __name__ == "__main__":
    unittest.main()
