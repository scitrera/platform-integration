# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import document_services as ds  # noqa: E402


class DocumentServicesTests(unittest.TestCase):
    def config(self):
        return {'version': 1, 'profile': ds.PROFILE, 'transport': 'modal', 'endpoint': 'https://embed.example',
                'proxy': {'image': 'registry.example/proxy@sha256:' + '1' * 64,
                          'secret_name': 'embed-modal', 'key_env': 'EMBED_MODAL_KEY', 'secret_env': 'EMBED_MODAL_SECRET'}}

    def test_compose_helm_share_dimensions_and_keep_credentials_out(self):
        compose, helm, manifest = ds.render(self.config(), 'alpha', 1920)
        self.assertEqual(compose['services']['ml-migrate-alpha']['environment']['MEMORYLAYER_EMBEDDING_DIMENSIONS'], '1920')
        self.assertEqual(helm['embeddingDimensions'], 1920)
        self.assertEqual(helm['documentServices']['environment']['MEMORYLAYER_EMBED_SERVER_URL'], 'http://127.0.0.1:8081')
        self.assertFalse(manifest['applied'])
        for env in (compose['services']['memorylayer-alpha']['environment'], helm['documentServices']['environment']):
            self.assertEqual(env['MEMORYLAYER_EMBEDDING_PRELOAD_ENABLED'], 'false')
        self.assertNotIn('ports', compose['services']['embed-proxy-alpha'])
        self.assertEqual(compose['services']['memorylayer-alpha']['environment']['MEMORYLAYER_TRANSCRIPTION_SERVICE'], 'embed_server')

    def test_refuses_dimension_switch_and_inline_credentials(self):
        with self.assertRaisesRegex(ValueError, 'prepared 1920'):
            ds.render(self.config(), 'alpha', 1536)
        for url in ['http://embed.example', 'https://key:secret@embed.example', 'https://embed.example?token=x']:
            config = self.config()
            config['endpoint'] = url
            with self.assertRaises(ValueError):
                ds.validate(config)

    def test_gpu_chart_has_one_gpu_only_in_serving_phase(self):
        chart = ROOT / 'charts/memorylayer-embed'
        digest = 'registry.example/embed@sha256:' + '1' * 64
        subprocess.run(['helm', 'lint', str(chart), '--set', 'image=' + digest], check=True, capture_output=True)
        for phase in ['prepare', 'serve']:
            rendered = subprocess.check_output(['helm', 'template', 'embed', str(chart), '--set', 'image=' + digest,
                                                 '--set', 'phase=' + phase], text=True)
            docs = [x for x in yaml.safe_load_all(rendered) if x]
            kind = 'Job' if phase == 'prepare' else 'Deployment'
            workload = next(x for x in docs if x['kind'] == kind)
            containers = workload['spec']['template']['spec']['containers']
            self.assertEqual(len(containers), 1)
            self.assertEqual(containers[0]['resources']['limits'].get('nvidia.com/gpu'), None if phase == 'prepare' else '1')
            self.assertNotIn('docker.sock', rendered)
            self.assertNotIn('NodePort', rendered)


    def test_modal_sidecar_renders_with_loopback_and_secret_references(self):
        chart = ROOT / 'charts/platform-tenant'
        values = yaml.safe_load((chart / 'values.yaml').read_text())
        values['images'] = {key: 'registry.example/' + key.lower() + '@sha256:' + '1' * 64
                            for key in values['images']}
        values['storage']['className'] = 'synthetic'
        values['phase'] = 2
        _, overlay, _ = ds.render(self.config(), values['tenant'], 1920)
        values.update(overlay)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'values.yaml'
            path.write_text(yaml.safe_dump(values))
            rendered = subprocess.check_output(['helm', 'template', 'example', str(chart),
                                                 '-f', str(path), '--kube-version', '1.34.0'], text=True)
        deployment = next(obj for obj in yaml.safe_load_all(rendered)
                          if obj and obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'example-memorylayer')
        containers = {c['name']: c for c in deployment['spec']['template']['spec']['containers']}
        proxy = containers['embed-proxy']
        self.assertIn('127.0.0.1', proxy['args'])
        self.assertIn('8081', proxy['args'])
        proxy_env = {e['name']: e for e in proxy['env']}
        self.assertEqual(proxy_env['MODAL_SECRET']['valueFrom']['secretKeyRef']['name'], 'embed-modal')
        env = containers['memorylayer']['env']
        self.assertEqual(len(env), len({e['name'] for e in env}))
        self.assertIn({'name': 'MEMORYLAYER_EMBED_SERVER_URL', 'value': 'http://127.0.0.1:8081'}, env)

    def test_local_image_requires_explicit_opt_in_and_never_renders_helm(self):
        config = self.config()
        config['proxy']['image'] = 'sha256:' + '2' * 64
        with self.assertRaises(ValueError):
            ds.render(config, 'alpha', 1920)
        compose, helm, manifest = ds.render(config, 'alpha', 1920, allow_local_image=True)
        self.assertEqual(compose['services']['embed-proxy-alpha']['image'], config['proxy']['image'])
        self.assertIsNone(helm)
        self.assertEqual(manifest['deployment_targets'], ['compose-local'])


