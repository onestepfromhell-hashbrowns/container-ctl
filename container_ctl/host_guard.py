"""container-ctl 只能由宿主 agent 或「已驗證的控制面容器」執行；其餘容器內（例如 dev 容器內 agent）一律拒絕，沒有旁路旗標。

理由：部署、機密產生、資料庫發放、宿主目錄寫入屬宿主權限；dev 容器內 agent 只能改程式碼與提交，
不得自行部署或碰宿主機密（宣告 → 宿主 agent 執行工具）。

控制面容器化（ADR-012）：控制面容器天生在容器內。放行依據**不是**環境變數或旗標（任何容器都能自報），
而是 host-agent 的認證結果：連 `CONTAINER_CTL_HOST_AGENT` 指向的 socket 呼叫 `auth.whoami`，agent 以 SO_PEERPIDFD →
conmon 所屬 systemd 單元 → label → 映像釘選認證呼叫端，回 `control-plane` 才放行。socket 不存在、不是 socket、
連不上、逾時、回覆異常、或回其他主體（host／unauthorized）一律拒絕。dev 容器沒有該 socket、也過不了認證。
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
from pathlib import Path
from typing import Callable

_MARKERS = ("/run/.containerenv", "/.dockerenv")
_AGENT_ENV = "CONTAINER_CTL_HOST_AGENT"
_MAX_REPLY = 4096


_DEFAULT_AGENT_SOCKET = "/run/container-ctl/host-agent.sock"


def inside_container(markers=_MARKERS, environ=os.environ) -> bool:
    return any(Path(m).exists() for m in markers) or bool(environ.get("container"))


def ask_agent_principal(path: str, timeout: float = 3.0, *, allow_non_root: bool = False) -> str | None:
    """向 host-agent 詢問自己被認證成誰；任何異常回 None（呼叫端視為不是控制面）。"""
    try:
        if not os.path.isabs(path) or not stat.S_ISSOCK(os.stat(path).st_mode):
            return None
        st = os.stat(path)
        # 門禁加固：在非測試/未特別指定環境下，socket 檔案必須由 root (uid 0) 擁有
        if not allow_non_root and os.geteuid() == 0 and st.st_uid != 0:
            return None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            # 檢查 peer 憑證（若在 root 權限下且有 SO_PEERCRED，對端必須是 root）
            if not allow_non_root and os.geteuid() == 0 and hasattr(socket, "SO_PEERCRED"):
                try:
                    cred = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _, peer_uid, _ = struct.unpack("3i", cred)
                    if peer_uid != 0:
                        return None
                except OSError:
                    pass
            s.sendall(b'{"verb":"auth.whoami","params":{}}\n')
            data = b""
            while len(data) <= _MAX_REPLY:
                chunk = s.recv(1024)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        if len(data) > _MAX_REPLY:
            return None
        reply = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(reply, dict) or reply.get("ok") is not True or reply.get("error"):
            return None
        who = reply.get("stdout")
        return who.strip() if isinstance(who, str) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def refuse_in_container(*, ask: Callable[[str], str | None] = ask_agent_principal, **kw) -> None:
    if not inside_container(**kw):
        return
    environ = kw.get("environ", os.environ)
    sock = environ.get(_AGENT_ENV)
    # 安全加固：阻擋路徑遍歷與未經授權的相對路徑；若為非絕對路徑或包含 .. 則拒絕
    if sock and (not os.path.isabs(sock) or ".." in sock.split("/")):
        sock = None
    if sock and ask(sock) == "control-plane":
        return
    raise SystemExit("[REFUSE] container-ctl 只能由宿主 agent 或已通過 host-agent 認證的控制面容器執行；偵測到目前在容器內且未通過認證。"
                     "容器內 agent 請提交宣告／程式碼，並請宿主 agent 執行部署工具。")
