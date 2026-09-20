#!/usr/bin/env python3
"""Private HTTP ext_auth adapter for one configured tenant and public origin.

Envoy sends /authorize + the original path; NGINX sends /check with a trusted
X-Original-URI header and handles a 401 through /denied. Never publish this
listener. Only the gateway/web proxy may call it. No sessions are cached.
"""
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

IDENTITY = (
    "X-Scitrera-User", "X-Scitrera-Name", "X-Scitrera-Tenants",
    "X-Scitrera-Default-Tenant", "X-Auth-Principal-Type",
)
CREDENTIALS = ("Cookie", "Authorization")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Config:
    verify_url: str
    public_origin: str
    login_origin: str
    tenant: str
    workspace: str = "auth-app"
    timeout: float = 5.0

    def __post_init__(self):
        for value in (self.verify_url, self.public_origin, self.login_origin):
            url = urllib.parse.urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Invalid auth gate URL")
        for value in (self.public_origin, self.login_origin):
            if urllib.parse.urlsplit(value).path not in {"", "/"}:
                raise ValueError("Auth gate origins must not include a path")
        for value in (self.tenant, self.workspace):
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
                raise ValueError("Invalid auth scope")

    def scoped_verify_url(self):
        return self.verify_url + "?" + urllib.parse.urlencode(
            {"tenant_id": self.tenant, "workspace_id": self.workspace})


def login_redirect(config, method, path, headers):
    # Fetches, API clients and WebSocket handshakes receive 401, not login HTML.
    navigation = (method in {"GET", "HEAD"} and "text/html" in headers.get("Accept", "")
                  and headers.get("Sec-Fetch-Dest", "document") in {"document", "iframe"}
                  and not headers.get("Upgrade"))
    if not navigation:
        return None
    if (not path.startswith("/") or path.startswith("//") or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)):
        path = "/"
    return config.login_origin.rstrip("/") + "/?" + urllib.parse.urlencode(
        {"tenant": config.tenant, "rd": config.public_origin.rstrip("/") + path})


def verify(config, headers, opener=None):
    request = urllib.request.Request(config.scoped_verify_url(),
                                     headers={k: headers[k] for k in CREDENTIALS if headers.get(k)},
                                     method="GET")
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=config.timeout) as response:
            # /auth/verify is specified as 200. Unexpected upstream responses fail closed.
            if response.status != 200:
                return 503, {}
            identity = {key: response.headers.get(key, "") for key in IDENTITY}
            if not identity["X-Scitrera-User"]:
                return 503, {}
            identity["X-Auth-Tenant-ID"] = config.tenant
            identity["X-Auth-User-ID"] = identity["X-Scitrera-User"]
            return 200, identity
    except urllib.error.HTTPError as error:
        status = error.code if error.code in {401, 403} else 503
        error.close()
        return status, {}
    except (OSError, ValueError):
        return 503, {}


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        # Request lines and cookies are deliberately excluded from access logs.
        def log_message(self, format, *args):
            pass

        def do_request(self):
            path = self.path
            if path == "/healthz":
                self.respond(200)
                return
            nginx = path in {"/check", "/denied"}
            if nginx:
                original = self.headers.get("X-Original-URI", "/")
                method = self.headers.get("X-Original-Method", self.command)
            elif path.startswith("/authorize/") or path.startswith("/authorize?") or path == "/authorize":
                original = path[len("/authorize"):] or "/"
                method = self.command
            else:
                self.respond(404)
                return
            if path == "/denied":
                status, identity = 401, {}
            else:
                status, identity = verify(config, self.headers)
            if status == 401 and path != "/check":
                location = login_redirect(config, method, original, self.headers)
                if location:
                    self.respond(302, {"Location": location})
                    return
            self.respond(status, identity)

        def respond(self, status, headers=None):
            self.send_response(status)
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            # Never leave an unread original request body on a keepalive connection.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_request

    return Handler


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    config = Config(verify_url=os.environ["AUTH_GATE_VERIFY_URL"],
                    public_origin=os.environ["AUTH_GATE_PUBLIC_ORIGIN"],
                    login_origin=os.environ["AUTH_GATE_LOGIN_ORIGIN"],
                    tenant=os.environ["AUTH_GATE_TENANT"],
                    workspace=os.environ.get("AUTH_GATE_WORKSPACE", "auth-app"))
    # Bound concurrent verifier requests independently of proxy rate limits.
    server = BoundedServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), handler(config))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
