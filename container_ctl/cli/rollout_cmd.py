"""container-ctl rollout apply|status|history|undo、reconcile（ADR-009 P4）。

apply：把設定 repo 現行宣告套用到宿主（不建置、不發布；改 env／埠等配置的正規路徑）。預設預覽。
undo：回到最近一個「實際健康」的 rollout 的 image 和／或設定修訂；預設預覽。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from container_ctl import rollout
from container_ctl.cli.image import ImagePrepareError, _source_commit
from container_ctl.cli.prod_up import StepFailed, _check_declaration_matches_tag, converge
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import list_container_configs


def _git(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise ImagePrepareError(f"{' '.join(cmd[:4])} 失敗：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def rollout_apply(project: str, service: str, *, ref: str = "HEAD", env: str | None = None, yes: bool = False,
                  timeout: int = 180, db_path: str | None = None, detail: str = "rollout apply") -> int:
    from container_ctl.cli.prod_doctor import run_prod_doctor
    repo, kind = resolve_deploy_repo(project)
    cfg = next((c for c in list_container_configs(repo, ref, env=env) if c.get("name") == service), None)
    if cfg is None or cfg.get("tier") != "prod":
        print(f"[FAIL] {project}/{service} 不是已宣告的 prod 服務")
        return 1
    version = str(cfg.get("version"))
    try:
        _check_declaration_matches_tag(project, service, version, ref)
    except StepFailed as exc:
        print(f"[FAIL] {exc.step}：{exc.detail}\n[NEXT] {exc.next_cmd}")
        return 1
    ok, msgs = run_prod_doctor(project, ref, [service], anchor_strict=True)
    for m in msgs:
        if m.startswith("[ERROR]"):
            print(m)
    if not ok:
        print("[FAIL] doctor 有阻塞，未套用")
        return 1
    _, report = rollout.reconcile_report(project, service, ref=ref, env=env, db_path=db_path)
    print(f"[PLAN] 套用 {service} version={version} config={_source_commit(repo, ref)[:12]}")
    for line in report:
        print(line)
    if not yes:
        print(f"[INFO] 預覽結束；加 --yes 執行：container-ctl rollout apply {project} {service} --yes")
        return 0
    done = lambda step, d="ok": print(f"[DONE] {step}: {d}")  # noqa: E731
    try:
        converge(project, service, version, ref, repo, timeout, db_path, done)
    except StepFailed as exc:
        print(f"[FAIL] 步驟 {exc.step}：{exc.detail}\n[NEXT] {exc.next_cmd}")
        rollout.record_from_declaration(project, service, version, ref, "failed", f"{exc.step}: {exc.detail}"[:300], db_path, env)
        return 1
    rid = rollout.record_from_declaration(project, service, version, ref, "healthy", detail, db_path, env)
    print(f"[OK]   rollout{f' #{rid}' if rid else ''} healthy")
    return 0


def _commit_files(project: str, files: dict[str, bytes | None], message: str) -> str:
    repo, _ = resolve_deploy_repo(project)
    branch = _git(["git", "--git-dir", repo, "symbolic-ref", "--short", "HEAD"])
    with tempfile.TemporaryDirectory(prefix="container-ctl-undo-") as name:
        co = Path(name) / "checkout"
        _git(["git", "clone", "-q", "--branch", branch, "--single-branch", repo, str(co)])
        git = ["git", "-C", str(co), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        for path, data in files.items():
            target = co / path
            if data is None:
                _git([*git, "rm", "-q", "--", path])
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                _git([*git, "add", "--", path])
        _git([*git, "commit", "-q", "-m", message])
        commit = _git(["git", "-C", str(co), "rev-parse", "HEAD"])
        _git(["git", "-C", str(co), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"])
    return commit


def rollout_undo(project: str, service: str, *, to: int | None = None, mode: str = "both", yes: bool = False,
                 ref: str = "HEAD", timeout: int = 180, db_path: str | None = None) -> int:
    try:
        plan = rollout.plan_undo(project, service, to=to, db_path=db_path)
        t, cur = plan["target"], plan["current"]
        files = rollout.undo_files(project, service, t["config_commit"], mode)
    except (ValueError, ImagePrepareError) as exc:
        print(f"[FAIL] undo 前置：{exc}")
        return 1
    print(f"[PLAN] {service}: rollout #{cur['id']}（{cur['release_version']}, 設定 {str(cur['config_commit'])[:12]}）"
          f" → #{t['id']}（{t['release_version']}, 設定 {str(t['config_commit'])[:12]}）；模式 {mode}")
    if t.get("image_id") and subprocess.run(["podman", "image", "exists", t["image_id"]]).returncode != 0:
        if t.get("manifest_digest"):
            print(f"[INFO] 本機缺映像 {t['image_id'][:19]}，將自 OCI store 還原（manifest {t['manifest_digest'][:19]}）")
        else:
            print(f"[FAIL] 本機已無映像 {t['image_id'][:19]} 且該版無 OCI store 記錄；不可回滾")
            return 1
    if not files:
        print("[OK]   設定 repo 已與目標一致，無需提交（若運行不符，請 rollout apply）")
        return 0
    for path, data in files.items():
        print(f"  {'刪除' if data is None else '還原'} {path}")
    if not yes:
        print(f"[INFO] 預覽結束；加 --yes 執行（提交設定 repo 後 apply）：container-ctl rollout undo {project} {service} --mode {mode} --yes")
        return 0
    if t.get("manifest_digest") and subprocess.run(["podman", "image", "exists", t["image_id"]]).returncode != 0:
        from container_ctl import artifact_store
        artifact_store.load(project, service, t["release_version"])
    commit = _commit_files(project, files, f"rollout undo {service} to #{t['id']} ({mode})")
    print(f"[DONE] commit: {commit[:12]}")
    return rollout_apply(project, service, ref="HEAD", yes=True, timeout=timeout, db_path=db_path, detail=f"undo to #{t['id']} ({mode})")


def status_lines(project: str, service: str, db_path: str | None = None) -> list[str]:
    rows = rollout.history(project, service, limit=10, db_path=db_path)
    if not rows:
        return ["(無 rollout 紀錄)"]
    return [f"#{r['id']:<4} {r['occurred_at'][:19]} {r['result']:<8} v{r['release_version']:<10} "
            f"{str(r['config_commit'])[:10]:<10} {str(r['manifest_digest'] or r['image_id'] or '')[:19]:<20} {r['actor'] or ''} {r['detail'] or ''}"
            for r in rows]


def main_entry(args) -> None:
    sub = args.rollout_cmd
    if sub == "apply":
        sys.exit(rollout_apply(args.project, args.service, ref=args.ref, env=args.env, yes=args.yes, timeout=args.timeout))
    if sub == "undo":
        sys.exit(rollout_undo(args.project, args.service, to=args.to, mode=args.mode, yes=args.yes, timeout=args.timeout))
    if sub in ("status", "history"):
        for line in status_lines(args.project, args.service):
            print(line)
        sys.exit(0)
    sys.exit(2)


def reconcile_entry(args) -> None:
    ok, lines = rollout.reconcile_report(args.project, args.service, ref=args.ref, env=args.env)
    for line in lines:
        print(line)
    sys.exit(0 if ok else 1)
