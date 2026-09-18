#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Validate/render document services independently of LLM routes; never applies migrations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import yaml

from models import UniqueLoader

PROFILE = 'qwen-colmodern-unlimited-l4-v1'
MODELS = {
    'single': {'model': 'Qwen/Qwen3-VL-Embedding-2B', 'revision': '9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda',
               'dimensions': 1920, 'truncate': True, 'normalize_after_truncation': False},
    'multi': {'model': 'ModernVBERT/colmodernvbert-merged', 'revision': 'a8b921f15ec3c8ba4264d40e1dbd7d77ea1336f0', 'dimensions': 128, 'pool_factor': 1,
              'serving_engine': 'vllm==0.25.0', 'image_splitting': False},
    'ocr': {'model': 'baidu/Unlimited-OCR', 'revision': '07dea832e22aefee32ad281d4b80551282e1c168', 'output_contract': 'unlimited_ocr'},
}


def validate(config, *, check_secrets=False, allow_local_image=False):
    if isinstance(config, dict) and config.get('version') == 2:
        from document_services_v2 import validate as validate_v2
        return validate_v2(config, check_secrets=check_secrets, allow_local_image=allow_local_image)
    if not isinstance(config, dict) or set(config) - {'version', 'profile', 'transport', 'endpoint', 'proxy'}:
        raise ValueError('Unsupported document-service configuration fields')
    if config.get('version') != 1 or config.get('profile') != PROFILE:
        raise ValueError('Unsupported document-service profile/version')
    transport = config.get('transport')
    url = urlsplit(config.get('endpoint', ''))
    if transport not in {'modal', 'http'} or url.scheme not in {'http', 'https'} or not url.hostname:
        raise ValueError('Valid endpoint and modal/http transport required')
    if url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
        raise ValueError('Endpoint must contain only scheme, host and optional port')
    if transport == 'modal':
        if url.scheme != 'https':
            raise ValueError('Modal requires HTTPS')
        proxy = config.get('proxy', {})
        if set(proxy) != {'image', 'secret_name', 'key_env', 'secret_env'}:
            raise ValueError('Modal proxy requires image, secret_name, key_env and secret_env')
        local_image = allow_local_image and re.fullmatch(r'sha256:[a-f0-9]{64}', proxy['image'])
        if not local_image and not re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', proxy['image']):
            raise ValueError('Proxy image must be pinned by digest (local image IDs require --allow-local-image)')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,61}[a-z0-9]', proxy['secret_name']):
            raise ValueError('Invalid Kubernetes secret name')
        for field in ['key_env', 'secret_env']:
            name = proxy[field]
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', name):
                raise ValueError('Credentials must be named environment references')
            if check_secrets and not os.environ.get(name):
                raise ValueError(f'Required environment variable is missing: {name}')
    elif config.get('proxy'):
        raise ValueError('HTTP mode does not use Modal proxy credentials')
    return config


