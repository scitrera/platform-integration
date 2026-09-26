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
class AuthCORSTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            'development': True, 'phase': 2,
            'publicOrigin': 'https://customer.example.test', 'adminOrigin': 'https://auth2.example.test',
            'images': dict.fromkeys(['auth', 'web', 'sparkroute', 'postgres', 'valkey'], 'fixture:synthetic'),
            'components': {'auth': True, 'gateway': False, 'web': False},
            'managedDatabases': {'auth': False, 'gateway': False}, 'ingress': {'enabled': False},
            'authIngress': {'enabled': True, 'parentName': 'public', 'parentNamespace': 'gateway'},
            'authSession': {'allowedOrigins': ['https://app.example.test', 'https://app2.example.test']},
        }

    def render(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'values.json'
            path.write_text(json.dumps(self.values))
            result = subprocess.run(['helm', 'template', 'synthetic', str(ROOT / 'charts/platform-shared'),
                                     '--kube-version', '1.36.0', '-f', str(path)], text=True, capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr)
        return [d for d in yaml.safe_load_all(result.stdout) if d]

    def test_cors_status_and_application_guards_share_exact_origins(self):
        self.values['authIngress']['additionalHostnames'] = ['auth.example.test']
        objects = self.render()
        deployment = next(d for d in objects if d['kind'] == 'Deployment')
        env = {e['name']: e['value'] for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        origins = ['https://customer.example.test', 'https://app.example.test', 'https://app2.example.test']
        self.assertEqual(env['SCITRERA_AUTH_CHECKZ_ALLOWED_ORIGINS'].split(','), origins)
        self.assertEqual(env['SCITRERA_AUTH_ALLOWED_REDIRECT_ORIGINS'].split(','), origins)
        policy = next(d for d in objects if d['kind'] == 'SecurityPolicy')
        self.assertEqual(policy['spec']['cors']['allowOrigins'], origins)
        self.assertTrue(policy['spec']['cors']['allowCredentials'])
        self.assertEqual(policy['spec']['cors']['allowMethods'], ['GET', 'HEAD', 'OPTIONS'])
        self.assertEqual([t['name'] for t in policy['spec']['targetRefs']], ['synthetic-auth-status'])
        routes = {d['metadata']['name']: d for d in objects if d['kind'] == 'HTTPRoute'}
        self.assertEqual(len(routes), 2)
        for route in routes.values():
            self.assertEqual(route['spec']['hostnames'], ['auth2.example.test', 'auth.example.test'])
            self.assertEqual(route['spec']['rules'][0]['backendRefs'][0]['port'], 8081)
        rule = routes['synthetic-auth-status']['spec']['rules'][0]
        self.assertEqual(rule['matches'], [{'path': {'type': 'Exact', 'value': path}} for path in ['/checkz', '/auth/checkz']])
        response = next(f['responseHeaderModifier'] for f in rule['filters'] if f['type'] == 'ResponseHeaderModifier')
        self.assertIn({'name': 'Cache-Control', 'value': 'no-store'}, response['set'])
        self.assertIn({'name': 'Vary', 'value': 'Origin'}, response['add'])

    def test_default_origin_retained_without_allow_all(self):
        self.values['authSession']['allowedOrigins'] = []
        policy = next(d for d in self.render() if d['kind'] == 'SecurityPolicy')
        self.assertEqual(policy['spec']['cors']['allowOrigins'], [self.values['publicOrigin']])

    def test_no_ingress_means_no_public_cors_resources(self):
        self.values['authIngress']['enabled'] = False
        self.assertFalse(any(d['kind'] in ('HTTPRoute', 'SecurityPolicy') for d in self.render()))

    def test_unsafe_origins_and_aliases_rejected(self):
        for origin in ('*', 'https://*.example.test', 'https://example.test/path', 'https://user:pass@example.test', 'null'):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, 'exact HTTP'):
                self.values['authSession']['allowedOrigins'] = [origin]
                self.render()
        self.values['authSession']['allowedOrigins'] = []
        self.values['authIngress']['additionalHostnames'] = ['*.example.test']
        with self.assertRaisesRegex(ValueError, 'exact DNS'):
            self.render()

    def test_production_rejects_plaintext_origin(self):
        self.values['development'] = False
        self.values['images'] = dict.fromkeys(self.values['images'], 'fixture@sha256:'+'1'*64)
        self.values['authSession']['allowedOrigins'] = ['http://app.example.test']
        with self.assertRaisesRegex(ValueError, 'must use HTTPS'):
            self.render()
