# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which('helm'), 'helm is required')
class SessionHATests(unittest.TestCase):
    def render(self, chart, values):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'values.json'
            path.write_text(json.dumps(values))
            result = subprocess.run(['helm', 'template', 'synthetic', str(ROOT / 'charts' / chart),
                                     '--kube-version', '1.34.0', '-f', str(path)],
                                    text=True, capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr)
        return [o for o in yaml.safe_load_all(result.stdout) if o]

    def test_replica_placement_and_resource_bounds(self):
        objs = self.render('platform-sessions', {'development': True, 'image': 'valkey:synthetic'})
        workloads = {o['metadata']['name']: o for o in objs if o['kind'] == 'StatefulSet'}
        self.assertEqual([workloads[n]['spec']['replicas'] for n in sorted(workloads)], [2, 3])
        for obj in workloads.values():
            pod = obj['spec']['template']['spec']
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertIn('requiredDuringSchedulingIgnoredDuringExecution', pod['affinity']['podAntiAffinity'])
            self.assertIn('memory', pod['containers'][0]['resources']['limits'])
            self.assertEqual(obj['spec']['podManagementPolicy'], 'Parallel')
        self.assertEqual(sorted(o['spec']['minAvailable'] for o in objs if o['kind'] == 'PodDisruptionBudget'), [1, 2])
        self.assertFalse(any(o['kind'] == 'Service' and o['spec'].get('type') == 'LoadBalancer' for o in objs))

    def test_sentinel_auth_excludes_standalone_store(self):
        images = dict.fromkeys(['auth', 'web', 'sparkroute', 'postgres', 'valkey'], 'fixture:synthetic')
        values = {'development': True, 'phase': 4, 'images': images, 'authReplicas': 2,
                  'components': {'auth': True, 'gateway': False, 'web': False},
                  'managedDatabases': {'auth': False, 'gateway': False}, 'ingress': {'enabled': False},
                  'authSession': {'ttl': '48h', 'sentinel': {'master': 'sessions', 'addresses': ['one:26379', 'two:26379', 'three:26379']}}}
        objs = self.render('platform-shared', values)
        auth = next(o for o in objs if o['kind'] == 'Deployment')
        self.assertEqual(auth['spec']['replicas'], 2)
        self.assertEqual(auth['spec']['strategy']['rollingUpdate']['maxUnavailable'], 1)
        env = {e['name']: e['value'] for e in auth['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(env['AUTH_PROXY_SESSION_TTL'], '48h')
        self.assertNotIn('AUTH_PROXY_SESSION_REDIS_ADDR', env)
        self.assertEqual(env['AUTH_PROXY_SESSION_SENTINEL_ADDRS'], 'one:26379,two:26379,three:26379')
        self.assertFalse(any(o['kind'] == 'PersistentVolumeClaim' for o in objs))
        values['authSession']['sentinel']['master'] = ''
        with self.assertRaisesRegex(ValueError, 'requires both master and addresses'):
            self.render('platform-shared', values)

    def test_production_rejects_mutable_session_image(self):
        with self.assertRaisesRegex(ValueError, 'registry manifest digest'):
            self.render('platform-sessions', {'image': 'valkey:latest'})
