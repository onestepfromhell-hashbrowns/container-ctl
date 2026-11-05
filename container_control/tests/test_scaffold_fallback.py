"""
test_scaffold_fallback.py — RED tests for ticket #01.

These tests verify the observable behavior of the generated control.sh
when container-control is NOT available in PATH (native fallback mode).
All process-level tests will FAIL against the current CONTROL_SH_TEMPLATE
because it contains `exit 1` in the fallback branch.
"""
import json
import os
import random
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

pkg_root = Path(__file__).resolve().parent.parent.parent
if str(pkg_root) not in sys.path:
    sys.path.insert(0, str(pkg_root))

from container_control.scaffold import init_project  # noqa: E402


def _env_without_container_control() -> dict:
    """Expose fallback runtime tools while guaranteeing container-control is absent."""
    isolated_bin = Path(tempfile.mkdtemp())
    tools = ["bash", "env", "python3", "mkdir", "dirname", "cat", "rm",
             "nohup", "sleep", "tail", "touch"]
    for name in tools:
        executable = shutil.which(name)
        if executable:
            (isolated_bin / name).symlink_to(executable)
    env = os.environ.copy()
    env["PATH"] = str(isolated_bin)
    return env


def _free_test_port(socket_module) -> int:
    """Choose a free non-ephemeral port to reduce races with parallel local tests."""
    for port in random.sample(range(20000, 32000), 64):
        with socket_module.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("unable to reserve a fallback test port")


def _wait_for_port(port: int, timeout: float = 5.0) -> bool:
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _read_pid(pid_file: Path):
    try:
        return int(pid_file.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── Base class ────────────────────────────────────────────────────────────────

class FallbackTestBase(unittest.TestCase):
    PORT = 19871  # each subclass overrides this

    def setUp(self):
        import socket
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir_ctx.__enter__())
        self.PORT = _free_test_port(socket)
        init_project(target_dir=self.root, service_name="fallback-test-svc", port=self.PORT)
        self.control_sh = self.root / "control.sh"
        self.pid_file = self.root / "app.pid"
        self.env = _env_without_container_control()

    def tearDown(self):
        pid = _read_pid(self.pid_file)
        if pid and _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        self._tmpdir_ctx.__exit__(None, None, None)

    def _run(self, *args, timeout=12, **kw):
        return subprocess.run(
            [str(self.control_sh), *args],
            capture_output=True, text=True, timeout=timeout,
            env=self.env, cwd=str(self.root), **kw,
        )


# ── Static content tests ──────────────────────────────────────────────────────

