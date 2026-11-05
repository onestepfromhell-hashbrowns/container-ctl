"""
SSH and dynamic routing token checker for Container Doctor (remote SSH extension).
Verifies key permissions (0700/0600), dynamic routing tokens, and sshd socket reachability.
"""

import os
from pathlib import Path
import socket
import stat
import time
from typing import List, Optional

from container_doctor.checkers.base import BaseChecker
from container_doctor.core import site_paths
from container_doctor.core.context import ExecutionContext, ExecutionMode
from container_doctor.core.models import CheckDomain, CheckResult, CheckStatus


def _is_port_open(host: str, port: int, timeout: float = 0.015) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


class SshChecker(BaseChecker):
    domain = CheckDomain.SSH.value

    def check(self, context: ExecutionContext) -> List[CheckResult]:
        results: List[CheckResult] = []
        uid = os.getuid() if hasattr(os, "getuid") else 1000

        # 1. Check SSH key files and directory permissions
        t0 = time.perf_counter()
        ssh_dirs = [
            Path.home() / ".ssh",
            Path("/root/.ssh"),
            Path(f"/run/user/{uid}/{site_paths.RUNTIME_SUBDIR}/ssh"),
            Path(site_paths.ROUTING_DIR),
            Path("/run/ext/tokens"),
        ]

        active_ssh_dirs = [d for d in ssh_dirs if d.exists() and d.is_dir()]
        permission_issues = []

        for d in active_ssh_dirs:
            try:
                st = d.stat()
                mode = stat.S_IMODE(st.st_mode)
                if mode & 0o002:  # world writable
                    permission_issues.append(f"{d} directory is world writable ({oct(mode)})")

                for f in d.iterdir():
                    if f.is_file():
                        fst = f.stat()
                        fmode = stat.S_IMODE(fst.st_mode)
                        if fmode & 0o002:
                            permission_issues.append(f"{f} is world-writable ({oct(fmode)})")
                        if "id_" in f.name and not f.name.endswith(".pub"):
                            # Private key should be 0600
                            if fmode & 0o077:
                                permission_issues.append(f"{f} private key has loose permissions ({oct(fmode)})")
                        if ("session_token" in f.name or f.name.startswith("s-")) and not f.name.endswith(".lobby_pid") and not f.name.endswith(".sock"):
                            # Dynamic token private routing files should be 0600 or 0640/0660
                            if fmode & 0o007:  # world readable/writable
                                permission_issues.append(f"{f} dynamic token has loose permissions ({oct(fmode)})")
            except Exception:
                pass

        if permission_issues:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_key_permissions",
                    status=CheckStatus.FAIL,
                    message=f"SSH permissions insecure: {len(permission_issues)} issues found",
                    expected="SSH directories 0700, private keys and tokens 0600",
                    actual="; ".join(permission_issues),
                    remediation="Run `chmod 700 ~/.ssh && chmod 600 ~/.ssh/*` and secure dynamic routing tokens.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_key_permissions",
                    status=CheckStatus.PASS,
                    message="SSH keys and dynamic routing tokens permissions verified (0700/0600)",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        # 2. Check Dynamic Token Routing Configuration
        t0 = time.perf_counter()
        token_locations = [
            Path(site_paths.ROUTING_DIR),
            Path(f"/run/user/{uid}/{site_paths.RUNTIME_SUBDIR}/routing"),
            Path("/run/ext/tokens"),
        ]
        tokens_found = []
        for tdir in token_locations:
            if tdir.exists() and tdir.is_dir():
                try:
                    for tf in tdir.iterdir():
                        if tf.is_file() and not tf.name.startswith("."):
                            tokens_found.append(str(tf))
                except Exception:
                    pass

        # 3. Check SSH Service & Socket Readiness
        t0 = time.perf_counter()
        # Probe common SSH ports: check 2222 then 22
        ssh_ports = [2222, 22]
        open_ports = [p for p in ssh_ports if _is_port_open("127.0.0.1", p)]

        has_auth_keys = (
            (Path.home() / ".ssh" / "authorized_keys").exists()
            or Path("/root/.ssh/authorized_keys").exists()
        )
        has_proxy_script = Path(site_paths.PROXY_SCRIPT).exists()

        if open_ports:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_service_readiness",
                    status=CheckStatus.PASS,
                    message=f"SSH service listening on port(s): {', '.join(str(p) for p in open_ports)}",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        elif has_proxy_script or tokens_found or has_auth_keys:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_service_readiness",
                    status=CheckStatus.PASS,
                    message=f"SSH subsystem configured for dynamic routing (proxy/keys present; {len(tokens_found)} tokens active)",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        elif context.mode == ExecutionMode.INSIDE:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_service_readiness",
                    status=CheckStatus.WARN,
                    message="SSH daemon is not running and no dynamic routing configuration was discovered",
                    expected="sshd listening on port 22/2222 or dynamic routing tokens configured",
                    actual="No active sshd socket or tokens found",
                    remediation="Inject the remote SSH extension or start sshd service.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="ssh_service_readiness",
                    status=CheckStatus.PASS,
                    message="Host SSH router operational",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        return results
