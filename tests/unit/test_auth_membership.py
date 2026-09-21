# SPDX-License-Identifier: AGPL-3.0-only
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import auth_membership


class Registry:
    def __init__(self):
        self.revision = 1
        self.memberships = ['example']
        self.checks = {}
        self.calls = []
        self.conflict = False

    def read(self, path):
        if path == '/status':
            data = {}
        elif path.startswith('/users?q='):
            data = [{'id': '11111111-1111-4111-8111-111111111111',
                     'email': 'member@partner.example', 'memberships': self.memberships}]
        else:
            data = {'checks': self.checks}
        return {'revision': self.revision, 'data': deepcopy(data)}

    def call(self, method, path, body, revision):
        if self.conflict or revision != self.revision:
            raise HTTPError(path, 409, 'conflict', {}, None)
        self.calls.append((method, path, body))
        self.checks = deepcopy(body['checks'])
        self.revision += 1
        return {'revision': self.revision, 'data': {'saved': True}}


def desired():
    return {'version': 1, 'tenant': 'example', 'email': 'member@partner.example',
            'checks': {'azure': {'tid': ['22222222-2222-4222-8222-222222222222']}}}


class MembershipTests(unittest.TestCase):
    def load(self, config):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'member.json'
            path.write_text(json.dumps(config))
            return auth_membership.load_config(path)

    def test_apply_is_idempotent_and_clear_restores_inheritance(self):
        r = Registry()
        config = desired()
        self.assertTrue(auth_membership.apply_config(r, config))
        self.assertFalse(auth_membership.apply_config(r, config))
        config['checks'] = {}
        self.assertTrue(auth_membership.apply_config(r, config))
        self.assertEqual(r.checks, {})
        self.assertEqual(len(r.calls), 2)
        self.assertTrue(all(c[0] == 'PUT' and c[1].endswith('/memberships/example/auth') for c in r.calls))

    def test_missing_membership_is_not_created(self):
        r = Registry()
        r.memberships = []
        with self.assertRaisesRegex(ValueError, 'Existing membership'):
            auth_membership.apply_config(r, desired())
        self.assertEqual(r.calls, [])

    def test_stale_revision_never_retries(self):
        r = Registry()
        r.conflict = True
        with self.assertRaises(HTTPError):
            auth_membership.apply_config(r, desired())
        self.assertEqual(r.calls, [])

    def test_email_normalization_and_invalid_overrides(self):
        config = desired()
        config['email'] = ' Member@Partner.Example '
        self.assertEqual(self.load(config)['email'], 'member@partner.example')
        for checks in [None, [], {'azure': None}, {'azure': {}}, {'azure': {'tid': None}}]:
            config['checks'] = checks
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                self.load(config)
        for email in ['*@partner.example', 'Name <member@partner.example>', 'not-email']:
            config = desired()
            config['email'] = email
            with self.subTest(email=email), self.assertRaises(ValueError):
                self.load(config)

    def test_concurrent_snapshot_change_refuses_writes(self):
        r = Registry()
        read = r.read
        def changed(path):
            value = read(path)
            r.revision += 1
            return value
        r.read = changed
        with self.assertRaisesRegex(ValueError, 'Registry changed'):
            auth_membership.apply_config(r, desired())
        self.assertEqual(r.calls, [])
