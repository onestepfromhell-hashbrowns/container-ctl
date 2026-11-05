"""lifecycle_adapter.py — Systemd User Manager Dispatch & Precise Admission Adapter (C05, C07, T05, T08).

Provides:
1. SystemdManager: Encapsulates rootless user manager dispatch per owner_uid.
   - Non-blocking job execution (--no-block).
   - Strict separation between job dispatch and active running verification (job submission != running).
2. Governance Admission & Pause Gates:
   - resolve_action_stable_id: Extracts canonical stable ID from action blueprints.
   - is_workload_paused: Fast check against GovernanceLedger.
   - check_plan_admission: Pre-mutation gate ensuring no paused workload in deploy plan.
   - precise_start_workload: Just-In-Time pre-start gate for deployment, SSH, monitor, and recovery.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from container_ctl import agent_exec
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.governance_port import GovernanceLedger, PauseRecord
from container_ctl import paths


@dataclass
class ServiceStartResult:
    """Represents the outcome of a systemd start request."""
    job_submitted: bool
    is_running: bool = False
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


class SystemdManager:
    """Manages systemd user manager operations with support for owner-specific rootless dispatch."""

    def __init__(self, default_owner_uid: int = 1000):
        self.default_owner_uid = default_owner_uid

    def _run(self, cmd, **kw):
        """subprocess.run 的唯一出口。CONTAINER_CTL_HOST_AGENT 存在（控制面容器）→ 經 host-agent；否則照舊本機執行。"""
        if agent_exec.agent_socket():
            return agent_exec.run_via_agent(cmd, **kw)
        return subprocess.run(cmd, **kw)

    def _build_env(self, owner_uid: int) -> Dict[str, str]:
        env = os.environ.copy()
        runtime_dir = f"/run/user/{owner_uid}"
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env.setdefault("CONTAINER_HOST", f"unix://{runtime_dir}/podman/podman.sock")
        return env

    def start_unit(
        self,
        unit_name: str,
        owner_uid: Optional[int] = None,
        no_block: bool = True,
    ) -> ServiceStartResult:
        """Dispatch systemctl --user start for a given unit.

        Invariants:
        - Uses --no-block to prevent start hanging on dependencies.
        - Job submission success (returncode=0) DOES NOT imply the service is running.
        """
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "start"]
        if no_block:
            cmd.append("--no-block")
        cmd.append(unit_name)

        env = self._build_env(uid)
        try:
            proc = self._run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            submitted = (proc.returncode == 0)
            err_text = str(proc.stderr.strip()) if hasattr(proc.stderr, "strip") else str(proc.stderr or "")
            out_text = str(proc.stdout) if proc.stdout else ""
            return ServiceStartResult(
                job_submitted=submitted,
                is_running=False,  # Non-blocking start job submission != running
                returncode=proc.returncode,
                stdout=out_text,
                stderr=err_text,
                error=None if submitted else (err_text or "systemctl start failed"),
            )
        except Exception as e:
            return ServiceStartResult(
                job_submitted=False,
                is_running=False,
                returncode=-1,
                error=str(e),
            )

    def restart_unit(
        self,
        unit_name: str,
        owner_uid: Optional[int] = None,
        no_block: bool = True,
    ) -> ServiceStartResult:
        """Dispatch a bounded, rootless systemd user-manager restart request."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "restart"]
        if no_block:
            cmd.append("--no-block")
        cmd.append(unit_name)
        try:
            proc = self._run(
                cmd,
                capture_output=True,
                text=True,
                env=self._build_env(uid),
                check=False,
            )
            err_text = proc.stderr.strip() if proc.stderr else ""
            return ServiceStartResult(
                job_submitted=proc.returncode == 0,
                is_running=False,
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=err_text,
                error=None if proc.returncode == 0 else (err_text or "systemctl restart failed"),
            )
        except Exception as e:
            return ServiceStartResult(
                job_submitted=False,
                is_running=False,
                returncode=-1,
                error=str(e),
            )

    def unit_state(self, unit_name: str, owner_uid: Optional[int] = None, timeout: float = 5.0) -> str:
        """Return `systemctl is-active` state text (active/inactive/failed/...), or the error text."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        try:
            proc = self._run(
                ["systemctl", "--user", "is-active", unit_name],
                env=self._build_env(uid), capture_output=True, text=True, timeout=timeout, check=False,
            )
            return (proc.stdout or "").strip() or (proc.stderr or "").strip()
        except Exception as e:
            return str(e)

    def daemon_reload(self, owner_uid: Optional[int] = None) -> bool:
        """Reload the user manager so it picks up new/removed Quadlet units."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        try:
            proc = self._run(
                ["systemctl", "--user", "daemon-reload"],
                env=self._build_env(uid),
                capture_output=True,
                check=False,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def is_active(self, unit_name: str, owner_uid: Optional[int] = None, timeout: float = 1.0) -> bool:
        """Check if a service unit is active."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "is-active", "--quiet", unit_name]
        env = self._build_env(uid)
        try:
            proc = self._run(cmd, env=env, timeout=timeout, check=False)
            return proc.returncode == 0
        except Exception:
            return False

    def stop_unit(self, unit_name: str, owner_uid: Optional[int] = None, timeout: float = 1.0) -> bool:
        """Stop a service unit with bounded 1.0s timeout and truncated output."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "stop", unit_name]
        env = self._build_env(uid)
        try:
            proc = self._run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
            return proc.returncode == 0
        except Exception:
            return False

    def stop_unit_blocking(
        self,
        unit_name: str,
        owner_uid: Optional[int] = None,
        timeout: Optional[float] = None,
        run: Optional[Callable] = None,
    ) -> subprocess.CompletedProcess:
        """Stop a unit and wait for completion. Raises subprocess.TimeoutExpired on timeout."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        run = run or self._run
        return run(
            ["systemctl", "--user", "stop", unit_name],
            env=self._build_env(uid),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def list_units(
        self,
        pattern: str,
        owner_uid: Optional[int] = None,
        run: Optional[Callable] = None,
    ) -> str:
        """Return `systemctl --user list-units <pattern>` rows (no legend, plain), stripped."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        run = run or self._run
        res = run(
            ["systemctl", "--user", "list-units", pattern, "--no-legend", "--plain"],
            env=self._build_env(uid), capture_output=True, text=True,
        )
        return res.stdout.strip()

    def run_transient(
        self,
        unit: str,
        description: str,
        properties: List[str],
        argv: List[str],
        owner_uid: Optional[int] = None,
        run: Optional[Callable] = None,
    ) -> subprocess.CompletedProcess:
        """Start a transient user service via systemd-run (--collect, one -p per property)."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        run = run or self._run
        cmd = ["systemd-run", "--user", f"--unit={unit}", "--collect", f"--description={description}"]
        for prop in properties:
            cmd += ["-p", prop]
        return run(cmd + list(argv), env=self._build_env(uid), capture_output=True, text=True)

    def kill_unit(self, unit_name: str, signal: str = "SIGKILL", owner_uid: Optional[int] = None, timeout: float = 1.0) -> bool:
        """Send signal to unit processes with bounded 1.0s timeout."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "kill", f"--signal={signal}", unit_name]
        env = self._build_env(uid)
        try:
            proc = self._run(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
            return proc.returncode == 0
        except Exception:
            return False

    def reset_failed(self, unit_name: str, owner_uid: Optional[int] = None, timeout: float = 1.0) -> bool:
        """Reset failed state for a unit."""
        uid = owner_uid if owner_uid is not None else self.default_owner_uid
        cmd = ["systemctl", "--user", "reset-failed", unit_name]
        env = self._build_env(uid)
        try:
            proc = self._run(cmd, env=env, capture_output=True, timeout=timeout, check=False)
            return proc.returncode == 0
        except Exception:
            return False


# Global default manager instance
_DEFAULT_MANAGER = SystemdManager()


def get_default_manager() -> SystemdManager:
    return _DEFAULT_MANAGER


def resolve_action_stable_id(action: ServiceAction) -> str:
    """Extract canonical stable ID from a ServiceAction blueprint."""
    cfg = action.config
    labels = cfg.get("labels", {})
    if isinstance(labels, dict):
        gov_label = labels.get(f"{paths.LABEL_NS}.governance.id") or labels.get("governance.id")
    else:
        gov_label = None

    gov_dict = cfg.get("governance", {})
    gov_id = gov_dict.get("id") if isinstance(gov_dict, dict) else None

    return (
        cfg.get("stable_id")
        or gov_id
        or gov_label
        or action.name
    )


def is_workload_paused(
    stable_id: str,
    ledger: Optional[GovernanceLedger] = None,
) -> Tuple[bool, Optional[PauseRecord]]:
    """Check if a given stable_id has an active pause record in GovernanceLedger."""
    led = ledger or GovernanceLedger()
    if not led.exists():
        return False, None

    try:
        pauses = led.get_pauses()
        for p in pauses:
            if p.stable_id == stable_id:
                return True, p
    except Exception:
        pass
    return False, None


def check_plan_admission(
    plan: DeployPlan,
    ledger: Optional[GovernanceLedger] = None,
) -> Tuple[bool, List[str]]:
    """Pre-mutation admission gate: verify no actions in deploy plan are paused."""
    reasons: List[str] = []
    led = ledger or GovernanceLedger()
    if not led.exists():
        return True, []

    for action in plan.actions:
        stable_id = resolve_action_stable_id(action)
        paused, pause_rec = is_workload_paused(stable_id, ledger=led)
        if paused and pause_rec is not None:
            reasons.append(
                f"Workload '{stable_id}' (action: {action.action}, service: {action.name}) "
                f"is paused by governance (incident: {pause_rec.incident_id}, reason: {pause_rec.reason})"
            )

    return (len(reasons) == 0, reasons)


def precise_start_workload(
    stable_id: str,
    unit_name: str,
    owner_uid: int = 1000,
    ledger: Optional[GovernanceLedger] = None,
    manager: Optional[SystemdManager] = None,
) -> Tuple[bool, ServiceStartResult, str]:
    """Just-In-Time start adapter: verify pause status immediately before non-blocking dispatch."""
    paused, pause_rec = is_workload_paused(stable_id, ledger=ledger)
    if paused and pause_rec is not None:
        return (
            False,
            ServiceStartResult(
                job_submitted=False,
                is_running=False,
                error=f"Workload '{stable_id}' is paused: {pause_rec.reason}",
            ),
            f"governance_paused:{pause_rec.reason}",
        )

    mgr = manager or get_default_manager()
    result = mgr.start_unit(unit_name, owner_uid=owner_uid, no_block=True)
    reason = "ok" if result.job_submitted else (result.error or "start_failed")
    return (result.job_submitted, result, reason)


def precise_restart_workload(
    stable_id: str,
    unit_name: str,
    owner_uid: int = 1000,
    ledger: Optional[GovernanceLedger] = None,
    manager: Optional[SystemdManager] = None,
) -> Tuple[bool, ServiceStartResult, str]:
    """Governance-gated restart request for a single existing workload."""
    paused, pause_rec = is_workload_paused(stable_id, ledger=ledger)
    if paused and pause_rec is not None:
        return (
            False,
            ServiceStartResult(
                job_submitted=False,
                is_running=False,
                error=f"Workload '{stable_id}' is paused: {pause_rec.reason}",
            ),
            f"governance_paused:{pause_rec.reason}",
        )

    mgr = manager or get_default_manager()
    result = mgr.restart_unit(unit_name, owner_uid=owner_uid, no_block=True)
    reason = "ok" if result.job_submitted else (result.error or "restart_failed")
    return (result.job_submitted, result, reason)


def precise_stop_workload(
    unit_name: str,
    owner_uid: int = 1000,
    manager: Optional[SystemdManager] = None,
    timeout: float = 1.0,
) -> bool:
    """Precise stop adapter: execute systemd user manager stop for unit with array parameters (C06)."""
    mgr = manager or get_default_manager()
    return mgr.stop_unit(unit_name, owner_uid=owner_uid, timeout=timeout)


def check_workload_admission(
    target: str,
    container_name: Optional[str] = None,
    db_path: Optional[str] = None,
    ledger: Optional[GovernanceLedger] = None,
) -> Tuple[bool, Optional[str], Optional[PauseRecord]]:
    """Check whether a target workload, project, or container is admitted or paused (C05, C07).

    Evaluates:
    1. Direct match on target (e.g. stable_id or project name).
    2. Match on container_name (if provided).
    3. Match against stable_ids associated with instances in central deployments.db.

    Returns:
        (allowed: bool, matched_stable_id: Optional[str], pause_record: Optional[PauseRecord])
    """
    import json
    from container_ctl.db import DEFAULT_DB_PATH, init_db

    led = ledger or GovernanceLedger()
    if not led.exists():
        return True, None, None

    try:
        pauses = led.get_pauses()
    except Exception:
        return True, None, None

    if not pauses:
        return True, None, None

    pause_map = {p.stable_id: p for p in pauses}

    # 1. Check direct target
    if target in pause_map:
        return False, target, pause_map[target]

    # 2. Check container_name if given
    if container_name and container_name in pause_map:
        return False, container_name, pause_map[container_name]

    # 3. Check central database instances for target project/service
    actual_db = db_path or os.environ.get("CONTAINER_CTL_DEPLOYMENTS_DB") or DEFAULT_DB_PATH
    if os.path.exists(actual_db):
        try:
            conn = init_db(actual_db)
            rows = conn.execute(
                """
                SELECT id, service_name, project_name, blueprint
                FROM instances
                WHERE (project_name = ? OR service_name = ? OR id = ?)
                  AND status NOT IN ('destroyed')
                """,
                (target, target, target),
            ).fetchall()
            for row in rows:
                cfg = {}
                if "blueprint" in row.keys() and row["blueprint"]:
                    try:
                        cfg = json.loads(row["blueprint"])
                    except Exception:
                        cfg = {}
                stable_id = (
                    cfg.get("stable_id")
                    or (cfg.get("governance", {}).get("id") if isinstance(cfg.get("governance"), dict) else None)
                    or (cfg.get("labels", {}).get(f"{paths.LABEL_NS}.governance.id") if isinstance(cfg.get("labels"), dict) else None)
                    or row["service_name"]
                )
                if stable_id in pause_map:
                    conn.close()
                    return False, stable_id, pause_map[stable_id]
                composite_id = f"{row['project_name']}.{row['service_name']}"
                if composite_id in pause_map:
                    conn.close()
                    return False, composite_id, pause_map[composite_id]
            conn.close()
        except Exception:
            pass

    return True, None, None
