# SPDX-License-Identifier: AGPL-3.0-only
import copy
import shutil
import unittest
import test_auth_cors

@unittest.skipUnless(shutil.which('helm'), 'helm is required')
class ConnectionIngressTests(unittest.TestCase):
    def setUp(self):
        self.renderer = test_auth_cors.AuthCORSTests()
        self.renderer.setUp()
        self.values = self.renderer.values
        self.values.update(tenant='acme', hostname='acme.example.test')
        self.values['components'] = {'auth': False, 'web': True, 'gateway': False}
        self.values['ingress'] = {
            'enabled': True, 'createGateway': False, 'parentName': 'public', 'parentNamespace': 'gateway',
            'connectionHostnames': ['connections.example.test'],
            'connectionOrigins': ['https://app.example.test'],
            'hostAuth': {'enabled': True, 'image': 'fixture:synthetic', 'tenant': 'acme',
                         'verifyURL': 'http://auth:8080/auth/verify', 'loginOrigin': 'https://auth.example.test'},
        }

    def test_alias_is_limited_to_tenant_connections_and_requires_auth(self):
        objects = self.renderer.render()
        route = next(d for d in objects if d['kind'] == 'HTTPRoute' and d['metadata']['name'].endswith('-connections'))
        self.assertEqual(route['spec']['hostnames'], ['connections.example.test'])
        rule = route['spec']['rules'][0]
        self.assertEqual(rule['matches'], [{'path': {'type': 'PathPrefix', 'value': '/acme/rfe1-ws'}}])
        self.assertEqual(rule['backendRefs'], [{'name': 'synthetic-web', 'port': 8080}])
        self.assertNotIn('urlRewrite', str(rule))
        self.assertIn('X-Auth-Tenant-ID', rule['filters'][0]['requestHeaderModifier']['remove'])
        policy = next(d for d in objects if d['kind'] == 'SecurityPolicy' and d['metadata']['name'].endswith('-connections-auth'))
        self.assertFalse(policy['spec']['extAuth']['failOpen'])
        self.assertEqual(policy['spec']['extAuth']['http']['backendRefs'], [{'name': 'synthetic-auth-gate', 'port': 8080}])
        self.assertTrue(policy['spec']['cors']['allowCredentials'])
        self.assertEqual(policy['spec']['cors']['allowOrigins'], ['https://app.example.test'])
        original = next(d for d in objects if d['kind'] == 'HTTPRoute' and d['metadata']['name'].endswith('-web'))
        self.assertEqual(original['spec']['hostnames'], ['acme.example.test'])

    def test_aliases_are_opt_in(self):
        self.values['ingress']['connectionHostnames'] = []
        self.assertFalse(any(d['metadata']['name'].endswith(('-connections', '-connections-auth')) for d in self.renderer.render()))

    def test_unsafe_or_unscoped_alias_configuration_fails(self):
        original = copy.deepcopy(self.values['ingress'])
        for update in ({'connectionHostnames': ['*.example.test']}, {'connectionOrigins': ['*']},
                       {'connectionOrigins': []}, {'createGateway': True},
                       {'hostAuth': {**original['hostAuth'], 'enabled': False}},
                       {'hostAuth': {**original['hostAuth'], 'tenant': 'other'}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.values['ingress'] = {**original, **update}
                self.renderer.render()
