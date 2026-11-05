"""
Mounts and Scope Extension checker for Container Doctor (scope extensions & container mounts).
Reconciles /proc/mounts against declared mounts in .deploy/containers/*.json and .scopes/*.json.
"""

from pathlib import Path
import time
from typing import Dict, List, Optional

from container_doctor.checkers.base import BaseChecker
from container_doctor.core import site_paths
from container_doctor.core.context import ExecutionContext, ExecutionMode
from container_doctor.core.models import CheckDomain, CheckResult, CheckStatus


class MountChecker(BaseChecker):
    domain = CheckDomain.MOUNTS.value

    def __init__(self, mounts_file: Path = Path("/proc/mounts")):
        self.mounts_file = mounts_file

    def _parse_mounts(self) -> Dict[str, Dict[str, str]]:
        mounts: Dict[str, Dict[str, str]] = {}
        if not self.mounts_file.exists():
            return mounts

        try:
            with open(self.mounts_file, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        device, mount_point, fstype, options = parts[0], parts[1], parts[2], parts[3]
                        opts_list = options.split(",")
                        mounts[mount_point] = {
                            "device": device,
                            "fstype": fstype,
                            "options": options,
                            "is_ro": "ro" in opts_list,
                            "is_rw": "rw" in opts_list,
                        }
        except Exception:
            pass
        return mounts

    def check(self, context: ExecutionContext) -> List[CheckResult]:
        results: List[CheckResult] = []
        mounts = self._parse_mounts()
        declared_cfg = context.declared_config

        # 1. Check the site package mount (site_paths.OPT_ROOT)
        t0 = time.perf_counter()
        opt_path = context.opt_lab_path
        if not opt_path.exists():
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="opt_root_present",
                    status=CheckStatus.FAIL,
                    message=f"Required package directory {opt_path} does not exist",
                    expected=f"{opt_path} exists and is accessible",
                    actual="missing",
                    remediation=f"Ensure {opt_path} is bind-mounted into container.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            mount_info = mounts.get(str(opt_path))
            if mount_info and not mount_info.get("is_ro", False) and context.mode == ExecutionMode.INSIDE:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="opt_root_ro",
                        status=CheckStatus.WARN,
                        message=f"{opt_path} is mounted rw instead of ro",
                        expected="ro (read-only)",
                        actual="rw (read-write)",
                        remediation="Set mount mode to ':ro' in Quadlet or container config.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="opt_root_present",
                        status=CheckStatus.PASS,
                        message=f"{opt_path} is present and accessible",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )

        # 2. Check /workspace (or project working dir)
        t0 = time.perf_counter()
        ws_path = context.workspace_path
        if context.mode == ExecutionMode.INSIDE:
            if not ws_path.exists():
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="workspace_present",
                        status=CheckStatus.FAIL,
                        message=f"Working directory {ws_path} does not exist",
                        expected=f"{ws_path} exists",
                        actual="missing",
                        remediation=f"Ensure {ws_path} is bind-mounted as workspace.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                mount_info = mounts.get(str(ws_path))
                if mount_info and mount_info.get("is_ro", False):
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="workspace_rw",
                            status=CheckStatus.FAIL,
                            message=f"{ws_path} is mounted ro (read-only) but must be rw",
                            expected="rw (read-write)",
                            actual="ro (read-only)",
                            remediation=f"Ensure {ws_path} is mounted rw.",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            domain=self.domain,
                            name="workspace_present",
                            status=CheckStatus.PASS,
                            message=f"{ws_path} is present and writable",
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                        )
                    )

        # 3. Reconcile Declared Mounts from .scopes and .deploy
        t0 = time.perf_counter()
        if declared_cfg and declared_cfg.declared_mounts:
            mount_issues = []
            checked_count = 0
            for dm in declared_cfg.declared_mounts:
                target_p = Path(dm.dst)
                checked_count += 1
                # If path doesn't exist on host/container
                if not target_p.exists():
                    mount_issues.append(f"{dm.dst} (declared in {Path(dm.origin_file).name if dm.origin_file else 'scope'}, missing on target)")
                else:
                    m_info = mounts.get(dm.dst)
                    if m_info:
                        if dm.is_ro and not m_info.get("is_ro", False) and context.mode == ExecutionMode.INSIDE:
                            mount_issues.append(f"{dm.dst} (expected ro, mounted rw)")
                        elif dm.is_rw and m_info.get("is_ro", False):
                            mount_issues.append(f"{dm.dst} (expected rw, mounted ro)")

            if mount_issues and context.mode == ExecutionMode.INSIDE:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="declared_mounts_reconciliation",
                        status=CheckStatus.WARN,
                        message=f"Declared scope mount discrepancies: {len(mount_issues)} issues",
                        expected="All declared scope and volume mounts present with correct mode",
                        actual="; ".join(mount_issues[:3]),
                        remediation="Apply scope definitions or remount declared volumes.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="declared_mounts_reconciliation",
                        status=CheckStatus.PASS,
                        message=f"Declared mounts reconciled ({checked_count} scope/deploy mounts verified)",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )

        # 4. Check Extension / Agents directory linkage
        t0 = time.perf_counter()
        agents_candidates = [
            ws_path / ".agents",
            opt_path / ".agents",
            Path.home() / ".agents",
            Path("/run/ext/skills"),
        ]
        has_agents = any(p.is_dir() for p in agents_candidates)
        if has_agents:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="agents_extension_present",
                    status=CheckStatus.PASS,
                    message="Agent skills and rules structure (.agents) discovered",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="agents_extension_present",
                    status=CheckStatus.WARN,
                    message="No .agents directory found in workspace or opt",
                    expected=f".agents directory in /workspace or {site_paths.OPT_ROOT}",
                    actual="none found",
                    remediation="Mount or link .agents directory via a scope extension.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        return results
