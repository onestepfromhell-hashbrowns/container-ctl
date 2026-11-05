"""container-ctl db provision — 依消費者 `.deploy/pg-hub.json` 一鍵發放 database／角色／密碼檔（委派 PG-Hub 的 bin/create-service-db）。

流程（唯讀預設）：
  1 讀消費者程式碼 repo HEAD 的 `.deploy/pg-hub.json`，以 PG-Hub 自己的 service_declaration 驗證
  2 從 PG-Hub 程式碼 repo `git archive HEAD` 取出 bin/create-service-db（不用可變工作樹）
  3 由消費者容器宣告的 `<PREFIX>_PG_PASSWORD_FILE` 推出每個 runtime 角色的容器內路徑，再由 volumes 對映到宿主路徑
  4 mode=render|dry-run|apply；apply 前需全部目標密碼檔都不存在（已存在則略過；部分存在 → ERROR，不自動輪替）
  5 apply 後在 `podman unshare` 內把新檔 chown 給容器 user、chmod 0400（工具產生，內容不輸出）
管理員身分走 `podman exec -u … psql -U infra_admin`（本機 socket）；不需任何管理員密碼檔。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import shutil
import tempfile
from pathlib import Path

from container_ctl import agent_exec
from container_ctl.secret_provision import exists_any
from container_ctl import paths

PG_PROJECT = os.environ.get("CONTAINER_CTL_PG_PROJECT") or paths.setting("CONTAINER_CTL_PG_PROJECT", "pg-hub")
STAGE_ROOT = paths.DEPLOYS_DIR  # 暫存（0700 mkdtemp）；成功或失敗皆清除


class DbProvisionError(Exception):
    pass


def _git(repo: str, *args: str, text: bool = True):
    r = subprocess.run(["git", "--git-dir", repo, *args], capture_output=True, text=text)
    if r.returncode:
        raise DbProvisionError(f"git {' '.join(args[:2])} 失敗：{(r.stderr if text else r.stderr.decode())[:150]}")
    return r.stdout


def extract_tooling(pg_repo: str, dest: Path) -> tuple[Path, object]:
    """取出 PG-Hub 的 create-service-db 與 service_declaration（固定 HEAD 快照）。"""
    wanted = ["bin/create-service-db", "packages/neutral_postgres/service_declaration.py"]
    tar = subprocess.run(["git", "--git-dir", pg_repo, "archive", "HEAD", *wanted], capture_output=True)
    if tar.returncode:
        raise DbProvisionError(f"PG-Hub 工具取出失敗：{tar.stderr.decode()[:150]}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)
    spec = importlib.util.spec_from_file_location("service_declaration", dest / wanted[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dest / wanted[0], mod


def derive_requests(decl: dict, env: str, consumer_cfgs: list[dict], sd) -> tuple[list[dict], list[str]]:
    """回傳 (requests, errors)。request: {db, runtimes[(role, host_path, container_path, limit)], schemas[(schema, role)], migrator}。"""
    from container_ctl.cli.prod_doctor import _host_for_container_path
    errors = sd.validate(decl)
    if errors:
        return [], [f"[ERROR] pg-hub.json 無效：{e}" for e in errors]
    conns = [c for c in decl["connections"] if env in c["env"]]
    if not conns:
        return [], [f"[ERROR] pg-hub.json 沒有 {env} 的連線宣告"]
    envmap: dict[str, str] = {}
    owner_cfg: dict = {}
    for cfg in consumer_cfgs:
        for k, v in (cfg.get("env") or {}).items():
            if k.endswith("_PG_PASSWORD_FILE") and isinstance(v, str):
                envmap[k], owner_cfg[k] = v, cfg
    by_db: dict[str, dict] = {}
    errs: list[str] = []
    for c in conns:
        e = c["env"][env]
        key = f"{c['prefix']}_PG_PASSWORD_FILE"
        cpath = envmap.get(key)
        if not cpath:
            errs.append(f"[ERROR] 消費者容器宣告缺少 env {key}（容器內密碼檔路徑）；plan-integration 會印出完整 env 區塊")
            continue
        host = _host_for_container_path(owner_cfg[key], cpath)
        if host is None:
            errs.append(f"[ERROR] {key}={cpath} 不在消費者 volumes 掛載內")
            continue
        g = by_db.setdefault(e["database"], {"db": e["database"], "runtimes": {}, "schemas": [], "migrator": None,
                                             "uid": owner_cfg[key].get("user")})
        g["runtimes"].setdefault(e["role"], (e["role"], str(host), cpath, e["role_connlimit"]))
        for s in c["schemas"]:
            g["schemas"].append((s, e["role"]))
        if decl.get("migrator") and g["migrator"] is None:
            g["migrator"] = str(Path(host).parent / f"{env}_{decl['service']}_migrator.pw")
    return ([] if errs else [dict(g, runtimes=list(g["runtimes"].values())) for g in by_db.values()]), errs


def build_cmd(tool: Path, mode: str, env: str, service: str, container: str, req: dict) -> list[str]:
    cmd = ["python3", str(tool), mode, "--env", env, "--service", service, "--db", req["db"], "--container", container]
    for role, host, _c, limit in req["runtimes"]:
        cmd += ["--runtime", f"{role}:{host}:{limit}"]
    for schema, role in req["schemas"]:
        cmd += ["--schema", f"{schema}:{role}"]
    if req["migrator"]:
        cmd += ["--migrator-secret", req["migrator"]]
    return cmd


def _unshare_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}


def stage_request(req: dict, stage_dir: Path) -> tuple[dict, list[tuple[str, str]]]:
    """create-service-db 以宿主 uid 執行，無法寫入屬映射 uid 的密碼目錄 → 先寫到暫存目錄，成功後在 userns 內安裝。"""
    pairs: list[tuple[str, str]] = []

    def stage(real: str) -> str:
        staged = str(stage_dir / f"{len(pairs)}-{Path(real).name}")
        pairs.append((staged, real))
        return staged
    out = dict(req, runtimes=[(r, stage(h), c, lim) for r, h, c, lim in req["runtimes"]],
               migrator=stage(req["migrator"]) if req["migrator"] else None)
    return out, pairs


_INSTALL = 'set -e; [ -e "$2" ] && exit 3; mv "$1" "$2"; chown "$3" "$2"; chmod 0400 "$2"'


def install_staged(pairs: list[tuple[str, str]], user, run=subprocess.run) -> list[str]:
    """在 podman unshare 內把暫存檔移到目標、chown 給容器 user、0400；目標已存在則不覆寫。"""
    uid = str(user or "").split(":")[0]
    if not uid.isdigit():
        return ["[ERROR] 消費者宣告缺少數字 user，無法決定密碼檔擁有者"]
    out = []
    for staged, real in pairs:
        if not Path(staged).exists():
            continue  # 該角色未產生密碼（例如 migrator 未啟用）
        try:
            if agent_exec.agent_socket() and run is subprocess.run:  # 控制面容器：經 host-agent 型別化動詞
                r = agent_exec.unshare_install_file(staged, real, uid)
            else:
                r = run(["podman", "--remote=false", "unshare", "sh", "-c", _INSTALL, "sh", staged, real, uid],
                        capture_output=True, text=True, env=_unshare_env())
        except agent_exec.AgentError as exc:
            out.append(f"[ERROR] 安裝 {real} 失敗（{exc}）")
            continue
        out.append(f"[OK]   {real} 已安裝（擁有者 {uid}、0400，內容未輸出）" if r.returncode == 0
                   else f"[ERROR] 安裝 {real} 失敗（exit {r.returncode}；3＝目標已存在，未覆寫）")
    return out


def _declaration_repos(project: str) -> list[str]:
    """設定 repo 優先（宣告真源），其次消費者程式碼 repo（相容舊位置）。"""
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.repo_reader import repo_path_for
    repos = []
    for fn in (lambda: resolve_deploy_repo(project)[0], lambda: repo_path_for(project)):
        try:
            r = fn()
        except Exception:  # noqa: BLE001 — 找不到 repo 視為沒有宣告
            continue
        if r and r not in repos:
            repos.append(r)
    return repos


def read_declaration_text(project: str) -> str | None:
    for repo in _declaration_repos(project):
        r = subprocess.run(["git", "--git-dir", repo, "show", "HEAD:.deploy/pg-hub.json"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
    return None


def has_declaration(project: str) -> bool:
    """有 .deploy/pg-hub.json（設定 repo 或程式碼 repo）才需要 db provision；否則 DB 由其他方式供應，略過。"""
    return read_declaration_text(project) is not None


def db_provision(project: str, env: str, mode: str = "dry-run", *, run=subprocess.run, container_lookup=None) -> int:
    from container_ctl.cli.prod_up import _container_name
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.repo_reader import list_container_configs, repo_path_for
    lookup = container_lookup or _container_name
    try:
        decl_text = read_declaration_text(project)
        if decl_text is None:
            raise DbProvisionError(f"{project} 沒有 .deploy/pg-hub.json（設定 repo 與程式碼 repo 皆無）")
        import json
        decl = json.loads(decl_text)
        if not any(env in c.get("env", {}) for c in decl.get("connections", [])):
            print(f"[INFO] pg-hub.json 沒有宣告 {env} 連線；{env} 的資料庫由其他方式供應，略過 db provision")
            return 0
        pg_repo = repo_path_for(PG_PROJECT)
        container = lookup(PG_PROJECT, f"{env}-postgres")
        if not container:
            raise DbProvisionError(f"state.db 找不到 {PG_PROJECT}/{env}-postgres 實例（資料庫容器未部署）")
        deploy_repo, _ = resolve_deploy_repo(project)
        tier = "prod" if env == "prod" else "dev"
        cfgs = list_container_configs(deploy_repo, "HEAD", tier=tier)
        with tempfile.TemporaryDirectory(prefix="container-ctl-dbprov-") as td:
            tool, sd = extract_tooling(pg_repo, Path(td))
            reqs, errs = derive_requests(decl, env, cfgs, sd)
            for e in errs:
                print(e)
            if errs:
                return 1
            rc = 0
            for req in reqs:
                files = [r[1] for r in req["runtimes"]] + ([req["migrator"]] if req["migrator"] else [])
                present = [f for f in files if exists_any(f, run=run)]
                if mode == "apply" and present:
                    if len(present) == len(files):
                        print(f"[OK]   {req['db']}：密碼檔皆已存在，略過（輪替請走 PG-Hub bin/rotate-credentials）")
                        continue
                    print(f"[ERROR] {req['db']}：部分密碼檔已存在（{len(present)}/{len(files)}），不自動覆寫；請處理後重跑")
                    rc = 1
                    continue
                if mode == "apply":
                    missing = sorted({str(Path(f).parent) for f in files if not exists_any(str(Path(f).parent), run=run)})
                    if missing:  # 先擋：否則 PG 已改密碼、暫存檔裝不進去就遺失
                        print(f"[ERROR] {req['db']}：密碼檔父目錄不存在，未動 PG：{', '.join(missing)}（請先建立，例如 mkdir -p）")
                        rc = 1
                        continue
                stage_dir = Path(tempfile.mkdtemp(prefix=".dbprov-", dir=STAGE_ROOT))
                try:
                    staged_req, pairs = stage_request(req, stage_dir)
                    cmd = build_cmd(tool, mode, env, decl["service"], container, staged_req)
                    print(f"[RUN]  create-service-db {mode} db={req['db']} runtimes={[r[0] for r in req['runtimes']]}")
                    r = run(cmd, capture_output=True, text=True)
                    tail = "\n".join((r.stdout or "").strip().splitlines()[-15:])
                    if r.returncode:
                        print(f"[ERROR] {req['db']}：create-service-db 失敗\n{(r.stderr or '').strip()[-400:]}")
                        rc = 1
                        continue
                    if tail:
                        print(tail)
                    if mode == "apply":
                        for line in install_staged(pairs, req["uid"], run=run):
                            print(line)
                            if line.startswith("[ERROR]"):
                                rc = 1
                finally:
                    shutil.rmtree(stage_dir, ignore_errors=True)
        if mode != "apply":
            print(f"[INFO] {mode} 結束。加 --apply 實際發放。")
        return rc
    except (DbProvisionError, ValueError, OSError) as exc:
        print(f"[ERROR] db provision：{exc}")
        return 1
