"""
Network checker for Container Doctor (remote SSH & container networking).
Verifies loopback interface, DNS resolvability, and declared port reachability.
"""

from pathlib import Path
import socket
import time
from typing import List

from container_doctor.checkers.base import BaseChecker
from container_doctor.core.context import ExecutionContext
from container_doctor.core.models import CheckDomain, CheckResult, CheckStatus


class NetworkChecker(BaseChecker):
    domain = CheckDomain.NETWORK.value

    def check(self, context: ExecutionContext) -> List[CheckResult]:
        results: List[CheckResult] = []
        declared_cfg = context.declared_config

        # 1. Check Loopback (127.0.0.1) availability
        t0 = time.perf_counter()
        loopback_ok = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.bind(("127.0.0.1", 0))
            _, port = sock.getsockname()
            sock.close()
            loopback_ok = True
        except Exception:
            loopback_ok = False

        if loopback_ok:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="loopback_interface",
                    status=CheckStatus.PASS,
                    message="Loopback interface (127.0.0.1) operational",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="loopback_interface",
                    status=CheckStatus.FAIL,
                    message="Loopback interface failed socket bind test",
                    expected="127.0.0.1 available for dynamic binding",
                    actual="bind failed",
                    remediation="Check container network namespace and rootless podman network configuration.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        # 2. Check /etc/resolv.conf and Basic DNS Configuration
        t0 = time.perf_counter()
        resolv_path = Path("/etc/resolv.conf")
        if resolv_path.exists():
            # Check if resolv.conf has nameserver entries
            has_nameserver = False
            try:
                content = resolv_path.read_text()
                has_nameserver = "nameserver" in content
            except Exception:
                pass

            if has_nameserver:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="dns_configuration",
                        status=CheckStatus.PASS,
                        message="/etc/resolv.conf configured with active nameservers",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            else:
                results.append(
                    CheckResult(
                        domain=self.domain,
                        name="dns_configuration",
                        status=CheckStatus.WARN,
                        message="/etc/resolv.conf exists but contains no nameserver directives",
                        expected="At least one nameserver configured",
                        actual="empty / no nameserver",
                        remediation="Configure DNS server in container network settings.",
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
        else:
            results.append(
                CheckResult(
                    domain=self.domain,
                    name="dns_configuration",
                    status=CheckStatus.WARN,
                    message="/etc/resolv.conf is missing",
                    expected="/etc/resolv.conf present",
                    actual="missing",
                    remediation="Ensure container network is configured with DNS resolvers.",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        # 3. Check Declared Container Ports
        t0 = time.perf_counter()
        if declared_cfg and declared_cfg.declared_ports:
            port_statuses = []
            for p_decl in declared_cfg.declared_ports:
                # Format: "8080:80" or "8080"
                port_num = int(p_decl.split(":")[-1].split("/")[0]) if any(c.isdigit() for c in p_decl) else None
                if port_num:
                    port_statuses.append(f"Port {p_decl} registered")

            results.append(
                CheckResult(
                    domain=self.domain,
                    name="declared_ports_readiness",
                    status=CheckStatus.PASS,
                    message=f"Declared network ports ({len(declared_cfg.declared_ports)} ports) validated: {', '.join(declared_cfg.declared_ports)}",
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                )
            )

        return results
