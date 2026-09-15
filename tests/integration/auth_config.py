#!/usr/bin/env python3
"""Exercise tenant reconciliation against disposable auth-go/PostgreSQL containers."""
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from auth_config import apply_config, load_config, plan_changes, read_snapshot
from auth_operator import Operator


def docker(*args, **kwargs):
    return subprocess.run(['docker', *args], check=True, text=True, capture_output=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--auth-image', required=True)
    parser.add_argument('--postgres-image', required=True)
    parser.add_argument('--file', required=True, type=Path)
    args = parser.parse_args()
    desired = load_config(args.file)
    slug = desired['tenant']['slug']
    name = 'auth-config-test-' + uuid4().hex[:12]
    db, auth = name + '-db', name + '-auth'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    origin = 'http://127.0.0.1:' + str(port)
    with tempfile.TemporaryDirectory(prefix=name) as directory:
        private = Path(directory)
        private.chmod(0o700)
        env = private / 'auth.env'
        env.write_text('\n'.join([
            'SCITRERA_MT_DB_URL=postgres://auth:synthetic-test-only@' + db + ':5432/auth?sslmode=disable',
            'AUTH_PROXY_MODE=verify', 'AUTH_PROXY_LISTEN_ADDR=:8080',
            'AUTH_PROXY_TOKEN_HMAC_KEY=synthetic-auth-config-test-key-not-for-deployment',
            'SCITRERA_AUTH_ADMIN_ADDR=:8082', 'SCITRERA_AUTH_ADMIN_ORIGIN=' + origin,
            'SCITRERA_AUTH_ADMIN_TOKEN_FILE=/run/auth/operators.json',
        ]) + '\n')
        env.chmod(0o600)
        uid = str(os.getuid()) + ':' + str(os.getgid())
        try:
            docker('network', 'create', name)
            docker('run', '-d', '--pull', 'never', '--name', db, '--network', name,
                   '-e', 'POSTGRES_USER=auth', '-e', 'POSTGRES_DB=auth',
                   '-e', 'POSTGRES_PASSWORD=synthetic-test-only', args.postgres_image)
            deadline = time.monotonic() + 45
            while True:
                try:
                    docker('exec', db, 'pg_isready', '-U', 'auth', '-d', 'auth')
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() > deadline:
                        raise RuntimeError('Disposable PostgreSQL did not become ready') from None
                    time.sleep(0.25)
            docker('run', '--rm', '--pull', 'never', '--network', 'none', '--user', uid,
                   '-v', directory + ':/out', args.auth_image,
                   'bootstrap', '--token-file', '/out/operators.json', '--operator', 'operator')
            docker('run', '--rm', '--pull', 'never', '--network', name, '--env-file', str(env),
                   args.auth_image, 'migrate')
            docker('run', '-d', '--pull', 'never', '--name', auth, '--network', name, '--user', uid,
                   '--env-file', str(env), '-v', directory + ':/run/auth:ro',
                   '-p', '127.0.0.1:' + str(port) + ':8082', args.auth_image)
            token = json.loads((private / 'operators.json').read_text())['operators']['operator']
            operator = Operator(origin, token)
            try:
                before = read_snapshot(operator, slug)
                assert before['tenant'] is None
                assert plan_changes(desired, before)
                assert read_snapshot(operator, slug)['revision'] == before['revision']
                changes = apply_config(operator, desired)
                assert changes and not changes[0]['data']['enabled']
                actual = read_snapshot(operator, slug)
                assert actual['auth'] == desired['auth']
                assert actual['domains'] == desired['domains']
                assert actual['tenant']['enabled'] == desired['tenant']['enabled']
                assert apply_config(operator, desired) == []
                assert read_snapshot(operator, slug)['revision'] == actual['revision']

                # Changing domains really removes the old association and restores auto-add.
                changed = deepcopy(desired)
                changed['domains'] = ['changed.example.test']
                changes = apply_config(operator, changed)
                assert changes[0]['data']['auto_add'] is False
                assert read_snapshot(operator, slug)['domains'] == ['changed.example.test']
                apply_config(operator, desired)

                # An unrelated owner of the same domain is not reassigned silently.
                collision = deepcopy(desired)
                collision['tenant']['slug'] = 'collision'
                try:
                    apply_config(operator, collision)
                    raise AssertionError('Domain collision was accepted')
                except HTTPError as error:
                    assert error.code == 409
                failed = read_snapshot(operator, 'collision')
                assert failed['tenant']['enabled'] is False and failed['auth']['auto_add'] is False

                # Native API rejects a stale revision; no retry bypasses it.
                revision = read_snapshot(operator, slug)['revision']
                path = '/tenants/' + slug + '/auth'
                operator.call('PUT', path, {'auto_add': False}, revision=revision)
                try:
                    operator.call('PUT', path, {'auto_add': True}, revision=revision)
                    raise AssertionError('Stale edit was accepted')
                except HTTPError as error:
                    assert error.code == 409
                apply_config(operator, desired)
                assert operator.read('/users')['data'] == []
            finally:
                operator.call('DELETE', '/session')
            # Also exercise the public Python command and operator-file loading.
            for action in ('plan', 'apply'):
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/auth_config.py'), action,
                    '--file', str(args.file.resolve()), '--origin', origin,
                    '--operators', str(private / 'operators.json')], text=True, capture_output=True, check=True)
                if action == 'plan':
                    assert json.loads(result.stdout)['changes'] == []
                else:
                    assert '0 changes' in result.stdout
            print('PASS: real auth-go create, policy/domain readback, no-op rerun, domain replacement,')
            print('      conflict handling, interrupted-tenant isolation, no synthetic user creation, CLI plan/apply.')
        except Exception:
            log = subprocess.run(['docker', 'logs', auth], capture_output=True, text=True)
            # Synthetic test environment only; no deployment credentials are loaded.
            sys.stderr.write(log.stderr[-4000:])
            raise
        finally:
            subprocess.run(['docker', 'rm', '-fv', auth, db], capture_output=True)
            subprocess.run(['docker', 'network', 'rm', name], capture_output=True)


if __name__ == '__main__':
    main()
