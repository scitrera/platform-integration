# SPDX-License-Identifier: AGPL-3.0-only
import copy
import unittest
import test_connection_ingress

class StorageIngressTests(unittest.TestCase):
    def setUp(self):
        setup = test_connection_ingress.ConnectionIngressTests()
        setup.setUp()
        self.renderer, self.values = setup.renderer, setup.values
        self.values['ingress']['storageOrigins'] = ['https://app.example.test', 'https://app2.example.test']

    def test_only_authenticated_tenant_blob_reads_gain_cors(self):
        objects = self.renderer.render()
        route = next(d for d in objects if d['metadata']['name'] == 'synthetic-storage-previews')
        self.assertEqual(route['spec']['hostnames'], ['acme.example.test'])
        rule = route['spec']['rules'][0]
        self.assertEqual(rule['matches'], [{'path': {'type': 'PathPrefix', 'value': '/storage/acme/blob'}, 'method': m}
                                         for m in ('GET', 'HEAD', 'OPTIONS')])
        self.assertIn('X-Auth-Tenant-ID', rule['filters'][0]['requestHeaderModifier']['remove'])
        self.assertIn({'name': 'Cache-Control', 'value': 'no-store'}, rule['filters'][1]['responseHeaderModifier']['set'])
        policy = next(d for d in objects if d['metadata']['name'] == 'synthetic-storage-previews-auth')['spec']
        self.assertFalse(policy['extAuth']['failOpen'])
        self.assertEqual(policy['extAuth']['http']['backendRefs'], [{'name': 'synthetic-auth-gate', 'port': 8080}])
        self.assertTrue(policy['cors']['allowCredentials'])
        self.assertEqual(policy['cors']['allowOrigins'], self.values['ingress']['storageOrigins'])
        self.assertEqual(policy['cors']['allowHeaders'], ['Accept', 'X-Blob-Capability'])
        self.assertEqual(policy['cors']['allowMethods'], ['GET', 'HEAD', 'OPTIONS'])

    def test_storage_cors_is_opt_in(self):
        self.values['ingress']['storageOrigins'] = []
        self.assertFalse(any('storage-previews' in d['metadata']['name'] for d in self.renderer.render()))

    def test_unsafe_or_unauthenticated_configuration_is_rejected(self):
        original = copy.deepcopy(self.values['ingress'])
        for update in ({'storageOrigins': ['*']}, {'storageOrigins': ['https://*.example.test']},
                       {'storageOrigins': ['https://user:pass@example.test']}, {'storageOrigins': ['https://example.test/path']},
                       {'hostAuth': {**original['hostAuth'], 'enabled': False}},
                       {'hostAuth': {**original['hostAuth'], 'tenant': 'other'}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.values['ingress'] = {**original, **update}
                self.renderer.render()
