"""prod up 的 smoke：容器 healthy 之後，確認服務真的可用（唯讀，不讀 secret，不印回應內容）。

檢查：
  1 穩定性  觀察窗內 RestartCount 不增加、State 仍為 running（擋 crash loop）
  2 TCP     宣告的每個單一 TCP 埠（host_ip:host_port）可連線（埠範圍略過）
  3 HTTP    宣告 `smoke.http` 時，GET url 回傳預期狀態碼（預設 200；insecure=true 略過憑證驗證）
宣告（選填）：smoke = {"stable_seconds": 10, "tcp": true, "http": {"url": "...", "status": 200, "insecure": false}}
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

DEFAULT_STABLE_SECONDS = 10


def _inspect(container: str) -> dict:
    r = subprocess.run(["podman", "inspect", container, "--format", "{{json .}}"], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"podman inspect 失敗：{r.stderr.strip()[:120]}")
    return json.loads(r.stdout)


def _published_tcp(ports: list) -> list[tuple[str, int]]:
    out = []
    for spec in ports or []:
        s = str(spec)
        proto = "tcp"
        if "/" in s:
            s, proto = s.rsplit("/", 1)
        if proto != "tcp":
            continue
        parts = s.split(":")
        if len(parts) == 3:
            host, hport = parts[0], parts[1]
        elif len(parts) == 2:
            host, hport = "127.0.0.1", parts[0]
        else:
            continue
        if "-" in hport or not hport.isdigit():
            continue
        out.append((host, int(hport)))
    return out


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_status(url: str, insecure: bool, timeout: float = 5.0) -> int | str:
    ctx = None
    if url.startswith("https://") and insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, context=ctx, timeout=timeout) as resp:  # noqa: S310 — url 來自宣告
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"error:{type(exc).__name__}"


def run_smoke(container: str, cfg: dict, *, sleep=time.sleep, inspect=_inspect,
              tcp_check=_tcp_ok, http_check=_http_status) -> tuple[bool, list[str]]:
    spec = cfg.get("smoke") or {}
    msgs: list[str] = []
    ok = True
    window = int(spec.get("stable_seconds", DEFAULT_STABLE_SECONDS))
    try:
        before = inspect(container)
        sleep(window)
        after = inspect(container)
    except (RuntimeError, ValueError) as exc:
        return False, [f"[ERROR] smoke 穩定性：{exc}"]
    r0, r1 = before.get("RestartCount", 0), after.get("RestartCount", 0)
    state = (after.get("State") or {}).get("Status")
    if state != "running" or r1 > r0:
        ok = False
        msgs.append(f"[ERROR] smoke 穩定性：{window}s 內狀態={state}，RestartCount {r0}→{r1}（疑似 crash loop）")
    else:
        msgs.append(f"[OK]   smoke 穩定性：{window}s 內 running，RestartCount 不變（{r1}）")
    if spec.get("tcp", True):
        for host, port in _published_tcp(cfg.get("ports")):
            if tcp_check(host, port):
                msgs.append(f"[OK]   smoke TCP {host}:{port} 可連線")
            else:
                ok = False
                msgs.append(f"[ERROR] smoke TCP {host}:{port} 無法連線")
    http = spec.get("http")
    if http:
        want = int(http.get("status", 200))
        got = http_check(str(http.get("url", "")), bool(http.get("insecure", False)))
        if got == want:
            msgs.append(f"[OK]   smoke HTTP {http.get('url')} → {got}")
        else:
            ok = False
            msgs.append(f"[ERROR] smoke HTTP {http.get('url')} → {got}（預期 {want}）")
    return ok, msgs
