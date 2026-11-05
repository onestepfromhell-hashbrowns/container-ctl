import shutil
import subprocess

import pytest

from container_ctl.path_scan_runner import SCAN_TOOL, path_scan_findings


def _scanner_python_has_yaml() -> bool:
    py = shutil.which("python3")      # scan-path-refs runs via `#!/usr/bin/env python3`
    return bool(py) and subprocess.run([py, "-c", "import yaml"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _scanner_python_has_yaml(), reason="scan-path-refs needs PyYAML (pip install -e '.[scan]')")
def test_real_tool_flags_workspace_in_prod_only():
    bad = {"name": "prod-x", "tier": "prod", "env": {"APP_ROOT": "/workspace"}}
    good = {"name": "prod-y", "tier": "prod", "env": {"APP_ROOT": "/opt/y"}}
    dev = {"name": "dev-z", "tier": "dev", "env": {"APP_ROOT": "/workspace"}}
    msgs = path_scan_findings([bad, good, dev])
    assert any("prod-x" in m and "R-001" in m for m in msgs)
    assert not any("prod-y" in m or "dev-z" in m for m in msgs)


def test_missing_tool_warns():
    assert path_scan_findings([{"name": "a"}], tool="/nonexistent")[0].startswith("[WARN]")


def test_unparsable_failure_is_error():
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="bad config")
    assert path_scan_findings([{"name": "a", "tier": "prod"}], runner=run, tool=SCAN_TOOL)[0].startswith("[ERROR]")
