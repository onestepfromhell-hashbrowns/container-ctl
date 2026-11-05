"""
Data models and domain definitions for Container Doctor.
Optimized for ultra-fast startup without heavy inspect/dataclasses overhead.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Set


class CheckDomain(str, Enum):
    MOUNTS = "mounts"
    PATHS = "paths"
    NETWORK = "network"
    RESOURCES = "resources"
    SSH = "ssh"

    @classmethod
    def all_values(cls) -> Set[str]:
        return {d.value for d in cls}

    @classmethod
    def is_valid(cls, val: str) -> bool:
        return val in cls.all_values()


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


class CheckResult:
    __slots__ = (
        "domain",
        "name",
        "status",
        "message",
        "expected",
        "actual",
        "remediation",
        "duration_ms",
    )

    def __init__(
        self,
        domain: str,
        name: str,
        status: CheckStatus,
        message: str = "",
        expected: Optional[str] = None,
        actual: Optional[str] = None,
        remediation: Optional[str] = None,
        duration_ms: float = 0.0,
    ):
        self.domain = domain
        self.name = name
        self.status = status
        self.message = message
        self.expected = expected
        self.actual = actual
        self.remediation = remediation
        self.duration_ms = duration_ms

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "domain": self.domain,
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "duration_ms": round(self.duration_ms, 2),
        }
        if self.expected is not None:
            data["expected"] = self.expected
        if self.actual is not None:
            data["actual"] = self.actual
        if self.remediation is not None:
            data["remediation"] = self.remediation
        return data


class DiagnosticReport:
    __slots__ = (
        "overall_status",
        "passed_count",
        "failed_count",
        "warn_count",
        "duration_total_ms",
        "results",
        "domains_checked",
    )

    def __init__(
        self,
        overall_status: CheckStatus,
        passed_count: int,
        failed_count: int,
        warn_count: int,
        duration_total_ms: float,
        results: Optional[List[CheckResult]] = None,
        domains_checked: Optional[List[str]] = None,
    ):
        self.overall_status = overall_status
        self.passed_count = passed_count
        self.failed_count = failed_count
        self.warn_count = warn_count
        self.duration_total_ms = duration_total_ms
        self.results = results if results is not None else []
        self.domains_checked = domains_checked if domains_checked is not None else []

    @property
    def is_success(self) -> bool:
        return self.overall_status in (CheckStatus.PASS, CheckStatus.WARN)

    @property
    def failed_items(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.FAIL]

    @property
    def warning_items(self) -> List[CheckResult]:
        return [r for r in self.results if r.status == CheckStatus.WARN]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.overall_status.value,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "warnings": self.warn_count,
            "duration_ms": round(self.duration_total_ms, 2),
            "domains": self.domains_checked,
            "results": [r.to_dict() for r in self.results],
            "failed_items": [r.to_dict() for r in self.failed_items],
        }
