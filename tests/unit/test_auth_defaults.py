# SPDX-License-Identifier: AGPL-3.0-only
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
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
