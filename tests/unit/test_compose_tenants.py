# SPDX-License-Identifier: AGPL-3.0-only
import copy
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from compose_tenants import load_tenants, render_compose, select_tenants, validate_tenants
from cold_restore import validate_archive
from configure import nginx
import compose


class ComposeTenantChecks(unittest.TestCase):
    def setUp(self):
        self.tenant = {'slug': 'customer-one', 'name': 'Customer',
                       'email': 'reviewer@example.test', 'workspace': 'default'}

    def test_default_tenants_reproduce_existing_compose(self):
        tenants = json.loads((ROOT / 'examples/compose/tenants.json').read_text())
        for path in ['compose/compose.yaml', 'compose/profiles/fixtures.yaml']:
            with self.subTest(path=path):
                document = yaml.safe_load((ROOT / path).read_text())
                self.assertEqual(render_compose(document, tenants), document)

    def test_custom_tenant_routes_dependencies_credentials_and_fixtures(self):
        document = yaml.safe_load((ROOT / 'compose/compose.yaml').read_text())
        rendered = render_compose(document, [self.tenant])
        services = rendered['services']
        for name in ['aether', 'platform', 'bridge', 'catalog', 'provider', 'memorylayer', 'tools']:
            self.assertIn(name + '-customer-one', services)
        for name, service in services.items():
            self.assertTrue(set(service.get('depends_on', {})) <= services.keys(), name)
            self.assertTrue(set(service.get('networks', {})) <= rendered['networks'].keys(), name)
        self.assertEqual(services['platform-customer-one']['environment']['SCITRERA_TENANT'], 'customer-one')
        self.assertEqual(services['catalog-customer-one']['environment']['ADMIN_EMAIL'], 'reviewer@example.test')
        self.assertIn('${ML_PASSWORD_CUSTOMER_ONE}', json.dumps(rendered))
        self.assertIn('local/tenant-customer-one local/staging', services['objects-init']['command'][0])
        self.assertEqual(services['gateway']['networks']['tenant-customer-one']['aliases'], ['llm-gateway.mt'])
        self.assertNotIn('tenant-alpha', rendered['networks'])
        self.assertNotIn('tenant-beta', rendered['networks'])
        fixture = render_compose(yaml.safe_load((ROOT / 'compose/profiles/fixtures.yaml').read_text()), [self.tenant])
        self.assertEqual(fixture['services']['inference']['networks'], ['tenant-customer-one'])
        grants = json.loads(fixture['services']['acl-customer-one']['environment']['ACL_SERVICE_GRANTS'])
        self.assertTrue(any('reviewer@example.test' in grant['resource_id'] for grant in grants))
        self.assertTrue(fixture['services'].keys() <= services.keys() | {'idp', 'inference'})
        self.assertIn('X-Auth-Tenant-ID customer-one;', nginx([self.tenant]))
        self.assertIn('$platform_customer_one', nginx([self.tenant]))
        self.assertNotIn('$platform_customer-one', nginx([self.tenant]))

    def test_multiple_custom_tenants_have_separate_resources(self):
        second = dict(self.tenant, slug='customer-two', email='other@example.test')
        rendered = render_compose(yaml.safe_load((ROOT / 'compose/compose.yaml').read_text()), [self.tenant, second])
        for tenant in [self.tenant, second]:
            slug = tenant['slug']
            self.assertIn('aether-' + slug, rendered['volumes'])
            self.assertEqual(rendered['services']['platform-' + slug]['networks'], ['tenant-' + slug, 'gateway'])
            self.assertEqual(rendered['services']['catalog-' + slug]['environment']['ADMIN_EMAIL'], tenant['email'])

    def test_invalid_duplicate_and_reserved_tenants_are_rejected(self):
        for slug in ['../escape', 'UPPER', 'has space', 'gateway', 'sandbox-state', '$(id)']:
            with self.subTest(slug=slug), self.assertRaises(ValueError):
                validate_tenants([dict(self.tenant, slug=slug)])
        for tenants in [[], {}, [self.tenant, copy.deepcopy(self.tenant)], [dict(self.tenant, email='bad"@example.test')]]:
            with self.subTest(tenants=tenants), self.assertRaises(ValueError):
                validate_tenants(tenants)

    def test_configure_remembers_tenants_and_refuses_implicit_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'examples/compose').mkdir(parents=True)
            shutil.copy(ROOT / 'examples/compose/tenants.json', root / 'examples/compose/tenants.json')
            source = root / 'custom.json'
            source.write_text(json.dumps([self.tenant]))
            self.assertEqual(select_tenants(root, source), [self.tenant])
            self.assertEqual(select_tenants(root), [self.tenant])
            source.write_text(json.dumps([dict(self.tenant, slug='renamed')]))
            with self.assertRaisesRegex(ValueError, 'fresh integration directory'):
                select_tenants(root, source)
            self.assertEqual(load_tenants(root), [self.tenant])

    def test_only_landing_preference_can_change_in_an_existing_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.local').mkdir()
            (root / '.local/tenants.json').write_text(json.dumps([self.tenant]))
            source = root / 'tenants.json'
            changed = dict(self.tenant, default_workspace=None)
            source.write_text(json.dumps([changed]))
            self.assertEqual(select_tenants(root, source), [changed])
            source.write_text(json.dumps([dict(changed, workspace='different')]))
            with self.assertRaisesRegex(ValueError, 'fresh integration directory'):
                select_tenants(root, source)

    def test_landing_preference_rejects_malformed_values(self):
        for value in ['', False, 1, []]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'default_workspace'):
                validate_tenants([dict(self.tenant, default_workspace=value)])

    def test_compose_uses_generated_files_and_refuses_missing_tenant_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            local = root / '.local'
            local.mkdir()
            for name in ['compose.env', 'images.env', 'fixtures.enabled', 'tenants.json']:
                (local / name).write_text('')
            with patch.object(compose, 'ROOT', root):
                with self.assertRaisesRegex(SystemExit, 'rerun configure.py'):
                    compose.command('config')
                (local / 'compose.yaml').write_text('services: {}')
                (local / 'fixtures.yaml').write_text('services: {}')
                command = compose.command('config')
                self.assertIn(str(local / 'compose.yaml'), command)
                self.assertIn(str(local / 'fixtures.yaml'), command)
                self.assertNotIn(str(root / 'compose/compose.yaml'), command)

    def test_legacy_installation_cannot_be_renamed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'examples/compose').mkdir(parents=True)
            shutil.copy(ROOT / 'examples/compose/tenants.json', root / 'examples/compose/tenants.json')
            (root / '.local').mkdir()
            (root / '.local/compose.env').write_text('COMPOSE_PROJECT_NAME=legacy\n')
            source = root / 'custom.json'
            source.write_text(json.dumps([self.tenant]))
            with self.assertRaisesRegex(ValueError, 'fresh integration directory'):
                select_tenants(root, source)
            self.assertFalse((root / '.local/tenants.json').exists())

    def test_restore_uses_declared_tenants_for_private_config_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            for extra, accepted in [('customer-one/tls/ca/tls.key', True), ('other/tls/tls.key', False)]:
                path = Path(folder) / 'config.tar.gz'
                with tarfile.open(path, 'w:gz') as archive:
                    for name, body in [('tenants.json', json.dumps([self.tenant]).encode()), (extra, b'synthetic')]:
                        entry = tarfile.TarInfo(name)
                        entry.size = len(body)
                        archive.addfile(entry, io.BytesIO(body))
                if accepted:
                    validate_archive(path, config=True)
                else:
                    with self.assertRaises(SystemExit):
                        validate_archive(path, config=True)


if __name__ == '__main__':
    unittest.main()
