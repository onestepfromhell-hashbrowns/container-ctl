"""
Diagnostic engine for Container Doctor.
Central manager for check registration, filtering, and report generation.
"""

import time
from typing import Any, Dict, List, Optional, Set

from container_doctor.core.context import ExecutionContext
from container_doctor.core.models import (
    CheckDomain,
    CheckResult,
    CheckStatus,
    DiagnosticReport,
)


class DiagnosticEngine:
    """
    Central engine managing check registration, filtering, and report generation.
    """

    __slots__ = ("context", "_checkers")

    def __init__(self, context: ExecutionContext):
        self.context = context
        self._checkers: Dict[str, Any] = {}

    def register_checker(self, domain: str, checker: Any) -> None:
        """
        Register a domain checker instance or callable.
        """
        self._checkers[domain] = checker

    def get_registered_domains(self) -> List[str]:
        return list(self._checkers.keys())

    def run(self, include_domains: Optional[Set[str]] = None) -> DiagnosticReport:
        """
        Execute registered checkers and generate a diagnostic report.
        """
        start_time = time.perf_counter()
        results: List[CheckResult] = []
        domains_checked: List[str] = []

        # Validate include_domains against allowlisted values
        if include_domains:
            invalid_domains = [d for d in include_domains if d != "all" and not CheckDomain.is_valid(d)]
            if invalid_domains:
                elapsed = (time.perf_counter() - start_time) * 1000.0
                results.append(
                    CheckResult(
                        domain="engine",
                        name="domain_validation",
                        status=CheckStatus.FAIL,
                        message=f"Unknown domain(s) specified: {', '.join(invalid_domains)}",
                        expected=f"One or more valid domains: {', '.join(sorted(CheckDomain.all_values()))}",
                        actual=f"Invalid: {', '.join(invalid_domains)}",
                        remediation=f"Specify valid domains using --include {{{','.join(sorted(CheckDomain.all_values()))}}}",
                        duration_ms=elapsed,
                    )
                )
                return DiagnosticReport(
                    overall_status=CheckStatus.FAIL,
                    passed_count=0,
                    failed_count=1,
                    warn_count=0,
                    duration_total_ms=elapsed,
                    results=results,
                    domains_checked=[],
                )

        domains_to_run = (
            [d for d in self._checkers.keys() if d in include_domains]
            if include_domains and "all" not in include_domains
            else list(self._checkers.keys())
        )

        if not domains_to_run:
            elapsed = (time.perf_counter() - start_time) * 1000.0
            results.append(
                CheckResult(
                    domain="engine",
                    name="checker_execution",
                    status=CheckStatus.FAIL,
                    message="No matching domain checkers were found or executed.",
                    expected="At least one registered domain checker executed",
                    actual="0 checkers executed",
                    remediation="Check domain filters or registered checkers.",
                    duration_ms=elapsed,
                )
            )
            return DiagnosticReport(
                overall_status=CheckStatus.FAIL,
                passed_count=0,
                failed_count=1,
                warn_count=0,
                duration_total_ms=elapsed,
                results=results,
                domains_checked=[],
            )

        for domain in domains_to_run:
            checker = self._checkers[domain]
            domains_checked.append(domain)
            t0 = time.perf_counter()
            try:
                if hasattr(checker, "check"):
                    domain_results = checker.check(self.context)
                elif callable(checker):
                    domain_results = checker(self.context)
                else:
                    domain_results = []

                if isinstance(domain_results, list):
                    results.extend(domain_results)
                elif isinstance(domain_results, CheckResult):
                    results.append(domain_results)
            except Exception as e:
                elapsed = (time.perf_counter() - t0) * 1000.0
                results.append(
                    CheckResult(
                        domain=domain,
                        name=f"{domain}_execution",
                        status=CheckStatus.FAIL,
                        message=f"Checker raised unexpected exception: {str(e)}",
                        remediation="Check checker implementation and runtime environment logs.",
                        duration_ms=elapsed,
                    )
                )

        duration_total_ms = (time.perf_counter() - start_time) * 1000.0

        passed_count = sum(1 for r in results if r.status == CheckStatus.PASS)
        failed_count = sum(1 for r in results if r.status == CheckStatus.FAIL)
        warn_count = sum(1 for r in results if r.status == CheckStatus.WARN)

        if failed_count > 0:
            overall_status = CheckStatus.FAIL
        elif warn_count > 0:
            overall_status = CheckStatus.WARN
        else:
            overall_status = CheckStatus.PASS

        return DiagnosticReport(
            overall_status=overall_status,
            passed_count=passed_count,
            failed_count=failed_count,
            warn_count=warn_count,
            duration_total_ms=duration_total_ms,
            results=results,
            domains_checked=domains_checked,
        )
