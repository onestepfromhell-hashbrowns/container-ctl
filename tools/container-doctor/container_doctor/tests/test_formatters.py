"""
Tests for formatters and token budgets.
"""

import json
from container_doctor.core.models import (
    CheckResult,
    CheckStatus,
    DiagnosticReport,
)
from container_doctor.formatters import (
    CompactFormatter,
    DiffFormatter,
    FullFormatter,
    JsonFormatter,
)


def sample_pass_report():
    results = [
        CheckResult("mounts", "m1", CheckStatus.PASS, "Mount ok"),
        CheckResult("paths", "p1", CheckStatus.PASS, "Paths ok"),
        CheckResult("network", "n1", CheckStatus.PASS, "Network ok"),
        CheckResult("resources", "r1", CheckStatus.PASS, "Resources ok"),
        CheckResult("ssh", "s1", CheckStatus.PASS, "SSH ok"),
    ]
    return DiagnosticReport(
        overall_status=CheckStatus.PASS,
        passed_count=5,
        failed_count=0,
        warn_count=0,
        duration_total_ms=14.2,
        results=results,
        domains_checked=["mounts", "paths", "network", "resources", "ssh"],
    )


def sample_fail_report():
    results = [
        CheckResult("mounts", "opt_root_present", CheckStatus.FAIL, "Missing /opt/container-ctl", expected="/opt/container-ctl", actual="missing", remediation="Mount /opt/container-ctl"),
        CheckResult("paths", "p1", CheckStatus.PASS, "Paths ok"),
    ]
    return DiagnosticReport(
        overall_status=CheckStatus.FAIL,
        passed_count=1,
        failed_count=1,
        warn_count=0,
        duration_total_ms=15.1,
        results=results,
        domains_checked=["mounts", "paths"],
    )


def test_compact_formatter_pass():
    fmt = CompactFormatter()
    output = fmt.format(sample_pass_report())
    assert output.startswith("[PASS] Container doctor: 5/5 domains OK")
    assert "[14ms]" in output
    # Rough token estimation: 1 word ~= 1.3 tokens, characters count / 4
    assert len(output) < 120  # Well within 25 tokens


def test_compact_formatter_fail():
    fmt = CompactFormatter()
    output = fmt.format(sample_fail_report())
    assert output.startswith("[FAIL] Container doctor: 1 PASS, 1 FAIL")
    assert "mounts: FAIL" in output


def test_diff_formatter():
    fmt = DiffFormatter()
    pass_out = fmt.format(sample_pass_report())
    assert "[PASS]" in pass_out

    fail_out = fmt.format(sample_fail_report())
    assert "[-] [FAIL] mounts :: opt_root_present" in fail_out
    assert "- Expected: /opt/container-ctl" in fail_out
    assert "- Remedy:   Mount /opt/container-ctl" in fail_out


def test_json_formatter():
    fmt = JsonFormatter()
    out = fmt.format(sample_pass_report())
    data = json.loads(out)
    assert data["status"] == "PASS"
    assert data["passed"] == 5
    assert data["failed"] == 0
    assert "failed_items" in data


def test_full_formatter():
    fmt = FullFormatter()
    out = fmt.format(sample_fail_report())
    assert "CONTAINER DOCTOR DIAGNOSTIC REPORT" in out
    assert "[Domain: mounts]" in out
    assert "[FAIL]" in out
