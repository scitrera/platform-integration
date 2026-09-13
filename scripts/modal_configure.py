#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare an optional Modal task orchestrator; makes no Modal API calls."""
import argparse
from pathlib import Path
import re
import yaml

from compose_tenants import load_tenants
from configure import certificate

ROOT = Path(__file__).resolve().parents[1]


def render(root, *, tenant, image, worker_image, worker_gateway, environment, env_file=None):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', tenant):
        raise ValueError('Invalid tenant slug')
    if not re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', worker_gateway):
        raise ValueError('Worker gateway must be a reachable hostname:port, without a URL scheme')
    if not 1 <= int(worker_gateway.rsplit(':', 1)[1]) <= 65535:
        raise ValueError('Invalid worker gateway port')
    tls = root / '.local' / tenant / 'tls'
    service = {
        'image': image,
        'environment': {
            'AETHER_GATEWAY': f'aether-{tenant}:50051',
            'AETHER_IMPLEMENTATION': 'modal',
            'AETHER_PROFILES': 'modal',
            'SCITRERA_TENANT': tenant,
            'AETHER_WORKER_GATEWAY': worker_gateway,
            'AETHER_TLS_CA_CERT': '/run/tls/ca.crt',
            'AETHER_TLS_CLIENT_CERT': '/run/tls/tls.crt',
            'AETHER_TLS_CLIENT_KEY': '/run/tls/tls.key',
            'MODAL_LAUNCHER': 'sandbox',
            'MODAL_WORKER_TLS_DIR': '/run/worker-tls',
            'MODAL_ENVIRONMENT': environment,
            'MODAL_APP_NAME': f'{tenant}-aether-tasks',
            'MODAL_IMAGE': worker_image,
            'MODAL_SANDBOX_TIMEOUT': '1h',
            'MODAL_CPU_REQUEST': '0.5',
            'MODAL_MEMORY_REQUEST': '1Gi',
        },
        'volumes': [f'{tls}/modal-orchestrator:/run/tls:ro',
                    f'{tls}/anonymous:/run/worker-tls:ro'],
        'networks': [f'tenant-{tenant}'],
        'depends_on': {f'acl-{tenant}': {'condition': 'service_completed_successfully'}},
        'restart': 'unless-stopped',
    }
    if env_file is not None:
        service['env_file'] = [str(env_file.resolve())]
    return {'services': {f'orchestrator-modal-{tenant}': service}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', required=True)
    parser.add_argument('--image', required=True, help='Compatible orchestrator image with worker-tls-dir support')
    parser.add_argument('--worker-image', required=True, help='Published worker image, preferably an immutable digest')
    parser.add_argument('--worker-gateway', required=True, help='Modal-reachable Aether TLS hostname:port')
    parser.add_argument('--environment', default='main', help='Existing Modal environment')
    parser.add_argument('--env-file', type=Path, help='Private orchestrator env file; otherwise Modal credentials come from Aether KV')
    args = parser.parse_args()
    if args.tenant not in {t['slug'] for t in load_tenants(ROOT)}:
        parser.error('Tenant is not configured in this installation')
    if args.env_file and (not args.env_file.is_file() or args.env_file.stat().st_mode & 0o077):
        parser.error('Orchestrator env file must exist and be owner-only (chmod 600)')
    document = render(ROOT, tenant=args.tenant, image=args.image, worker_image=args.worker_image,
                      worker_gateway=args.worker_gateway, environment=args.environment, env_file=args.env_file)
    tls = ROOT / '.local' / args.tenant / 'tls'
    if not (tls / 'ca/tls.key').is_file():
        parser.error('Run configure.py first')
    certificate(tls / 'modal-orchestrator', f'orc::modal::{args.tenant}', tls / 'ca')
    output = ROOT / '.local/compose.modal.yaml'
    # Preserve other tenants' optional orchestrators; no secret values in YAML.
    if output.exists():
        existing = yaml.safe_load(output.read_text())
        existing['services'].update(document['services'])
        document = existing
    output.write_text(yaml.safe_dump(document, sort_keys=False))
    output.chmod(0o600)
    print('Prepared Modal orchestrator. Verify remote TLS reachability and credentials before starting it.')


if __name__ == '__main__':
    main()
