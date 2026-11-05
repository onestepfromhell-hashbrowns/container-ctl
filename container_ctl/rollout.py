"""Rollout：把 (release, 設定修訂, 環境) 套用到宿主的一次事件（ADR-009 P4 / ADR-011）。

紀錄在 state.db `rollouts`；回滾與稽核以「實際健康的上一個 rollout」為準，不再由收據或 state.db 版本欄推算。
"""

from __future__ import annotations

import getpass
import os
import sqlite3
from datetime import datetime, timezone

from container_ctl.db import DEFAULT_DB_PATH, init_db


def recording_enabled(db_path: str | None) -> bool:
    return bool(db_path) or os.environ.get("CONTAINER_CTL_ROLLOUT_RECORD", "on").lower() != "off"


def record(project: str, service: str, env: str, version: str, result: str, *, image_id: str | None = None,
           manifest_digest: str | None = None, config_commit: str | None = None, detail: str = "",
           db_path: str | None = None) -> int:
    conn = init_db(db_path or DEFAULT_DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO rollouts (project_name, service_name, env, release_version, image_id, manifest_digest, config_commit,"
            " result, detail, actor, occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project, service, env, version, image_id, manifest_digest, config_commit, result, detail,
             getpass.getuser(), datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def record_from_declaration(project: str, service: str, version: str, ref: str, result: str, detail: str,
                            db_path: str | None, env: str | None = None) -> int | None:
    """由設定 repo（ref）與 release tag 取得 digest／config commit 後寫入。"""
    if not recording_enabled(db_path):
        return None
    from container_ctl.cli.image import _source_commit
    from container_ctl.cli.image_prod import read_release_record
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.env_overlay import DEFAULT_ENV
    repo, _ = resolve_deploy_repo(project)
    rec = read_release_record(repo, service, version) or {}
    return record(project, service, env or DEFAULT_ENV or "", version, result, image_id=rec.get("image_id"),
                  manifest_digest=rec.get("manifest_digest"), config_commit=_source_commit(repo, ref),
                  detail=detail, db_path=db_path)


def history(project: str, service: str, *, limit: int = 20, db_path: str | None = None) -> list[dict]:
    try:
        with sqlite3.connect(f"file:{db_path or DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM rollouts WHERE project_name=? AND service_name=? ORDER BY id DESC LIMIT ?",
                                (project, service, limit)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def plan_undo(project: str, service: str, *, to: int | None = None, db_path: str | None = None) -> dict:
    """目標＝最近一個 result=healthy 且與「目前（最新 healthy）」不同的 rollout；--to 指定 id 則必須是 healthy。"""
    rows = history(project, service, limit=200, db_path=db_path)
    healthy = [r for r in rows if r["result"] == "healthy"]
    if not healthy:
        raise ValueError("沒有任何 healthy 的 rollout 紀錄，無法判定回滾目標")
    current = healthy[0]
    if to is not None:
        target = next((r for r in healthy if r["id"] == to), None)
        if target is None:
            raise ValueError(f"rollout #{to} 不存在或不是 healthy")
    else:
        key = lambda r: (r["release_version"], r["image_id"], r["config_commit"])  # noqa: E731
        target = next((r for r in healthy[1:] if key(r) != key(current)), None)
        if target is None:
            raise ValueError("沒有與目前不同的 healthy rollout 可回滾")
    return {"current": current, "target": target}


def undo_files(project: str, service: str, target_commit: str, mode: str) -> dict[str, bytes | None]:
    """以目標 rollout 的設定 repo 修訂還原。mode: both（整份）、image（只 image／version）、config（除 image／version 外）。
    回傳 {路徑: 新內容或 None(刪除)}；當前 HEAD 的檔案為基礎。"""
    import json
    import subprocess
    from container_ctl.deploy_source import resolve_deploy_repo
    repo, kind = resolve_deploy_repo(project)
    if kind != "config":
        raise ValueError("rollout undo 只支援設定 repo 模式")

    def show(ref: str, path: str) -> bytes | None:
        r = subprocess.run(["git", "--git-dir", repo, "show", f"{ref}:{path}"], capture_output=True)
        return r.stdout if r.returncode == 0 else None

    def listing(ref: str) -> set[str]:
        out = subprocess.run(["git", "--git-dir", repo, "ls-tree", "-r", "--name-only", ref, ".deploy/"],
                             capture_output=True, text=True).stdout.split()
        return {p for p in out if p.endswith(f"/{service}.json") and ("/containers/" in p)}

    paths = listing(target_commit) | listing("HEAD")
    files: dict[str, bytes | None] = {}
    for path in sorted(paths):
        old, cur = show(target_commit, path), show("HEAD", path)
        is_base = path == f".deploy/containers/{service}.json"
        if mode == "both" or not is_base:
            if mode == "image":
                continue  # overlay 不含 image／version
            new = old
        else:
            if cur is None or old is None:
                continue
            c, o = json.loads(cur), json.loads(old)
            if mode == "image":
                for k in ("image", "version"):
                    if k in o:
                        c[k] = o[k]
                    else:
                        c.pop(k, None)
                new = (json.dumps(c, ensure_ascii=False, indent=2) + "\n").encode()
            else:  # config：保留目前的 image／version
                merged = {k: v for k, v in o.items() if k not in ("image", "version")}
                for k in ("image", "version"):
                    if k in c:
                        merged[k] = c[k]
                new = (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode()
        if new != cur:
            files[path] = new
    return files


def reconcile_report(project: str, service: str, *, ref: str = "HEAD", env: str | None = None,
                     db_path: str | None = None) -> tuple[bool, list[str]]:
    """唯讀：desired（設定 repo＋release）vs actual（podman）。回傳 (無漂移, 訊息)。"""
    from container_ctl.cli.image import _source_commit
    from container_ctl.cli.image_prod import read_release_record
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.live_drift import find_live_drift, inspect_container
    from container_ctl.repo_reader import list_container_configs
    repo, kind = resolve_deploy_repo(project)
    cfg = next((c for c in list_container_configs(repo, ref, env=env) if c.get("name") == service), None)
    if cfg is None:
        return False, [f"[ERROR] 設定中沒有服務 {service}"]
    out = [f"[INFO] desired：{service} version={cfg.get('version')} config={_source_commit(repo, ref)[:12]}（{kind}）"]
    ok = True
    if cfg.get("tier") == "dev":  # dev 不帶版本、不發 release tag（SPEC §20）：不要求 release 記錄
        out.append("[INFO] dev 服務無 release 記錄要求，略過 release 檢查")
    else:
        rec = read_release_record(repo, service, str(cfg.get("version"))) or {}
        if not rec:
            out.append(f"[ERROR] 沒有 release/{service}/{cfg.get('version')} 記錄")
            ok = False
    instances = _instances(project, service, db_path)
    live = [(n, inspect_container(n)) for n in instances]
    running = [(n, c) for n, c in live if c and (c.get("State") or {}).get("Running")]
    if not running:
        out.append("[ERROR] 沒有運行中的實例")
        return False, out
    if len(running) > 1:
        out.append(f"[WARN] 同時有 {len(running)} 個運行實例：{', '.join(n for n, _ in running)}（舊命名並存或孤兒單元）")
    for name, c in running:
        drift = find_live_drift(cfg, c)
        if drift:
            ok = False
            out.extend(f"[DRIFT] {name}: {d}" for d in drift)
        else:
            out.append(f"[OK]   {name} 與宣告一致")
    last = next(iter(history(project, service, limit=1, db_path=db_path)), None)
    if last is None:
        out.append("[WARN] 沒有 rollout 紀錄（舊流程部署）；回滾目標未定")
    elif last["result"] != "healthy":
        out.append(f"[WARN] 最近一次 rollout #{last['id']} 結果為 {last['result']}：{last.get('detail') or ''}")
    elif last.get("config_commit") and last["config_commit"] != _source_commit(repo, ref):
        out.append(f"[WARN] 設定 repo 已前進（上次 rollout 的設定 {last['config_commit'][:12]}）；執行 rollout apply 套用")
    return ok, out


def _instances(project: str, service: str, db_path: str | None) -> list[str]:
    try:
        with sqlite3.connect(f"file:{db_path or DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
            return [r[0] for r in conn.execute(
                "SELECT container_name FROM instances WHERE project_name=? AND service_name=? AND status!='destroyed' "
                "AND container_name IS NOT NULL ORDER BY updated_at DESC", (project, service))]
    except sqlite3.Error:
        return []
