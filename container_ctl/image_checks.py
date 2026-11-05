"""prod 映像的 runtime 規範檢查（SPEC §23.1）：WORKDIR、/workspace、debugpy。輸入是 `podman image inspect` 的物件。"""
from __future__ import annotations

import json
import re
import subprocess

_URL = re.compile(r"[a-z][a-z0-9+.-]*://", re.I)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOSTPORT = re.compile(r"^[A-Za-z0-9.-]+:\d{2,5}$")
_SECRET_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "PRIVATE_KEY", "API_KEY")
# 建置慣例變數，不視為環境值
_BENIGN = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "HOSTNAME", "TERM", "SSL_CERT_FILE", "PYTHONPATH", "NODE_VERSION"}


def image_findings(inspected: dict, name: str, *, check_workdir: bool = True) -> list[str]:
    cfg = inspected.get("Config") or {}
    out: list[str] = []
    wd = str(cfg.get("WorkingDir") or "")
    if wd.startswith("/workspace"):
        out.append(f"[ERROR] {name}: 映像 WORKDIR={wd}；prod 映像不得使用 /workspace（改 /opt/<PROJECT_NAME>）")
    elif check_workdir and not wd.startswith("/opt/"):
        out.append(f"[WARN] {name}: 映像 WORKDIR={wd or '(空)'} 不在 /opt/<PROJECT_NAME> 之下")
    env = cfg.get("Env") or []
    for item in env:
        key, _, value = str(item).partition("=")
        if "/workspace" in value:
            out.append(f"[ERROR] {name}: 映像 ENV {key} 含 /workspace")
    for item in env:
        key, _, value = str(item).partition("=")
        if key in _BENIGN or not value:
            continue
        upper = key.upper()
        if any(w in upper for w in _SECRET_WORDS) and not upper.endswith(("_FILE", "_PATH", "_DIR")):
            out.append(f"[WARN] {name}: 映像 ENV {key} 疑似機密；映像不得內嵌（ADR-009 P2），改執行期 *_FILE 注入")
        elif _URL.search(value) or _IPV4.search(value) or _HOSTPORT.match(value):
            out.append(f"[WARN] {name}: 映像 ENV {key} 含位址（URL／IP／host:port）；環境值不得烙進映像（ADR-009 P2），改執行期注入")
    blob = " ".join([*(cfg.get("Cmd") or []), *(cfg.get("Entrypoint") or []), *map(str, env)]).lower()
    if "debugpy" in blob:
        out.append(f"[ERROR] {name}: 映像啟動設定含 debugpy；prod 禁止除錯工具")
    return out


def inspect_image(ref: str) -> dict | None:
    res = subprocess.run(["podman", "image", "inspect", ref, "--format", "{{json .}}"],
                         capture_output=True, text=True, check=False, timeout=30)
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
