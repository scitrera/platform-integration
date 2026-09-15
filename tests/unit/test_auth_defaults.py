# SPDX-License-Identifier: AGPL-3.0-only
import sys
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import auth_setup
from auth_setup import reconcile_default_workspace


class TenantLandingTests(unittest.TestCase):
    def test_explicit_null_clears_default_and_preserves_other_metadata(self):
        operator = Mock()
        operator.read.return_value = {'data': {'name': 'Customer', 'enabled': True,
            'metadata': {'default_workspace': 'default', 'logo': 'customer-logo'}}}
        reconcile_default_workspace(operator, {'slug': 'customer', 'default_workspace': None})
        operator.write.assert_called_once_with('PUT', '/tenants/customer', {
            'name': 'Customer', 'enabled': True,
            'metadata': {'default_workspace': None, 'logo': 'customer-logo'}})

    def test_missing_preference_does_not_change_existing_tenant(self):
        operator = Mock()
        reconcile_default_workspace(operator, {'slug': 'customer', 'workspace': 'default'})
        operator.read.assert_not_called()
        operator.write.assert_not_called()

    def test_already_applied_preference_is_noop(self):
        operator = Mock()
        operator.read.return_value = {'data': {'name': 'Customer', 'enabled': True, 'metadata': {}}}
        reconcile_default_workspace(operator, {'slug': 'customer', 'default_workspace': None})
        operator.write.assert_not_called()


class NonFixtureBootstrapTests(unittest.TestCase):
    def test_normal_bootstrap_does_not_enroll_the_fixture_email(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / '.local'
            local.mkdir()
            (local / 'compose.env').write_text('AUTH_ADMIN_PORT=18082\n')
            (local / 'operators.json').write_text(json.dumps({'operators': {'operator': 'synthetic'}}))
            operator = Mock()
            tenant = {'slug': 'customer', 'name': 'Customer', 'email': 'alice@example.test', 'workspace': 'default'}
            with patch.object(auth_setup, 'ROOT', root), patch.object(auth_setup, 'Operator', return_value=operator), \
                 patch.object(auth_setup, 'load_tenants', return_value=[tenant]), patch.object(sys, 'argv', ['auth_setup.py']):
                auth_setup.main()
            operator.read.assert_called_once_with('/tenants/customer')
            operator.write.assert_not_called()
            operator.call.assert_called_once_with('DELETE', '/session')
