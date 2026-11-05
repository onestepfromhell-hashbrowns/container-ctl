"""設定 repo 管理：init（由程式碼 Bare Repo 建立）、status（唯讀比對）、set（env／volumes）、sync-image-context（dev 映像 context）。

見 DESIGN-deploy-config-repo-20261003。init 只建立新目錄，不改任何既有 repo、容器或 state.db。
"""

from __future__ import annotations

import getpass
import json
import re
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.cli.image import ImagePrepareError, _run, _source_commit, _source_config, _validate_name
from container_ctl.declaration_guard import check_declarations
from container_ctl.deploy_source import config_repo_path
from container_ctl.repo_reader import repo_path_for
from container_ctl import paths

WORKTREES_DIR = paths.WORKTREES_DIR


def _legacy_repo(project: str, repos_dir: str | None) -> str:
    repo = repo_path_for(project, repos_dir) if repos_dir else repo_path_for(project)
    if not Path(repo).is_dir():
        raise ImagePrepareError(f"程式碼 Bare Repo 不存在：{repo}")
    return repo


def _tree_hash(repo: str, ref: str) -> str | None:
    found = subprocess.run(["git", "--git-dir", repo, "rev-parse", "--verify", "--quiet", f"{ref}:.deploy"],
                           capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


def config_init(
    project: str,
    ref: str = "HEAD",
    *,
    repos_dir: str | None = None,
    config_dir: str | None = None,
) -> str:
    """由程式碼 Bare Repo 的 `<ref>:.deploy` 建立設定 repo；已存在則拒絕。回傳設定 repo 路徑。"""
    _validate_name(project)
    legacy = _legacy_repo(project, repos_dir)
    target = Path(config_repo_path(project, repos_dir, config_dir))
    if target.exists():
        raise ImagePrepareError(f"設定 repo 已存在：{target}")
    commit = _source_commit(legacy, ref)
    if _tree_hash(legacy, commit) is None:
        raise ImagePrepareError(f"{ref} 沒有 .deploy/，無法建立設定 repo")
    with tempfile.TemporaryDirectory(prefix="container-ctl-config-init-") as name:
        work = Path(name) / "work"
        work.mkdir()
        archive = subprocess.Popen(["git", "--git-dir", legacy, "archive", "--format=tar", commit, ".deploy"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(work, filter="data")
        if archive.wait() != 0:
            raise ImagePrepareError(f"git archive 失敗：{archive.stderr.read().decode(errors='replace').strip()}")
        git = ["git", "-C", str(work), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        _run(["git", "init", "-q", "-b", "main", str(work)])
        _run([*git, "add", "--", ".deploy"])
        _run([*git, "commit", "-q", "-m", f"Initial config for {project} from code repo {commit}"])
        target.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "-q", "--bare", str(work), str(target)])
    return str(target)


def _snapshot(repo: str, ref: str) -> dict[str, bytes]:
    listing = subprocess.run(["git", "--git-dir", repo, "ls-tree", "-r", "--name-only", ref, ".deploy"],
                             capture_output=True, text=True)
    files = {}
    for path in listing.stdout.split("\n"):
        if path:
            files[path] = subprocess.run(["git", "--git-dir", repo, "show", f"{ref}:{path}"], capture_output=True).stdout
    return files


def _dir_snapshot(root: Path) -> dict[str, bytes]:
    base = root / ".deploy"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()} if base.is_dir() else {}


def _diff(a: dict[str, bytes], b: dict[str, bytes]) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def config_status(
    project: str,
    *,
    repos_dir: str | None = None,
    config_dir: str | None = None,
    worktrees_dir: str = WORKTREES_DIR,
) -> tuple[bool, list[str]]:
    """唯讀：比對設定 repo、程式碼 Bare Repo 與工作樹的 `.deploy`。回傳 (無嚴重漂移, 訊息)。"""
    _validate_name(project)
    legacy = _legacy_repo(project, repos_dir)
    config = config_repo_path(project, repos_dir, config_dir)
    legacy_files = _snapshot(legacy, "HEAD")
    messages = [f"[INFO] 程式碼 repo：{legacy}（.deploy 檔案 {len(legacy_files)} 個）"]
    ok = True
    if not Path(config).is_dir():
        messages.append(f"[WARN] 尚未建立設定 repo（legacy 模式）；可用 `container-ctl config init {project}` 建立")
        authoritative = legacy_files
    else:
        config_files = _snapshot(config, "HEAD")
        messages.append(f"[INFO] 設定 repo：{config}（.deploy 檔案 {len(config_files)} 個，權威來源）")
        authoritative = config_files
        diff = _diff(config_files, legacy_files) if legacy_files else []
        if not legacy_files:
            messages.append("[INFO] 程式碼 repo 已無 .deploy/（遷移完成）")
        if diff:
            messages.append(f"[WARN] 設定 repo 與舊程式碼 repo 的 .deploy 不同（以設定 repo 為準）：{', '.join(diff[:8])}")
    for wt in (Path(worktrees_dir) / project, Path(worktrees_dir) / project / "main"):
        if (wt / ".deploy").is_dir():
            diff = _diff(authoritative, _dir_snapshot(wt))
            if diff:
                messages.append(f"[WARN] 工作樹 {wt} 的 .deploy 與權威來源不同（非權威，勿直接編輯）：{', '.join(diff[:8])}")
            else:
                messages.append(f"[OK]   工作樹 {wt} 的 .deploy 與權威來源一致")
            break
    return ok, messages


AUDIT_ROOT = paths.DEPLOYS_DIR
# 這些鍵屬「發布」流程（image digest 與 release tag 綁定），config set 不得碰
_PUBLISH_OWNED = ("image", "version")


def _parse_assignments(assignments: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in assignments:
        key, sep, value = item.partition("=")
        if not sep or not key or not key.replace("_", "").isalnum():
            raise ImagePrepareError(f"格式須為 KEY=VALUE（KEY 僅英數與底線）：{item!r}")
        out[key] = value
    return out


_VOL_OPTS = re.compile(r"^[A-Za-z]+(,[A-Za-z]+)*$")


def _parse_volume(item: str) -> tuple[str, str, str | None]:
    """`CONTAINER_PATH=HOST_PATH[:OPTIONS]` → (container_path, host_path, options|None)。"""
    cpath, sep, rest = item.partition("=")
    host, _, opts = rest.partition(":")
    for label, path in (("CONTAINER_PATH", cpath), ("HOST_PATH", host)):
        if not sep or not path.startswith("/") or ".." in path.split("/") or re.search(r"[\s:,\x00-\x1f]", path) or "//" in path:
            raise ImagePrepareError(f"--volume 格式須為 CONTAINER_PATH=HOST_PATH[:OPTIONS]，{label} 須為正規化絕對路徑：{item!r}")
    if opts and not _VOL_OPTS.fullmatch(opts):
        raise ImagePrepareError(f"--volume OPTIONS 只允許英文字母與逗號（例 rw、ro,Z）：{item!r}")
    return cpath, host, opts or None


def _apply_volumes(volumes: list, vol_sets: dict, vol_unsets: list[str]) -> tuple[list, list]:
    """回傳 (新 volumes 列表, changes)。以 container_path 為鍵：存在則換 host_path（保留其他欄位與原 options），不存在則新增（options 預設 rw）。"""
    if set(vol_sets) & set(vol_unsets):
        raise ImagePrepareError("同一個 container_path 不能同時 --volume 與 --unset-volume")
    out, changes = [dict(v) if isinstance(v, dict) else v for v in volumes], []
    index = {v.get("container_path"): i for i, v in enumerate(out) if isinstance(v, dict)}
    for cpath, (host, opts) in vol_sets.items():
        if cpath in index:
            old = out[index[cpath]]
            before = f"{old.get('host_path')}:{old.get('options', '')}"
            new = dict(old, host_path=host)
            if opts:
                new["options"] = opts
            if new == old:
                continue
            out[index[cpath]] = new
            changes.append((f"volume:{cpath}", before, f"{host}:{new.get('options', '')}"))
        else:
            out.append({"host_path": host, "container_path": cpath, "options": opts or "rw"})
            changes.append((f"volume:{cpath}", None, f"{host}:{opts or 'rw'}"))
    for cpath in vol_unsets:
        if cpath not in index or out[index[cpath]] is None:
            raise ImagePrepareError(f"volumes 沒有 container_path={cpath}，無法移除")
        old = out[index[cpath]]
        out[index[cpath]] = None
        changes.append((f"volume:{cpath}", f"{old.get('host_path')}:{old.get('options', '')}", None))
    return [v for v in out if v is not None], changes


def plan_config_set(
    project: str,
    service: str,
    sets: dict[str, str],
    unsets: list[str],
    *,
    vol_sets: dict | None = None,
    vol_unsets: list[str] | None = None,
    env_name: str | None = None,
    repos_dir: str | None = None,
    config_dir: str | None = None,
) -> dict:
    """唯讀：算出 env 變更後的宣告並驗證。驗證有 ERROR 則丟例外。

    寫入目標：給 env_name 或該環境已有 overlay → 寫 `.deploy/envs/<env>/containers/<svc>.json`（只含差異）；否則寫 base。
    """
    from container_ctl.env_overlay import DEFAULT_ENV, OverlayError, merge_overlay, overlay_path, read_overlay, read_profile
    _validate_name(project)
    _validate_name(service)
    config = config_repo_path(project, repos_dir, config_dir)
    if not Path(config).is_dir():
        raise ImagePrepareError(f"尚未建立設定 repo；先 `container-ctl config init {project}`（config set 只改設定 repo）")
    head = _source_commit(config, "HEAD")
    base = _source_config(config, head, service)
    env_label = env_name or DEFAULT_ENV
    try:
        overlay = read_overlay(config, head, env_label, service)
        old = merge_overlay(base, overlay, overlay_path(env_label, service))
        profile = read_profile(config, head, env_label)
    except OverlayError as exc:
        raise ImagePrepareError(str(exc)) from exc
    use_overlay = env_name is not None or overlay is not None
    effective = dict(old.get("env") or {})
    target_env = dict((overlay or {}).get("env") or {}) if use_overlay else dict(base.get("env") or {})
    changes = []
    for key, value in sets.items():
        if effective.get(key) != value:
            changes.append((key, effective.get(key), value))
        effective[key] = value
        target_env[key] = value
    for key in unsets:
        if key not in effective:
            raise ImagePrepareError(f"env 沒有 {key}，無法移除")
        if key not in target_env:
            raise ImagePrepareError(f"{key} 來自 base，overlay 不能移除；改用不帶 --env 的 config set --unset，或在 overlay 覆寫其值")
        changes.append((key, effective.pop(key), None))
        target_env.pop(key)
    new_vols = None
    if vol_sets or vol_unsets:
        # list 在 overlay 合併是整體取代：以『有效值』為底改完後，整份寫入目標（overlay 或 base）
        new_vols, vol_changes = _apply_volumes(list(old.get("volumes") or []), vol_sets or {}, vol_unsets or [])
        changes.extend(vol_changes)
    if use_overlay:
        content = dict(overlay or {}, env=target_env)
        if new_vols is not None:
            content["volumes"] = new_vols
        path = overlay_path(env_label, service)
        try:
            new = merge_overlay(base, content, path)
        except OverlayError as exc:
            raise ImagePrepareError(str(exc)) from exc
        if not target_env:
            content.pop("env")
    else:
        content = dict(base, env=target_env)
        if not target_env and "env" not in base:
            content.pop("env")   # 只改 volumes 時不要憑空多出空的 env 鍵
        if new_vols is not None:
            content["volumes"] = new_vols
        path = f".deploy/containers/{service}.json"
        new = content
    ok, messages = check_declarations([new], profile)
    from container_ctl.contract import ContractError, check_contract, read_contract
    try:
        messages = messages + check_contract(new, read_contract(config, head))
    except ContractError as exc:
        raise ImagePrepareError(str(exc)) from exc
    errors = [m for m in messages if m.startswith("[ERROR]")]
    if not ok or errors:
        raise ImagePrepareError("驗證未通過：" + "；".join(errors))
    branch = _run(["git", "--git-dir", config, "symbolic-ref", "--short", "HEAD"]).stdout.strip()
    return {"repo": config, "branch": branch, "head": head, "old": old, "new": new, "changes": changes,
            "path": path, "content": content, "env": env_label if use_overlay else None,
            "messages": [m for m in messages if m.startswith("[WARN]")]}


def config_set(
    project: str,
    service: str,
    sets: dict[str, str],
    unsets: list[str],
    *,
    vol_sets: dict | None = None,
    vol_unsets: list[str] | None = None,
    yes: bool = False,
    reason: str = "",
    env_name: str | None = None,
    repos_dir: str | None = None,
    config_dir: str | None = None,
    audit_root: str = AUDIT_ROOT,
) -> tuple[bool, list[str]]:
    """受控入口：驗證 → 預覽 diff →（--yes）提交設定 repo 並寫稽核紀錄。只改 env；不重啟容器，生效須另行 redeploy。"""
    plan = plan_config_set(project, service, sets, unsets, vol_sets=vol_sets, vol_unsets=vol_unsets,
                           env_name=env_name, repos_dir=repos_dir, config_dir=config_dir)
    out = [f"[PLAN] {project}/{service} @ {plan['head'][:12]}（{plan['branch']}）→ {plan['path']}"]
    for key, before, after in plan["changes"]:
        label = key if key.startswith("volume:") else f"env.{key}"
        out.append(f"  {label}: {'(無)' if before is None else repr(before)} → {'(移除)' if after is None else repr(after)}")
    out.extend(plan["messages"])
    if not plan["changes"]:
        out.append("[OK]   無變更")
        return True, out
    if not yes:
        out.append("[INFO] 預覽結束；加 --yes 提交（不重啟容器，生效須 container-ctl deploy redeploy）")
        return True, out
    who = getpass.getuser()
    summary = ", ".join(k for k, _, _ in plan["changes"])
    with tempfile.TemporaryDirectory(prefix="container-ctl-config-set-") as name:
        co = Path(name) / "checkout"
        _run(["git", "clone", "-q", "--branch", plan["branch"], "--single-branch", plan["repo"], str(co)])
        if _source_commit(str(co / ".git"), "HEAD") != plan["head"]:
            raise ImagePrepareError("設定 repo 在預覽後前進，請重跑")
        path = co / plan["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan["content"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git = ["git", "-C", str(co), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        _run([*git, "add", "--", plan["path"]])
        msg = f"config set {service}{' @' + plan['env'] if plan['env'] else ''}: {summary}\n\nby: {who}" + (f"\nreason: {reason}" if reason else "")
        _run([*git, "commit", "-q", "-m", msg])
        commit = _run(["git", "-C", str(co), "rev-parse", "HEAD"]).stdout.strip()
        _run(["git", "-C", str(co), "push", "-q", "origin", f"HEAD:refs/heads/{plan['branch']}"])
    record = {"ts": datetime.now(timezone.utc).isoformat(), "by": who, "project": project, "service": service,
              "from": plan["head"], "to": commit, "reason": reason, "env": plan["env"], "path": plan["path"],
              "changes": [{"key": k, "before": b, "after": a} for k, b, a in plan["changes"]]}
    try:
        audit = Path(audit_root) / project
        audit.mkdir(parents=True, exist_ok=True)
        with (audit / "config-audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        out.append(f"[WARN] 稽核紀錄未寫入（git 提交已留存）：{exc}")
    out.append(f"[OK]   已提交 {commit[:12]}；生效：container-ctl deploy redeploy {project} HEAD --service {service} --yes")
    return True, out


_CTX_FORBIDDEN_DIRS = {"src", "app", "packages", "ui", "tests", "web"}  # 與 image._dev_build_context 同一組
_CTX_MAX_FILES = 200
_CTX_MAX_BYTES = 1024 * 1024


def _source_context_files(legacy: str, commit: str, service: str) -> tuple[str, list[tuple[str, str]]]:
    """列出程式碼 repo 在 commit 的 `.deploy/dev-images/<service>/` 檔案（mode, path）；不合規則直接拒絕。"""
    base = f".deploy/dev-images/{service}"
    found = subprocess.run(["git", "--git-dir", legacy, "ls-tree", "-r", "-l", "-z", commit, "--", base],
                           capture_output=True, text=True)
    if found.returncode != 0 or not found.stdout:
        raise ImagePrepareError(f"{commit[:12]} 沒有 {base}/（只同步專用 context 目錄）")
    files, total = [], 0
    for rec in filter(None, found.stdout.split("\0")):
        meta, _, path = rec.partition("\t")
        mode, kind, _sha, size = meta.split(None, 3)
        rel = path[len(base) + 1:]
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ImagePrepareError(f"context 只允許一般檔案（拒絕 mode {mode} {kind}）：{path}")
        if any(part in _CTX_FORBIDDEN_DIRS for part in rel.split("/")[:-1]):
            raise ImagePrepareError(f"dev 映像專用 context 含專案原始碼目錄：{path}")
        if any(part in ("", ".", "..") for part in rel.split("/")):
            raise ImagePrepareError(f"不合法路徑：{path}")
        total += int(size)
        files.append((mode, path))
    if len(files) > _CTX_MAX_FILES or total > _CTX_MAX_BYTES:
        raise ImagePrepareError(f"context 過大（{len(files)} 檔／{total} bytes；上限 {_CTX_MAX_FILES} 檔／{_CTX_MAX_BYTES} bytes）")
    if not any(path == f"{base}/Dockerfile.dev" for _, path in files):
        raise ImagePrepareError(f"{base}/ 缺少 Dockerfile.dev")
    return base, files


def _stage_context(co: Path, legacy: str, commit: str, base: str) -> None:
    """在設定 repo 的 checkout 內以來源內容整個取代 base 目錄（含刪除），並 git add。"""
    target = co / base
    if target.exists():
        for f in sorted(target.rglob("*"), reverse=True):
            f.unlink() if (f.is_file() or f.is_symlink()) else f.rmdir()
        target.rmdir()
    archive = subprocess.Popen(["git", "--git-dir", legacy, "archive", "--format=tar", commit, "--", base],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert archive.stdout is not None
    with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
        tar.extractall(co, filter="data")
    if archive.wait() != 0:
        raise ImagePrepareError(f"git archive 失敗：{archive.stderr.read().decode(errors='replace').strip()}")
    _run(["git", "-C", str(co), "add", "-A", "--", base])


def plan_sync_image_context(
    project: str,
    service: str,
    from_ref: str,
    *,
    repos_dir: str | None = None,
    config_dir: str | None = None,
) -> dict:
    """唯讀：算出把程式碼 repo `<from_ref>:.deploy/dev-images/<service>/` 同步進設定 repo 的變更。"""
    _validate_name(project)
    _validate_name(service)
    legacy = _legacy_repo(project, repos_dir)
    config = config_repo_path(project, repos_dir, config_dir)
    if not Path(config).is_dir():
        raise ImagePrepareError(f"尚未建立設定 repo；先 `container-ctl config init {project}`")
    head = _source_commit(config, "HEAD")
    decl = _source_config(config, head, service)  # 服務必須已在設定 repo 宣告，且是 dev
    if decl.get("tier") != "dev":
        raise ImagePrepareError(f"{service} 不是 dev 容器；只同步 dev 映像 context")
    src_commit = _source_commit(legacy, from_ref)
    base, files = _source_context_files(legacy, src_commit, service)
    branch = _run(["git", "--git-dir", config, "symbolic-ref", "--short", "HEAD"]).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="container-ctl-ctx-plan-") as name:
        co = Path(name) / "checkout"
        _run(["git", "clone", "-q", "--branch", branch, "--single-branch", config, str(co)])
        _stage_context(co, legacy, src_commit, base)
        status = _run(["git", "-C", str(co), "diff", "--cached", "--name-status"]).stdout.splitlines()
        diff = _run(["git", "-C", str(co), "diff", "--cached", "--no-color"]).stdout
    return {"repo": config, "legacy": legacy, "branch": branch, "head": head, "src_commit": src_commit, "base": base,
            "files": [p for _, p in files], "changes": [ln.split("\t", 1) for ln in status], "diff": diff}


def sync_image_context(
    project: str,
    service: str,
    from_ref: str,
    *,
    yes: bool = False,
    reason: str = "",
    repos_dir: str | None = None,
    config_dir: str | None = None,
    audit_root: str = AUDIT_ROOT,
) -> tuple[bool, list[str]]:
    """受控入口：把程式碼 repo 的 dev 映像 context 同步進設定 repo（驗證→預覽 diff→（--yes）提交→稽核）。
    只動 `.deploy/dev-images/<service>/`；不碰宣告（image pin 等）、不建置、不重啟容器。"""
    plan = plan_sync_image_context(project, service, from_ref, repos_dir=repos_dir, config_dir=config_dir)
    out = [f"[PLAN] {project}/{service} dev 映像 context：{from_ref}@{plan['src_commit'][:12]} → 設定 repo {plan['head'][:12]}（{plan['branch']}）"]
    for status, path in plan["changes"]:
        out.append(f"  {status}\t{path}")
    if not plan["changes"]:
        out.append("[OK]   無變更")
        return True, out
    out.append(plan["diff"].rstrip())
    if not yes:
        out.append("[INFO] 預覽結束；加 --yes 提交。提交後 image pin 會過期，需重跑 image prepare-dev → publish-dev")
        return True, out
    who = getpass.getuser()
    with tempfile.TemporaryDirectory(prefix="container-ctl-ctx-sync-") as name:
        co = Path(name) / "checkout"
        _run(["git", "clone", "-q", "--branch", plan["branch"], "--single-branch", plan["repo"], str(co)])
        if _source_commit(str(co / ".git"), "HEAD") != plan["head"]:
            raise ImagePrepareError("設定 repo 在預覽後前進，請重跑")
        _stage_context(co, plan["legacy"], plan["src_commit"], plan["base"])
        git = ["git", "-C", str(co), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        msg = (f"config sync-image-context {service}: {len(plan['changes'])} 檔 from {plan['src_commit']}\n\nby: {who}"
               + (f"\nreason: {reason}" if reason else ""))
        _run([*git, "commit", "-q", "-m", msg])
        commit = _run(["git", "-C", str(co), "rev-parse", "HEAD"]).stdout.strip()
        _run(["git", "-C", str(co), "push", "-q", "origin", f"HEAD:refs/heads/{plan['branch']}"])
    record = {"ts": datetime.now(timezone.utc).isoformat(), "by": who, "project": project, "service": service,
              "action": "sync-image-context", "from": plan["head"], "to": commit, "source": plan["src_commit"],
              "reason": reason, "path": plan["base"], "changes": [{"status": s, "path": p} for s, p in plan["changes"]]}
    try:
        audit = Path(audit_root) / project
        audit.mkdir(parents=True, exist_ok=True)
        with (audit / "config-audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        out.append(f"[WARN] 稽核紀錄未寫入（git 提交已留存）：{exc}")
    out.append(f"[OK]   已提交 {commit[:12]}；下一步：container-ctl image prepare-dev {project} {service} <設定 repo ref> --output-dir <dir>，再 publish-dev")
    return True, out


def _worktree_dirs(project: str, worktrees_dir: str) -> list[Path]:
    root = Path(worktrees_dir) / project
    return [d for d in (root / "main", root) if (d / ".git").exists()][:1]


def ineffective_source_messages(
    project: str,
    *,
    repos_dir: str | None = None,
    config_dir: str | None = None,
    worktrees_dir: str = WORKTREES_DIR,
) -> list[str]:
    """設定 repo 為權威時：舊 .deploy 的差異不會生效（item 3）；工作樹 HEAD 領先 bare repo 的修正工具看不到（item 7）。"""
    out: list[str] = []
    try:
        legacy = _legacy_repo(project, repos_dir)
    except ImagePrepareError:
        return out
    config = config_repo_path(project, repos_dir, config_dir)
    if Path(config).is_dir():
        authoritative = _snapshot(config, "HEAD")
        legacy_files = _snapshot(legacy, "HEAD")
        diff = _diff(authoritative, legacy_files) if legacy_files else []  # 程式碼 repo 已無 .deploy＝已遷移完成，不警告
        if diff:
            out.append(f"[WARN] 程式碼 repo 的 .deploy 已失效（設定 repo 為權威，改它不會生效）：{', '.join(diff[:6])}；"
                       f"改環境值請用 `container-ctl config set {project} <service> KEY=VALUE`")
        for wt in _worktree_dirs(project, worktrees_dir):
            wt_files = _dir_snapshot(wt)
            wdiff = _diff(authoritative, wt_files) if wt_files else []
            if wdiff:
                out.append(f"[WARN] 工作樹 {wt} 的 .deploy 與設定 repo 不同，不會生效：{', '.join(wdiff[:6])}")
    for wt in _worktree_dirs(project, worktrees_dir):
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        main = subprocess.run(["git", "--git-dir", legacy, "rev-parse", "--verify", "--quiet", "refs/heads/main"],
                              capture_output=True, text=True).stdout.strip()
        if head and main and head != main:
            ahead = subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", main, head]).returncode == 0
            out.append(f"[WARN] 工作樹 HEAD {head[:8]} 與 bare repo main {main[:8]} 不同"
                       + ("（工作樹領先，修正尚未 push，建置／migration／restore-drill 讀不到）" if ahead else "；請確認要部署的是哪個"))
    return out


def config_show(project: str, service: str, env_name: str | None = None, *, repos_dir: str | None = None,
                config_dir: str | None = None) -> str:
    """唯讀：印出 base ⊕ overlay 的有效宣告（供 `config set` 前後比對；機密只以 *_FILE 路徑出現）。"""
    from container_ctl.env_overlay import DEFAULT_ENV, merge_overlay, overlay_path, read_overlay
    _validate_name(project)
    config = config_repo_path(project, repos_dir, config_dir)
    if not Path(config).is_dir():
        raise ImagePrepareError(f"尚未建立設定 repo：{config}")
    head = _source_commit(config, "HEAD")
    env_label = env_name or DEFAULT_ENV
    eff = merge_overlay(_source_config(config, head, service), read_overlay(config, head, env_label, service),
                        overlay_path(env_label, service))
    return json.dumps(eff, ensure_ascii=False, indent=2, sort_keys=True)


def config_check(project: str, env_name: str | None = None, ref: str = "HEAD", *, repos_dir: str | None = None,
                 config_dir: str | None = None) -> tuple[bool, list[str]]:
    """唯讀：以契約驗證指定環境的有效宣告（base ⊕ overlay）。"""
    from container_ctl.contract import ContractError, check_contract, contract_hash, read_contract
    from container_ctl.env_overlay import DEFAULT_ENV
    from container_ctl.repo_reader import list_container_configs
    _validate_name(project)
    from container_ctl.deploy_source import resolve_deploy_repo
    repo, _kind = resolve_deploy_repo(project, repos_dir, config_dir)
    try:
        contract = read_contract(repo, ref)
    except ContractError as exc:
        return False, [f"[ERROR] {exc}"]
    configs = list_container_configs(repo, ref, env=env_name)
    out = [f"[INFO] 契約 {contract_hash(contract) if contract else '(無)'}；環境 {env_name or DEFAULT_ENV or '(無)'}；服務 {len(configs)} 個"]
    for cfg in configs:
        out.extend(check_contract(cfg, contract))
    ok = not any(m.startswith("[ERROR]") for m in out)
    out.append("[OK]   契約檢查通過" if ok else "[FAIL] 契約檢查未通過")
    return ok, out
