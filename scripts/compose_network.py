"""Public addresses for browser-facing development services."""
# SPDX-License-Identifier: AGPL-3.0-only
from ipaddress import IPv4Address
import re
from urllib.parse import urlsplit


def validate_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname)
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
            or any(c.isspace() for c in value)):
        raise ValueError('Public origin must be an HTTP(S) origin with a DNS name or IPv4 address, without a path or credentials')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid public origin port')
    return parsed.scheme + '://' + parsed.netloc.lower()


def network_settings(env, *, initialized=False, bind_address=None, public_origin=None, fixture_public_origin=None):
    defaults = {
        'WEB_BIND_ADDRESS': '127.0.0.1',
        'PUBLIC_ORIGIN': 'http://127.0.0.1:' + str(env['WEB_PORT']),
        'FIXTURE_PUBLIC_ORIGIN': 'http://127.0.0.1:' + str(env['FIXTURE_IDP_PORT']),
    }
    public = validate_origin(public_origin or env.get('PUBLIC_ORIGIN', defaults['PUBLIC_ORIGIN']))
    host = urlsplit(public).hostname
    fixture = validate_origin(fixture_public_origin or env.get('FIXTURE_PUBLIC_ORIGIN',
        'http://' + host + ':' + str(env['FIXTURE_IDP_PORT'])))
    binding = str(IPv4Address(bind_address or env.get('WEB_BIND_ADDRESS', defaults['WEB_BIND_ADDRESS'])))
    settings = {'WEB_BIND_ADDRESS': binding, 'PUBLIC_ORIGIN': public,
                'PUBLIC_HOSTNAME': host, 'FIXTURE_PUBLIC_ORIGIN': fixture}
    if initialized:
        for key, fallback in defaults.items():
            if settings[key] != env.get(key, fallback):
                raise ValueError('Public addresses differ from this installation; use a fresh integration directory')
    return settings
