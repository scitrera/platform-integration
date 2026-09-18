# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in split document services; never changes a running consumer."""
import hashlib
import json
import re

from document_services import MODELS, PROFILE
from document_services import render as render_v1
from document_services import validate as validate_v1

PROFILE_V2 = 'qwen-colmodern-dual-ocr-l4-v2'


def validate(config, *, check_secrets=False, allow_local_image=False):
    if set(config) - {'version', 'profile', 'proxy', 'services', 'consumer'} or not {'version', 'profile', 'proxy', 'services'} <= set(config) or config['version'] != 2 or config['profile'] != PROFILE_V2:
        raise ValueError('Unsupported split document-services configuration')
    if not {'image'} <= set(config['proxy']) or set(config['proxy']) - {'image', 'timeout_seconds'} or set(config['services']) != {'embedding', 'transcription'}:
        raise ValueError('Split configuration requires one proxy and both roles')
    timeout = config['proxy'].get('timeout_seconds', 900)
    if type(timeout) is not int or not 60 <= timeout <= 1800:
        raise ValueError('Proxy timeout_seconds must be an integer between 60 and 1800')
    consumer = config.get('consumer', {})
    if not isinstance(consumer, dict) or set(consumer) - {'embedding_concurrency', 'transcription_concurrency'}:
        raise ValueError('Unsupported consumer concurrency fields')
    for key, value in consumer.items():
        if type(value) is not int or not 1 <= value <= 32:
            raise ValueError('Consumer concurrency must be an integer between 1 and 32')
    image = config['proxy']['image']
    if not isinstance(image, str) or not (re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', image) or
                                         allow_local_image and re.fullmatch(r'sha256:[a-f0-9]{64}', image)):
        raise ValueError('Proxy image must be pinned by digest')
    for role, service in config['services'].items():
        allowed = {'transport', 'endpoint', 'auth', 'capabilities'} if role == 'embedding' else {'transport', 'endpoint', 'auth', 'providers', 'policy'}
        if not isinstance(service, dict) or set(service) - allowed:
            raise ValueError('Unsupported service fields')
        compatible = {'version': 1, 'profile': PROFILE, 'transport': service.get('transport'), 'endpoint': service.get('endpoint')}
        if service.get('transport') == 'modal':
            auth = service.get('auth', {})
            if set(auth) != {'secret_name', 'key_env', 'secret_env'}:
                raise ValueError('Each Modal role needs explicit credential references')
            compatible['proxy'] = {'image': image, **auth}
        elif service.get('auth'):
            raise ValueError('HTTP services do not accept Modal credentials')
        validate_v1(compatible, check_secrets=check_secrets, allow_local_image=allow_local_image)
        if role == 'embedding':
            caps = service.get('capabilities')
            if not isinstance(caps, list) or len(caps) != len(set(caps)) or set(caps) not in (
                    {'single_vector', 'multi_vector', 'score'}, {'single_vector', 'multi_vector', 'score', 'ner'}):
                raise ValueError('Embedding capabilities must include both vector modes and score; NER is optional')
        elif service.get('providers') not in (['unlimited_ocr', 'deepseek_ocr'], ['deepseek_ocr', 'unlimited_ocr']) or service.get('policy') != 'fallback':
            raise ValueError('v2 requires an ordered pair of Unlimited and DeepSeek OCR providers with fallback')
    return config


