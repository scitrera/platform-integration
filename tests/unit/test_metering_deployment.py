# SPDX-License-Identifier: AGPL-3.0-only
import unittest
import yaml
from metering import generate
from metering_config import openmeter_config


class MeteringDeploymentTests(unittest.TestCase):
    def config(self):
        return {'schemaVersion': 1, 'production': True,
                'images': {'backend': 'registry.example/backend@sha256:' + '1'*64},
                'kubernetes': {'namespace': 'shared', 'storageClass': 'gp3',
                    'postgresHost': 'openmeter-db-rw', 'postgresSecret': 'openmeter-db',
                    'credentialsSecret': 'metering-credentials', 'reportingEnvironmentSecret': 'usage-env',
                    'producerSecret': 'usage-producers', 'journalCluster': 'usage-db',
                    'nodeSelector': {'pool': 'shared'}}}

    def test_bounded_compose_independent_databases_and_private_endpoints(self):
        result = generate(self.config(), 'compose')
        services = result['compose.metering.yaml']['services']
        for service in services.values():
            self.assertNotIn('ports', service)
            self.assertIn('mem_limit', service)
        self.assertEqual(services['metering-clickhouse']['mem_limit'], '2g')
        self.assertEqual(services['metering-kafka']['mem_limit'], '1536m')
        self.assertNotEqual(services['usage-postgres']['volumes'], services['metering-postgres']['volumes'])
        self.assertIn('metering-migrate', services['metering-api']['depends_on'])
        self.assertIn('usage_unknown', [m['slug'] for m in openmeter_config()['meters']])

    def test_kubernetes_uses_same_config_and_budgets(self):
        result = list(yaml.safe_load_all(generate(self.config(), 'kubernetes')['metering.kubernetes.yaml']))
        config = next(o for o in result if o['kind'] == 'ConfigMap')
        self.assertEqual(yaml.safe_load(config['data']['openmeter.yaml']), openmeter_config())
        for obj in result:
            if obj['kind'] in {'Deployment', 'Job'}:
                pod = obj['spec']['template']['spec']
                self.assertEqual(pod['nodeSelector'], {'pool': 'shared'})
                self.assertFalse(pod['automountServiceAccountToken'])
                for container in pod['containers']:
                    self.assertIn('memory', container['resources']['limits'])
            if obj['kind'] == 'Service':
                self.assertNotIn('type', obj['spec'])
        self.assertEqual(len([o for o in result if o['kind'] == 'Job']), 1)
        self.assertFalse(any(o['kind'] == 'Secret' for o in result))
        api = next(o for o in result if o['kind'] == 'Deployment' and o['metadata']['name'] == 'metering-api')
        env = {e['name']: e for e in api['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['POSTGRES_HOST']['value'], 'openmeter-db-rw')
        self.assertEqual(env['POSTGRES_PASSWORD']['valueFrom']['secretKeyRef']['name'], 'openmeter-db')
        kafka = next(o for o in result if o['kind'] == 'Service' and o['metadata']['name'] == 'metering-kafka')
        self.assertEqual({p['port'] for p in kafka['spec']['ports']}, {9092, 9093})
        self.assertTrue(kafka['spec']['publishNotReadyAddresses'])
        pod = next(o for o in result if o['kind'] == 'Deployment' and o['metadata']['name'] == 'metering-kafka')
        env = {e['name']: e.get('value') for e in pod['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['KAFKA_CONTROLLER_QUORUM_VOTERS'], '1@127.0.0.1:9093')
        self.assertEqual(env['KAFKA_LOG_DIRS'], '/var/lib/kafka/data/logs')

    def test_production_refuses_mutable_images_and_embedded_passwords(self):
        config = self.config()
        config['images']['backend'] = 'backend:latest'
        with self.assertRaises(ValueError):
            generate(config, 'compose')
        config = self.config()
        config['kubernetes']['password'] = 'must-not-be-config'
        with self.assertRaises(ValueError):
            generate(config, 'kubernetes')


def test_metrics_bridge_ingress_is_namespace_and_component_scoped():
    from metering_kubernetes import render
    objects = render(namespace="shared", storage_class="test", postgres_host="openmeter-db-rw",
        postgres_secret="db", credentials_secret="metering", tenant_namespaces=["tenant-example"])
    policy = next(o for o in objects if o["metadata"]["name"] == "metering-bridge-tenant-example")
    rule = policy["spec"]["ingress"][0]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8888}]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "tenant-example"}},
                              "podSelector": {"matchLabels": {"app.kubernetes.io/component": "metrics-bridge"}}}]
    meters = {m["slug"]: m for m in openmeter_config()["meters"]}
    assert meters["active_users"]["aggregation"] == "UNIQUE_COUNT"
    assert meters["licensed_users"]["eventType"] == "licensed_users"

def test_usage_meters_preserve_model_workspace_agent_and_user_dimensions():
    for meter in openmeter_config()["meters"]:
        if meter["aggregation"] == "SUM" and meter["slug"] != "licensed_users":
            assert meter["groupBy"] == {key: "$." + key for key in
                ("model", "provider", "workspace", "user", "agent", "kind")}
