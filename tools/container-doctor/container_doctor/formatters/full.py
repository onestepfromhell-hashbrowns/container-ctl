"""
Full diagnostic tree formatter for Container Doctor.
"""

from container_doctor.core.models import CheckStatus, DiagnosticReport


class FullFormatter:
    """
    Verbose hierarchical diagnostic report detailing every domain and check item.
    """

    def format(self, report: DiagnosticReport) -> str:
        lines = []
        lines.append("=" * 65)
        lines.append(f" CONTAINER DOCTOR DIAGNOSTIC REPORT [{report.overall_status.value}]")
        lines.append(f" Duration: {report.duration_total_ms:.1f}ms | Passed: {report.passed_count} | Failed: {report.failed_count} | Warnings: {report.warn_count}")
        lines.append("=" * 65)

        # Group results by domain
        domains: dict = {}
        for r in report.results:
            domains.setdefault(r.domain, []).append(r)

        for domain, items in domains.items():
            lines.append(f"\n[Domain: {domain}]")
            for item in items:
                status_tag = f"[{item.status.value}]"
                lines.append(f"  {status_tag:<8} {item.name}: {item.message} ({item.duration_ms:.1f}ms)")
                if item.expected:
                    lines.append(f"           Expected:    {item.expected}")
                if item.actual:
                    lines.append(f"           Actual:      {item.actual}")
                if item.remediation:
                    lines.append(f"           Remediation: {item.remediation}")

        lines.append("\n" + "=" * 65)
        return "\n".join(lines)
