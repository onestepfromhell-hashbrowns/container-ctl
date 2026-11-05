"""
Diff-only mismatch formatter for Container Doctor.
"""

from container_doctor.core.models import CheckStatus, DiagnosticReport


class DiffFormatter:
    """
    Diff formatter suppressing passing checks and surfacing only discrepancies
    with remediation hints.
    """

    def format(self, report: DiagnosticReport, show_fix_hints: bool = True) -> str:
        lines = []
        ms_str = f"[{report.duration_total_ms:.0f}ms]"

        if report.overall_status == CheckStatus.PASS:
            return f"[PASS] Container doctor: all {report.passed_count} checks passed {ms_str}"

        header = f"[{report.overall_status.value}] Container doctor: {report.passed_count} PASS, {report.failed_count} FAIL, {report.warn_count} WARN {ms_str}"
        lines.append(header)

        mismatches = [r for r in report.results if r.status in (CheckStatus.FAIL, CheckStatus.WARN)]

        for item in mismatches:
            prefix = "[-]" if item.status == CheckStatus.FAIL else "[!]"
            lines.append(f"  {prefix} [{item.status.value}] {item.domain} :: {item.name}: {item.message}")
            if item.expected is not None:
                lines.append(f"      - Expected: {item.expected}")
            if item.actual is not None:
                lines.append(f"      - Actual:   {item.actual}")
            if show_fix_hints and item.remediation:
                lines.append(f"      - Remedy:   {item.remediation}")

        return "\n".join(lines)
