# SPDX-License-Identifier: AGPL-3.0-only
"""Private fixed-origin document-services adapter; no inference POST retries."""
import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

MAX_BODY = 12 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
INFERENCE = {'/v1/embeddings', '/v1/embeddings/multi', '/v1/embeddings/images', '/v1/transcribe', '/v1/score', '/v1/ner'}
HEALTH = {'/health', '/health/ready', '/health/load', '/health/gpu'}
ALLOWED = INFERENCE | HEALTH


def origin(url, *, allow_http=False):
    parsed = urlsplit(url)
    schemes = {'https', 'http'} if allow_http else {'https'}
    if parsed.username or parsed.password or parsed.scheme not in schemes or not parsed.hostname or parsed.fragment:
        raise ValueError('Expected credential-free upstream URL')
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


def secret(name):
    path = os.environ.get(name + '_FILE')
    value = Path(path).read_text().strip() if path else os.environ.get(name, '')
    if not value or '\n' in value or '\r' in value:
        raise ValueError(f'{name} is required')
    return value


class Upstream:
    def __init__(self, endpoint, key=None, token=None, *, mode='modal', transport=None, deadline=1800,
                 max_in_flight=16, max_queued=16, capabilities=None, spool_responses=False):
        if mode not in {'modal', 'http'}:
            raise ValueError('Unsupported upstream transport')
        self.allowed_origin = origin(endpoint, allow_http=mode == 'http')
        parsed = urlsplit(endpoint)
        if parsed.path not in ('', '/') or parsed.query:
            raise ValueError('Upstream must be an origin without a path or query')
        if mode == 'modal' and (not key or not token):
            raise ValueError('Modal proxy credentials are required')
        if deadline <= 0 or max_in_flight < 1 or max_queued < 0:
            raise ValueError('Invalid upstream limits')
        self.endpoint, self.mode, self.deadline = endpoint.rstrip('/'), mode, deadline
        self.headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
        if mode == 'modal':
            self.headers.update({'Modal-Key': key, 'Modal-Secret': token})
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(deadline, connect=min(10, deadline)),
            limits=httpx.Limits(max_connections=max_in_flight, max_keepalive_connections=max_in_flight, keepalive_expiry=4),
            follow_redirects=False, trust_env=False)
        self.capabilities = set(capabilities) if capabilities is not None else None
        self.capacity = max_in_flight + max_queued
        self.pending = 0
        self.spool_responses = spool_responses
        self.slots = asyncio.Semaphore(max_in_flight)

    async def forward(self, method, path, request=None):
        if self.pending >= self.capacity:
            return JSONResponse({'detail': 'Upstream queue full'}, status_code=503, headers={'Retry-After': '1'})
        self.pending += 1
        deferred = False
        try:
            async with asyncio.timeout(self.deadline), self.slots:
                # Queued requests do not buffer bodies until a forwarding slot opens.
                body = bytearray()
                if request is not None:
                    async with asyncio.timeout(30):
                        async for chunk in request.stream():
                            body.extend(chunk)
                            if len(body) > MAX_BODY:
                                return JSONResponse({'detail': 'Request too large'}, status_code=413)
                body = bytes(body)
                url = self.endpoint + path
                for _ in range(17):
                    async with self.client.stream(method, url, headers=self.headers, content=body) as response:
                        if response.status_code == 303 and self.mode == 'modal':
                            location = response.headers.get('location')
                            if not location:
                                raise ValueError('Missing continuation URL')
                            candidate = urljoin(str(response.url), location)
                            if origin(candidate) != self.allowed_origin:
                                raise ValueError('Foreign continuation origin')
                            url, method, body = candidate, 'GET', b''
                            continue
                        if 300 <= response.status_code < 400:
                            raise ValueError('Unexpected redirect')
                        outgoing = {name: response.headers[name] for name in ('content-type', 'retry-after') if name in response.headers}
                        if self.spool_responses and path not in HEALTH:
                            spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115 -- response owns lifetime
                            transferred = False
                            try:
                                size = 0
                                async for chunk in response.aiter_bytes(chunk_size=65536):
                                    size += len(chunk)
                                    if size > MAX_RESPONSE:
                                        raise ValueError('Upstream response too large')
                                    spool.write(chunk)
                                spool.seek(0)
                                closed = False
                                def cleanup(spool=spool):
                                    nonlocal closed
                                    if not closed:
                                        closed = True
                                        spool.close()
                                        self.pending -= 1
                                async def chunks(spool=spool, cleanup=cleanup):
                                    try:
                                        async with asyncio.timeout(self.deadline):
                                            while chunk := spool.read(65536):
                                                yield chunk
                                    finally:
                                        cleanup()
                                result = StreamingResponse(chunks(), status_code=response.status_code,
                                    headers=outgoing, background=BackgroundTask(cleanup))
                                # Admission includes downstream transmission, even for
                                # slow readers. Files close on completion/disconnection.
                                transferred = deferred = True
                                return result
                            finally:
                                if not transferred:
                                    spool.close()
                        result = bytearray()
                        async for chunk in response.aiter_bytes():
                            result.extend(chunk)
                            if len(result) > MAX_RESPONSE:
                                raise ValueError('Upstream response too large')
                        return Response(bytes(result), status_code=response.status_code, headers=outgoing)
                raise ValueError('Too many continuation redirects')
        except (TimeoutError, httpx.TimeoutException):
            return JSONResponse({'detail': 'Upstream deadline exceeded'}, status_code=504)
        except (ValueError, OSError, httpx.HTTPError):
            return JSONResponse({'detail': 'Upstream failed'}, status_code=502)
        finally:
            if not deferred:
                self.pending -= 1


