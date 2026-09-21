"""Private auth-go operator session client shared by installation tools."""
# SPDX-License-Identifier: AGPL-3.0-only
import http.cookiejar
import http.client
from ipaddress import ip_address
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request


def validate_origin(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(c.isspace() for c in origin)):
        raise ValueError("Supply an HTTP(S) operator origin without a path or credentials")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid operator port")
    if parsed.scheme == "http":
        try:
            local = ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname == "localhost"
        if not local:
            raise ValueError("Remote operator access requires HTTPS; HTTP is limited to loopback")
    return parsed.scheme + "://" + parsed.netloc.lower()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Operator redirects are not allowed", headers, fp)


class LoopbackTunnel(HTTPSHandler):
    """Keep HTTPS Host/Origin/cookie semantics over a local kubectl/SSH tunnel.

    No certificate verification is bypassed on a remote connection: the only
    plaintext transport allowed here is an explicitly selected loopback port.
    """
    def __init__(self, connect_to):
        super().__init__()
        target = urlsplit(validate_origin(connect_to))
        if target.scheme != "http":
            raise ValueError("Operator tunnel must be an HTTP loopback endpoint")
        self.target = target

    def https_open(self, req):
        req.add_unredirected_header("Host", req.host)
        def connection(host, timeout=15, **kwargs):
            return http.client.HTTPConnection(self.target.hostname, self.target.port or 80, timeout=timeout)
        return self.do_open(connection, req)


class Operator:
    def __init__(self,origin,token,operator="operator",connect_to=None):
        origin = validate_origin(origin)
        if not token or not operator:
            raise ValueError("An operator name and nonempty token are required")
        self.origin=origin
        self.base=origin+'/api/auth-admin/v1'
        handlers = [NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if connect_to:
            if urlsplit(origin).scheme != "https":
                raise ValueError("A tunnel requires the configured HTTPS operator origin")
            handlers.extend([ProxyHandler({}), LoopbackTunnel(connect_to)])
        self.opener=build_opener(*handlers)
        self.csrf=''
        deadline=time.monotonic()+60
        while True:
            try:
                result=self.call('POST','/session',{'operator':operator,'token':token})
                self.csrf=result['csrf_token']
                break
            except HTTPError:
                raise
            except (URLError, ConnectionError, TimeoutError):
                if time.monotonic()>deadline:raise RuntimeError('Auth operator plane was not ready within 60 seconds') from None
                time.sleep(0.5)

    def call(self,method,path,data=None,revision=None):
        headers={'Origin':self.origin,'Content-Type':'application/json'}
        if self.csrf:headers['X-CSRF-Token']=self.csrf
        if revision is not None:headers['If-Match']='"'+str(revision)+'"'
        request=Request(self.base+path,method=method,headers=headers,
                        data=json.dumps(data).encode() if data is not None else None)
        with self.opener.open(request,timeout=15) as response:
            return json.load(response)

    def read(self,path):return self.call('GET',path)

    def write(self,method,path,data):
        # Optimistic concurrency: conflicts fail rather than overwriting another operator.
        revision=self.read('/status')['revision']
        return self.call(method,path,data,revision)
