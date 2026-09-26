# SPDX-License-Identifier: AGPL-3.0-only
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import auth_config
from auth_operator import validate_origin


def config():
    return {'version': 1, 'tenant': {'slug': 'customer', 'name': 'Customer', 'enabled': True,
            'metadata': {'default_workspace': None}}, 'domains': ['customer.example'],
            'auth': {'auto_add': True, 'providers': ['azure'],
                     'checks': {'azure': {'tid': ['11111111-1111-4111-8111-111111111111']}}}}


class Registry:
    """In-memory model of the native API's revision and patch semantics."""
    def __init__(self):
        self.revision = 1
        self.tenant = None
        self.domains = []
        self.auth = {'auto_add': False, 'providers': [], 'checks': {}}
        self.calls = []
        self.conflict_at = None
        self.domain_conflict = False

    def read(self, path):
        if path == '/status':
            data = {}
        elif self.tenant is None:
            raise HTTPError(path, 404, 'missing', {}, None)
        elif path.endswith('/domains'):
            data = self.domains
        elif path.endswith('/auth'):
            data = self.auth
        else:
            data = self.tenant
        return {'revision': self.revision, 'data': deepcopy(data)}

    def call(self, method, path, data=None, revision=None):
        if self.conflict_at == len(self.calls):
            self.revision += 1
        if revision != self.revision or (self.domain_conflict and method == 'POST' and path.endswith('/domains')):
            raise HTTPError(path, 409, 'conflict', {}, None)
        self.calls.append((method, path, deepcopy(data), revision))
        if path == '/tenants':
            self.tenant = deepcopy(data)
        elif '/domains/' in path:
            self.domains.remove(path.rsplit('/', 1)[1])
        elif path.endswith('/domains'):
            self.domains.append(data['domain'])
        elif path.endswith('/auth'):
            self.auth.update({k: deepcopy(v) for k, v in data.items() if k != 'checks'})
            for provider, checks in data.get('checks', {}).items():
                if checks:
                    self.auth['checks'][provider] = deepcopy(checks)
                else:
                    self.auth['checks'].pop(provider, None)
        else:
            metadata = self.tenant.setdefault('metadata', {})
            for key, value in data.get('metadata', {}).items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            self.tenant.update({k: deepcopy(v) for k, v in data.items() if k != 'metadata'})
        self.revision += 1
        return {'revision': self.revision, 'data': {'saved': True}}


