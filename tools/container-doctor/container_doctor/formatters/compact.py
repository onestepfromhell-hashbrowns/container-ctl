"""
Minimal / compact single-line formatter for Container Doctor.
"""

from container_doctor.core.models import CheckStatus, DiagnosticReport


class CompactFormatter:
    """
    Ultra-low token single-line formatter.
    Target: < 20 tokens for clean runs, compact summary on failure.
    """

    def format(self, report: DiagnosticReport) -> str:
        ms_str = f"[{report.duration_total_ms:.0f}ms]"
        
        if report.overall_status == CheckStatus.PASS:
            domains_str = ", ".join(report.domains_checked)
            total_checks = len(report.results)
            return f"[PASS] Container doctor: {len(report.domains_checked)}/{len(report.domains_checked)} domains OK ({domains_str}) {ms_str}"

        if report.overall_status == CheckStatus.WARN and report.failed_count == 0:
            warn_items = report.warning_items
            summary = f"[WARN] Container doctor: {report.passed_count} PASS, {report.warn_count} WARN"
            if warn_items:
                first = warn_items[0]
                summary += f" [{first.domain}: {first.name} ({first.message})]"
            return f"{summary} {ms_str}"

        # Failed
        fail_items = report.failed_items
        summary = f"[FAIL] Container doctor: {report.passed_count} PASS, {report.failed_count} FAIL"
        if fail_items:
            first = fail_items[0]
            summary += f" [{first.domain}: FAIL ({first.message})]"
            if len(fail_items) > 1:
                summary += f" (+{len(fail_items)-1} more)"
        return f"{summary} {ms_str}"
