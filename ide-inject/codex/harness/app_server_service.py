from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import urllib.parse
from typing import Any


CONTRACT_UDS_PARENT_DIRS = (
    "/run/user/",
    "/run/secrets/codex/",
    "/run/ext/",
    "/run/",
    "/tmp/codex-uds/",
    "/tmp/",
)


def validate_b2_endpoint(
    listen_uri: str,
    auth_mode: str | None = None,
    token_file: str | None = None,
    token_sha256: str | None = None,
) -> tuple[bool, str]:
    """SPEC-004 & TICKET-004a: Validate Codex App Server B-2 transport endpoint contract.

    Permitted modes:
    1. stdio:// or --stdio: standard stdio transport.
    2. unix://<path>: local POSIX Unix Domain Socket (requires 0600 isolation and contracted parent directory).
    3. ws://127.0.0.1:<port> or ws://localhost:<port>: loopback WebSocket.
    4. ws://0.0.0.0:<port> or other non-loopback: MUST enforce capability-token auth.
    All others (including http://, path traversal, or uncontracted schemes) are rejected (Fail-Closed).
    """
    if not listen_uri or not isinstance(listen_uri, str):
        return False, "Listen URI cannot be empty"

    clean = listen_uri.strip()
    if clean in ("stdio://", "--stdio"):
        return True, "stdio transport"

    if clean.startswith("unix://"):
        sock_path = clean[len("unix://"):].strip()
        if not sock_path:
            return False, "Unix socket path cannot be empty"
        if not os.path.isabs(sock_path):
            return False, f"Unix socket path must be absolute: {sock_path}"
        if ".." in sock_path.split(os.sep):
            return False, f"Unix socket path must not contain path traversal ('..'): {sock_path}"
        if os.path.normpath(sock_path) != sock_path:
            return False, f"Unix socket path must be normalized (no traversal or redundant separators): {sock_path}"
        if not any(sock_path.startswith(p) for p in CONTRACT_UDS_PARENT_DIRS):
            return False, f"Unix socket parent directory not in contract allowlist ({CONTRACT_UDS_PARENT_DIRS}): {sock_path}"
        return True, f"POSIX UDS transport ({sock_path})"

    if clean.startswith("ws://"):
        u = urllib.parse.urlparse(clean)
        host = u.hostname or ""
        port = u.port
        if not port or port < 1024 or port > 65535:
            return False, f"Invalid port in WebSocket URI (must be 1024-65535): {clean}"

        if host in ("127.0.0.1", "localhost", "::1"):
            return True, f"Loopback WebSocket transport ({host}:{port})"

        # Non-loopback transport requires capability-token auth and valid token file or sha256
        if auth_mode != "capability-token":
            return False, f"Non-loopback transport ({host}:{port}) requires --ws-auth capability-token"
        if not token_file and not token_sha256:
            return False, "Non-loopback transport requires valid --ws-token-file or --ws-token-sha256"

        if token_file:
            if not os.path.exists(token_file):
                return False, f"Token file {token_file} does not exist"
            st = os.stat(token_file)
            if st.st_mode & 0o777 != 0o600:
                return False, f"Token file {token_file} permissions must be 0600, got {oct(st.st_mode & 0o777)}"
        elif token_sha256:
            if len(token_sha256) != 64:
                return False, f"Invalid token SHA256 digest length: {len(token_sha256)}"

        return True, f"Authenticated non-loopback WebSocket transport ({host}:{port})"

    return False, f"Unsupported transport endpoint scheme: {listen_uri}"


def create_b2_capability_token(token_dir: str) -> tuple[str, str, str]:
    """Generate a high-entropy 256-bit ephemeral capability token, write to tmpfs with 0600 mode.
    Returns (raw_token, token_file_path, sha256_hex_digest).
    """
    os.makedirs(token_dir, exist_ok=True)
    raw_token = secrets.token_hex(32)  # 256-bit entropy
    token_file = os.path.join(token_dir, "app_server_token")

    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw_token.strip() + "\n")

    os.chmod(token_file, 0o600)
    token_sha256 = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return raw_token, token_file, token_sha256


def build_b2_app_server_command(
    listen_uri: str,
    *,
    codex_bin: str = "codex",
    config_path: str | None = None,
    auth_mode: str | None = None,
    token_file: str | None = None,
    token_sha256: str | None = None,
) -> list[str]:
    """Construct the official codex app-server command according to SPEC-004."""
    valid, reason = validate_b2_endpoint(listen_uri, auth_mode, token_file, token_sha256)
    if not valid:
        raise ValueError(f"Invalid B-2 endpoint configuration: {reason}")

    cmd = [codex_bin]
    if config_path:
        cmd.extend(["-c", config_path])
    cmd.extend(["app-server", "--strict-config", "--listen", listen_uri])

    if auth_mode:
        cmd.extend(["--ws-auth", auth_mode])
    # Note: --ws-token-file and --ws-token-sha256 are mutually exclusive in codex-cli
    if token_file:
        cmd.extend(["--ws-token-file", token_file])
    elif token_sha256:
        cmd.extend(["--ws-token-sha256", token_sha256])

    return cmd


