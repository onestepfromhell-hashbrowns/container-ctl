from pathlib import Path
from container_params.checker import AntiPatternChecker, AntiPatternType


def test_ap1_absolute_path_detection(tmp_path):
    bad_file = tmp_path / "bad_paths.py"
    bad_file.write_text("""
def load_config():
    with open("/opt/shipping-status/app/configs/template.json") as f:
        return f.read()
""")
    checker = AntiPatternChecker()
    violations = checker.scan_file(bad_file)
    assert len(violations) >= 1
    assert any(v.pattern_type == AntiPatternType.AP1_ABSOLUTE_PATH for v in violations)


def test_ap2_env_branching_detection(tmp_path):
    bad_file = tmp_path / "bad_branch.py"
    bad_file.write_text("""
import os

def get_data():
    if os.getenv("APP_ENV") == "dev":
        return "mock"
    return "real"
""")
    checker = AntiPatternChecker()
    violations = checker.scan_file(bad_file)
    assert len(violations) >= 1
    assert any(v.pattern_type == AntiPatternType.AP2_ENV_BRANCHING for v in violations)


def test_ap3_hardcoded_endpoint_detection(tmp_path):
    bad_file = tmp_path / "bad_endpoint.py"
    bad_file.write_text("""
GATEWAY_URL = "http://svc-dev-webservice-v1.0.0-shared:8273"
LOCAL_URL = "http://localhost:8001/api"
""")
    checker = AntiPatternChecker()
    violations = checker.scan_file(bad_file)
    assert len(violations) >= 2
    assert all(v.pattern_type == AntiPatternType.AP3_HARDCODED_ENDPOINT for v in violations)


def test_ap4_control_script_path_detection(tmp_path):
    bad_script = tmp_path / "control.sh"
    bad_script.write_text("""#!/usr/bin/env bash
PID_FILE="/workspace/run/service.pid"
""")
    checker = AntiPatternChecker()
    violations = checker.scan_file(bad_script)
    assert len(violations) >= 1
    assert any(v.pattern_type == AntiPatternType.AP4_CONTROL_SCRIPT_PATH for v in violations)


def test_ap5_hardcoded_credentials_detection(tmp_path):
    bad_file = tmp_path / "bad_db.py"
    bad_file.write_text("""
DB_URI = "postgresql://app_user:secret_pass_123@postgres:5432/app_db"
""")
    checker = AntiPatternChecker()
    violations = checker.scan_file(bad_file)
    assert len(violations) >= 1
    assert any(v.pattern_type == AntiPatternType.AP5_HARDCODED_CREDENTIALS for v in violations)


def test_whitelist_skips_detection(tmp_path):
    bad_file = tmp_path / "fixtures" / "sample.py"
    bad_file.parent.mkdir()
    bad_file.write_text('DB_URI = "postgresql://user:pass@host:5432/db"')

    checker = AntiPatternChecker(whitelist_paths=["fixtures"])
    violations = checker.scan_file(bad_file)
    assert len(violations) == 0
