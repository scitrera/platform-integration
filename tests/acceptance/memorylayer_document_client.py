#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Check packaged document-client capabilities without model calls or network access."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import subprocess


async def check_packaged_client():
    from memorylayer_server.services.document.embed_client import EmbedServerClient
    from memorylayer_saas.api.v1.documents import DocumentPageStatus

    assert 'ingestion_status' in DocumentPageStatus.model_fields, 'Missing document lifecycle status'
    client = EmbedServerClient('http://unused.invalid', logger=logging.getLogger('check'),
        image_batch_size=1, image_concurrency=16, text_concurrency=16,
        transcription_concurrency=8, transcription_providers=['deepseek-ocr', 'unlimited-ocr'],
        text_batch_size=8, text_batch_bytes=3072)
    calls = []

    async def send(method, path, payload):
        assert len(payload['images']) == 1, 'Image batch limit ignored'
        calls.append(payload['provider'])
        return {'results': [{'page_index': 0, 'success': True, 'content': 'synthetic'}]}

    client.request_json = send
    result = await client.transcribe_pages(['synthetic'] * 8)
    assert len(result['results']) == 8
    assert calls == ['deepseek-ocr'] * 8, 'OCR provider order or batch splitting ignored'
    print(json.dumps({'single_page_requests': len(calls), 'lifecycle_status': True, 'network_used': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image')
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        asyncio.run(check_packaged_client())
    else:
        if not args.image:
            parser.error('--image is required')
        subprocess.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--entrypoint', 'python',
                        args.image, '-', '--inside'], input=Path(__file__).read_bytes(), check=True)


if __name__ == '__main__':
    main()
