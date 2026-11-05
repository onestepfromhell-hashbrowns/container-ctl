"""agent_exec.py — 控制面容器內，把 SystemdManager 的 systemctl 呼叫改經 host-agent 執行。

僅當環境變數 CONTAINER_CTL_HOST_AGENT（host-agent 的 UDS 絕對路徑）存在時啟用；未設定時 lifecycle_adapter 照舊走
subprocess.run，行為與先前完全相同。啟用後**不回退**本機 systemctl（容器內沒有 user bus，回退只會掩蓋錯誤）。

協定：每連線一行 JSON 請求 {"verb","params"}、一行 JSON 回覆 {"ok","rc","stdout","stderr","error"}。
argv → 動詞只認 SystemdManager 實際產生的形態，其餘一律 AgentError（不猜、不放行）。
設計見 ADR-012 control-plane containerization。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from typing import Optional

AGENT_ENV = "CONTAINER_CTL_HOST_AGENT"
_MAX_REPLY = 1 << 20
_DEFAULT_TIMEOUT = 70.0  # 略大於 agent 端最長動詞逾時（stop 60s）


class AgentError(RuntimeError):
    """不支援的 argv、agent 不可達、或 agent 拒絕（unauthorized／不在白名單／保護單元）。"""


def agent_socket(environ=os.environ) -> Optional[str]:
    v = environ.get(AGENT_ENV)
    return v or None


def _translate(argv: list[str]) -> tuple[str, dict]:
    if len(argv) < 3 or argv[0] != "systemctl" or argv[1] != "--user":
        raise AgentError(f"agent 模式不支援的命令：{argv[:3]}")
    act, rest = argv[2], argv[3:]
    if act in ("start", "restart"):
        if len(rest) != 2 or rest[0] != "--no-block":
            raise AgentError(f"{act} 只支援 --no-block 單一單元")
        return f"systemd.{act}", {"unit": rest[1]}
    if act == "stop" and len(rest) == 1:
        return "systemd.stop", {"unit": rest[0]}
    if act == "is-active":
        rest = [r for r in rest if r != "--quiet"]
        if len(rest) == 1:
            return "systemd.is_active", {"unit": rest[0]}
    if act == "daemon-reload" and not rest:
        return "systemd.daemon_reload", {}
    if act == "list-units" and len(rest) == 3 and rest[1:] == ["--no-legend", "--plain"]:
        return "systemd.list_units", {"pattern": rest[0]}
    if act == "kill" and len(rest) == 2 and rest[0].startswith("--signal="):
        return "systemd.kill", {"unit": rest[1], "signal": rest[0].split("=", 1)[1]}
    if act == "reset-failed" and len(rest) == 1:
        return "systemd.reset_failed", {"unit": rest[0]}
    raise AgentError(f"agent 模式不支援的 systemctl 形態：{argv[2:]}")


def _call(verb: str, params: dict, argv, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    path = agent_socket()
    if not path or not os.path.isabs(path):
        raise AgentError(f"{AGENT_ENV} 未設定或不是絕對路徑")
    sock_timeout = (timeout + 2.0) if timeout else _DEFAULT_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(sock_timeout)
            s.connect(path)
            s.sendall(json.dumps({"verb": verb, "params": params}).encode() + b"\n")
            data = b""
            while b"\n" not in data and len(data) <= _MAX_REPLY:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except socket.timeout as exc:
        raise subprocess.TimeoutExpired(argv, sock_timeout) from exc
    except OSError as exc:
        raise AgentError(f"無法連線 host-agent {path}：{exc}") from exc
    try:
        reply = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AgentError("host-agent 回覆格式異常") from exc
    if not isinstance(reply, dict):
        raise AgentError("host-agent 回覆格式異常")
    if reply.get("error"):
        raise AgentError(f"host-agent 拒絕 {verb}：{reply['error']}")
    rc = reply.get("rc")
    if not isinstance(rc, int):
        raise AgentError("host-agent 回覆缺少 rc")
    return subprocess.CompletedProcess(argv, rc, reply.get("stdout") or "", reply.get("stderr") or "")


def run_via_agent(argv: list[str], *, timeout: Optional[float] = None, **_ignored) -> subprocess.CompletedProcess:
    """以 subprocess.run 相容的介面執行 argv；env／capture_output／text 等參數在 agent 模式下無意義而忽略。"""
    if not agent_socket():
        raise AgentError(f"{AGENT_ENV} 未設定")
    verb, params = _translate(list(argv))
    return _call(verb, params, argv, timeout)


def unshare_install_file(src: str, dst: str, uid) -> subprocess.CompletedProcess:
    """取代 `podman unshare sh -c _INSTALL`：rc 3＝目標已存在（未覆寫）。路徑須落在 agent 的 stage／secret 根目錄內。"""
    return _call("unshare.install_file", {"src": src, "dst": dst, "uid": str(uid).split(":")[0]},
                 ["unshare.install_file", src, dst])


def unshare_write_secret(path: str, uid, nbytes: int) -> subprocess.CompletedProcess:
    """取代 `podman unshare python3 -c _INNER`：stdout 為 CREATED／EXISTS。"""
    return _call("unshare.write_secret", {"path": path, "uid": uid, "nbytes": nbytes}, ["unshare.write_secret", path])


def port_open(host: str, port: int) -> bool:
    """宿主視角的 TCP 探測（net.port_open）；rc 0＝有 listener。agent 拒絕（如非本機位址）→ AgentError。"""
    return _call("net.port_open", {"host": host, "port": port}, ["net.port_open", host, str(port)]).returncode == 0


def addr_show(iface: str) -> str:
    """取代 `ip -4 -o addr show <iface>`，回傳其 stdout。"""
    return _call("net.addr_show", {"iface": iface}, ["net.addr_show", iface]).stdout


def portfwd_open(listen: int, target_port: int, bind: str, ttl_sec: int, instance: str, allow=None) -> subprocess.CompletedProcess:
    """取代 SystemdManager.run_transient 起 socat 轉發單元；argv 由 agent 依型別化參數組成。"""
    params = {"listen": listen, "target_port": target_port, "bind": bind, "ttl_sec": ttl_sec, "instance": instance}
    if allow:
        params["allow"] = allow
    return _call("portfwd.open", params, ["portfwd.open", str(listen)])


def unshare_stat(path: str) -> subprocess.CompletedProcess:
    """取代 `podman unshare stat -c "%u %a" PATH`：stdout＝"uid octal-mode"（userns 視角）。"""
    return _call("unshare.stat", {"path": path}, ["unshare.stat", path])


def unshare_mkdir(path: str) -> subprocess.CompletedProcess:
    """取代 `podman unshare mkdir -p PATH`（路徑須落在 agent 的 mutate_roots）。"""
    return _call("unshare.mkdir", {"path": path}, ["unshare.mkdir", path])


def unshare_chown(path: str, user) -> subprocess.CompletedProcess:
    """取代 `podman unshare chown USER PATH`（非遞迴；user 為數字 uid 或 uid:gid）。"""
    return _call("unshare.chown", {"path": path, "user": str(user)}, ["unshare.chown", path])


def unshare_publish_ca(src: str, dst: str) -> subprocess.CompletedProcess:
    """取代 public_files 的 `podman unshare cat／sh -c`：讀取、PEM 驗證、原子寫入全在 agent 端；stdout＝PUBLISHED／UNCHANGED，rc 4＝內容被拒。"""
    return _call("unshare.publish_ca", {"src": src, "dst": dst}, ["unshare.publish_ca", src, dst])
