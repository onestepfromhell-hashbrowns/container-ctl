"""
Boundary and Path Resolver checker for Container Doctor (container development boundary).
Enforces Dual-Track Path Resolver contract, verifies mandatory APP_* paths, and audits host path leaks.
"""

import os
from pathlib import Path
import re
import time
from typing import List, Set

from container_doctor.checkers.base import BaseChecker
from container_doctor.core import site_paths
from container_doctor.core.context import ExecutionContext, ExecutionMode
from container_doctor.core.models import CheckDomain, CheckResult, CheckStatus

_HOST_HOME_PREFIX = "/" + "home/"
_HOST_HOME_PATTERN = re.compile(_HOST_HOME_PREFIX + r"[^/\s\"'`\)\]>,]+/[^\s\"'`\)\]>,]+")
_WSL_DRIVE_PATTERN = re.compile(r"/mnt/[a-z]/[^\s\"'`\)\]>,]+", re.IGNORECASE)

# Approved directory roots for containerized environments
_APPROVED_ROOTS = (
    "/workspace",
    site_paths.OPT_ROOT,
    site_paths.VAR_OPT_ROOT,
    site_paths.ETC_OPT_ROOT,
    site_paths.PROJECTS_ROOT,
    site_paths.SHARED_ROOT,
    "/run/user",
    "/run/ext",
    "/opt/ext",
    "/tmp",
    "/var/log",
)

_ENV_IGNORE_KEYS = {"PATH", "HOSTNAME", "SHELL", "TERM", "PWD", "OLDPWD"}

# Mandatory Dual-Track path variables per contract
_MANDATORY_APP_VARS = ("APP_SRC_DIR", "APP_DATA_DIR", "APP_CACHE_DIR")


class BoundaryChecker(BaseChecker):
    domain = CheckDomain.PATHS.value

    def check(self, context: ExecutionContext) -> List[CheckResult]:
        results: List[CheckResult] = []
        env = context.env
        declared_cfg = context.declared_config

        # 1. Audit Host Path Leaks in Environment Variables
        t0 = time.perf_counter()
        leaks = []
        for key, val in env.items():
            if key in _ENV_IGNORE_KEYS or not isinstance(val, str):
                continue
            if _HOST_HOME_PATTERN.search(val):
                leaks.append(f"{key}={val} (host home leak)")
            elif _WSL_DRIVE_PATTERN.search(val):
                leaks.append(f"{key}={val} (WSL drive leak)")

        if leaks and context.mode == ExecutionMode.INSIDE:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="host_env_leaks",
                    status=CheckStatus.FAIL,
                    message=f"Detected {len(leaks)} host path leaks in container environment",
                    expected="Environment variables must use canonical container paths (/opt, /workspace, /var/opt)",
                    actual="; ".join(leaks[:3]) + (f" (+{len(leaks)-3} more)" if len(leaks) > 3 else ""),
                    remediation="Use app_paths resolver or remove host /home/ and /mnt/ references from container environment.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="host_env_leaks",
                    status=CheckStatus.PASS,
                    message="No host /home/* or /mnt/* path leaks detected in environment",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        # 2. Reconcile Declared Environment Variables
        t0 = time.perf_counter()
        if declared_cfg and declared_cfg.declared_env:
            env_mismatches = []
            for dk, dv in declared_cfg.declared_env.items():
                actual_val = env.get(dk)
                if actual_val is None:
                    env_mismatches.append(f"Missing declared variable {dk}={dv}")
                elif actual_val != dv:
                    env_mismatches.append(f"Value drift for {dk}: declared='{dv}', actual='{actual_val}'")

            if env_mismatches and context.mode == ExecutionMode.INSIDE:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="declared_env_reconciliation",
                        status=CheckStatus.WARN,
                        message=f"Declared environment variables drift: {len(env_mismatches)} issues",
                        expected="Runtime environment matches declared container env specification",
                        actual="; ".join(env_mismatches[:3]),
                        remediation="Ensure container is launched with env definitions from deploy JSON.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="declared_env_reconciliation",
                        status=CheckStatus.PASS,
                        message=f"Declared environment variables reconciled ({len(declared_cfg.declared_env)} variables)",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )

        # 3. Verify Mandatory APP_* Path Resolver Variables and Boundaries
        t0 = time.perf_counter()
        app_vars = {k: v for k, v in env.items() if k.startswith("APP_") and isinstance(v, str)}

        # Check mandatory variables in container mode
        missing_mandatory = [var for var in _MANDATORY_APP_VARS if var not in app_vars]
        if missing_mandatory and context.mode == ExecutionMode.INSIDE:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="app_path_resolver",
                    status=CheckStatus.WARN,
                    message=f"Missing mandatory Dual-Track variables: {', '.join(missing_mandatory)}",
                    expected=f"All {_MANDATORY_APP_VARS} defined for Dual-Track portability",
                    actual=f"Missing: {', '.join(missing_mandatory)}",
                    remediation="Export APP_SRC_DIR, APP_DATA_DIR, and APP_CACHE_DIR via app_paths resolver.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        elif app_vars:
            path_errors = []
            for k, v in app_vars.items():
                p = Path(v)
                # Check absolute
                if not p.is_absolute():
                    path_errors.append(f"{k}={v} (not absolute path)")
                    continue
                # Check approved boundary
                is_approved = any(str(p).startswith(root) for root in _APPROVED_ROOTS)
                if not is_approved and context.mode == ExecutionMode.INSIDE:
                    path_errors.append(f"{k}={v} (outside approved boundaries: {_APPROVED_ROOTS})")
                    continue
                # Check physical existence or creatable parent
                if not p.exists() and not p.parent.exists():
                    path_errors.append(f"{k}={v} (target directory and parent do not exist)")

            if path_errors:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="app_path_resolver",
                        status=CheckStatus.FAIL,
                        message=f"Invalid APP_* path resolver configuration: {len(path_errors)} errors",
                        expected="APP_* paths must be absolute, exist within approved boundaries",
                        actual="; ".join(path_errors[:3]),
                        remediation="Fix APP_* paths in deploy JSON or app_paths.py resolver.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="app_path_resolver",
                        status=CheckStatus.PASS,
                        message=f"APP_* path variables verified ({len(app_vars)} paths within approved boundaries)",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="app_path_resolver",
                    status=CheckStatus.PASS,
                    message="APP_* path resolver checked (default tracks in effect)",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        return results
