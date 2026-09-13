# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from modal_configure import render


class ModalConfigureTests(unittest.TestCase):
    def test_scoped_orchestrator_and_remote_worker_transport(self):
        doc = render(Path('/installation'), tenant='jgl', image='orch@sha256:example',
                     worker_image='worker@sha256:example', worker_gateway='aether.example.test:443',
                     environment='jgl', env_file=Path('/private/modal.env'))
        self.assertEqual(list(doc['services']), ['orchestrator-modal-jgl'])
        svc = doc['services']['orchestrator-modal-jgl']
        env = svc['environment']
        self.assertEqual(env['AETHER_IMPLEMENTATION'], 'modal')
        self.assertEqual(env['AETHER_PROFILES'], 'modal')
        self.assertEqual(env['SCITRERA_TENANT'], 'jgl')
        self.assertEqual(env['AETHER_WORKER_GATEWAY'], 'aether.example.test:443')
        self.assertEqual(env['MODAL_WORKER_TLS_DIR'], '/run/worker-tls')
        self.assertEqual(svc['networks'], ['tenant-jgl'])
        self.assertEqual(svc['env_file'], ['/private/modal.env'])
        self.assertNotIn('MODAL_TOKEN_SECRET', env)
        self.assertNotIn('AETHER_API_KEY', env)
        self.assertTrue(any('/tls/anonymous:' in p for p in svc['volumes']))
        self.assertFalse(any('/tls/ca:' in p for p in svc['volumes']))

    def test_reject_invalid_address(self):
        for address in ['https://example.test:443', 'example.test', 'example.test:0', 'example.test:99999', 'user@example.test:443']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                render(Path('/installation'), tenant='jgl', image='orch', worker_image='worker',
                       worker_gateway=address, environment='jgl')

    def test_compose_optional_overlay(self):
        spec = importlib.util.spec_from_file_location('modal_test_compose', ROOT / 'scripts/compose.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            module.ROOT = Path(directory)
            local = module.ROOT / '.local'
            local.mkdir()
            (local / 'compose.env').touch()
            (local / 'images.env').touch()
            before = module.command('ps')
            self.assertNotIn(str(local / 'compose.modal.yaml'), before)
            (local / 'compose.modal.yaml').write_text('services: {}\n')
            after = module.command('ps')
            self.assertEqual(after[-3:], ['-f', str(local / 'compose.modal.yaml'), 'ps'])
