# SPDX-License-Identifier: AGPL-3.0-only
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from auth_gate import Config, login_redirect, verify


class AuthGateTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("http://auth:8080/auth/verify", "https://app.example.test",
                             "https://auth.example.test", "example")

    def test_deep_link_preserved_and_origin_not_client_controlled(self):
        location = login_redirect(self.config, "GET", "/example/bids?x=1&y=2",
                                  {"Accept": "text/html", "Host": "attacker.test"})
        query = parse_qs(urlsplit(location).query)
        self.assertEqual(query["tenant"], ["example"])
        self.assertEqual(query["rd"], ["https://app.example.test/example/bids?x=1&y=2"])

    def test_clients_do_not_receive_html(self):
        for method, headers in (("PUT", {"Accept": "text/html"}), ("GET", {"Accept": "*/*"}),
                                ("GET", {"Accept": "text/html", "Upgrade": "websocket"}),
                                ("GET", {"Accept": "text/html", "Sec-Fetch-Dest": "empty"})):
            self.assertIsNone(login_redirect(self.config, method, "/anything", headers))

    def test_fixed_scope_and_untrusted_headers_stripped(self):
        response = Mock(status=200, headers={"X-Scitrera-User": "alice"})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        status, identity = verify(self.config, {"Cookie": "session=private", "X-Auth-Tenant-ID": "other",
                                               "X-Scitrera-User": "attacker"}, opener)
        self.assertEqual(status, 200)
        self.assertEqual(identity["X-Auth-Tenant-ID"], "example")
        self.assertEqual(identity["X-Scitrera-User"], "alice")
        request = opener.open.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {"tenant_id": ["example"], "workspace_id": ["auth-app"]})
        self.assertEqual(dict(request.header_items()), {"Cookie": "session=private"})

    def test_auth_outage_never_allows_or_redirects(self):
        for error, expected in ((URLError("unavailable"), 503),
                                (HTTPError("url", 401, "no session", {}, io.BytesIO()), 401),
                                (HTTPError("url", 403, "wrong tenant", {}, io.BytesIO()), 403),
                                (HTTPError("url", 302, "unexpected login", {}, io.BytesIO()), 503)):
            opener = Mock()
            opener.open.side_effect = error
            self.assertEqual(verify(self.config, {}, opener), (expected, {}))

    def test_invalid_return_paths_do_not_escape_origin(self):
        for path in ("//evil.test", "/\\evil.test", "/a\r\nLocation: evil"):
            location = login_redirect(self.config, "GET", path, {"Accept": "text/html"})
            self.assertEqual(parse_qs(urlsplit(location).query)["rd"], ["https://app.example.test/"])
