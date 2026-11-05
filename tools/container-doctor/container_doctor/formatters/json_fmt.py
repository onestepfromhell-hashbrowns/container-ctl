"""
Compact JSON formatter for Container Doctor.
"""

import json
from container_doctor.core.models import DiagnosticReport


class JsonFormatter:
    """
    Single-line compact JSON formatter for programmatic agent parsing.
    """

    def format(self, report: DiagnosticReport) -> str:
        data = {
            "status": report.overall_status.value,
            "passed": report.passed_count,
            "failed": report.failed_count,
            "warnings": report.warn_count,
            "duration_ms": round(report.duration_total_ms, 2),
            "domains": report.domains_checked,
            "failed_items": [r.to_dict() for r in report.failed_items],
            "warning_items": [r.to_dict() for r in report.warning_items],
        }
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
