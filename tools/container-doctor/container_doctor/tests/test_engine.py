"""
Tests for Core Engine, Models, and Context detection.
"""

from unittest.mock import patch
from pathlib import Path
import pytest

from container_doctor.core.models import (
    CheckResult,
    CheckStatus,
    DiagnosticReport,
)
from container_doctor.core.context import (
    ExecutionContext,
    ExecutionMode,
    detect_runtime_environment,
    is_running_in_container,
)
from container_doctor.core.engine import DiagnosticEngine


def test_check_result_to_dict():
    res = CheckResult(
        domain="mounts",
        name="opt_root_ro",
        status=CheckStatus.PASS,
        message="Mounted read-only",
        expected="ro",
        actual="ro",
        duration_ms=1.234,
    )
    d = res.to_dict()
    assert d["domain"] == "mounts"
    assert d["name"] == "opt_root_ro"
    assert d["status"] == "PASS"
    assert d["expected"] == "ro"
    assert d["actual"] == "ro"
    assert d["duration_ms"] == 1.23


def test_diagnostic_report_aggregation():
    r1 = CheckResult(domain="mounts", name="c1", status=CheckStatus.PASS)
    r2 = CheckResult(domain="mounts", name="c2", status=CheckStatus.PASS)
    r3 = CheckResult(domain="paths", name="c3", status=CheckStatus.FAIL, remediation="Fix path")
    r4 = CheckResult(domain="network", name="c4", status=CheckStatus.WARN)

    report = DiagnosticReport(
        overall_status=CheckStatus.FAIL,
        passed_count=2,
        failed_count=1,
        warn_count=1,
        duration_total_ms=10.5,
        results=[r1, r2, r3, r4],
        domains_checked=["mounts", "paths", "network"],
    )

    assert not report.is_success
    assert len(report.failed_items) == 1
    assert report.failed_items[0].name == "c3"
    assert len(report.warning_items) == 1

    d = report.to_dict()
    assert d["status"] == "FAIL"
    assert d["passed"] == 2
    assert d["failed"] == 1
    assert d["warnings"] == 1
    assert len(d["failed_items"]) == 1


def test_context_detection_container():
    with patch("container_doctor.core.context.Path.exists") as mock_exists:
        mock_exists.side_effect = lambda: True
        # Or patch is_running_in_container directly
    with patch("container_doctor.core.context.is_running_in_container", return_value=True):
        ctx = detect_runtime_environment()
        assert ctx.mode == ExecutionMode.INSIDE
        assert ctx.is_container is True


def test_context_detection_host():
    with patch.object(Path, "exists", return_value=False):
        with patch.dict("os.environ", {}, clear=True):
            ctx = detect_runtime_environment()
            assert ctx.mode == ExecutionMode.HOST
            assert ctx.is_container is False


def test_context_forced_mode():
    ctx = detect_runtime_environment(forced_mode=ExecutionMode.HOST, target_container="my-ctn")
    assert ctx.mode == ExecutionMode.HOST
    assert ctx.target_container == "my-ctn"


def test_diagnostic_engine_run():
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    engine = DiagnosticEngine(ctx)

    class DummyChecker:
        def check(self, context):
            return [
                CheckResult(domain="mounts", name="m1", status=CheckStatus.PASS),
                CheckResult(domain="mounts", name="m2", status=CheckStatus.PASS),
            ]

    def dummy_path_checker(context):
        return [
            CheckResult(domain="paths", name="p1", status=CheckStatus.PASS),
        ]

    engine.register_checker("mounts", DummyChecker())
    engine.register_checker("paths", dummy_path_checker)

    report = engine.run()
    assert report.overall_status == CheckStatus.PASS
    assert report.passed_count == 3
    assert report.failed_count == 0
    assert report.domains_checked == ["mounts", "paths"]


def test_diagnostic_engine_filtering():
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    engine = DiagnosticEngine(ctx)

    engine.register_checker("mounts", lambda c: [CheckResult(domain="mounts", name="m1", status=CheckStatus.PASS)])
    engine.register_checker("paths", lambda c: [CheckResult(domain="paths", name="p1", status=CheckStatus.PASS)])

    report = engine.run(include_domains={"paths"})
    assert report.passed_count == 1
    assert report.domains_checked == ["paths"]


def test_diagnostic_engine_exception_handling():
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    engine = DiagnosticEngine(ctx)

    def failing_checker(c):
        raise RuntimeError("Disk read error")

    engine.register_checker("broken", failing_checker)
    report = engine.run()
    assert report.overall_status == CheckStatus.FAIL
    assert report.failed_count == 1
    assert "Disk read error" in report.results[0].message