def verify_b2_schema_generation(codex_bin: str = "codex", out_dir: str | None = None) -> dict[str, Any]:
    """Execute 'codex app-server generate-json-schema --out <DIR>' and verify valid schema output."""
    import tempfile
    managed_tmp = None
    target_dir = out_dir
    if not target_dir:
        managed_tmp = tempfile.TemporaryDirectory()
        target_dir = managed_tmp.name

    try:
        proc = subprocess.run(
            [codex_bin, "app-server", "generate-json-schema", "--out", target_dir],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"generate-json-schema failed (code {proc.returncode}): {proc.stderr}")

        schema_file = os.path.join(target_dir, "codex_app_server_protocol.schemas.json")
        if not os.path.exists(schema_file):
            # Try any .json file in the output directory
            candidates = [f for f in os.listdir(target_dir) if f.endswith(".json")]
            if not candidates:
                raise FileNotFoundError(f"No schema JSON files found in {target_dir}")
            schema_file = os.path.join(target_dir, candidates[0])

        with open(schema_file, "r", encoding="utf-8") as f:
            schema = json.load(f)

        if not isinstance(schema, dict) or ("$schema" not in schema and "title" not in schema and "definitions" not in schema and "properties" not in schema):
            raise ValueError(f"Schema output does not contain standard JSON Schema root keys: {list(schema.keys())[:5]}")

        return schema
    finally:
        if managed_tmp:
            managed_tmp.cleanup()


def prepare_uds_umask() -> int:
    """Set restrictive umask 0177 before socket creation, returning old umask."""
    return os.umask(0o177)


def restore_umask(old_umask: int) -> None:
    """Restore previous umask."""
    os.umask(old_umask)


def verify_uds_socket_permissions(sock_path: str) -> bool:
    """Verify that a POSIX Unix Domain Socket exists and has permissions 0600."""
    if not os.path.exists(sock_path):
        return False
    st = os.stat(sock_path)
    # Check socket file type and mode
    import stat as stat_mod
    if not stat_mod.S_ISSOCK(st.st_mode):
        return False
    return (st.st_mode & 0o777) == 0o600


