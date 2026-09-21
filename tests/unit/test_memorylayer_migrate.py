# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from urllib.parse import parse_qs, urlsplit
from memorylayer_migrate import lock_dsn

class MigrationDSNTests(unittest.TestCase):
    def test_preserves_tls_and_credentials_between_drivers(self):
        for option in ['ssl', 'sslmode']:
            url = 'postgresql+asyncpg://user:p%40ss@db:5432/memory?' + option + '=require&application_name=migration'
            parsed = urlsplit(lock_dsn(url))
            self.assertEqual(parsed.scheme, 'postgresql')
            self.assertEqual(parsed.netloc, 'user:p%40ss@db:5432')
            self.assertEqual(parsed.path, '/memory')
            self.assertEqual(parse_qs(parsed.query), {'sslmode':['require'], 'application_name':['migration']})
        self.assertEqual(lock_dsn('postgresql://u:p@db/memory'), 'postgresql://u:p@db/memory')

    def test_ambiguous_tls_options_fail(self):
        with self.assertRaises(ValueError):
            lock_dsn('postgresql://db/memory?ssl=require&sslmode=disable')
