"""
tests/test_scan.py — Self-tests for tools/scan-path-refs

執行方式：
    cd <packages>/container-ctl/tools/scan-path-refs
    python3 -m pytest tests/test_scan.py -v --tb=short

測試覆蓋：
    - 規則 R-001：config 檔案含 /workspace
    - 規則 R-002：Python 代碼含 /workspace
    - 規則 R-003：Python 代碼含硬編碼 /opt/<proj>
    - 規則 R-004：Quadlet 含 /home/<user>
    - 通過案例（clean fixture）
    - 例外路徑不觸發違規
    - --dry-run 模式 exit 0
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ── Path Setup ────────────────────────────────────────────────────────────────

TOOL_DIR = Path(__file__).resolve().parent.parent
SCANNER = TOOL_DIR / "scan-path-refs"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
VIOLATIONS_DIR = FIXTURES_DIR / "violations"
CLEAN_DIR = FIXTURES_DIR / "clean"
CORE_RULES = TOOL_DIR / "rules" / "core.yaml"


def run_scanner(*args: str, target: str = ".") -> subprocess.CompletedProcess[str]:
    """Helper to invoke the scanner as a subprocess."""
    return subprocess.run(
        [sys.executable, str(SCANNER), "--rules", str(CORE_RULES), "--target", target, *args],
        capture_output=True,
        text=True,
    )


# ── Fixture Creation ──────────────────────────────────────────────────────────


@pytest.fixture()
def shared_fixtures(tmp_path: Path) -> Path:
    """Create clean and violation fixture files under tmp_path. Return the root."""
    violations_dir = tmp_path / "violations"
    clean_dir = tmp_path / "clean"
    violations_dir.mkdir(parents=True)
    clean_dir.mkdir(parents=True)

    # Violation fixtures
    (violations_dir / "bad_config.yaml").write_text(
        "database:\n  host: mydb\n  data_dir: /workspace/data\n"
    )
    (violations_dir / "bad_code.py").write_text(
        textwrap.dedent("""\
        # This file hardcodes /workspace — should be caught by R-002
        DATA_DIR = "/workspace/data"
        """)
    )
    (violations_dir / "bad_hardcoded.py").write_text(
        textwrap.dedent("""\
        # This file hardcodes /opt/myapp — should be caught by R-003
        ROOT = "/opt/myapp"
        """)
    )
    (violations_dir / "bad.container").write_text(
        "Environment=HOME=/home/alice\nExecStart=/bin/myapp\n"
    )

    # Clean fixtures
    (clean_dir / "good.yaml").write_text(
        "database:\n  host: mydb\n  data_dir: ${APP_DATA_DIR}\n"
    )
    (clean_dir / "good.py").write_text(
        textwrap.dedent("""\
        import os
        from pathlib import Path
        DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/var/opt/myapp"))
        """)
    )
    (clean_dir / "good.container").write_text(
        "Environment=APP_ROOT=/opt/myapp\nExecStart=/bin/myapp\n"
    )

    return tmp_path


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestViolations:
    """R-001 through R-004: Each violation fixture should trigger exit code 1."""

    def test_r001_workspace_in_yaml(self, shared_fixtures: Path) -> None:
        result = run_scanner(target=str(shared_fixtures / "violations"))
        assert result.returncode == 1, f"Expected exit 1, got {result.returncode}\n{result.stdout}"
        assert "VIOLATION" in result.stdout

    def test_r002_workspace_in_python(self, shared_fixtures: Path) -> None:
        isolated = shared_fixtures / "only_r002"
        isolated.mkdir(exist_ok=True)
        (isolated / "bad_code.py").write_text('/workspace/data\n')
        result = run_scanner(target=str(isolated))
        assert result.returncode == 1
        assert "R-002" in result.stdout

    def test_r003_hardcoded_opt_path(self, shared_fixtures: Path) -> None:
        isolated = shared_fixtures / "only_r003"
        isolated.mkdir(exist_ok=True)
        (isolated / "bad_hardcoded.py").write_text('ROOT = "/opt/myapp"\n')
        result = run_scanner(target=str(isolated))
        assert result.returncode == 1
        assert "R-003" in result.stdout

    def test_r004_home_path_in_container(self, shared_fixtures: Path) -> None:
        isolated = shared_fixtures / "only_r004"
        isolated.mkdir(exist_ok=True)
        (isolated / "bad.container").write_text("Environment=HOME=/home/alice\n")
        result = run_scanner(target=str(isolated))
        assert result.returncode == 1
        assert "R-004" in result.stdout


class TestCleanFiles:
    """Clean fixture files should produce exit code 0."""

    def test_clean_files_pass(self, shared_fixtures: Path) -> None:
        result = run_scanner(target=str(shared_fixtures / "clean"))
        assert result.returncode == 0, (
            f"Expected exit 0, got {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
        assert "OK" in result.stdout


class TestDryRun:
    """--dry-run should print violations but exit with code 0."""

    def test_dry_run_exits_zero_despite_violations(self, shared_fixtures: Path) -> None:
        result = run_scanner("--dry-run", target=str(shared_fixtures / "violations"))
        assert result.returncode == 0, f"dry-run should exit 0, got {result.returncode}"
        assert "VIOLATION" in result.stdout


class TestProjectExceptions:
    """Project-level .scan-path-refs.yaml should suppress matched violations."""

    def test_project_exception_suppresses_violation(self, tmp_path: Path) -> None:
        # Create an isolated directory with only a file that would trigger R-002
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "fixture.py").write_text('path = "/workspace/data"\n')

        # Write a project config that exempts tests/**
        (tmp_path / ".scan-path-refs.yaml").write_text(
            "version: '1'\nexceptions:\n  - rule_id: R-002\n    paths: ['tests/**']\n    reason: test fixture\n"
        )

        result = run_scanner("--config", ".scan-path-refs.yaml", target=str(tmp_path))
        assert result.returncode == 0, (
            f"Expected exception to suppress violation, got exit {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )


class TestContainerServiceBoundaries:
    def test_r006_allows_var_opt_root(self, tmp_path: Path):
        f = tmp_path / "workspace" / "scripts" / "run.sh"
        f.parent.mkdir(parents=True)
        f.write_text("DATA=/var/opt/container-ctl/projects/ServiceX/var\n")
        result = run_scanner(target=str(tmp_path / "workspace"))
        assert "R-006" not in result.stdout + result.stderr

    @pytest.mark.parametrize(
        ("relative", "content", "rule_id"),
        [
            ("bin/run.sh", "container-ctl validate project\n", "R-005"),
            ("scripts/deploy.sh", "ROOT=/opt/container-ctl\n", "R-006"),
            ("control.sh", "/home/alice/.local/bin/container-control\n", "R-007"),
        ],
    )
    def test_rejects_host_boundary_patterns(self, tmp_path: Path, relative: str, content: str, rule_id: str):
        target = tmp_path / "workspace"
        file_path = target / relative
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        result = run_scanner(target=str(target))
        assert result.returncode == 1, result.stdout + result.stderr
        assert rule_id in result.stdout

    def test_clean_extensionless_bin_script_passes(self, tmp_path: Path) -> None:
        target = tmp_path / "workspace"
        script = target / "bin" / "healthcheck"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\nexec ../control.sh probe\n")
        result = run_scanner(target=str(target))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_rules_do_not_scan_context_or_adr_docs(self, tmp_path: Path) -> None:
        target = tmp_path / "workspace"
        docs = target / "docs"
        docs.mkdir(parents=True)
        (docs / "CONTEXT.md").write_text("container-ctl and /opt/container-ctl are host concepts\n")
        result = run_scanner(target=str(target))
        assert result.returncode == 0, result.stdout + result.stderr