def probe_b2_websocket_handshake(
    host: str,
    port: int,
    token: str | None = None,
    timeout: float = 2.0,
) -> tuple[int, str]:
    """Perform a live HTTP/WebSocket handshake probe against running App Server.
    
    Returns (status_code, status_line).
    Status code:
      - 101: Handshake accepted (Switching Protocols)
      - 401: Unauthorized
      - 403: Forbidden
      - 0: Connection closed / rejected without HTTP response
    """
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    try:
        headers = [
            "GET / HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version: 13",
        ]
        if token:
            headers.append(f"Authorization: Bearer {token}")
        payload = "\r\n".join(headers) + "\r\n\r\n"
        s.sendall(payload.encode("utf-8"))

        data = s.recv(1024)
        if not data:
            return 0, "Connection closed by peer"
        lines = data.decode("utf-8", errors="replace").split("\r\n")
        first_line = lines[0]
        parts = first_line.split(" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return code, first_line
    except (socket.timeout, ConnectionResetError, BrokenPipeError) as exc:
        return 0, str(exc)
    finally:
        try:
            s.close()
        except Exception:
            pass


class ManagedB2Server:
    """Managed B-2 Codex App Server lifecycle runner.

    SPEC-004 / TICKET-004a / Release Re-review:
    - Validates exact endpoint allowlist before launch.
    - Creates per-session tmpfs token with 0600 mode for non-loopback transports.
    - Sets restrictive umask 0177 for UDS socket creation (0600 mode).
    - Guarantees deterministic removal and cleanup of token and socket files upon termination.
    """

    def __init__(
        self,
        listen_uri: str,
        *,
        codex_bin: str = "codex",
        config_path: str | None = None,
        auth_mode: str | None = None,
        token_dir: str | None = None,
        endpoint_allowlist: set[str] | list[str] | None = None,
    ):
        self.listen_uri = listen_uri.strip() if listen_uri else "stdio://"
        self.codex_bin = codex_bin
        self.config_path = config_path
        self.auth_mode = auth_mode
        self.token_dir = token_dir or ("/run/secrets/codex" if os.path.exists("/run/secrets") else "/tmp/codex-secrets")
        self.endpoint_allowlist = set(endpoint_allowlist) if endpoint_allowlist is not None else None

        self.proc: subprocess.Popen | None = None
        self.token_file: str | None = None
        self.raw_token: str | None = None
        self.token_sha256: str | None = None
        self.managed_token: bool = False
        self.sock_path: str | None = None
        self._terminated: bool = False

    def validate_against_allowlist(self) -> None:
        """Enforce exact endpoint allowlist if configured."""
        if self.endpoint_allowlist is not None:
            if self.listen_uri not in self.endpoint_allowlist:
                raise ValueError(
                    f"Listen URI '{self.listen_uri}' rejected: not in declared endpoint allowlist {sorted(self.endpoint_allowlist)}"
                )

    def prepare(self) -> list[str]:
        self.validate_against_allowlist()

        # Handle UDS path extraction
        if self.listen_uri.startswith("unix://"):
            self.sock_path = self.listen_uri[len("unix://"):].strip()
            parent_dir = os.path.dirname(self.sock_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)

        # Handle Non-loopback WebSocket Token Generation
        u = urllib.parse.urlparse(self.listen_uri) if self.listen_uri.startswith("ws://") else None
        is_non_loopback_ws = u and (u.hostname not in ("127.0.0.1", "localhost", "::1"))

        if is_non_loopback_ws:
            self.auth_mode = "capability-token"
            self.raw_token, self.token_file, self.token_sha256 = create_b2_capability_token(self.token_dir)
            self.managed_token = True

        valid, reason = validate_b2_endpoint(
            self.listen_uri,
            auth_mode=self.auth_mode,
            token_file=self.token_file,
            token_sha256=self.token_sha256,
        )
        if not valid:
            self.cleanup()
            raise ValueError(f"Endpoint contract validation failed: {reason}")

        cmd = build_b2_app_server_command(
            self.listen_uri,
            codex_bin=self.codex_bin,
            config_path=self.config_path,
            auth_mode=self.auth_mode,
            token_file=self.token_file,
        )
        return cmd

    def start(self) -> subprocess.Popen:
        cmd = self.prepare()
        old_umask = None
        if self.sock_path:
            old_umask = prepare_uds_umask()

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            if old_umask is not None:
                restore_umask(old_umask)

        return self.proc

    def cleanup(self) -> None:
        """Strict deterministic cleanup of ephemeral token and socket files upon termination."""
        if self._terminated:
            return
        self._terminated = True

        # 1. Ephemeral token destruction immediately: zero disk residue
        if self.managed_token and self.token_file:
            try:
                if os.path.exists(self.token_file):
                    os.remove(self.token_file)
            except OSError:
                pass

        # 2. Cleanup UDS socket file if created
        if self.sock_path:
            try:
                if os.path.exists(self.sock_path):
                    os.remove(self.sock_path)
            except OSError:
                pass

        # 3. Terminate underlying subprocess
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def run_until_exit(self) -> int:
        import atexit
        import signal
        import sys

        atexit.register(self.cleanup)

        def sig_handler(signum, frame):
            self.cleanup()
            sys.exit(0)

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, sig_handler)
            except Exception:
                pass

        try:
            self.start()
            ret = self.proc.wait()
            return ret if ret is not None else 0
        except SystemExit:
            self.cleanup()
            return 0
        finally:
            self.cleanup()


def run_managed_b2_entrypoint(argv: list[str] | None = None) -> int:
    """CLI entrypoint for managed B-2 Codex App Server launch."""
    import argparse
    import shutil
    import sys

    parser = argparse.ArgumentParser(description="Managed Codex B-2 App Server Entrypoint")
    parser.add_argument(
        "--listen",
        default=os.environ.get("CODEX_APP_SERVER_LISTEN", "stdio://"),
        help="Listen URI (e.g. stdio://, unix:///run/user/1000/codex.sock, ws://127.0.0.1:49200)",
    )
    parser.add_argument(
        "--endpoint-allowlist",
        default=os.environ.get("CODEX_ENDPOINT_ALLOWLIST"),
        help="Comma-separated exact endpoint allowlist (Fail-Closed if not matched)",
    )
    parser.add_argument(
        "--config-path",
        "-c",
        default=None,
        help="Path to filtered config.toml",
    )
    parser.add_argument(
        "--codex-bin",
        default=shutil.which("codex") or "/usr/local/bin/codex",
        help="Path to underlying Codex binary",
    )
    parser.add_argument(
        "--token-dir",
        default=None,
        help="Tmpfs directory for ephemeral capability token",
    )

    args, _ = parser.parse_known_args(argv)

    allowlist = None
    if args.endpoint_allowlist:
        allowlist = {x.strip() for x in args.endpoint_allowlist.split(",") if x.strip()}

    server = ManagedB2Server(
        listen_uri=args.listen,
        codex_bin=args.codex_bin,
        config_path=args.config_path,
        token_dir=args.token_dir,
        endpoint_allowlist=allowlist,
    )
    return server.run_until_exit()


if __name__ == "__main__":
    import sys
    sys.exit(run_managed_b2_entrypoint())
