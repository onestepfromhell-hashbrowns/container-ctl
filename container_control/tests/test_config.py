import json
from pathlib import Path
import tempfile
import unittest

from container_control.config import ServiceConfig, load_service_config, find_config_file


class TestConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = ServiceConfig()
        self.assertEqual(cfg.name, "app")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.command, ["python3", "scripts/app.py"])
        self.assertEqual(cfg.pid_file, "app.pid")
        self.assertEqual(cfg.log_file, "logs/app.log")
        self.assertEqual(cfg.shutdown_timeout, 5.0)
        self.assertEqual(cfg.pre_start_hooks, [])
        self.assertEqual(cfg.post_start_hooks, [])
        self.assertEqual(cfg.pre_stop_hooks, [])
        self.assertIsNone(cfg.readiness_probe)

    def test_load_from_json_legacy_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            cfg_dir = tmp_path / "configs"
            cfg_dir.mkdir(parents=True)
            cfg_file = cfg_dir / "service_control.json"
            
            data = {
                "service": {
                    "name": "legacy-service",
                    "port": 9090,
                    "entry_point": "src/server.py",
                    "python_executable": "python3.11",
                    "pid_file": "run/server.pid",
                    "log_file": "logs/server.log",
                    "env": {"FOO": "BAR"}
                }
            }
            cfg_file.write_text(json.dumps(data), encoding="utf-8")

            cfg = load_service_config(base_dir=tmp_path)
            self.assertEqual(cfg.name, "legacy-service")
            self.assertEqual(cfg.port, 9090)
            self.assertEqual(cfg.command, ["python3.11", "src/server.py"])
            self.assertEqual(cfg.entry_point, "src/server.py")
            self.assertEqual(cfg.abs_entry_point, tmp_path / "src/server.py")
            self.assertEqual(cfg.env.get("FOO"), "BAR")

    def test_load_from_json_universal_format_with_hooks_and_probe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            cfg_dir = tmp_path / "configs"
            cfg_dir.mkdir(parents=True)
            cfg_file = cfg_dir / "service_control.json"
            
            data = {
                "service": {
                    "name": "universal-service",
                    "command": ["node", "dist/main.js", "--prod"],
                    "port": 3000,
                    "working_dir": "./dist",
                    "shutdown_timeout": 10.0,
                    "pre_start_hooks": ["echo 'hook 1'", "echo 'hook 2'"],
                    "post_start_hooks": ["echo 'started'"],
                    "pre_stop_hooks": ["echo 'stopping'"],
                    "readiness_probe": {
                        "type": "http",
                        "path": "/healthz",
                        "port": 3000,
                        "timeout_seconds": 2.5,
                        "expected_status": 200
                    }
                }
            }
            cfg_file.write_text(json.dumps(data), encoding="utf-8")

            cfg = load_service_config(base_dir=tmp_path)
            self.assertEqual(cfg.name, "universal-service")
            self.assertEqual(cfg.command, ["node", "dist/main.js", "--prod"])
            self.assertEqual(cfg.port, 3000)
            self.assertEqual(cfg.shutdown_timeout, 10.0)
            self.assertEqual(cfg.pre_start_hooks, ["echo 'hook 1'", "echo 'hook 2'"])
            self.assertEqual(cfg.post_start_hooks, ["echo 'started'"])
            self.assertEqual(cfg.pre_stop_hooks, ["echo 'stopping'"])
            self.assertIsNotNone(cfg.readiness_probe)
            self.assertEqual(cfg.readiness_probe.get("type"), "http")
            self.assertEqual(cfg.readiness_probe.get("path"), "/healthz")


if __name__ == "__main__":
    unittest.main()