class SplitDocumentServicesTests(unittest.TestCase):
    def config(self):
        from document_services_v2 import PROFILE_V2
        return {'version': 2, 'profile': PROFILE_V2, 'proxy': {'image': 'registry.example/proxy@sha256:' + '1' * 64},
                'services': {'embedding': {'transport': 'modal', 'endpoint': 'https://embedding.example',
                    'capabilities': ['single_vector', 'multi_vector', 'score'],
                    'auth': {'secret_name': 'embedding-modal', 'key_env': 'EMBED_MODAL_KEY', 'secret_env': 'EMBED_MODAL_SECRET'}},
                    'transcription': {'transport': 'modal', 'endpoint': 'https://transcription.example',
                    'providers': ['unlimited_ocr', 'deepseek_ocr'], 'policy': 'fallback',
                    'auth': {'secret_name': 'transcription-modal', 'key_env': 'OCR_MODAL_KEY', 'secret_env': 'OCR_MODAL_SECRET'}}}}

    def test_one_proxy_and_independent_identity_and_credentials(self):
        compose, helm, manifest = ds.render(self.config(), 'alpha', 1920)
        self.assertEqual(set(compose['services']), {'memorylayer-alpha', 'ml-migrate-alpha', 'embed-proxy-alpha'})
        proxy = compose['services']['embed-proxy-alpha']
        self.assertEqual(len(proxy['volumes']), 5)
        self.assertIn('embed-proxy-alpha-spool:/tmp', proxy['volumes'])
        self.assertTrue(all(key.endswith('_FILE') or key == 'EMBED_PROXY_CONFIG_JSON' for key in proxy['environment']))
        self.assertEqual(set(helm['documentServices']['proxy']['credentials']), {'embedding', 'transcription'})
        legacy = ds.render(DocumentServicesTests().config(), 'alpha', 1920)[2]
        self.assertEqual(manifest['compatible_embedding_cache_identity'], legacy['embedding_identity'])
        self.assertNotEqual(manifest['embedding_identity'], manifest['transcription_identity'])
        config = self.config()
        config['services']['transcription'] = {**config['services']['transcription'], 'transport': 'http', 'endpoint': 'http://ocr:61051'}
        del config['services']['transcription']['auth']
        mixed = ds.render(config, 'alpha', 1920)
        self.assertEqual(len(mixed[0]['services']['embed-proxy-alpha']['volumes']), 3)
        self.assertEqual(mixed[2]['embedding_identity'], manifest['embedding_identity'])

    def test_cold_start_deadline_and_client_headroom_are_rendered(self):
        import json
        for value in (900, 1200):
            config = self.config()
            config['proxy']['timeout_seconds'] = value
            compose, helm, _ = ds.render(config, 'alpha', 1920)
            routing = json.loads(compose['services']['embed-proxy-alpha']['environment']['EMBED_PROXY_CONFIG_JSON'])
            self.assertEqual({s['deadline'] for s in routing['services'].values()}, {value})
            self.assertEqual(helm['documentServices']['proxy']['routing'], routing)
            for env in (compose['services']['memorylayer-alpha']['environment'], helm['documentServices']['environment']):
                self.assertEqual(env['MEMORYLAYER_EMBEDDING_PRELOAD_ENABLED'], 'false')
                self.assertEqual(env['MEMORYLAYER_EMBED_SERVER_TIMEOUT'], str(value + 60))
                self.assertEqual(env['MEMORYLAYER_GLINER2_NER_TIMEOUT'], str(value + 60))
        for value in (0, 59, 1801, True, '900'):
            config['proxy']['timeout_seconds'] = value
            with self.assertRaises(ValueError): ds.validate(config)

    def test_consumer_limits_are_configurable_in_both_outputs(self):
        config = self.config()
        config['consumer'] = {'embedding_concurrency': 16, 'transcription_concurrency': 8}
        compose, helm, _ = ds.render(config, 'alpha', 1920)
        for env in (compose['services']['memorylayer-alpha']['environment'], helm['documentServices']['environment']):
            self.assertEqual(env['MEMORYLAYER_EMBED_IMAGE_CONCURRENCY'], '16')
            self.assertEqual(env['MEMORYLAYER_EMBED_TEXT_CONCURRENCY'], '16')
            self.assertEqual(env['MEMORYLAYER_EMBED_TRANSCRIPTION_CONCURRENCY'], '8')
        for value in (True, 0, 33, '8'):
            config['consumer']['embedding_concurrency'] = value
            with self.assertRaises(ValueError):
                ds.validate(config)

    def test_ocr_order_changes_only_transcription_identity(self):
        config = self.config()
        original = ds.render(config, 'alpha', 1920)
        config['services']['transcription']['providers'] = ['deepseek_ocr', 'unlimited_ocr']
        compose, helm, manifest = ds.render(config, 'alpha', 1920)
        self.assertEqual(compose['services']['memorylayer-alpha']['environment']['MEMORYLAYER_EMBED_TRANSCRIPTION_PROVIDERS'], 'deepseek-ocr,unlimited-ocr')
        self.assertEqual(helm['documentServices']['environment']['MEMORYLAYER_EMBED_TRANSCRIPTION_PROVIDERS'], 'deepseek-ocr,unlimited-ocr')
        self.assertEqual(manifest['embedding_identity'], original[2]['embedding_identity'])
        self.assertNotEqual(manifest['transcription_identity'], original[2]['transcription_identity'])

    def test_split_helm_has_one_sidecar_with_both_secret_refs(self):
        chart = ROOT / 'charts/platform-tenant'
        values = yaml.safe_load((chart / 'values.yaml').read_text())
        values['images'] = {key: 'registry.example/image@sha256:' + '1' * 64 for key in values['images']}
        values['storage']['className'] = 'synthetic'
        values['phase'] = 2
        values.update(ds.render(self.config(), values['tenant'], 1920)[1])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'values.yaml'
            path.write_text(yaml.safe_dump(values))
            rendered = subprocess.check_output(['helm', 'template', 'example', str(chart), '-f', str(path), '--kube-version', '1.34.0'], text=True)
        dep = next(obj for obj in yaml.safe_load_all(rendered) if obj and obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'example-memorylayer')
        proxy = next(c for c in dep['spec']['template']['spec']['containers'] if c['name'] == 'embed-proxy')
        env = {item['name']: item for item in proxy['env']}
        self.assertEqual(env['EMBEDDING_MODAL_KEY']['valueFrom']['secretKeyRef']['name'], 'embedding-modal')
        self.assertEqual(env['TRANSCRIPTION_MODAL_SECRET']['valueFrom']['secretKeyRef']['name'], 'transcription-modal')
        self.assertIn('/health/live', str(proxy['readinessProbe']))

    def test_split_validation_rejects_ambiguous_or_secret_containing_config(self):
        for edit in [lambda c: c['services'].pop('embedding'),
                     lambda c: c['services']['transcription'].update(policy='round_robin'),
                     lambda c: c['services']['embedding']['auth'].update(secret='inline'),
                     lambda c: c['services']['embedding'].update(endpoint='https://key:secret@host')]:
            config = self.config()
            edit(config)
            with self.assertRaises(ValueError):
                ds.validate(config)