class TestControlShContent(unittest.TestCase):
    """Generated control.sh must have no host-only paths or old exit-1 pattern."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir_ctx.__enter__())
        init_project(target_dir=self.root, service_name="content-test", port=19872)
        self.control_sh = self.root / "control.sh"

    def tearDown(self):
        self._tmpdir_ctx.__exit__(None, None, None)

    def test_no_old_exit1_error_message(self):
        content = self.control_sh.read_text()
        self.assertNotIn(
            "[Error] container-control utility not found in PATH or /opt/container-ctl.",
            content,
            "Generated control.sh still contains the old exit-1 error sentinel",
        )

    def test_no_opt_site_root_references(self):
        content = self.control_sh.read_text()
        self.assertNotIn("/opt/container-ctl", content,
                         "Generated control.sh leaks /opt/container-ctl host path")

    def test_no_hardcoded_home_paths(self):
        content = self.control_sh.read_text()
        self.assertNotIn("/home/alice", content,
                         "Generated control.sh leaks /home/alice host path")

    def test_has_native_fallback_implementation(self):
        content = self.control_sh.read_text()
        indicators = [
            "_json_get" in content,
            "native_start" in content,
            "NATIVE_FALLBACK" in content,
            "nohup" in content,
            ("service_control.json" in content and "python3 -c" in content),
        ]
        self.assertTrue(
            any(indicators),
            "control.sh does not appear to contain any native fallback implementation",
        )


# ── Process-level behavioral tests ───────────────────────────────────────────

class TestNativeFallbackStart(FallbackTestBase):
    PORT = 19871

    def test_start_returns_zero(self):
        result = self._run("start")
        self.assertEqual(result.returncode, 0,
                         f"start returned {result.returncode}\n"
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")

    def test_start_writes_pid_file(self):
        self._run("start")
        self.assertTrue(self.pid_file.exists(), "PID file not created after start")
        pid = _read_pid(self.pid_file)
        self.assertIsNotNone(pid, "PID file is empty")

    def test_start_process_is_alive(self):
        self._run("start")
        time.sleep(0.5)
        pid = _read_pid(self.pid_file)
        self.assertIsNotNone(pid)
        self.assertTrue(_pid_alive(pid), f"PID {pid} not alive after start")

    def test_start_port_reachable(self):
        self._run("start")
        reachable = _wait_for_port(self.PORT, timeout=8.0)
        self.assertTrue(reachable, f"Port {self.PORT} not reachable after start")


class TestNativeFallbackStop(FallbackTestBase):
    PORT = 19872

    def setUp(self):
        super().setUp()
        self._run("start")
        time.sleep(0.5)

    def test_stop_returns_zero(self):
        result = self._run("stop")
        self.assertEqual(result.returncode, 0,
                         f"stop returned {result.returncode}\n{result.stderr}")

    def test_stop_kills_process(self):
        pid = _read_pid(self.pid_file)
        self.assertIsNotNone(pid)
        self._run("stop")
        time.sleep(0.5)
        self.assertFalse(_pid_alive(pid), f"PID {pid} still alive after stop")

    def test_stop_removes_pid_file(self):
        self._run("stop")
        time.sleep(0.3)
        self.assertFalse(self.pid_file.exists(), "PID file still exists after stop")


class TestNativeFallbackStatus(FallbackTestBase):
    PORT = 19874

    def test_status_zero_when_running(self):
        self._run("start")
        self.assertTrue(_wait_for_port(self.PORT, timeout=6.0),
                        "service process started but configured port never became reachable")
        result = self._run("status")
        self.assertEqual(result.returncode, 0,
                         f"status exited {result.returncode} while service running\n"
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")

    def test_status_nonzero_when_stopped(self):
        result = self._run("status")
        self.assertNotEqual(result.returncode, 0,
                            "status returned 0 when no service was started")

    def test_status_nonzero_when_pid_alive_but_port_closed(self):
        process = subprocess.Popen(["sleep", "20"], env=self.env)
        try:
            self.pid_file.write_text(f"{process.pid}\n")
            result = self._run("status")
            self.assertNotEqual(result.returncode, 0,
                                "status returned 0 with a live PID and closed port")
            self.assertIn("Port", result.stdout)
            self.assertIn("CLOSED", result.stdout)
        finally:
            process.terminate()
            process.wait(timeout=5)


class TestNativeFallbackProbe(FallbackTestBase):
    PORT = 19875

    def test_probe_zero_when_healthy(self):
        self._run("start")
        _wait_for_port(self.PORT, timeout=8.0)
        result = self._run("probe", timeout=12)
        self.assertEqual(result.returncode, 0,
                         f"probe exited {result.returncode} when healthy\n"
                         f"stdout: {result.stdout}\nstderr: {result.stderr}")

    def test_probe_nonzero_when_stopped(self):
        result = self._run("probe", timeout=10)
        self.assertNotEqual(result.returncode, 0,
                            "probe returned 0 when no service running")


class TestNativeFallbackLogs(FallbackTestBase):
    PORT = 19876

    def test_logs_no_syntax_error_when_absent(self):
        result = self._run("logs")
        self.assertNotIn("syntax error", result.stderr.lower())
        self.assertNotIn("traceback", result.stderr.lower())

    def test_logs_shows_content_after_start(self):
        self._run("start")
        _wait_for_port(self.PORT, timeout=8.0)
        time.sleep(0.5)
        result = self._run("logs")
        self.assertEqual(result.returncode, 0,
                         f"logs exited {result.returncode}\n{result.stderr}")


class TestNativeFallbackPreStartHooks(FallbackTestBase):
    PORT = 19873  # separate port to avoid contention with other test classes

    def _set_hook(self, hook_cmd: str):
        cfg_path = self.root / "configs" / "service_control.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["service"]["pre_start_hooks"] = [hook_cmd]
        cfg_path.write_text(json.dumps(cfg, indent=2))

    def test_successful_hook_runs_and_service_starts(self):
        marker = self.root / "hook_ran.txt"
        self._set_hook(f"touch {marker}")
        result = self._run("start")
        self.assertEqual(result.returncode, 0, f"start failed: {result.stderr}")
        self.assertTrue(marker.exists(), "pre_start_hook did not run")

    def test_failing_hook_aborts_start(self):
        self._set_hook("exit 42")
        result = self._run("start")
        self.assertNotEqual(result.returncode, 0,
                            "start returned 0 despite failing hook")
        pid = _read_pid(self.pid_file)
        if pid is not None:
            self.assertFalse(_pid_alive(pid),
                             "Process alive despite failing pre_start_hook")


class TestNativeFallbackConfigSafety(unittest.TestCase):
    def setUp(self):
        import socket
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir_ctx.__enter__())
        port = _free_test_port(socket)
        init_project(target_dir=self.root, service_name="config-safety", port=port)
        self.control_sh = self.root / "control.sh"
        self.config_path = self.root / "configs/service_control.json"
        self.config = json.loads(self.config_path.read_text())
        self.env = _env_without_container_control()

    def tearDown(self):
        pid_path = Path(self.config["service"].get("pid_file", self.root / "app.pid"))
        pid = _read_pid(pid_path)
        if pid and _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        self._tmpdir_ctx.__exit__(None, None, None)

    def _write_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def _run(self, *args):
        return subprocess.run([str(self.control_sh), *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_command_arguments_keep_spaces_and_shell_metacharacters(self):
        output = self.root / "argument.txt"
        value = "one argument; touch should-not-exist"
        self.config["service"]["command"] = [
            sys.executable, "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
            str(output), value,
        ]
        self._write_config()
        result = self._run("start", "--foreground")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), value)
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_environment_values_are_data_not_shell_code(self):
        output = self.root / "environment.txt"
        marker = self.root / "injected"
        value = f"literal; touch {marker}"
        self.config["service"]["env"] = {"FALLBACK_VALUE": value}
        self.config["service"]["command"] = [
            sys.executable, "-c",
            "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.environ['FALLBACK_VALUE'])",
            str(output),
        ]
        self._write_config()
        result = self._run("start", "--foreground")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), value)
        self.assertFalse(marker.exists())

    def test_absolute_pid_and_log_paths_are_preserved(self):
        absolute_pid = self.root / "runtime" / "service.pid"
        absolute_log = self.root / "runtime" / "service.log"
        self.config["service"]["pid_file"] = str(absolute_pid)
        self.config["service"]["log_file"] = str(absolute_log)
        self._write_config()
        result = self._run("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(absolute_pid.exists())
        self.assertTrue(absolute_log.exists())
        self._run("stop")
        self.assertFalse(absolute_pid.exists())

    def test_foreground_sigterm_reaches_service_and_cleans_pid(self):
        process = subprocess.Popen([str(self.control_sh), "start", "--foreground"],
                                   cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        pid_file = self.root / "app.pid"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "foreground service did not write PID file")
        service_pid = _read_pid(pid_file)
        process.terminate()
        process.wait(timeout=8)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(pid_file.exists(), "foreground signal left stale PID file")
        self.assertIsNotNone(service_pid)
        self.assertFalse(_pid_alive(service_pid), "SIGTERM did not stop foreground service")

    def test_invalid_json_config_fails_closed(self):
        self.config_path.write_text("{ invalid json")
        result = self._run("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Native fallback config error", result.stderr)


if __name__ == "__main__":
    unittest.main()
