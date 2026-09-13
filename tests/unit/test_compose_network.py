# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from compose_network import network_settings, validate_origin


class ComposeNetworkChecks(unittest.TestCase):
    def setUp(self):
        self.env = {'WEB_PORT': '18180', 'FIXTURE_IDP_PORT': '18190'}

    def test_default_and_legacy_installations_remain_loopback(self):
        for initialized in [False, True]:
            settings = network_settings(self.env, initialized=initialized)
            self.assertEqual(settings['WEB_BIND_ADDRESS'], '127.0.0.1')
            self.assertEqual(settings['PUBLIC_ORIGIN'], 'http://127.0.0.1:18180')
            self.assertEqual(settings['FIXTURE_PUBLIC_ORIGIN'], 'http://127.0.0.1:18190')

    def test_lan_addresses_are_consistent_and_persisted(self):
        settings = network_settings(self.env, bind_address='0.0.0.0', public_origin='http://dev-host:18180/')
        self.assertEqual(settings, {'WEB_BIND_ADDRESS': '0.0.0.0', 'PUBLIC_ORIGIN': 'http://dev-host:18180',
            'PUBLIC_HOSTNAME': 'dev-host', 'FIXTURE_PUBLIC_ORIGIN': 'http://dev-host:18190'})
        self.assertEqual(network_settings(self.env | settings, initialized=True), settings)

    def test_existing_address_policy_cannot_be_changed_implicitly(self):
        with self.assertRaisesRegex(ValueError, 'fresh integration directory'):
            network_settings(self.env, initialized=True, public_origin='http://dev-host:18180')

    def test_origins_cannot_inject_credentials_paths_or_environment_lines(self):
        for value in ['file:///tmp/site', 'http://user:secret@host', 'http://host/path',
                      'http://host/?query', 'http://host/#fragment', 'http://host\nKEY=value',
                      'http://host:99999', 'http://$(id):18180']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_origin(value)
        with self.assertRaises(ValueError):
            network_settings(self.env, bind_address='arbitrary-host')


if __name__ == '__main__':
    unittest.main()
