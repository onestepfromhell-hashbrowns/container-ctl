"""container-ctl secret provision — 依宣告由工具產生 password／token 檔（agent 不讀、不印、不手寫內容）。

宣告（prod 容器 json）：
  "secrets": [{"env": "DB_PASSWORD_FILE", "kind": "password", "bytes": 32}]
  env 必須是 `*_FILE` 且值為容器內絕對路徑；host 路徑由 volumes 對映推得。
  kind=password|token → 工具產生；其他 kind（如 cert、key）一律不產生，只報告缺檔。
規則：
  只建立不存在的檔案，永不覆寫；內容不印、不進 argv、不進報告。
  在 `podman unshare` 內以 O_EXCL 寫入 0600 暫存檔、no-clobber 連結到目標、chown 給容器 user、改 0400。
  上層目錄必須已存在（由 `host prepare` 建立）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from container_ctl import agent_exec

GENERATABLE = {"password", "token"}
MAX_BYTES = 128

# 在 podman unshare 內執行；參數：目標路徑、容器 uid、bytes。內容只存在於此行程記憶體與目標檔。
_INNER = r'''
import os, secrets, sys
path, uid, nbytes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
tmp = path + ".tmp-" + secrets.token_hex(4)
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_urlsafe(nbytes) + "\n")
    try:
        os.link(tmp, path)
    except FileExistsError:
        print("EXISTS")
        sys.exit(0)
    os.chown(path, uid, -1)
    os.chmod(path, 0o400)
    print("CREATED")
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
'''


def exists_any(path, run=subprocess.run) -> bool:
    """路徑是否存在。上層目錄屬映射 uid、宿主不可搜尋時，Path.exists 會誤回 False；改在 userns 內確認。"""
    p = Path(path)
    if p.exists():
        return True
    if os.access(p.parent, os.X_OK):
        return False
    env = {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}
    return run(["podman", "--remote=false", "unshare", "test", "-e", str(p)], env=env).returncode == 0


def _host_path(cfg: dict, cpath: str) -> Path | None:
    from container_ctl.cli.prod_doctor import _host_for_container_path
    return _host_for_container_path(cfg, cpath)


def plan_secret_provision(configs: list[dict]) -> tuple[list[dict], list[str]]:
    """唯讀。回傳 (actions, messages)。action: {service, env, kind, path, uid, nbytes, state}，state∈missing|exists|manual。"""
    actions: list[dict] = []
    msgs: list[str] = []
    for cfg in configs:
        name = cfg.get("name", "unknown")
        env = cfg.get("env") or {}
        for entry in cfg.get("secrets") or []:
            key = str(entry.get("env", ""))
            kind = str(entry.get("kind", ""))
            value = env.get(key)
            if not key.endswith("_FILE") or not isinstance(value, str) or not value.startswith("/"):
                msgs.append(f"[ERROR] {name}: secrets.env={key!r} 必須是已宣告的 *_FILE env 且值為絕對路徑")
                continue
            host = _host_path(cfg, value)
            if host is None:
                msgs.append(f"[ERROR] {name}: {key}={value} 不在任何 volumes 掛載內")
                continue
            nbytes = int(entry.get("bytes", 32))
            if not 16 <= nbytes <= MAX_BYTES:
                msgs.append(f"[ERROR] {name}: {key} bytes 須介於 16 與 {MAX_BYTES}")
                continue
            try:
                uid = int(str(cfg.get("user", "")).split(":")[0])
            except ValueError:
                msgs.append(f"[ERROR] {name}: 宣告缺少數字 user，無法決定檔案擁有者")
                continue
            if exists_any(host):
                state = "exists"
            elif kind in GENERATABLE:
                state = "missing"
            else:
                state = "manual"
                msgs.append(f"[ERROR] {name}: {key} kind={kind or '未宣告'} 不由工具產生，宿主檔缺少 {host}")
            actions.append({"service": name, "env": key, "kind": kind, "path": str(host), "uid": uid,
                            "nbytes": nbytes, "state": state})
    return actions, msgs


def apply_secret_provision(actions: list[dict], run=subprocess.run) -> list[str]:
    out: list[str] = []
    env = {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}
    for a in actions:
        if a["state"] != "missing":
            continue
        use_agent = bool(agent_exec.agent_socket()) and run is subprocess.run  # 控制面容器：經 host-agent 型別化動詞
        parent = Path(a["path"]).parent
        if not use_agent and not parent.is_dir():
            # 目錄屬映射 uid 時宿主 is_dir 可能 EACCES→False；以 unshare 再確認
            probe = run(["podman", "--remote=false", "unshare", "test", "-d", str(parent)], env=env)
            if probe.returncode:
                out.append(f"[ERROR] {a['service']}: 上層目錄不存在 {parent} → container-ctl host prepare 後重跑")
                continue
        try:
            if use_agent:  # 上層目錄不存在時由 agent 端寫入失敗回報（容器內無 unshare 可探測）
                r = agent_exec.unshare_write_secret(a["path"], a["uid"], a["nbytes"])
            else:
                r = run(["podman", "--remote=false", "unshare", "python3", "-c", _INNER, a["path"], str(a["uid"]), str(a["nbytes"])],
                        capture_output=True, text=True, env=env)
        except agent_exec.AgentError as exc:
            out.append(f"[ERROR] {a['service']}: 產生 {a['path']} 失敗（{exc}）")
            continue
        status = (r.stdout or "").strip()
        if r.returncode == 0 and status in ("CREATED", "EXISTS"):
            out.append(f"[OK]   {a['service']}: {a['env']} → {a['path']}（{'已產生，內容未輸出' if status == 'CREATED' else '已存在，未改動'}）")
        else:
            out.append(f"[ERROR] {a['service']}: 產生 {a['path']} 失敗（{(r.stderr or '').strip()[:120]}）")
    return out


def secret_provision(project: str, ref: str = "HEAD", services: list[str] | None = None, tier: str = "prod",
                     apply: bool = False) -> int:
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.repo_reader import list_container_configs
    repo, _ = resolve_deploy_repo(project)
    configs = [c for c in list_container_configs(repo, ref, tier=tier) if not services or c.get("name") in services]
    actions, msgs = plan_secret_provision(configs)
    for m in msgs:
        print(m)
    todo = [a for a in actions if a["state"] == "missing"]
    for a in actions:
        if a["state"] == "exists":
            print(f"[OK]   {a['service']}: {a['env']} 已存在 {a['path']}")
    if todo and not apply:
        for a in todo:
            print(f"[PLAN] {a['service']}: 產生 {a['kind']} → {a['path']}（0400，uid {a['uid']}，內容不輸出）")
        print("[INFO] 預覽結束。加 --apply 產生。")
    elif todo:
        lines = apply_secret_provision(actions)
        for line in lines:
            print(line)
        msgs += [l for l in lines if l.startswith("[ERROR]")]
    return 1 if any(m.startswith("[ERROR]") for m in msgs) else 0
