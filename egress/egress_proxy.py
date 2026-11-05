"""Allowlist CONNECT proxy for session containers (egress fence).

Session containers sit on an internal network and reach the world only through this proxy.
Policy: CONNECT only, port 443 only, host must match the allowlist, resolved addresses must be public.
Denied requests are logged (host only) and answered 403; the proxy never falls back to a direct path.
Stdlib only, so it runs in the pinned agent image.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import sys
from typing import Iterable, Optional, Tuple

ALLOWED_PORT = 443
HEADER_LIMIT = 8192
IDLE_S = 300.0


def host_allowed(host: str, allow: Iterable[str]) -> bool:
    """Exact match, or `*.suffix` entries matching any subdomain (not the bare suffix)."""
    h = host.lower().rstrip(".")
    for a in allow:
        a = a.strip().lower()
        if not a:
            continue
        if a.startswith("*."):
            if h.endswith(a[1:]) and len(h) > len(a) - 1:
                return True
        elif h == a:
            return True
    return False


def parse_connect(line: str) -> Optional[Tuple[str, int]]:
    parts = line.strip().split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT" or not parts[2].upper().startswith("HTTP/"):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


def public_address(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(r.read(65536), IDLE_S)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


class EgressProxy:
    def __init__(self, allow: Iterable[str], log=None) -> None:
        self.allow = tuple(allow)
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))

    async def _resolve(self, host: str) -> Optional[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, ALLOWED_PORT, type=socket.SOCK_STREAM)
        except OSError:
            return None
        ips = [i[4][0] for i in infos]
        return ips[0] if ips and all(public_address(ip) for ip in ips) else None

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        async def deny(code: str, why: str, host: str = "-") -> None:
            self.log(f"deny {why} host={host}")
            w.write(f"HTTP/1.1 {code}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            try:
                await w.drain()
            finally:
                w.close()

        try:
            line = (await asyncio.wait_for(r.readline(), 15.0)).decode("latin1")
            total = len(line)
            while True:                                  # discard request headers
                h = await asyncio.wait_for(r.readline(), 15.0)
                total += len(h)
                if h in (b"\r\n", b"\n", b""):
                    break
                if total > HEADER_LIMIT:
                    return await deny("431 Request Header Fields Too Large", "headers")
        except (asyncio.TimeoutError, ConnectionError, OSError):
            w.close()
            return
        target = parse_connect(line)
        if target is None:
            return await deny("405 Method Not Allowed", "not-connect")
        host, port = target
        if port != ALLOWED_PORT:
            return await deny("403 Forbidden", f"port={port}", host)
        if not host_allowed(host, self.allow):
            return await deny("403 Forbidden", "not-allowlisted", host)
        ip = await self._resolve(host)
        if ip is None:
            return await deny("403 Forbidden", "non-public-or-unresolved", host)
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(ip, port), 10.0)
        except (asyncio.TimeoutError, OSError):
            return await deny("502 Bad Gateway", "upstream-connect", host)
        w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await w.drain()
        await asyncio.gather(_pipe(r, uw), _pipe(ur, w))


async def serve(bind: str, port: int, allow: Iterable[str]) -> None:
    proxy = EgressProxy(allow)
    server = await asyncio.start_server(proxy.handle, bind, port)
    async with server:
        await server.serve_forever()


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=3128)
    p.add_argument("--allow-file", required=True, help="one host or *.suffix per line; # comments allowed")
    a = p.parse_args(argv)
    with open(a.allow_file, encoding="utf-8") as f:
        allow = [ln.split("#", 1)[0].strip() for ln in f]
    allow = [x for x in allow if x]
    if not allow:
        print("empty allowlist: refusing to start", file=sys.stderr)
        return 2
    asyncio.run(serve(a.bind, a.port, allow))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
