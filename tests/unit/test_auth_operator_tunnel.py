import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from auth_operator import LoopbackTunnel, Operator


class TunnelTests(unittest.TestCase):
    def test_remote_plaintext_is_rejected(self):
        for origin in ["http://192.0.2.1:8082", "https://admin.example", "http://127.0.0.1:8082/path"]:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                LoopbackTunnel(origin)

    def test_tunnel_retains_host_origin_secure_cookie_and_csrf(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                requests.append(dict(self.headers))
                self.rfile.read(int(self.headers.get("Content-Length",0)))
                self.send_response(200)
                self.send_header("Set-Cookie","admin_session=fixture; Secure; HttpOnly; Path=/api/auth-admin/v1")
                self.end_headers();self.wfile.write(b'{"csrf_token":"fixture-csrf"}')
            def do_GET(self):
                requests.append(dict(self.headers));self.send_response(200);self.end_headers()
                self.wfile.write(b'{"revision":1}')
        server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            c=Operator("https://admin.example.test","fixture-token",connect_to="http://127.0.0.1:"+str(server.server_port))
            self.assertEqual(c.read("/status"),{"revision":1})
            for req in requests:
                self.assertEqual(req["Host"],"admin.example.test")
                self.assertEqual(req["Origin"],"https://admin.example.test")
            self.assertEqual(requests[1]["Cookie"],"admin_session=fixture")
            self.assertEqual(requests[1]["X-Csrf-Token"],"fixture-csrf")
        finally:
            server.shutdown();server.server_close();thread.join()

if __name__=="__main__":unittest.main()
