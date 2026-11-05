"""
Port detection and socket verification module for containerized services.
"""

import os
from pathlib import Path
import re
import subprocess
from typing import Optional, Tuple
from .process import ProcessManager


class PortDetector:
    @staticmethod
    def get_listening_pid(port: int) -> Optional[int]:
        """
        Detects the PID listening on a given TCP port using multi-tier strategies.
        """
        if port is None or port <= 0:
            return None

        if os.name == "nt":
            return PortDetector._get_pid_windows(port)
        else:
            # Try Linux strategies: ss -> lsof -> netstat -> proc
            pid = PortDetector._get_pid_ss(port)
            if pid is not None:
                return pid
            pid = PortDetector._get_pid_lsof(port)
            if pid is not None:
                return pid
            pid = PortDetector._get_pid_netstat(port)
            if pid is not None:
                return pid
            return PortDetector._get_pid_proc(port)

    @staticmethod
    def _get_pid_windows(port: int) -> Optional[int]:
        try:
            out = subprocess.check_output(f'netstat -ano | findstr ":{port}"', shell=True, text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "LISTENING" in line and f":{port}" in line:
                    parts = line.strip().split()
                    if parts:
                        return int(parts[-1])
        except Exception:
            pass
        return None

    @staticmethod
    def _get_pid_ss(port: int) -> Optional[int]:
        try:
            # ss -lntp sport = :<port>
            out = subprocess.check_output(["ss", "-lntp", f"sport = :{port}"], text=True, stderr=subprocess.DEVNULL)
            # Format: users:(("python3",pid=12345,fd=3))
            match = re.search(r"pid=(\d+)", out)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    @staticmethod
    def _get_pid_lsof(port: int) -> Optional[int]:
        try:
            out = subprocess.check_output(["lsof", "-t", "-i", f":{port}", "-sTCP:LISTEN"], text=True, stderr=subprocess.DEVNULL)
            pids = out.strip().split()
            if pids:
                return int(pids[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _get_pid_netstat(port: int) -> Optional[int]:
        try:
            out = subprocess.check_output(["netstat", "-lntp"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if f":{port} " in line and "LISTEN" in line:
                    # e.g. tcp 0 0 0.0.0.0:8080 0.0.0.0:* LISTEN 12345/python3
                    parts = line.strip().split()
                    last = parts[-1]
                    if "/" in last:
                        pid_str = last.split("/")[0]
                        return int(pid_str)
        except Exception:
            pass
        return None

    @staticmethod
    def _get_pid_proc(port: int) -> Optional[int]:
        """Fallback reading /proc/net/tcp and finding socket owner in /proc/[pid]/fd."""
        try:
            hex_port = f"{port:04X}"
            socket_inodes = set()
            
            for tcp_path in ("/proc/net/tcp", "/proc/net/tcp6"):
                p = Path(tcp_path)
                if not p.exists():
                    continue
                for line in p.read_text().splitlines()[1:]:
                    fields = line.strip().split()
                    if len(fields) >= 10:
                        local_addr = fields[1]
                        state = fields[3]
                        inode = fields[9]
                        # 0A = TCP_LISTEN
                        if state == "0A" and local_addr.endswith(":" + hex_port):
                            socket_inodes.add(inode)

            if not socket_inodes:
                return None

            # Scan /proc for matching socket inode
            for proc_dir in Path("/proc").iterdir():
                if not proc_dir.name.isdigit():
                    continue
                pid = int(proc_dir.name)
                fd_dir = proc_dir / "fd"
                if not fd_dir.exists():
                    continue
                try:
                    for fd in fd_dir.iterdir():
                        try:
                            target = os.readlink(fd)
                            for inode in socket_inodes:
                                if f"socket:[{inode}]" in target:
                                    return pid
                        except (OSError, ValueError):
                            continue
                except (PermissionError, OSError):
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def verify_status(
        port: Optional[int],
        recorded_pid: Optional[int],
        entry_point: str,
        root_dir: Path,
    ) -> Tuple[str, Optional[int], str, Optional[str]]:
        """
        Dual-verification algorithm.
        Returns: (STATUS_CODE, ACTIVE_PID, DETAIL, START_TIME)
        - RUNNING: Process alive and port bound to self
        - INITIALIZING: Process alive, port not bound yet (or no port configured)
        - CONFLICT: Port occupied by another process/instance
        - ZOMBIE: Recorded PID is dead or lingering lock
        - STOPPED: Process not running, port free
        """
        listening_pid = PortDetector.get_listening_pid(port) if port else None
        recorded_alive = ProcessManager.is_alive(recorded_pid) if recorded_pid else False

        # Case 1: Port is bound
        if listening_pid:
            cmdline, start_time = ProcessManager.get_process_info(listening_pid)
            is_ours = False
            if recorded_pid and listening_pid == recorded_pid:
                is_ours = True
            elif cmdline:
                entry_name = os.path.basename(entry_point)
                if entry_name in cmdline or str(root_dir).lower() in cmdline.lower():
                    is_ours = True

            if is_ours:
                detail = f"Port: {port}, PID: {listening_pid}, Cmd: {cmdline or 'N/A'}"
                return "RUNNING", listening_pid, detail, start_time
            else:
                detail = f"Port {port} occupied by OTHER process (PID: {listening_pid}, Cmd: {cmdline or 'N/A'})"
                return "CONFLICT", listening_pid, detail, start_time

        # Case 2: No port listening, but recorded PID is alive
        if recorded_alive and recorded_pid:
            cmdline, start_time = ProcessManager.get_process_info(recorded_pid)
            if port is not None:
                detail = f"Process running (PID: {recorded_pid}), waiting for port {port} to bind..."
                return "INITIALIZING", recorded_pid, detail, start_time
            else:
                detail = f"Process running (PID: {recorded_pid}), no port configured. Cmd: {cmdline or 'N/A'}"
                return "RUNNING", recorded_pid, detail, start_time

        # Case 3: Recorded PID is dead but PID file remains
        if recorded_pid and not recorded_alive:
            return "ZOMBIE", recorded_pid, f"PID file exists ({recorded_pid}) but process is not running.", None

        # Case 4: Service completely stopped
        return "STOPPED", None, "Service is currently stopped.", None
