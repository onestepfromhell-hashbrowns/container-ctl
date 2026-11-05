"""
Service Controller orchestrating start (foreground & detach), stop, restart,
Triple-Verification status dashboard, readiness probes, and logs.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import ServiceConfig, load_service_config
from .port import PortDetector
from .process import ProcessManager
from .probe import ReadinessProbe


class ServiceController:
    def __init__(self, config: Optional[ServiceConfig] = None, config_path: Optional[str | Path] = None, base_dir: Optional[str | Path] = None):
        if config:
            self.config = config
        else:
            self.config = load_service_config(config_path, base_dir)

    def _run_hooks(self, hooks: List[str], phase_name: str) -> bool:
        """Executes a list of hook commands sequentially. Returns False if any fails."""
        if not hooks:
            return True

        merged_env = os.environ.copy()
        if self.config.env:
            merged_env.update(self.config.env)

        for i, hook in enumerate(hooks, 1):
            print(f"[{self.config.name}] Running {phase_name} [{i}/{len(hooks)}]: {hook}")
            try:
                res = subprocess.run(
                    hook,
                    shell=True,
                    cwd=str(self.config.abs_working_dir),
                    env=merged_env,
                )
                if res.returncode != 0:
                    print(f"[{self.config.name}] Error: {phase_name} command failed with exit code {res.returncode}: {hook}", file=sys.stderr)
                    return False
            except Exception as e:
                print(f"[{self.config.name}] Error: {phase_name} command execution failed: {e}", file=sys.stderr)
                return False
        return True

    def probe(self) -> Tuple[bool, str]:
        """Executes configured readiness probe and returns (is_healthy, detail_message)."""
        return ReadinessProbe.check(self.config.readiness_probe, self.config.root_dir)

    def get_status(self) -> Dict[str, Any]:
        """
        Evaluates Triple-Verification state machine:
        - L1: Process alive (PID)
        - L2: Port socket bound (PortDetector)
        - L3: Readiness Probe healthy (ReadinessProbe)
        """
        recorded_pid = ProcessManager.read_pid(self.config.abs_pid_file)
        status, pid, detail, start_time = PortDetector.verify_status(
            port=self.config.port,
            recorded_pid=recorded_pid,
            entry_point=self.config.entry_point or (self.config.command[0] if self.config.command else "app"),
            root_dir=self.config.root_dir,
        )

        probe_healthy = None
        probe_detail = None
        if self.config.readiness_probe:
            if status in ("RUNNING", "INITIALIZING"):
                probe_healthy, probe_detail = self.probe()
                if status == "RUNNING" and not probe_healthy:
                    status = "INITIALIZING"
                    detail = f"{detail} | Probe pending: {probe_detail}"
                elif status == "RUNNING" and probe_healthy:
                    detail = f"{detail} | Probe: {probe_detail}"

        uptime_str = None
        if start_time:
            try:
                start_dt = datetime.fromisoformat(start_time)
                delta = datetime.now() - start_dt
                hours, rem = divmod(int(delta.total_seconds()), 3600)
                mins, secs = divmod(rem, 60)
                days = delta.days
                if days > 0:
                    uptime_str = f"{days}d {hours}h {mins}m {secs}s"
                else:
                    uptime_str = f"{hours}h {mins}m {secs}s"
            except Exception:
                uptime_str = start_time

        return {
            "status": status,
            "pid": pid,
            "port": self.config.port,
            "service_name": self.config.name,
            "command": self.config.command,
            "entry_point": self.config.entry_point,
            "abs_entry_point": str(self.config.abs_entry_point),
            "pid_file": str(self.config.abs_pid_file),
            "log_file": str(self.config.abs_log_file),
            "detail": detail,
            "probe_configured": self.config.readiness_probe is not None,
            "probe_healthy": probe_healthy,
            "probe_detail": probe_detail,
            "start_time": start_time,
            "uptime": uptime_str,
        }

    def print_dashboard(self) -> None:
        info = self.get_status()
        status = info["status"]
        pid = info["pid"]

        # Color codes
        GREEN = "\033[92m"
        YELLOW = "\033[93m"
        RED = "\033[91m"
        CYAN = "\033[96m"
        GRAY = "\033[90m"
        RESET = "\033[0m"

        status_display = {
            "RUNNING": f"{GREEN}● RUNNING{RESET} (PID: {pid})",
            "INITIALIZING": f"{YELLOW}● INITIALIZING{RESET} (PID: {pid})",
            "CONFLICT": f"{RED}✖ CONFLICT (Port Occupied){RESET}",
            "ZOMBIE": f"{RED}▲ ZOMBIE (Stale Lock){RESET}",
            "STOPPED": f"{GRAY}○ STOPPED{RESET}",
            "HOOK_FAILED": f"{RED}✖ HOOK FAILED{RESET}",
        }.get(status, f"{GRAY}{status}{RESET}")

        print("=" * 68)
        print(f"        Universal Service Manager: {self.config.name}")
        print("=" * 68)
        print(f"  [ Command         ] : {' '.join(self.config.command)}")
        print(f"  [ Configured Port ] : {self.config.port or 'None'}")
        print(f"  [ PID Lock File   ] : {self.config.pid_file}")
        print(f"  [ Stdout Log File ] : {self.config.log_file}")
        if self.config.readiness_probe:
            probe_type = self.config.readiness_probe.get("type", "http")
            print(f"  [ Readiness Probe ] : {probe_type} ({info.get('probe_detail') or 'Configured'})")
        print("-" * 68)
        print(f"  [ Status          ] : {status_display}")

        detail_lines = textwrap.wrap(info["detail"], width=60)
        if detail_lines:
            print(f"  [ Detail          ] : {detail_lines[0]}")
            for extra in detail_lines[1:]:
                print(f"                        {extra}")

        if info.get("start_time"):
            print(f"  [ Startup Time    ] : {info['start_time']}")
        if info.get("uptime"):
            print(f"  [ Uptime          ] : {info['uptime']}")
        print("=" * 68)

    def _build_execution_command(self) -> List[str]:
        """Resolves executable command array, converting relative script path if needed."""
        cmd = list(self.config.command)
        if not cmd:
            cmd = ["python3", "scripts/app.py"]
        return cmd

    def start(self, wait_seconds: float = 0.5, foreground: bool = False) -> bool:
        if foreground:
            return self.start_foreground()

        status_info = self.get_status()
        status = status_info["status"]

        if status == "RUNNING":
            print(f"Service is already running (PID: {status_info['pid']}).")
            return True
        elif status == "CONFLICT":
            print(f"Cannot start: {status_info['detail']}", file=sys.stderr)
            return False

        # 1. Execute Pre-Start Hooks
        if not self._run_hooks(self.config.pre_start_hooks, "pre_start_hook"):
            print("Aborting start due to pre_start_hook failure.", file=sys.stderr)
            return False

        # Clean stale lock
        if status == "ZOMBIE":
            ProcessManager.remove_pid(self.config.abs_pid_file)

        cmd = self._build_execution_command()
        pid = ProcessManager.start_detached(
            cmd=cmd,
            cwd=self.config.abs_working_dir,
            log_file=self.config.abs_log_file,
            env=self.config.env,
        )

        ProcessManager.write_pid(self.config.abs_pid_file, pid)
        time.sleep(wait_seconds)

        new_status = self.get_status()
        if new_status["status"] in ("RUNNING", "INITIALIZING"):
            # 2. Execute Post-Start Hooks
            self._run_hooks(self.config.post_start_hooks, "post_start_hook")
            print(f"Service '{self.config.name}' started successfully (PID: {pid}).")
            return True
        else:
            print(f"Warning: Service started with PID {pid}, but status is {new_status['status']}.", file=sys.stderr)
            return False

    def start_foreground(self) -> bool:
        """
        Launches service in foreground mode (PID 1 compliant).
        Traps SIGTERM and SIGINT to trigger graceful shutdown.
        """
        print(f"Starting service '{self.config.name}' in FOREGROUND mode...")

        # 1. Execute Pre-Start Hooks
        if not self._run_hooks(self.config.pre_start_hooks, "pre_start_hook"):
            print("Aborting start due to pre_start_hook failure.", file=sys.stderr)
            return False

        cmd = self._build_execution_command()
        merged_env = os.environ.copy()
        if self.config.env:
            merged_env.update(self.config.env)
        merged_env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.config.abs_working_dir),
            env=merged_env,
        )

        ProcessManager.write_pid(self.config.abs_pid_file, proc.pid)

        def sig_handler(signum, frame):
            sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
            print(f"\n[Foreground Controller] Caught {sig_name}, shutting down child PID {proc.pid} gracefully...")
            self.stop()
            try:
                proc.wait(timeout=self.config.shutdown_timeout)
            except Exception:
                proc.kill()
            sys.exit(0)

        # Register signal handlers
        signal.signal(signal.SIGTERM, sig_handler)
        signal.signal(signal.SIGINT, sig_handler)

        try:
            # 2. Execute Post-Start Hooks
            self._run_hooks(self.config.post_start_hooks, "post_start_hook")
            returncode = proc.wait()
            ProcessManager.remove_pid(self.config.abs_pid_file)
            return returncode == 0
        except Exception as e:
            print(f"Foreground process error: {e}", file=sys.stderr)
            self.stop()
            return False

    def stop(self, timeout: Optional[float] = None) -> bool:
        """Gracefully stops service, running pre_stop_hooks before termination."""
        status_info = self.get_status()
        pid = status_info["pid"]
        eff_timeout = timeout if timeout is not None else self.config.shutdown_timeout

        # Execute Pre-Stop Hooks
        self._run_hooks(self.config.pre_stop_hooks, "pre_stop_hook")

        if not pid and status_info["status"] == "STOPPED":
            print("Service is already stopped.")
            ProcessManager.remove_pid(self.config.abs_pid_file)
            return True

        if pid:
            print(f"Stopping service '{self.config.name}' (PID: {pid})...")
            success = ProcessManager.kill_process(pid, timeout=eff_timeout)
            ProcessManager.remove_pid(self.config.abs_pid_file)
            if success:
                print("Service stopped successfully.")
                return True
            else:
                print(f"Failed to cleanly stop PID {pid}.", file=sys.stderr)
                return False
        else:
            ProcessManager.remove_pid(self.config.abs_pid_file)
            print("Service lock cleared.")
            return True

    def restart(self, wait_seconds: float = 1.0) -> bool:
        print("Restarting service...")
        self.stop()
        time.sleep(wait_seconds)
        return self.start()

    def logs(self, lines: int = 20, follow: bool = False) -> None:
        log_path = self.config.abs_log_file
        if not log_path.is_file():
            print(f"No log file found at {log_path}")
            return

        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line, end="")

        if follow:
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(0, os.SEEK_END)
                    while True:
                        line = f.readline()
                        if line:
                            print(line, end="", flush=True)
                        else:
                            time.sleep(0.5)
            except KeyboardInterrupt:
                pass
