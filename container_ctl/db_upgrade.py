"""db snapshot／restore-drill／migrate：prod 升級前備份、還原演練、依賴 ledger 的 migration 套用。

- 管理員身分走 `podman exec <pg容器> … -U infra_admin`（本機 socket，pg_hba trust；不碰任何密碼檔）。
- 備份寫到 $CONTAINER_CTL_DB_BACKUPS_DIR/<P>/<ts>/（0700/0600，非 volume 內）；globals 含角色雜湊，等同機密。
- 只刪過期的備份檔，不碰 volume。
- 演練容器以與 prod 相同的映像 digest、tmpfs 資料目錄、無網路啟動；用完即棄。
- migration：宣告 `.deploy/db-migrate.json`（config repo）；套用前強制快照；版本號跨目錄重複 → ERROR（部署前攔下）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from container_ctl import paths

BACKUP_ROOT = Path(paths.DB_BACKUPS_DIR)
KEEP_LAST, KEEP_DAYS = 5, 7
_COUNT_SQL = (
    "select table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format("
    "'select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text "
    "from information_schema.tables where table_type='BASE TABLE' and table_schema not in ('pg_catalog','information_schema') order by 1"
)


class UpgradeError(Exception):
    pass


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def pg_container(service: str) -> str:
    out = _run(["podman", "ps", "--format", "{{.Names}}", "--filter", f"name=^svc-{service}-"], text=True).stdout.split()
    if not out:
        raise UpgradeError(f"找不到執行中的 PG 容器 svc-{service}-*")
    return out[0]


def _psql(container: str, db: str, sql: str, admin: str = "infra_admin", stdin: str | None = None, single_tx: bool = False) -> str:
    cmd = ["podman", "exec", "-i", container, "psql", "-U", admin, "-d", db, "-v", "ON_ERROR_STOP=1", "-At"]
    cmd += ["-1", "-f", "-"] if single_tx else ["-c", sql]
    r = _run(cmd, input=(stdin if single_tx else None), text=True)
    if r.returncode:
        raise UpgradeError(f"psql 失敗（{db}）：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def table_counts(container: str, db: str, admin: str = "infra_admin") -> dict[str, int]:
    out = _psql(container, db, _COUNT_SQL, admin)
    return {k: int(v) for k, v in (line.split("|", 1) for line in out.splitlines() if "|" in line)}


def project_databases(project: str, tier: str = "prod", ref: str = "HEAD") -> tuple[list[str], str]:
    """從 prod 宣告的 *_PG_DATABASE／*_PG_HOST 推出 (資料庫清單, PG 服務別名)。"""
    from container_ctl.deploy_source import resolve_deploy_repo
    from container_ctl.repo_reader import list_container_configs
    repo, _ = resolve_deploy_repo(project)
    dbs, hosts = [], set()
    for cfg in list_container_configs(str(repo), ref, tier=tier):
        for k, v in (cfg.get("env") or {}).items():
            if k.endswith("_PG_DATABASE") and v not in dbs:
                dbs.append(v)
            if k.endswith("_PG_HOST"):
                hosts.add(v)
    if not dbs or not hosts:
        raise UpgradeError(f"{project} 的 {tier} 宣告沒有 *_PG_DATABASE／*_PG_HOST")
    return dbs, sorted(hosts)[0]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prune(project_dir: Path, now: datetime | None = None) -> list[str]:
    """保留最近 KEEP_LAST 份；其餘超過 KEEP_DAYS 天者刪除（只刪備份檔）。"""
    now = now or datetime.now(timezone.utc)
    snaps = sorted(d for d in project_dir.iterdir() if d.is_dir() and re.fullmatch(r"\d{8}T\d{6}Z", d.name))
    removed = []
    for d in snaps[:-KEEP_LAST]:
        ts = datetime.strptime(d.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if now - ts > timedelta(days=KEEP_DAYS):
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            removed.append(d.name)
    return removed


def snapshot(project: str, env: str = "prod", root: Path = BACKUP_ROOT, ref: str = "HEAD", reason: str = "") -> Path:
    dbs, service = project_databases(project, env, ref)
    c = pg_container(service)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pdir = root / project
    pdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pdir.chmod(0o700)
    out = pdir / stamp
    out.mkdir(mode=0o700)
    manifest = {"project": project, "env": env, "created": stamp, "reason": reason, "pg_container": c,
                "pg_image": _run(["podman", "inspect", c, "--format", "{{.Image}}"], text=True).stdout.strip(),
                "pg_version": _psql(c, "postgres", "show server_version"), "databases": {}}
    for db in dbs:
        f = out / f"{db}.dump"
        with f.open("wb") as fh:
            r = subprocess.run(["podman", "exec", c, "pg_dump", "-U", "infra_admin", "-Fc", "-d", db], stdout=fh, stderr=subprocess.PIPE)
        f.chmod(0o600)
        if r.returncode:
            raise UpgradeError(f"pg_dump 失敗（{db}）：{r.stderr.decode()[:200]}")
        with f.open("rb") as fh:
            toc = subprocess.run(["podman", "exec", "-i", c, "pg_restore", "-l"], stdin=fh, capture_output=True)
        if toc.returncode or not toc.stdout:
            raise UpgradeError(f"備份檔無法解析（{db}）：{toc.stderr.decode()[:200]}")
        manifest["databases"][db] = {"file": f.name, "sha256": _sha256(f), "bytes": f.stat().st_size,
                                     "toc_entries": len(toc.stdout.decode().splitlines()), "counts": table_counts(c, db)}
    g = out / "globals.sql"
    r = _run(["podman", "exec", c, "pg_dumpall", "-U", "infra_admin", "--globals-only"])
    if r.returncode:
        raise UpgradeError(f"pg_dumpall 失敗：{r.stderr.decode()[:200]}")
    g.write_bytes(r.stdout)
    g.chmod(0o600)
    manifest["globals"] = {"file": g.name, "sha256": _sha256(g), "note": "含角色密碼雜湊，視同機密"}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "MANIFEST.json").chmod(0o600)
    pruned = prune(pdir)
    print(f"[OK] 快照：{out}（{len(dbs)} 個資料庫；清除過期 {len(pruned)} 份）")
    return out


# ---- migration -------------------------------------------------------------------------------

def read_migrate_decl(project: str, ref: str = "HEAD") -> dict | None:
    from container_ctl.db_provision import _declaration_repos
    for repo in _declaration_repos(project):
        r = _run(["git", "--git-dir", repo, "show", f"{ref}:.deploy/db-migrate.json"], text=True)
        if r.returncode == 0:
            return json.loads(r.stdout)
    return None


def discover(code_repo: str, code_ref: str, path_regex: str) -> list[tuple[str, str, str]]:
    """(version, 檔名, sql)；版本號跨目錄重複 → UpgradeError。"""
    names = subprocess.run(["git", "--git-dir", code_repo, "ls-tree", "-r", "--name-only", code_ref],
                           capture_output=True, text=True, check=True).stdout.split("\n")
    rx = re.compile(path_regex)
    found: dict[str, str] = {}
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if rx.fullmatch(n) and re.match(r"\d{4}_", base) and not base.endswith("_rollback.sql"):
            v = base.split("_", 1)[0]
            if v in found:
                raise UpgradeError(f"migration 版本 {v} 重複定義：{found[v]} 與 {n}（請改版號）")
            found[v] = n
    out = []
    for v in sorted(found):
        sql = subprocess.run(["git", "--git-dir", code_repo, "show", f"{code_ref}:{found[v]}"], capture_output=True, text=True, check=True).stdout
        out.append((v, found[v], sql))
    return out


def pending(container: str, db: str, decl: dict, files: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    applied = set(_psql(container, db, f"select version from {decl['ledger']}").split())
    return [f for f in files if f[0] not in applied]


def apply_pending(container: str, db: str, decl: dict, todo: list[tuple[str, str, str]]) -> list[str]:
    done = []
    for v, name, sql in todo:
        script = (f"SET LOCAL search_path TO {decl.get('search_path', 'public')};\n{sql}\n;\n"
                  f"INSERT INTO {decl['ledger']} (version) VALUES ('{v}');\n")
        _psql(container, db, "", stdin=script, single_tx=True)
        done.append(v)
        print(f"[OK] 已套用 {name}")
    return done


def _warn_unsynced_source(project: str) -> None:
    from container_ctl.cli.config import ineffective_source_messages
    for line in ineffective_source_messages(project):
        if "工作樹 HEAD" in line:
            print(line)


def migrate(project: str, env: str, apply: bool, code_ref: str = "HEAD", snapshot_first: bool = True) -> int:
    from container_ctl.repo_reader import repo_path_for
    _warn_unsynced_source(project)
    decl = read_migrate_decl(project)
    if not decl:
        print(f"[INFO] {project} 沒有 .deploy/db-migrate.json，略過 migration")
        return 0
    dbs, service = project_databases(project, env)
    db = decl.get("database") or dbs[0]
    c = pg_container(decl.get("service", service))
    files = discover(str(repo_path_for(project)), code_ref, decl["path_regex"])
    todo = pending(c, db, decl, files)
    if not todo:
        print(f"[OK] {db}：無待套用 migration（ledger 已含 {len(files)} 個版本）")
        return 0
    print(f"[PLAN] {db} 待套用：{', '.join(n for _v, n, _s in todo)}")
    if not apply:
        return 0
    if snapshot_first:
        snapshot(project, env, reason=f"pre-migrate {todo[0][0]}..{todo[-1][0]}")
    apply_pending(c, db, decl, todo)
    return 0


# ---- restore drill ---------------------------------------------------------------------------

def _drill_run_argv(name: str, image: str) -> list[str]:
    """演練容器的 podman run 命令。--pull=never：演練只用備份清單記錄的本機映像，不碰 registry
    （控制面容器內經 socket-proxy 的 5.x 用戶端會先送 images/pull，proxy 僅在 policy=never 時放行；ADR-012）。"""
    return ["podman", "run", "-d", "--pull=never", "--name", name, "--network", "none", "--user", "70:70",
            "--tmpfs", "/var/lib/postgresql/data:mode=700,U", "--tmpfs", "/var/run/postgresql:U", "--tmpfs", "/tmp",
            "--entrypoint", "sh", image, "-c",
            "initdb -D /var/lib/postgresql/data --auth=trust -U infra_admin >/dev/null && exec postgres -D /var/lib/postgresql/data "
            "-c listen_addresses= -c unix_socket_directories=/var/run/postgresql"]


def restore_drill(project: str, env: str = "prod", snap: Path | None = None, code_ref: str = "HEAD") -> int:
    """把最新（或指定）快照還原到用後即棄的 PG（同映像、無網路、tmpfs），套用待遷移並比對列數。"""
    _warn_unsynced_source(project)
    snap = snap or sorted(p for p in (BACKUP_ROOT / project).iterdir() if p.is_dir())[-1]
    man = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
    for db, info in man["databases"].items():
        if _sha256(snap / info["file"]) != info["sha256"]:
            raise UpgradeError(f"備份檔 SHA256 不符：{info['file']}")
    name = f"drill-{project.lower().replace('_', '-')}-{int(time.time())}"
    r = _run(_drill_run_argv(name, man["pg_image"]), text=True)
    if r.returncode:
        raise UpgradeError(f"演練容器啟動失敗：{r.stderr[:200]}")
    try:
        for _ in range(30):
            if _run(["podman", "exec", name, "pg_isready", "-h", "/var/run/postgresql"]).returncode == 0:
                break
            time.sleep(1)
        else:
            raise UpgradeError("演練 PG 未就緒")
        with (snap / "globals.sql").open("rb") as fh:  # 角色已存在的錯誤可忽略
            _run(["podman", "exec", "-i", name, "psql", "-U", "infra_admin", "-d", "postgres", "-q"], stdin=fh)
        for db, info in man["databases"].items():
            _psql(name, "postgres", f'create database "{db}"')
            with (snap / info["file"]).open("rb") as fh:
                rr = subprocess.run(["podman", "exec", "-i", name, "pg_restore", "-U", "infra_admin", "-d", db, "--no-owner", "--exit-on-error"],
                                    stdin=fh, capture_output=True)
            if rr.returncode:
                raise UpgradeError(f"pg_restore 失敗（{db}）：{rr.stderr.decode()[:300]}")
            got = table_counts(name, db)
            diff = {t: (n, got.get(t)) for t, n in info["counts"].items() if got.get(t) != n}
            if diff:
                raise UpgradeError(f"{db} 還原後列數不符：{diff}")
            print(f"[OK] 演練還原 {db}：{len(got)} 表、列數與快照一致")
            decl = read_migrate_decl(project)
            if decl and (decl.get("database") or db) == db:
                from container_ctl.repo_reader import repo_path_for
                todo = pending(name, db, decl, discover(str(repo_path_for(project)), code_ref, decl["path_regex"]))
                apply_pending(name, db, decl, todo)
                after = table_counts(name, db)
                lost = {t: (n, after.get(t)) for t, n in info["counts"].items() if after.get(t) != n and t != decl["ledger"]}
                if after.get(decl["ledger"]) != info["counts"].get(decl["ledger"], 0) + len(todo):
                    raise UpgradeError("ledger 筆數與套用數不符")
                if lost:
                    raise UpgradeError(f"演練套用 migration 後列數改變：{lost}")
                print(f"[OK] 演練套用 {len(todo)} 個 migration：既有表列數不變")
        return 0
    finally:
        _run(["podman", "rm", "-f", name])
