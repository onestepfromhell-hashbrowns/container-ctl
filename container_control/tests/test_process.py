import os
from pathlib import Path
import tempfile
import time
import unittest

from container_control.process import ProcessManager


class TestProcessManager(unittest.TestCase):
    def test_pid_file_io(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = Path(tmpdir) / "test.pid"
            self.assertIsNone(ProcessManager.read_pid(pid_file))
            
            ProcessManager.write_pid(pid_file, 12345)
            self.assertEqual(ProcessManager.read_pid(pid_file), 12345)
            
            ProcessManager.remove_pid(pid_file)
            self.assertIsNone(ProcessManager.read_pid(pid_file))

    def test_is_alive_and_kill(self):
        # Current process should be alive
        self.assertTrue(ProcessManager.is_alive(os.getpid()))
        # Non-existent high PID should be false
        self.assertFalse(ProcessManager.is_alive(99999999))

    def test_start_detached_and_kill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            log_file = tmp_path / "app.log"
            # Spawn a simple python sleep process
            cmd = ["python3", "-c", "import time; time.sleep(10)"]
            pid = ProcessManager.start_detached(cmd, tmp_path, log_file)
            
            self.assertTrue(ProcessManager.is_alive(pid))
            self.assertTrue(log_file.exists())
            
            # Kill process
            stopped = ProcessManager.kill_process(pid, timeout=2.0)
            self.assertTrue(stopped)
            self.assertFalse(ProcessManager.is_alive(pid))


if __name__ == "__main__":
    unittest.main()
