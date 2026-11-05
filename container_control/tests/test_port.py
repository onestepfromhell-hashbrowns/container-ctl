from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from container_control.port import PortDetector


class TestPortDetector(unittest.TestCase):
    def test_verify_status_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, pid, detail, start_time = PortDetector.verify_status(
                port=59999,
                recorded_pid=None,
                entry_point="scripts/app.py",
                root_dir=Path(tmpdir),
            )
            self.assertEqual(status, "STOPPED")
            self.assertIsNone(pid)

    def test_verify_status_zombie(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # PID that is definitely not running
            status, pid, detail, _ = PortDetector.verify_status(
                port=59999,
                recorded_pid=99999999,
                entry_point="scripts/app.py",
                root_dir=Path(tmpdir),
            )
            self.assertEqual(status, "ZOMBIE")
            self.assertEqual(pid, 99999999)

    @patch.object(PortDetector, "get_listening_pid", return_value=1234)
    @patch("container_control.port.ProcessManager.is_alive", return_value=True)
    @patch("container_control.port.ProcessManager.get_process_info", return_value=("python3 /app/scripts/app.py", "2026-09-02T12:00:00"))
    def test_verify_status_running(self, mock_info, mock_alive, mock_port):
        status, pid, detail, start_time = PortDetector.verify_status(
            port=8080,
            recorded_pid=1234,
            entry_point="scripts/app.py",
            root_dir=Path("/app"),
        )
        self.assertEqual(status, "RUNNING")
        self.assertEqual(pid, 1234)
        self.assertIn("Port: 8080", detail)

    @patch.object(PortDetector, "get_listening_pid", return_value=5678)
    @patch("container_control.port.ProcessManager.is_alive", return_value=True)
    @patch("container_control.port.ProcessManager.get_process_info", return_value=("nginx: master process", "2026-09-02T12:00:00"))
    def test_verify_status_conflict(self, mock_info, mock_alive, mock_port):
        status, pid, detail, start_time = PortDetector.verify_status(
            port=8080,
            recorded_pid=1234,
            entry_point="scripts/app.py",
            root_dir=Path("/app"),
        )
        self.assertEqual(status, "CONFLICT")
        self.assertEqual(pid, 5678)
        self.assertIn("occupied by OTHER", detail)


if __name__ == "__main__":
    unittest.main()
