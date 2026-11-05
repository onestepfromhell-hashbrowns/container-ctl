"""container-ctl host prepare — 依宣告備好宿主目錄（SPEC 方案 P0）。

預設只預覽；--apply 才動手。只做「新建」：
  1. 建立宣告裡缺少的宿主 volume 目錄（限白名單根目錄，禁止站點程式碼根與程式碼 Bare Repo）。
  2. 在父層 volume 內預建巢狀掛載點（容器內路徑落在另一個 volume 之下時）。
  3. 對「本次新建」的目錄做非遞迴 `podman unshare chown <user>`（rootless 映射 uid）。
  4. 宣告 `public_files` 時，把公開 CA 憑證複製到專用目錄（內容驗證，見 public_files.py）。
既有目錄、既有檔案一律不改（擁有者不符只回報修正指令）；不刪除、不寫 secret 內容。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import list_container_configs
from container_ctl.volume_resolver import ALLOWED_MOUNT_READ_ROOTS, FORBIDDEN_WRITE_PATHS


def _contained(path: Path, roots) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def _chown_cmd(user: str, path: Path) -> list[str]:
    return ["podman", "--remote=false", "unshare", "chown", str(user), str(path)]


def plan_host_prepare(configs: list[dict], allowed_roots=ALLOWED_MOUNT_READ_ROOTS,
                      forbidden=FORBIDDEN_WRITE_PATHS) -> tuple[list[dict], list[str]]:
    """回傳 (actions, errors)。action: {op: mkdir|chown, path, user?, service}。純計算，不改檔案系統。"""
    actions: list[dict] = []
    errors: list[str] = []
    planned: set[Path] = set()
    for cfg in configs:
        name = cfg.get("name", "unknown")
        user = cfg.get("user")
        vols = [v for v in cfg.get("volumes", []) if isinstance(v, dict) and v.get("host_path")]
        for vol in vols:
            host = Path(vol["host_path"])
            if host.exists():
                single_ro_file = host.is_file() and "ro" in str(vol.get("options", "rw")).split(",")
                if not host.is_dir() and not single_ro_file:
                    errors.append(f"[ERROR] {name}: {host} 已存在但不是目錄")
                continue
            if not _contained(Path(os.path.abspath(host)), allowed_roots):
                errors.append(f"[ERROR] {name}: {host} 不在允許的根目錄內，拒絕建立")
                continue
            if _contained(Path(os.path.abspath(host)), forbidden):
                errors.append(f"[ERROR] {name}: {host} 位於受保護路徑，拒絕建立")
                continue
            # 缺少的父層逐層建立（由上而下）
            chain = [p for p in reversed([host, *host.parents]) if not p.exists()]
            for p in chain:
                if p in planned:
                    continue
                planned.add(p)
                actions.append({"op": "mkdir", "path": str(p), "service": name})
                if user:
                    actions.append({"op": "chown", "path": str(p), "user": str(user), "service": name})
        for entry in cfg.get("public_files") or []:
            from container_ctl.public_files import check_declaration
            perrs = check_declaration(name, entry, allowed_roots, forbidden)
            errors.extend(perrs)
            if perrs:
                continue
            dest = Path(entry["dest"])
            for p in [q for q in reversed(list(dest.parents)) if not q.exists() and q not in planned]:
                planned.add(p)
                actions.append({"op": "mkdir", "path": str(p), "service": name})
            actions.append({"op": "publish", "src": entry["src"], "path": str(dest), "service": name})
        # 巢狀掛載點：容器路徑落在另一個 volume 之下 → 在父層宿主目錄內預建
        for child in vols:
            cpath = child["container_path"].rstrip("/")
            for parent in vols:
                base = parent["container_path"].rstrip("/")
                if base != cpath and cpath.startswith(base + "/"):
                    abs_parent = Path(os.path.abspath(parent["host_path"]))
                    if not _contained(abs_parent, allowed_roots) or _contained(abs_parent, forbidden):
                        continue
                    mount_point = Path(parent["host_path"]) / cpath[len(base) + 1:]
                    if mount_point.exists() or mount_point in planned:
                        continue
                    pp = Path(parent["host_path"])
                    if pp.exists() and not os.access(pp, os.W_OK):
                        errors.append(
                            f"[ERROR] {name}: 無法在 {pp} 預建掛載點 {mount_point.name}（父層已屬映射 uid）"
                            f" → podman unshare mkdir -p {mount_point}"
                        )
                        continue
                    chain = [p for p in reversed([mount_point, *mount_point.parents]) if not p.exists() and p != pp and pp in p.parents]
                    for p in chain:
                        if p in planned:
                            continue
                        planned.add(p)
                        actions.append({"op": "mkdir", "path": str(p), "service": name})
                        if user:
                            actions.append({"op": "chown", "path": str(p), "user": str(user), "service": name})
        # 既有目錄擁有者不符：只回報
    return actions, errors


def _agent_op(fn, *args):
    """agent 模式：回傳 CompletedProcess；AgentError 轉成 rc=1 的結果，讓呼叫端沿用既有 [ERROR] 路徑。"""
    from container_ctl import agent_exec
    try:
        return fn(*args)
    except agent_exec.AgentError as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def apply_actions(actions: list[dict], run=subprocess.run) -> list[str]:
    from container_ctl import agent_exec
    agent = bool(agent_exec.agent_socket()) and run is subprocess.run
    log = []
    for a in actions:
        path = Path(a["path"])
        if a["op"] == "publish":
            from container_ctl.public_files import publish
            log.append(publish(a["src"], a["path"], run=run))
        elif a["op"] == "mkdir" and agent:
            r = _agent_op(agent_exec.unshare_mkdir, str(path))
            if r.returncode != 0:
                log.append(f"[ERROR] mkdir {path} 失敗：{(r.stderr or '').strip()[:150]}")
                continue
            log.append(f"[DONE] mkdir {path}")
        elif a["op"] == "chown" and agent:
            r = _agent_op(agent_exec.unshare_chown, str(path), a["user"])
            if r.returncode != 0:
                log.append(f"[ERROR] chown {a['user']} {path} 失敗：{(r.stderr or '').strip()[:150]}")
            else:
                log.append(f"[DONE] chown {a['user']} {path}")
        elif a["op"] == "mkdir":
            try:
                path.mkdir(exist_ok=True)
            except PermissionError:
                # 父層已 chown 給映射 uid：宿主 uid 不可寫，改在 userns 內建立
                env = {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}
                r = run(["podman", "--remote=false", "unshare", "mkdir", "-p", str(path)], capture_output=True, text=True, env=env)
                if r.returncode != 0:
                    log.append(f"[ERROR] mkdir {path} 失敗：{(r.stderr or '').strip()[:150]}")
                    continue
            log.append(f"[DONE] mkdir {path}")
        elif a["op"] == "chown":
            env = {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}
            r = run(_chown_cmd(a["user"], path), capture_output=True, text=True, env=env)
            if r.returncode != 0:
                log.append(f"[ERROR] chown {a['user']} {path} 失敗：{(r.stderr or '').strip()[:150]}")
            else:
                log.append(f"[DONE] chown {a['user']} {path}")
    return log


def host_prepare(project: str, ref: str = "HEAD", services: list[str] | None = None, tier: str = "prod",
                 apply: bool = False, output: str = "table", repos_dir: str | None = None) -> None:
    repo, _kind = resolve_deploy_repo(project, repos_dir)
    configs = [c for c in list_container_configs(repo, ref) if c.get("tier") == tier]
    if services:
        configs = [c for c in configs if c.get("name") in services]
    if not configs:
        print("[ERROR] 找不到符合的服務宣告", file=sys.stderr)
        sys.exit(1)
    actions, errors = plan_host_prepare(configs)
    lines = [(f"[PLAN] publish {a['src']} → {a['path']}" if a['op'] == 'publish' else f"[PLAN] {a['op']} {a.get('user', '')} {a['path']}".replace("  ", " ")) for a in actions]
    if not actions and not errors:
        lines.append("[OK]   宿主目錄皆已就緒，無需動作")
    result = lines + errors
    if apply and not errors:
        result += apply_actions(actions)
        result.append("[OK]   host prepare 完成；請重跑 `container-ctl prod doctor`")
    elif apply and errors:
        result.append("[ABORT] 有 [ERROR]，未執行任何動作")
    elif actions:
        result.append("[INFO] 預覽模式；加 --apply 才會執行")
    bad = bool(errors) or any(l.startswith("[ERROR]") for l in result)
    if output == "json":
        print(json.dumps({"project": project, "ok": not bad, "actions": actions, "messages": result}, ensure_ascii=False, indent=2))
    else:
        print("\n".join(result))
    sys.exit(1 if bad else 0)
