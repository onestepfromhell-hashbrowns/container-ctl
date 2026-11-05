"""
Resource limits and cgroup checker for Container Doctor (container runtime).
Reconciles active cgroup v1/v2 memory limits and CPU quotas against declared bounds.
"""

from pathlib import Path
import time
from typing import List, Optional, Tuple

from container_doctor.checkers.base import BaseChecker
from container_doctor.core.context import ExecutionContext
from container_doctor.core.models import CheckDomain, CheckResult, CheckStatus


class ResourceChecker(BaseChecker):
    domain = CheckDomain.RESOURCES.value

    def __init__(self, cgroup_root: Path = Path("/sys/fs/cgroup")):
        self.cgroup_root = cgroup_root

    def _read_memory_limits(self) -> Tuple[Optional[int], Optional[int]]:
        """
        Returns (memory_limit_bytes, memory_current_bytes) or (None, None) if unlimited/unconstrained.
        """
        # Cgroups v2
        mem_max_file = self.cgroup_root / "memory.max"
        mem_current_file = self.cgroup_root / "memory.current"

        if mem_max_file.exists():
            try:
                max_txt = mem_max_file.read_text().strip()
                limit = None if max_txt in ("max", "") else int(max_txt)
                current = None
                if mem_current_file.exists():
                    current = int(mem_current_file.read_text().strip())
                return limit, current
            except Exception:
                pass

        # Cgroups v1
        mem_v1_limit = self.cgroup_root / "memory" / "memory.limit_in_bytes"
        mem_v1_usage = self.cgroup_root / "memory" / "memory.usage_in_bytes"
        if mem_v1_limit.exists():
            try:
                limit_val = int(mem_v1_limit.read_text().strip())
                limit = None if limit_val > (1024**5) else limit_val
                current = None
                if mem_v1_usage.exists():
                    current = int(mem_v1_usage.read_text().strip())
                return limit, current
            except Exception:
                pass

        return None, None

    def _read_cpu_quota(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Returns (effective_cpu_cores, raw_description) or (None, 'unlimited').
        """
        # Cgroups v2: cpu.max contains "quota period" (e.g., "200000 100000" or "max 100000")
        cpu_max_file = self.cgroup_root / "cpu.max"
        if cpu_max_file.exists():
            try:
                content = cpu_max_file.read_text().strip().split()
                if len(content) >= 2:
                    quota_str, period_str = content[0], content[1]
                    if quota_str == "max":
                        return None, "unlimited"
                    quota = int(quota_str)
                    period = int(period_str)
                    if period > 0:
                        return quota / period, f"{quota}/{period}"
            except Exception:
                pass

        # Cgroups v1: cpu/cpu.cfs_quota_us and cpu/cpu.cfs_period_us
        v1_quota_file = self.cgroup_root / "cpu" / "cpu.cfs_quota_us"
        v1_period_file = self.cgroup_root / "cpu" / "cpu.cfs_period_us"
        if v1_quota_file.exists() and v1_period_file.exists():
            try:
                quota = int(v1_quota_file.read_text().strip())
                period = int(v1_period_file.read_text().strip())
                if quota < 0:
                    return None, "unlimited"
                if period > 0:
                    return quota / period, f"{quota}/{period}"
            except Exception:
                pass

        return None, "unlimited"

    def check(self, context: ExecutionContext) -> List[CheckResult]:
        results: List[CheckResult] = []
        declared_res = context.declared_config.declared_resources if context.declared_config else None

        # 1. Check Memory Limit & Bounds
        t0 = time.perf_counter()
        limit, current = self._read_memory_limits()

        if limit is None:
            if declared_res and declared_res.memory_limit_bytes:
                decl_mb = declared_res.memory_limit_bytes / (1024 * 1024)
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_memory_bound",
                        status=CheckStatus.FAIL,
                        message=f"Declared memory limit ({decl_mb:.0f}MB) not enforced; runtime is unlimited",
                        expected=f"Memory limit <= {decl_mb:.0f}MB",
                        actual="unlimited / unconstrained",
                        remediation="Configure container memory limit in Quadlet or deploy JSON.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_memory_bound",
                        status=CheckStatus.WARN,
                        message="Memory limit: unlimited / unconstrained (no cgroup limit set; unbounded memory risk)",
                        expected="Constrained cgroup memory limit",
                        actual="unlimited",
                        remediation="Set container memory limit in Quadlet (.container) or deploy config.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
        else:
            limit_mb = limit / (1024 * 1024)
            current_mb = (current / (1024 * 1024)) if current is not None else 0.0
            usage_pct = (current / limit * 100.0) if (current is not None and limit > 0) else 0.0

            if declared_res and declared_res.memory_limit_bytes:
                decl_mb = declared_res.memory_limit_bytes / (1024 * 1024)
                if abs(limit_mb - decl_mb) > 5.0:
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="cgroup_memory_bound",
                            status=CheckStatus.WARN,
                            message=f"Memory limit drift: runtime={limit_mb:.0f}MB vs declared={decl_mb:.0f}MB",
                            expected=f"{decl_mb:.0f}MB",
                            actual=f"{limit_mb:.0f}MB",
                            remediation="Align runtime Quadlet memory limit with declared specification.",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="cgroup_memory_bound",
                            status=CheckStatus.PASS,
                            message=f"Memory limit configured: {limit_mb:.0f}MB (matches declared {decl_mb:.0f}MB)",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )
            elif usage_pct > 90.0:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_memory_bound",
                        status=CheckStatus.WARN,
                        message=f"High memory pressure: {current_mb:.1f}MB / {limit_mb:.1f}MB ({usage_pct:.1f}%)",
                        expected="Memory usage < 90%",
                        actual=f"{usage_pct:.1f}% used",
                        remediation="Increase container memory limit or kill memory-heavy processes.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_memory_bound",
                        status=CheckStatus.PASS,
                        message=f"Memory limit configured: {limit_mb:.0f}MB (Current: {current_mb:.1f}MB)",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )

        # 2. Check CPU Quota & Bounds
        t0 = time.perf_counter()
        cpu_cores, raw_cpu = self._read_cpu_quota()

        if cpu_cores is None:
            if declared_res and declared_res.cpu_cores:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_cpu_quota",
                        status=CheckStatus.FAIL,
                        message=f"Declared CPU quota ({declared_res.cpu_cores:.1f} cores) not enforced; runtime is unlimited",
                        expected=f"CPU quota <= {declared_res.cpu_cores:.1f} cores",
                        actual="unlimited / unconstrained",
                        remediation="Configure CPU quota in Quadlet or deploy JSON.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_cpu_quota",
                        status=CheckStatus.WARN,
                        message="CPU quota: unlimited / unconstrained (no cgroup CPU limit set)",
                        expected="Constrained cgroup CPU quota",
                        actual="unlimited",
                        remediation="Set CPU quota (e.g. CPUQuota=200%) in Quadlet or deploy config.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
        else:
            if declared_res and declared_res.cpu_cores:
                if abs(cpu_cores - declared_res.cpu_cores) > 0.1:
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="cgroup_cpu_quota",
                            status=CheckStatus.WARN,
                            message=f"CPU quota drift: runtime={cpu_cores:.1f} cores vs declared={declared_res.cpu_cores:.1f} cores",
                            expected=f"{declared_res.cpu_cores:.1f} cores",
                            actual=f"{cpu_cores:.1f} cores",
                            remediation="Align runtime CPU quota with declared specification.",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="cgroup_cpu_quota",
                            status=CheckStatus.PASS,
                            message=f"CPU quota configured: {cpu_cores:.1f} cores (matches declared)",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="cgroup_cpu_quota",
                        status=CheckStatus.PASS,
                        message=f"CPU quota configured: {cpu_cores:.1f} cores ({raw_cpu})",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )

        return results
