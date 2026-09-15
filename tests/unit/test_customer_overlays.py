# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import compose
import dev


class OverlayTests(unittest.TestCase):
    def test_overlays_follow_fixture_and_modal_files_before_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / '.local'
            local.mkdir()
            for file in ['compose.env', 'images.env', 'compose.yaml', 'tenants.json',
                         'fixtures.enabled', 'fixtures.yaml', 'compose.modal.yaml']:
                (local / file).touch()
            overlay = root / 'customer.yaml'
            overlay.touch()
            with patch.object(compose, 'ROOT', root):
                args = compose.command('up', '-d', overlays=[overlay])
                self.assertEqual(args[-4:], ['-f', str(overlay), 'up', '-d'])
                self.assertLess(args.index(str(local / 'compose.modal.yaml')), args.index(str(overlay)))
                with self.assertRaisesRegex(ValueError, 'does not exist'):
                    compose.command('up', overlays=[root / 'missing'])

    def test_start_and_readiness_share_customer_command(self):
        with patch.object(dev, 'base_command', return_value=['docker', 'compose', 'start']) as command, \
             patch.object(dev, 'run') as run, patch.object(dev, 'wait_ready') as ready:
            dev.main(['start', '--overlay', '/tmp/customer.yaml'])
            command.assert_called_once_with('start', overlays=[Path('/tmp/customer.yaml')])
            run.assert_called_once()
            wrapped = ready.call_args.kwargs['compose_command']
            wrapped('ps')
            command.assert_called_with('ps', overlays=[Path('/tmp/customer.yaml')])