def _application(upstreams, split):
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            for upstream in upstreams.values():
                await upstream.client.aclose()

    async def health(request):
        # Never wake a remote GPU for the local installation's routine probe.
        return JSONResponse({'status': 'healthy', 'mode': 'split' if split else 'combined'})

    async def proxy(request: Request):
        path, method = request.url.path, request.method
        if request.url.query:
            return JSONResponse({'detail': 'Unsupported query'}, status_code=404)
        if not split:
            if path not in ALLOWED or method != ('POST' if path in INFERENCE else 'GET'):
                return JSONResponse({'detail': 'Unsupported method/path'}, status_code=404)
            return await upstreams['combined'].forward(method, path, request)
        if method == 'POST' and path in INFERENCE:
            role = 'transcription' if path == '/v1/transcribe' else 'embedding'
            if path == '/v1/ner' and upstreams[role].capabilities is not None and 'ner' not in upstreams[role].capabilities:
                return JSONResponse({'detail': 'NER disabled by configuration'}, status_code=404)
            return await upstreams[role].forward(method, path, request)
        if method == 'GET':
            for role in upstreams:
                prefix = '/health/' + role
                if path == prefix or path.startswith(prefix + '/'):
                    translated = '/health' + path[len(prefix):]
                    if translated in HEALTH:
                        return await upstreams[role].forward(method, translated)
            if path in HEALTH:
                results = await asyncio.gather(*(upstream.forward('GET', path) for upstream in upstreams.values()))
                roles = {}
                for role, result in zip(upstreams, results, strict=True):
                    try:
                        detail = json.loads(result.body)
                    except (ValueError, UnicodeError):
                        detail = {'detail': 'Non-JSON upstream health response'}
                    roles[role] = {'status_code': result.status_code, 'result': detail}
                ready = all(result.status_code == 200 for result in results)
                return JSONResponse({'status': 'ready' if ready else 'not_ready', 'services': roles}, status_code=200 if ready else 503)
        return JSONResponse({'detail': 'Unsupported method/path'}, status_code=404)

    return Starlette(routes=[Route('/health/live', health), Route('/{path:path}', proxy, methods=['GET', 'POST'])], lifespan=lifespan)


def create_app(upstream, key, token, *, transport=None, deadline=1800):
    """Retained single-upstream API and environment contract."""
    return _application({'combined': Upstream(upstream, key, token, transport=transport, deadline=deadline)}, False)


def create_split_app(services):
    if set(services) != {'embedding', 'transcription'}:
        raise ValueError('Split configuration requires embedding and transcription')
    return _application({role: Upstream(**{'spool_responses': True, **config}) for role, config in services.items()}, True)


def factory():
    config_file = os.environ.get('EMBED_PROXY_CONFIG_FILE')
    config_json = os.environ.get('EMBED_PROXY_CONFIG_JSON')
    if config_file or config_json:
        config = json.loads(Path(config_file).read_text() if config_file else config_json)
        if config.get('version') != 2 or set(config) != {'version', 'services'}:
            raise ValueError('Unsupported routing configuration')
        services = {}
        for role, item in config['services'].items():
            service = dict(item)
            auth = service.pop('auth', None)
            if service.get('mode', 'modal') == 'modal':
                if not auth or set(auth) != {'key_env', 'secret_env'}:
                    raise ValueError('Modal auth references are required')
                service.update(key=secret(auth['key_env']), token=secret(auth['secret_env']))
            elif auth:
                raise ValueError('HTTP upstream cannot receive Modal credentials')
            services[role] = service
        return create_split_app(services)
    return create_app(os.environ['MODAL_UPSTREAM'], secret('MODAL_KEY'), secret('MODAL_SECRET'))
