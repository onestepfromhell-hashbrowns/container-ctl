"""
Readiness and liveness probe execution engine for containerized services.
Supports HTTP GET, TCP Socket, and Exec command probes.
"""

import http.client
from pathlib import Path
import socket
import subprocess
from typing import Any, Dict, Optional, Tuple


class ReadinessProbe:
    @staticmethod
    def check(probe_config: Optional[Dict[str, Any]], root_dir: Optional[Path] = None) -> Tuple[bool, str]:
        """
        Executes configured probe and returns (is_healthy, detail_message).
        """
        if not probe_config:
            return True, "No probe configured"

        probe_type = str(probe_config.get("type", "http")).lower()
        timeout = float(probe_config.get("timeout_seconds", 2.0))
        root = root_dir or Path.cwd()

        if probe_type == "http":
            return ReadinessProbe._check_http(probe_config, timeout)
        elif probe_type == "tcp":
            return ReadinessProbe._check_tcp(probe_config, timeout)
        elif probe_type == "exec":
            return ReadinessProbe._check_exec(probe_config, timeout, root)
        else:
            return False, f"Unknown probe type '{probe_type}'"

    @staticmethod
    def _check_http(cfg: Dict[str, Any], timeout: float) -> Tuple[bool, str]:
        host = str(cfg.get("host", "127.0.0.1"))
        port = cfg.get("port")
        if not port:
            return False, "HTTP probe requires 'port'"

        path = str(cfg.get("path", "/health"))
        if not path.startswith("/"):
            path = "/" + path

        expected_status = cfg.get("expected_status", 200)
        if isinstance(expected_status, int):
            expected_codes = [expected_status]
        elif isinstance(expected_status, list):
            expected_codes = [int(x) for x in expected_status]
        else:
            expected_codes = [200]

        conn = None
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
            conn.request("GET", path)
            resp = conn.getresponse()
            status = resp.status
            if status in expected_codes:
                return True, f"HTTP {status} OK ({path})"
            else:
                return False, f"HTTP status {status} not in expected {expected_codes} ({path})"
        except Exception as e:
            return False, f"HTTP probe failed ({host}:{port}{path}): {e}"
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def _check_tcp(cfg: Dict[str, Any], timeout: float) -> Tuple[bool, str]:
        host = str(cfg.get("host", "127.0.0.1"))
        port = cfg.get("port")
        if not port:
            return False, "TCP probe requires 'port'"

        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, f"TCP {host}:{port} reachable"
        except Exception as e:
            return False, f"TCP probe failed ({host}:{port}): {e}"

    @staticmethod
    def _check_exec(cfg: Dict[str, Any], timeout: float, root_dir: Path) -> Tuple[bool, str]:
        cmd = cfg.get("command")
        if not cmd:
            return False, "Exec probe requires 'command'"

        is_shell = isinstance(cmd, str)
        try:
            res = subprocess.run(
                cmd,
                cwd=str(root_dir),
                shell=is_shell,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                return True, f"Exec probe passed (exit 0)"
            else:
                err_msg = res.stderr.strip() or res.stdout.strip()
                return False, f"Exec probe failed with exit code {res.returncode}: {err_msg}"
        except subprocess.TimeoutExpired:
            return False, f"Exec probe timed out after {timeout}s"
        except Exception as e:
            return False, f"Exec probe failed: {e}"