def render(config, tenant, storage_dimensions, *, allow_local_image=False):
    validate(config, allow_local_image=allow_local_image)
    name = 'embed-proxy-' + tenant
    compatible = {'version': 1, 'profile': PROFILE, 'transport': 'http', 'endpoint': f'http://{name}:8080'}
    compose, helm, legacy = render_v1(compatible, tenant, storage_dimensions)
    timeout = config['proxy'].get('timeout_seconds', 900)
    routing = {'version': 2, 'services': {}}
    env, mounts, credentials = {}, [], {}
    for role, service in config['services'].items():
        item = {'endpoint': service['endpoint'], 'mode': service['transport'],
                'max_in_flight': 32 if role == 'embedding' else 16,
                'max_queued': 64 if role == 'embedding' else 32, 'deadline': timeout}
        if role == 'embedding':
            item['capabilities'] = service['capabilities']
        if service['transport'] == 'modal':
            auth = service['auth']
            prefix = role.upper() + '_MODAL'
            item['auth'] = {'key_env': prefix + '_KEY', 'secret_env': prefix + '_SECRET'}
            for kind in ('KEY', 'SECRET'):
                target = f'/run/secrets/{role}-{kind.lower()}'
                env[prefix + '_' + kind + '_FILE'] = target
                source = auth[kind.lower() + '_env'] + '_FILE'
                mounts.append('${' + source + ':?Set private credential file path}:' + target + ':ro')
            credentials[role] = {'secretName': auth['secret_name']}
        routing['services'][role] = item
    env['EMBED_PROXY_CONFIG_JSON'] = json.dumps(routing, separators=(',', ':'))
    compose['services'][name] = {
        'image': config['proxy']['image'], 'restart': 'unless-stopped', 'networks': [f'tenant-{tenant}'],
        'environment': env, 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'mem_limit': '2g', 'volumes': [*mounts, name + '-spool:/tmp'],
        'healthcheck': {'test': ['CMD', 'python', '-c', "import urllib.request; urllib.request.urlopen('http://localhost:8080/health/live')"],
                        'interval': '10s', 'timeout': '3s', 'retries': 3}}
    compose.setdefault('volumes', {})[name + '-spool'] = {}
    compose['services'][f'memorylayer-{tenant}']['depends_on'] = {name: {'condition': 'service_healthy'}}
    consumer = compose['services'][f'memorylayer-{tenant}']['environment']
    consumer['MEMORYLAYER_GLINER2_NER_TIMEOUT'] = str(timeout + 60)
    consumer['MEMORYLAYER_EMBED_SERVER_TIMEOUT'] = str(timeout + 60)
    limits = config.get('consumer', {})
    client_limits = {
        'MEMORYLAYER_EMBED_IMAGE_CONCURRENCY': str(limits.get('embedding_concurrency', 2)),
        'MEMORYLAYER_EMBED_TEXT_CONCURRENCY': str(limits.get('embedding_concurrency', 1)),
        'MEMORYLAYER_EMBED_TRANSCRIPTION_CONCURRENCY': str(limits.get('transcription_concurrency', 2)),
    }
    client_limits['MEMORYLAYER_EMBED_TRANSCRIPTION_PROVIDERS'] = ','.join(
        name.replace('_', '-') for name in config['services']['transcription']['providers'])
    consumer.update(client_limits)
    # Capability availability never silently selects the extraction provider.
    helm['documentServices']['profile'] = PROFILE_V2
    helm['documentServices']['environment']['MEMORYLAYER_EMBED_SERVER_URL'] = 'http://127.0.0.1:8081'
    helm['documentServices']['environment']['MEMORYLAYER_GLINER2_NER_TIMEOUT'] = str(timeout + 60)
    helm['documentServices']['environment']['MEMORYLAYER_EMBED_SERVER_TIMEOUT'] = str(timeout + 60)
    helm['documentServices']['environment'].update(client_limits)
    helm['documentServices']['proxy'] = {'enabled': True, 'image': config['proxy']['image'],
                                         'routing': routing, 'credentials': credentials}
    embeddings = {key: MODELS[key] for key in ('single', 'multi')}
    ocr_models = {'unlimited_ocr': MODELS['ocr'], 'deepseek_ocr': {'model': 'deepseek-ai/DeepSeek-OCR-2',
        'revision': 'aaa02f3811945a91062062994c5c4a3f4c0af2b0', 'output_contract': 'deepseek_ocr'}}
    transcription = {'providers': [ocr_models[name] for name in config['services']['transcription']['providers']],
                     'policy': 'fallback'}
    identity = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    manifest = {'profile': PROFILE_V2, 'models': {'embedding': embeddings, 'transcription': transcription},
        'embedding_identity': identity(embeddings), 'transcription_identity': identity(transcription),
        'compatible_embedding_cache_identity': legacy['embedding_identity'], 'storage_dimensions': 1920,
        'ner_available': 'ner' in config['services']['embedding']['capabilities'],
        'reprocessing': 'explicit; never automatic', 'applied': False}
    if config['proxy']['image'].startswith('sha256:'):
        helm = None
        manifest['deployment_targets'] = ['compose-local']
    return compose, helm, manifest
