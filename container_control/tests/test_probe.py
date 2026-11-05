import http.server
from pathlib import Path
import socket
import socketserver
import subprocess
import threading
import time
import unittest

from container_control.probe import ReadinessProbe


def get_unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestReadinessProbe(unittest.TestCase):
    def test_no_probe_configured(self):
        ok, msg = ReadinessProbe.check(None)
        self.assertTrue(ok)
        self.assertIn("No probe configured", msg)

    def test_tcp_probe_success_and_failure(self):
        # 1. Test success on bound socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        bound_port = sock.getsockname()[1]

        try:
            ok, msg = ReadinessProbe.check({
                "type": "tcp",
                "port": bound_port,
                "timeout_seconds": 1.0,
            })
            self.assertTrue(ok)
            self.assertIn("reachable", msg)
        finally:
            sock.close()

        # 2. Test failure on unused port
        unused_port = get_unused_port()
        ok_fail, msg_fail = ReadinessProbe.check({
            "type": "tcp",
            "port": unused_port,
            "timeout_seconds": 0.2,
        })
        self.assertFalse(ok_fail)

    def test_http_probe_success(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/healthz":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, *args):
                pass

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            ok, msg = ReadinessProbe.check({
                "type": "http",
                "path": "/healthz",
                "port": port,
                "timeout_seconds": 1.0,
                "expected_status": 200,
            })
            self.assertTrue(ok)
            self.assertIn("HTTP 200 OK", msg)

            # Test 404 path
            ok404, msg404 = ReadinessProbe.check({
                "type": "http",
                "path": "/invalid",
                "port": port,
                "timeout_seconds": 1.0,
                "expected_status": 200,
            })
            self.assertFalse(ok404)
            self.assertIn("404", msg404)
        finally:
            server.shutdown()
            server.server_close()

    def test_exec_probe(self):
        ok, msg = ReadinessProbe.check({
            "type": "exec",
            "command": ["true"],
            "timeout_seconds": 2.0,
        })
        self.assertTrue(ok)

        ok_fail, msg_fail = ReadinessProbe.check({
            "type": "exec",
            "command": ["false"],
            "timeout_seconds": 2.0,
        })
        self.assertFalse(ok_fail)


if __name__ == "__main__":
    unittest.main()
