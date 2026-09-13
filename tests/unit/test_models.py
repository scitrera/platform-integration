# SPDX-License-Identifier: AGPL-3.0-only
import copy
import importlib.util
import shutil
import subprocess
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import models


class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'models.yaml'
        self.doc = {'version': 1, 'models': {'review': {
            'provider': 'openai_compatible', 'base_url': 'https://review.example.test/v1',
            'model': 'served/model', 'modal_key_env': 'PROXY_KEY', 'modal_secret_env': 'PROXY_SECRET',
            'capabilities': ['tools', 'vision']}},
            'routes': {'sahara-default': 'review', 'memorylayer-default': 'review'}}
        self.env = {'PROXY_KEY': 'synthetic-key', 'PROXY_SECRET': 'synthetic-secret'}
        self.old_central = {'providers': [{'name': 'fixture', 'type': 'openai_compatible',
                                          'base_url': 'http://inference:8080/v1'}],
                            'deployments': [{'name': 'fixture', 'provider': 'fixture', 'model': 'fixture-model'}],
                            'virtual_models': [{'name': 'memorylayer-default', 'response_model': 'virtual',
                              'selection': {'mode': 'weighted_random'},
                              'pools': [{'priority': 0, 'targets': [{'deployment': 'fixture', 'weight': 100}]}]}]}
        self.old_record = {'schema_version': 1, 'model': 'sahara-default',
                           'virtual_model': dict(self.old_central['virtual_models'][0], name='sahara-default'),
                           'providers': self.old_central['providers'], 'deployments': self.old_central['deployments']}
        self.canonical = self.root / '.local/jgl/model-catalog/sahara-default.json'
        models.write_json(self.canonical, self.old_record)
        models.write_json(self.root / '.local/gateway/config.json', self.old_central)
        models.atomic_write(self.root / '.local/compose.env', '# preserve me\nMODEL_PROVIDER_HOSTS=inference\nMODEL_PROVIDER_ALLOW_HTTP=true\nOTHER=keep\n')

    def compile(self, doc=None, environ=None, previous=None):
        self.file.write_text(yaml.safe_dump(self.doc if doc is None else doc))
        return models.compile_config(self.file, 'jgl', self.env if environ is None else environ, previous)

    def staged(self):
        plan = self.compile()
        models.write_json(models.plan_path(self.root, 'jgl'), plan)
        return plan

    def test_disabled_defaults_are_noop(self):
        plan = self.compile({'version': 1, 'models': None, 'routes': None})
        models.write_json(models.plan_path(self.root, 'jgl'), plan)
        self.assertFalse(models.managed(self.root))
        count = models.apply_plan(self.root, 'jgl', lambda *a: list(a),
                                  validate=lambda *a: self.fail('empty must not validate with Docker'),
                                  run=lambda *a: self.fail('empty must not start containers'))
        self.assertEqual(count, 0)
        self.assertEqual(models.read_json(self.canonical), self.old_record)

    def test_modal_pair_and_bearer_only_references_enter_catalog(self):
        self.doc['models']['review']['api_key_env'] = 'BEARER'
        plan = self.compile(environ=dict(self.env, BEARER='synthetic-bearer'))
        provider = plan['records']['sahara-default']['providers'][0]
        self.assertEqual(provider['auth']['type'], 'bearer')
        self.assertEqual(set(provider['default_headers']), {'Modal-Key', 'Modal-Secret'})
        self.assertNotIn('synthetic-', json.dumps(plan['records']))
        self.assertIn('synthetic-secret', plan['credentials'].values())
        for h in provider['default_headers'].values():
            self.assertIn(plan['credential_file'], h['value_from'])

    def test_google_native_auth(self):
        self.doc['models']['review'] = {'provider': 'gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta',
                                      'model': 'served', 'api_key_env': 'API_KEY'}
        plan = self.compile(environ={'API_KEY': 'synthetic-key'})
        auth = plan['records']['sahara-default']['providers'][0]['auth']
        self.assertEqual((auth['type'], auth['header']), ('header', 'x-goog-api-key'))

    def test_reuses_credentials_and_rotates_to_immutable_file(self):
        first = self.compile()
        second = self.compile(environ={}, previous=first)
        self.assertEqual(first, second)
        rotated = self.compile(environ=dict(self.env, PROXY_SECRET='new-synthetic'), previous=first)
        self.assertNotEqual(first['credential_file'], rotated['credential_file'])
        self.doc['models']['review']['modal_secret_env'] = 'DIFFERENT_VARIABLE'
        with self.assertRaises(models.ModelConfigError):
            self.compile(environ={}, previous=first)

    def test_reject_invalid_inputs_without_echoing_values(self):
        cases = [({'base_url': 'http://example.test/v1'}, None),
                 ({'base_url': 'https://user:SECRET@example.test/v1'}, None),
                 ({'base_url': 'https://example.test/v1?key=SECRET'}, None),
                 ({'model': None}, None), ({'max_concurrency': 0}, None),
                 ({'max_concurrency': True}, None), ({'allow_http': 'false'}, None),
                 ({'api_key': 'SECRET'}, None), ({'provider': 'unknown'}, None),
                 ({'modal_secret_env': 'SECRET value'}, None), ({}, {})]
        for values, env in cases:
            with self.subTest(values=values):
                doc = copy.deepcopy(self.doc)
                doc['models']['review'].update(values)
                with self.assertRaises(models.ModelConfigError) as exc:
                    self.compile(doc, environ=env)
                self.assertNotIn('SECRET', str(exc.exception))
        for change in [{'routes': {'sahara-default': 'missing'}}, {'routes': {'../escape': 'review'}}, {'version': 2}, {'typo': 1}]:
            with self.assertRaises(models.ModelConfigError):
                self.compile(dict(self.doc, **change))
        del self.doc['models']['review']['modal_secret_env']
        with self.assertRaisesRegex(models.ModelConfigError, 'both'):
            self.compile()

    def test_duplicate_yaml_and_parse_errors_are_sanitized(self):
        for content in ['version: 1\nmodels: {}\nmodels: {}', 'version: [SECRET:']:
            self.file.write_text(content)
            with self.assertRaises(models.ModelConfigError) as exc:
                models.compile_config(self.file, 'jgl')
            self.assertNotIn('SECRET', str(exc.exception))

    def test_plain_http_requires_explicit_opt_in(self):
        self.doc['models']['review'].update(base_url='http://local-model:8000/v1', allow_http=True)
        plan = self.compile()
        self.assertEqual(plan['records']['sahara-default']['providers'][0]['base_url'], 'http://local-model:8000/v1')

    def test_cas_failure_keeps_canonical_and_central_retry_recovers(self):
        plan = self.staged()
        old_text = self.canonical.read_text()
        calls = []
        def fail(args, root, label):
            calls.append(args)
            self.assertEqual(self.canonical.read_text(), old_text)
            self.assertIn('--expected', args)
            raise models.ModelConfigError('Synthetic CAS conflict')
        with self.assertRaisesRegex(models.ModelConfigError, 'CAS conflict'):
            models.apply_plan(self.root, 'jgl', lambda *a: list(a), validate=lambda *a: None, run=fail)
        self.assertEqual(self.canonical.read_text(), old_text)
        self.assertEqual(models.read_json(self.root / '.local/gateway/config.json'), self.old_central)
        self.assertTrue((self.canonical.parent / 'next-sahara-default.json').exists())
        count = models.apply_plan(self.root, 'jgl', lambda *a: list(a), validate=lambda *a: None,
                                  run=lambda args, *a: calls.append(args))
        self.assertEqual(count, 2)
        self.assertEqual(models.read_json(self.canonical), plan['records']['sahara-default'])
        self.assertFalse((self.canonical.parent / 'next-sahara-default.json').exists())
        self.assertEqual(calls[-1], ['up', '-d', '--no-deps', '--force-recreate', 'gateway'])
        self.assertIn('review.example.test', (self.root / '.local/compose.env').read_text())
        self.assertIn('# preserve me\nOTHER=keep', (self.root / '.local/compose.env').read_text())
        credential_path = self.root / '.local/gateway' / plan['credential_file']
        self.assertEqual(credential_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_owner_schema_stops_before_credentials_or_publication(self):
        plan = self.staged()
        def reject(*args):
            raise models.ModelConfigError('schema invalid')
        with self.assertRaisesRegex(models.ModelConfigError, 'schema invalid'):
            models.apply_plan(self.root, 'jgl', lambda *a: list(a), validate=reject, run=lambda *a: self.fail('must not publish'))
        self.assertFalse((self.root / '.local/gateway' / plan['credential_file']).exists())
        self.assertEqual(models.read_json(self.canonical), self.old_record)

    def test_central_changes_preserve_unmanaged_models_and_are_idempotent(self):
        plan = models.version_central_model(self.compile())
        current = copy.deepcopy(self.old_central)
        current['virtual_models'].append(dict(current['virtual_models'][0], name='unmanaged'))
        updated = models.central_config(current, plan['records'])
        self.assertEqual(updated['virtual_models'][0], current['virtual_models'][1])
        self.assertEqual(updated, models.central_config(updated, plan['records']))
        self.doc['models']['review']['model'] = 'different-model'
        next_plan = models.version_central_model(self.compile(previous=plan))
        latest = models.central_config(updated, next_plan['records'])
        self.assertEqual(latest['virtual_models'][0]['name'], 'unmanaged')
        self.assertEqual(len(latest['providers']), 3)

    def test_central_shadow_and_concurrent_plan_are_rejected(self):
        plan = self.staged()
        central = copy.deepcopy(self.old_central)
        central['virtual_models'].append(self.old_record['virtual_model'])
        with self.assertRaisesRegex(models.ModelConfigError, 'centrally'):
            models.central_config(central, plan['records'])
        with self.assertRaisesRegex(models.ModelConfigError, 'changed'):
            models.apply_plan(self.root, 'jgl', lambda *a: list(a), expected_plan={})

    def test_installation_lock_blocks_overlapping_updates(self):
        with models.installation_lock(self.root):
            with self.assertRaisesRegex(models.ModelConfigError, 'Another'):
                with models.installation_lock(self.root):
                    self.fail('must not enter')

    def test_startup_publishes_before_jobs_and_recreates_gateway(self):
        import dev
        models.write_json(self.root / '.local/images.json', {})
        models.write_json(self.root / '.local/operators.json', {})
        events = []
        def apply(root, command):
            events.append('apply')
            return 4
        with patch.object(dev, 'ROOT', self.root), patch.object(sys, 'argv', ['dev.py', 'up']), \
                patch.object(dev, 'command', lambda *a: list(a)), \
                patch.object(dev, 'run', lambda args: events.append(args)), \
                patch.object(dev, 'wait_ready', lambda: events.append('ready')), \
                patch.object(models, 'apply_staged', apply):
            dev.main()
        self.assertLess(events.index('apply'), events.index(['up', '-d']))
        self.assertLess(events.index(['up', '-d', '--no-deps', '--force-recreate', 'gateway']), events.index('ready'))

    def test_fixture_setup_preserves_yaml_provider_policy(self):
        self.staged()
        shutil.copytree(ROOT / 'scripts', self.root / 'scripts')
        env = self.root / '.local/compose.env'
        env.write_text('MODEL_PROVIDER_HOSTS=review.example.test\nMODEL_PROVIDER_ALLOW_HTTP=false\n'
                       'WEB_PORT=8080\nEMBED_ENDPOINT=http://inference:8080\n')
        subprocess.run([sys.executable, str(self.root / 'scripts/fixtures.py')], check=True, capture_output=True)
        self.assertIn('MODEL_PROVIDER_HOSTS=review.example.test', env.read_text())
        self.assertIn('MODEL_PROVIDER_ALLOW_HTTP=false', env.read_text())
        self.assertTrue((self.root / '.local/fixtures.enabled').exists())

    def test_fixture_gateway_preserves_live_routes_and_prepares_callers(self):
        import gateway_configure
        self.staged()
        live = models.central_config(self.old_central, models.version_central_model(self.compile())['records'])
        models.write_json(self.root / '.local/gateway/config.json', live)
        for name in ['ca.crt', 'tls.crt', 'tls.key']:
            models.atomic_write(self.root / '.local/jgl/tls/model-catalog' / name, 'synthetic')
        with patch.object(gateway_configure, 'ROOT', self.root), \
                patch.object(gateway_configure, 'load_tenants', lambda _: [{'slug': 'jgl'}]), \
                patch.object(gateway_configure, 'certificate'), \
                patch.object(sys, 'argv', ['gateway_configure.py', '--fixtures', '--extend-existing']):
            gateway_configure.main()
        self.assertEqual(models.read_json(self.root / '.local/gateway/config.json'), live)
        self.assertEqual(models.read_json(self.canonical), self.old_record)
        callers = models.read_json(self.root / '.local/gateway/callers.json')
        self.assertEqual({x['id'] for x in callers['clients']}, {'platform-jgl', 'memorylayer-jgl'})
