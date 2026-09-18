# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import importlib.util
from pathlib import Path
import unittest
import httpx

spec = importlib.util.spec_from_file_location('embed_proxy', Path(__file__).resolve().parents[2] / 'services/embed-proxy/app.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, **kwargs):
        app = proxy.create_app('https://embed.example', 'key', 'secret', transport=httpx.MockTransport(handler), **kwargs)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://private') as client:
                return await client.post('/v1/transcribe', json={'images': ['synthetic']}, headers={'Modal-Key': 'caller'})

    async def test_repeated_continuations_are_gets_with_no_original_body(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['Modal-Key'], 'key')
            self.assertEqual(request.headers['Modal-Secret'], 'secret')
            if len(requests) < 4:
                return httpx.Response(303, headers={'Location': '/v1/transcribe?result=' + str(len(requests))})
            return httpx.Response(200, json={'results': []})
        response = await self.request(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r.method for r in requests], ['POST', 'GET', 'GET', 'GET'])
        self.assertTrue(requests[0].content)
        self.assertTrue(all(not r.content for r in requests[1:]))

    async def test_foreign_redirects_and_post_replays_are_rejected(self):
        for code, location in [(303, '//evil.example/x'), (303, 'http://embed.example/x'),
                               (303, 'https://user@embed.example/x'), (307, '/retry')]:
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(code, headers={'Location': location})
            response = await self.request(handler)
            self.assertEqual(response.status_code, 502)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('secret', response.text)

    async def test_deadline_and_auth_failure(self):
        async def slow(request):
            await asyncio.sleep(.1)
            return httpx.Response(200)
        self.assertEqual((await self.request(slow, deadline=.01)).status_code, 504)
        self.assertEqual((await self.request(lambda r: httpx.Response(401))).status_code, 401)

    async def test_redirect_loop_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(303, headers={'Location': '?result=1'})
        self.assertEqual((await self.request(handler)).status_code, 502)
        self.assertEqual(len(calls), 17)


class SplitProxyTests(unittest.IsolatedAsyncioTestCase):
    def application(self, embedding, transcription, **overrides):
        return proxy.create_split_app({
            'embedding': {'endpoint': 'https://embed.example', 'key': 'embed-key', 'token': 'embed-secret',
                          'transport': httpx.MockTransport(embedding), **overrides},
            'transcription': {'endpoint': 'https://ocr.example', 'key': 'ocr-key', 'token': 'ocr-secret',
                              'transport': httpx.MockTransport(transcription), **overrides}})

    async def test_dispatch_auth_health_and_continuation_keep_selected_role(self):
        calls = []
        def embedding(request):
            calls.append(request)
            self.assertEqual(request.headers['Modal-Key'], 'embed-key')
            return httpx.Response(200, json={'role': 'embedding'})
        def transcription(request):
            calls.append(request)
            self.assertEqual(request.headers['Modal-Key'], 'ocr-key')
            if request.method == 'POST':
                # Continuation path must not cause redispatch to embedding.
                return httpx.Response(303, headers={'location': '/v1/embeddings?result=1'})
            return httpx.Response(200, json={'role': 'transcription'})
        app = self.application(embedding, transcription)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
            self.assertEqual((await client.get('/health/live')).status_code, 200)
            self.assertEqual(calls, [])
            for path in ('/v1/embeddings', '/v1/embeddings/images', '/v1/embeddings/multi', '/v1/score', '/v1/ner'):
                self.assertEqual((await client.post(path, json={})).json()['role'], 'embedding')
            self.assertEqual((await client.post('/v1/transcribe', json={})).json()['role'], 'transcription')
            self.assertEqual(calls[-1].method, 'GET')
            self.assertFalse(calls[-1].content)
            self.assertEqual((await client.get('/health/embedding/ready')).json()['role'], 'embedding')
            self.assertEqual((await client.get('/v1/transcribe')).status_code, 404)
            self.assertEqual((await client.post('/health/ready')).status_code, 404)
            self.assertEqual(set((await client.get('/health/ready')).json()['services']), {'embedding', 'transcription'})

    async def test_other_configured_origin_is_still_forbidden_redirect(self):
        def bad(request):
            return httpx.Response(303, headers={'location': 'https://embed.example/v1/embeddings'})
        def never(request):
            self.fail('Credentials must never cross roles')
        app = self.application(never, bad)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
            self.assertEqual((await client.post('/v1/transcribe', json={})).status_code, 502)

    async def test_saturated_ocr_does_not_block_embedding_or_local_health(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={})
        app = self.application(lambda request: httpx.Response(200, json={}), slow, max_in_flight=1, max_queued=0)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
            task = asyncio.create_task(client.post('/v1/transcribe', json={}))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual((await client.post('/v1/transcribe', json={})).status_code, 503)
            self.assertEqual((await client.post('/v1/embeddings', json={})).status_code, 200)
            self.assertEqual((await client.get('/health/live')).status_code, 200)
            release.set()
            self.assertEqual((await task).status_code, 200)

    async def test_http_upstream_never_receives_modal_credentials_or_redirects(self):
        def handler(request):
            self.assertNotIn('Modal-Key', request.headers)
            self.assertNotIn('Modal-Secret', request.headers)
            return httpx.Response(303, headers={'location': '/continued'})
        services = {role: {'endpoint': 'http://' + role + ':61051', 'mode': 'http', 'transport': httpx.MockTransport(handler)}
                    for role in ('embedding', 'transcription')}
        app = proxy.create_split_app(services)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://proxy') as client:
            self.assertEqual((await client.post('/v1/transcribe', json={})).status_code, 502)

class SpoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_response_retains_admission_until_delivery_and_cleans_up(self):
        payload = b'x' * (2 * 1024 * 1024)
        upstream = proxy.Upstream('http://embedding:61051', mode='http', spool_responses=True,
            max_in_flight=1, max_queued=0, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)))
        try:
            response = await upstream.forward('POST', '/v1/embeddings/multi')
            self.assertEqual(upstream.pending, 1)
            self.assertEqual((await upstream.forward('POST', '/v1/embeddings')).status_code, 503)
            self.assertEqual(b''.join([chunk async for chunk in response.body_iterator]), payload)
            await response.background()
            self.assertEqual(upstream.pending, 0)
        finally:
            await upstream.client.aclose()

    async def test_disconnected_reader_releases_spool_admission_once(self):
        upstream = proxy.Upstream('http://embedding:61051', mode='http', spool_responses=True,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * (2 * 1024 * 1024))))
        try:
            response = await upstream.forward('POST', '/v1/embeddings/multi')
            await anext(response.body_iterator)
            await response.body_iterator.aclose()
            await response.background()
            self.assertEqual(upstream.pending, 0)
        finally:
            await upstream.client.aclose()
