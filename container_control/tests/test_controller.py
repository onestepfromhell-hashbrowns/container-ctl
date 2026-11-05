import json
from pathlib import Path
import tempfile
import time
import unittest

from container_control.config import ServiceConfig
from container_control.controller import ServiceController


class TestServiceController(unittest.TestCase):
    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            entry_file = scripts_dir / "app.py"
            # Python script that stays alive
            entry_file.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

            cfg = ServiceConfig(
                name="test-app",
                port=None,
                command=["python3", "scripts/app.py"],
                pid_file="app.pid",
                log_file="logs/app.log",
                root_dir=tmp_path,
            )

            controller = ServiceController(config=cfg)
            status_before = controller.get_status()
            self.assertEqual(status_before["status"], "STOPPED")

            # Start
            started = controller.start(wait_seconds=0.2)
            self.assertTrue(started)

            status_running = controller.get_status()
            self.assertEqual(status_running["status"], "RUNNING")
            self.assertIsNotNone(status_running["pid"])

            # Stop
            stopped = controller.stop(timeout=2.0)
            self.assertTrue(stopped)

            status_after = controller.get_status()
            self.assertEqual(status_after["status"], "STOPPED")

    def test_pre_start_hook_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            marker = tmp_path / "hook_executed.txt"
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            entry_file = scripts_dir / "app.py"
            entry_file.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

            # 1. Success hook
            cfg_success = ServiceConfig(
                name="test-hooks",
                port=None,
                command=["python3", "scripts/app.py"],
                pre_start_hooks=[f"echo 'ready' > {marker}"],
                pid_file="app.pid",
                log_file="logs/app.log",
                root_dir=tmp_path,
            )
            ctrl_success = ServiceController(config=cfg_success)
            self.assertTrue(ctrl_success.start(wait_seconds=0.2))
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text().strip(), "ready")
            ctrl_success.stop()

            # 2. Failure hook
            cfg_fail = ServiceConfig(
                name="test-fail-hooks",
                port=None,
                command=["python3", "scripts/app.py"],
                pre_start_hooks=["false"],
                pid_file="app.pid",
                log_file="logs/app.log",
                root_dir=tmp_path,
            )
            ctrl_fail = ServiceController(config=cfg_fail)
            self.assertFalse(ctrl_fail.start(wait_seconds=0.2))
            status = ctrl_fail.get_status()
            self.assertEqual(status["status"], "STOPPED")

    def test_triple_verification_readiness_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            entry_file = scripts_dir / "server.py"
            
            # Simple HTTP server on dynamically passed port
            server_code = """
import http.server
import socketserver
import sys

port = int(sys.argv[1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args):
        pass

with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
    httpd.serve_forever()
"""
            entry_file.write_text(server_code, encoding="utf-8")

            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                test_port = s.getsockname()[1]

            cfg = ServiceConfig(
                name="test-probe-app",
                port=test_port,
                command=["python3", "scripts/server.py", str(test_port)],
                pid_file="app.pid",
                log_file="logs/app.log",
                readiness_probe={
                    "type": "http",
                    "path": "/health",
                    "port": test_port,
                    "timeout_seconds": 2.0,
                    "expected_status": 200,
                },
                root_dir=tmp_path,
            )

            ctrl = ServiceController(config=cfg)
            self.assertTrue(ctrl.start(wait_seconds=0.5))
            
            status = ctrl.get_status()
            self.assertEqual(status["status"], "RUNNING")
            self.assertTrue(status.get("probe_healthy"))

            # Test direct probe
            ok, msg = ctrl.probe()
            self.assertTrue(ok)
            self.assertIn("HTTP 200 OK", msg)

            ctrl.stop()


if __name__ == "__main__":
    unittest.main()
