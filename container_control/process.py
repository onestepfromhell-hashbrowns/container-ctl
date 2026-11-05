"""
Process lifecycle and process inspection manager for container environments.
"""

from datetime import datetime
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple


class ProcessManager:
    @staticmethod
    def read_pid(pid_file: Path) -> Optional[int]:
        if pid_file.is_file():
            try:
                content = pid_file.read_text(encoding="utf-8").strip()
                return int(content) if content else None
            except (ValueError, OSError):
                return None
        return None

    @staticmethod
    def write_pid(pid_file: Path, pid: int) -> None:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid), encoding="utf-8")

    @staticmethod
    def remove_pid(pid_file: Path) -> None:
        if pid_file.is_file():
            try:
                pid_file.unlink()
            except OSError:
                pass

    @staticmethod
    def is_alive(pid: Optional[int]) -> bool:
        if not pid or pid <= 0:
            return False
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                h_process = kernel32.OpenProcess(0x1000, False, pid)
                if h_process:
                    exit_code = ctypes.c_ulong()
                    kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
                    kernel32.CloseHandle(h_process)
                    return exit_code.value == 259  # STILL_ACTIVE
                return False
            except Exception:
                return False
        else:
            # First try reaping if it's a child of current process
            try:
                res, _ = os.waitpid(pid, os.WNOHANG)
                if res == pid:
                    return False
            except (ChildProcessError, OSError):
                pass

            try:
                os.kill(pid, 0)
            except OSError:
                return False

            # If /proc/<pid>/status exists, check if State is Zombie (Z) or Dead (X)
            proc_status = Path(f"/proc/{pid}/status")
            if proc_status.exists():
                try:
                    for line in proc_status.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.startswith("State:"):
                            state = line.split(":", 1)[1].strip()
                            if state.startswith("Z") or state.startswith("X"):
                                return False
                            break
                except Exception:
                    pass
            return True

    @staticmethod
    def get_cmdline(pid: int) -> Optional[str]:
        if not ProcessManager.is_alive(pid):
            return None
        if os.name != "nt":
            proc_cmdline = Path(f"/proc/{pid}/cmdline")
            if proc_cmdline.exists():
                try:
                    data = proc_cmdline.read_bytes()
                    parts = [p.decode("utf-8", errors="ignore") for p in data.split(b"\x00") if p]
                    return " ".join(parts)
                except Exception:
                    pass
        else:
            try:
                out = subprocess.check_output(
                    ["wmic", "process", "where", f"processid={pid}", "get", "CommandLine", "/format:list"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                for line in out.splitlines():
                    if line.startswith("CommandLine="):
                        return line.split("=", 1)[1].strip()
            except Exception:
                pass
        return None

    @staticmethod
    def get_process_info(pid: int) -> Tuple[Optional[str], Optional[str]]:
        """Returns (cmdline, start_time_str)."""
        cmdline = ProcessManager.get_cmdline(pid)
        start_time: Optional[str] = None

        if os.name != "nt":
            stat_file = Path(f"/proc/{pid}/stat")
            if stat_file.exists():
                try:
                    mtime = stat_file.stat().st_mtime
                    start_time = datetime.fromtimestamp(mtime).isoformat()
                except Exception:
                    pass
        return cmdline, start_time

    @staticmethod
    def start_detached(
        cmd: list[str],
        cwd: Path,
        log_file: Path,
        env: Optional[Dict[str, str]] = None,
    ) -> int:
        """
        Launches a detached process redirecting stdout/stderr to log_file.
        """
        log_file.parent.mkdir(parents=True, exist_ok=True)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        # Ensure unbuffered python output
        merged_env["PYTHONUNBUFFERED"] = "1"

        with open(log_file, "a", encoding="utf-8") as log_fd:
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdout=log_fd,
                    stderr=log_fd,
                    env=merged_env,
                    creationflags=creationflags,
                    close_fds=True,
                )
            else:
                # POSIX detached process using start_new_session
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdout=log_fd,
                    stderr=log_fd,
                    env=merged_env,
                    start_new_session=True,
                    close_fds=True,
                )
        return proc.pid

    @staticmethod
    def kill_process(pid: int, timeout: float = 5.0) -> bool:
        """
        Gracefully terminates process via SIGTERM, falling back to SIGKILL.
        """
        if not ProcessManager.is_alive(pid):
            return True

        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], check=True, capture_output=True)
                return True
            except subprocess.CalledProcessError:
                return not ProcessManager.is_alive(pid)

        # Linux / POSIX
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return not ProcessManager.is_alive(pid)

        # Poll for exit
        start = time.time()
        while time.time() - start < timeout:
            if not ProcessManager.is_alive(pid):
                return True
            time.sleep(0.1)

        # Force kill if still alive
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.1)
        except OSError:
            pass

        return not ProcessManager.is_alive(pid)