def render(config, tenant, storage_dimensions, *, allow_local_image=False):
    if isinstance(config, dict) and config.get('version') == 2:
        from document_services_v2 import render as render_v2
        return render_v2(config, tenant, storage_dimensions, allow_local_image=allow_local_image)
    validate(config, allow_local_image=allow_local_image)
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', tenant):
        raise ValueError('Invalid tenant slug')
    if storage_dimensions != 1920:
        raise ValueError('This profile needs prepared 1920-dimensional storage. Plan migration/re-embedding separately; no data is changed.')
    modal = config['transport'] == 'modal'
    proxy_name = f'embed-proxy-{tenant}'
    endpoint = f'http://{proxy_name}:8080' if modal else config['endpoint']
    env = {
        'MEMORYLAYER_EMBEDDING_PROVIDER': 'embed_server', 'MEMORYLAYER_EMBED_TRANSPORT': 'http',
        'MEMORYLAYER_EMBEDDING_MODEL': MODELS['single']['model'],
        'MEMORYLAYER_EMBEDDING_DIMENSIONS': '1920', 'MEMORYLAYER_EMBED_SERVER_URL': endpoint,
        'MEMORYLAYER_EMBED_SERVER_TIMEOUT': '1860', 'MEMORYLAYER_EMBED_IMAGE_BATCH_SIZE': '1',
        'MEMORYLAYER_EMBED_TEXT_BATCH_SIZE': '8', 'MEMORYLAYER_EMBED_TEXT_BATCH_BYTES': '3072',
        'MEMORYLAYER_DOCUMENT_TRANSCRIBE_ENABLED': 'true', 'MEMORYLAYER_TRANSCRIPTION_SERVICE': 'embed_server',
        'MEMORYLAYER_ENTITY_LINKER_PROVIDER': 'none', 'MEMORYLAYER_RENDER_DPI': '150',
        'MEMORYLAYER_EXTRACTION_SERVICE': 'default', 'MEMORYLAYER_DOCUMENT_CHAT_ENABLED': 'false',
        'MEMORYLAYER_DOCUMENT_CHAT_INGEST_ENABLED': 'false', 'MEMORYLAYER_VISUAL_TOKENIZER_ENABLED': 'false',
    }
    compose = {'services': {f'memorylayer-{tenant}': {'environment': env},
                            f'ml-migrate-{tenant}': {'environment': {'MEMORYLAYER_EMBEDDING_DIMENSIONS': '1920'}}}}
    helm_env = {k: v for k, v in env.items() if k not in {
        'MEMORYLAYER_EMBEDDING_DIMENSIONS', 'MEMORYLAYER_DOCUMENT_TRANSCRIBE_ENABLED',
        'MEMORYLAYER_EMBED_TRANSPORT', 'MEMORYLAYER_ENTITY_LINKER_PROVIDER'}}
    helm = {'embeddingDimensions': 1920, 'documentTranscription': True,
            'documentServices': {'enabled': True, 'profile': PROFILE, 'environment': helm_env, 'proxy': {'enabled': False}}}
    if modal:
        proxy = config['proxy']
        compose['services'][proxy_name] = {
            'image': proxy['image'], 'restart': 'unless-stopped', 'networks': [f'tenant-{tenant}'],
            'environment': {'MODAL_UPSTREAM': config['endpoint'],
                            'MODAL_KEY': '${' + proxy['key_env'] + ':?Configure Modal proxy key}',
                            'MODAL_SECRET': '${' + proxy['secret_env'] + ':?Configure Modal proxy secret}'},
            'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'healthcheck': {'test': ['CMD', 'python', '-c', "import urllib.request; urllib.request.urlopen('http://localhost:8080/health/live')"],
                            'interval': '10s', 'timeout': '3s', 'retries': 3}}
        compose['services'][f'memorylayer-{tenant}']['depends_on'] = {proxy_name: {'condition': 'service_healthy'}}
        helm_env['MEMORYLAYER_EMBED_SERVER_URL'] = 'http://127.0.0.1:8081'
        helm['documentServices']['proxy'] = {'enabled': True, 'image': proxy['image'],
                                            'upstream': config['endpoint'], 'secretName': proxy['secret_name']}
    identity = json.dumps(MODELS, sort_keys=True).encode()
    manifest = {'profile': PROFILE, 'models': MODELS, 'embedding_identity': hashlib.sha256(identity).hexdigest(),
                'storage_dimensions': 1920, 'reprocessing': 'explicit; never automatic', 'applied': False}
    if modal and config['proxy']['image'].startswith('sha256:'):
        helm = None  # Engine-local image IDs cannot be pulled by Kubernetes.
        manifest['deployment_targets'] = ['compose-local']
    return compose, helm, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'render'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--check-secrets', action='store_true')
    parser.add_argument('--allow-local-image', action='store_true',
                        help='Allow an engine-local immutable proxy image ID; omits Helm output')
    parser.add_argument('--tenant')
    parser.add_argument('--storage-dimensions', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        config = validate(yaml.load(args.config.read_text(), Loader=UniqueLoader), check_secrets=args.check_secrets, allow_local_image=args.allow_local_image)
        if args.action == 'render':
            if not args.tenant or not args.output:
                raise ValueError('render requires --tenant and a new --output directory')
            compose, helm, manifest = render(config, args.tenant, args.storage_dimensions, allow_local_image=args.allow_local_image)
            args.output.mkdir(parents=True, exist_ok=False)
            for name, value in [('compose.yaml', compose), ('helm.yaml', helm), ('manifest.yaml', manifest)]:
                if value is not None:
                    (args.output / name).write_text(yaml.safe_dump(value, sort_keys=False))
        print('Document services configuration valid; no services or data changed.')
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