class AuthConfigTests(unittest.TestCase):
    def test_optional_application_url_validation(self):
        for value in ['https://customer.example/customer', '', None]:
            candidate = config()
            candidate['tenant']['metadata']['application_url'] = value
            auth_config.validate_config(candidate)
        for value in ['http://customer.example', 'https://user:pass@customer.example',
                      'https://customer.example/?next=evil', 'https://customer.example/#',
                      'https://customer.example/?', 'javascript:alert(1)']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate = config()
                candidate['tenant']['metadata']['application_url'] = value
                auth_config.validate_config(candidate)


    def test_create_disabled_configure_enable_and_repeat_without_writes(self):
        registry = Registry()
        desired = auth_config.validate_config(config())
        changes = auth_config.apply_config(registry, desired)
        self.assertFalse(changes[0]['data']['enabled'])
        self.assertEqual(changes[-1]['data']['enabled'], True)
        self.assertEqual(registry.domains, ['customer.example'])
        self.assertEqual(registry.auth, desired['auth'])
        self.assertEqual(auth_config.apply_config(registry, desired), [])

    def test_domain_change_disables_auto_add_removes_old_then_adds_and_restores(self):
        registry = Registry()
        desired = config()
        auth_config.apply_config(registry, desired)
        registry.tenant['metadata'] = {'logo': 'https://example.test/logo', 'default_workspace': 'old'}
        registry.auth['checks']['google'] = {'hd': 'preserve.example'}
        desired['domains'] = ['new.example']
        changes = auth_config.apply_config(registry, desired)
        self.assertEqual(changes[0]['path'], '/tenants/customer/auth')
        self.assertFalse(changes[0]['data']['auto_add'])
        self.assertEqual(changes[1]['method'], 'DELETE')
        self.assertEqual(registry.domains, ['new.example'])
        self.assertEqual(registry.tenant['metadata'], {'logo': 'https://example.test/logo'})
        self.assertEqual(registry.auth['checks']['google'], {'hd': 'preserve.example'})
        self.assertTrue(registry.auth['auto_add'])

    def test_changed_tid_replaces_only_configured_provider_checks(self):
        registry = Registry()
        auth_config.apply_config(registry, config())
        registry.auth['checks']['azure']['department'] = 'old'
        desired = config()
        desired['auth']['checks']['azure']['tid'] = ['22222222-2222-4222-8222-222222222222']
        auth_config.apply_config(registry, desired)
        self.assertEqual(registry.auth['checks']['azure'], desired['auth']['checks']['azure'])

    def test_conflict_does_not_refresh_revision_or_continue(self):
        registry = Registry()
        registry.conflict_at = 1
        with self.assertRaises(HTTPError) as raised:
            auth_config.apply_config(registry, config())
        self.assertEqual(raised.exception.code, 409)
        self.assertEqual(len(registry.calls), 1)
        self.assertFalse(registry.tenant['enabled'])

    def test_domain_conflict_leaves_new_tenant_disabled_and_can_resume(self):
        registry = Registry()
        registry.domain_conflict = True
        with self.assertRaises(HTTPError):
            auth_config.apply_config(registry, config())
        self.assertFalse(registry.tenant['enabled'])
        self.assertFalse(registry.auth['auto_add'])
        registry.domain_conflict = False
        auth_config.apply_config(registry, config())
        self.assertTrue(registry.tenant['enabled'])
        self.assertTrue(registry.auth['auto_add'])

    def test_disabling_tenant_happens_before_other_changes(self):
        registry = Registry()
        auth_config.apply_config(registry, config())
        desired = config()
        desired['tenant']['enabled'] = False
        desired['domains'] = ['new.example']
        changes = auth_config.apply_config(registry, desired)
        self.assertEqual(changes[0]['path'], '/tenants/customer')
        self.assertFalse(changes[0]['data']['enabled'])

    def test_read_conflict_is_detected_before_mutation(self):
        registry = Registry()
        original = registry.read
        def read(path):
            response = original(path)
            registry.revision += 1
            return response
        registry.read = read
        with self.assertRaisesRegex(ValueError, 'changed while reading'):
            auth_config.apply_config(registry, config())
        self.assertEqual(registry.calls, [])

    def test_plan_is_read_only_and_check_does_not_connect(self):
        registry = Registry()
        desired = config()
        self.assertTrue(auth_config.plan_changes(desired, auth_config.read_snapshot(registry, 'customer')))
        self.assertEqual(registry.calls, [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'auth.json'
            path.write_text(json.dumps(desired))
            with patch.object(auth_config, 'Operator') as operator:
                auth_config.main(['check', '--file', str(path)])
                operator.assert_not_called()

    def test_fixture_apply_refuses_before_operator_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.local').mkdir()
            (root / '.local/fixtures.enabled').touch()
            (root / '.local/compose.env').write_text('AUTH_ADMIN_PORT=18082\n')
            path = root / 'auth.json'
            path.write_text(json.dumps(config()))
            with patch.object(auth_config, 'ROOT', root), patch.object(auth_config, 'Operator') as operator:
                with self.assertRaises(SystemExit):
                    auth_config.main(['apply', '--file', str(path)])
                operator.assert_not_called()

    def test_validation_rejects_unrestricted_provider_and_malformed_input(self):
        for key, value in [('providers', []), ('auto_add', 'true'), ('checks', {'azure': {'tid': ''}})]:
            desired = config()
            desired['auth'][key] = value
            with self.assertRaises(ValueError):
                auth_config.validate_config(desired)
        for name in ['customer.example,', '*.customer.example', 'https://customer.example']:
            desired = config()
            desired['domains'] = [name]
            with self.assertRaises(ValueError):
                auth_config.validate_config(desired)
        desired = config()
        desired['domains'] = ['Customer.Example.', 'customer.example']
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            auth_config.validate_config(desired)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            json.loads('{"version":1,"version":2}', object_pairs_hook=auth_config.unique_object)

    def test_google_hosted_domain_uses_owner_normalization_and_explicit_blank(self):
        desired = config()
        desired['auth']['providers'] = ['google']
        desired['auth']['checks'] = {'google': {'hd': [' Customer.Example. ', '']}}
        checked = auth_config.validate_config(desired)
        self.assertEqual(checked['auth']['checks']['google']['hd'], ['customer.example', ''])

    def test_origin_does_not_send_operator_token_to_insecure_remote_or_url_credentials(self):
        for origin in ['http://public.example', 'https://user:pass@private.example', 'https://private.example/api']:
            with self.assertRaises(ValueError):
                validate_origin(origin)
        self.assertEqual(validate_origin('http://127.0.0.1:18082/'), 'http://127.0.0.1:18082')
        self.assertEqual(validate_origin('https://private.example'), 'https://private.example')
