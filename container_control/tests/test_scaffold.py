import json
from pathlib import Path
import tempfile
import unittest

from container_control.scaffold import init_project


class TestScaffold(unittest.TestCase):
    def test_init_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            init_project(target_dir=tmp_path, service_name="demo", port=8888)

            entrypoint_sh = tmp_path / "entrypoint.sh"
            control_sh = tmp_path / "control.sh"
            cfg_file = tmp_path / "configs" / "service_control.json"

            self.assertTrue(entrypoint_sh.is_file())
            self.assertTrue(control_sh.is_file())
            self.assertTrue(cfg_file.is_file())
            self.assertTrue((tmp_path / "scripts" / "app.py").is_file())
            self.assertTrue((tmp_path / "logs").is_dir())

            # Verify contents
            entrypoint_content = entrypoint_sh.read_text(encoding="utf-8")
            self.assertIn("control.sh start --foreground", entrypoint_content)
            self.assertIn('exec "$@"', entrypoint_content)

            cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
            service_sec = cfg_data["service"]
            self.assertEqual(service_sec["name"], "demo")
            self.assertEqual(service_sec["port"], 8888)
            self.assertIn("command", service_sec)
            self.assertIn("pre_start_hooks", service_sec)
            self.assertIn("readiness_probe", service_sec)


if __name__ == "__main__":
    unittest.main()
