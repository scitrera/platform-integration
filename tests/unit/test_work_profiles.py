import json
from pathlib import Path
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[2]

class WorkProfileComposeTests(unittest.TestCase):
    def test_all_routing_services_receive_the_same_registry(self):
        services=yaml.safe_load((ROOT/"compose/compose.yaml").read_text())["services"]
        for tenant in ["alpha","beta"]:
            for role in ["platform","bridge","provider"]:
                service=services[role+"-"+tenant]
                self.assertEqual(service["environment"]["SCITRERA_WORK_PROFILES_FILE"],"/run/work-profiles.json")
                self.assertTrue(any(str(mount).endswith(":/run/work-profiles.json:ro") for mount in service["volumes"]))
        self.assertEqual(json.loads((ROOT/"compose/config/work-profiles.json").read_text()),{"profiles":{},"tenants":{}})
