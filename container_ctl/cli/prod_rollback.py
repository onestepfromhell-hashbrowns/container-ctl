"""container-ctl prod rollback — 回到已發布的舊版（向前提交，不改寫歷史，不刪映像）。

做法：把設定 repo 現行宣告的 `image`（與 `version`）換回 release/<service>/<ver> 凍結的內容，
以新 commit 推進（歷史只前進），再 redeploy → healthy → smoke → verify → audit。
其餘宣告（env、volumes、ports…）維持現行，不隨舊版還原。
未帶 --yes 只預覽。映像由本機保留（清理規則：只封存、不刪）；缺映像則停止並回報。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from container_ctl.cli.image import ImagePrepareError, _source_commit, _source_config
from container_ctl.cli.image_prod import _validate_version, read_release_record, release_tag
from container_ctl.deploy_source import resolve_deploy_repo


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise ImagePrepareError(f"{' '.join(cmd[:3])} 失敗：{r.stderr.strip()[:200]}")
    return r.stdout.strip()


def list_versions(repo: str, service: str) -> list[str]:
    out = subprocess.run(["git", "--git-dir", repo, "tag", "-l", f"release/{service}/*"], capture_output=True, text=True).stdout
    return sorted(t.rsplit("/", 1)[1] for t in out.split())


def plan_rollback(project: str, service: str, to_version: str, ref: str = "HEAD", *,
                  local_image_exists=None) -> dict:
    """唯讀：回傳 {noop, new_cfg, old_image, cur_image, image_id}；不可回退則丟 ImagePrepareError。"""
    _validate_version(to_version)
    repo, kind = resolve_deploy_repo(project)
    if kind != "config":
        raise ImagePrepareError("rollback 只支援設定 repo 模式（legacy 專案請先 config init）")
    record = read_release_record(repo, service, to_version)
    if record is None:
        raise ImagePrepareError(f"無 {release_tag(service, to_version)}；可用版本：{', '.join(list_versions(repo, service)) or '無'}")
    old = _source_config(repo, record["config_commit"], service)
    cur = _source_config(repo, _source_commit(repo, ref), service)
    if cur.get("tier") != "prod":
        raise ImagePrepareError(f"{service} 不是 prod 容器")
    image_id = record.get("image_id", "")
    exists = local_image_exists or (lambda i: subprocess.run(["podman", "image", "exists", i]).returncode == 0)
    if not exists(image_id):
        raise ImagePrepareError(f"本機已無映像 {image_id[:19]}；不可回退（映像不得被刪，請查封存）")
    new = dict(cur, image=old["image"])
    if "version" in old:
        new["version"] = old["version"]
    else:
        new.pop("version", None)
    return {"noop": new == cur, "new_cfg": new, "old_image": old["image"], "cur_image": cur.get("image"),
            "image_id": image_id, "from_version": cur.get("version"), "to_version": to_version}


def _commit_declaration(project: str, service: str, new_cfg: dict, message: str) -> str:
    repo, _ = resolve_deploy_repo(project)
    branch = _run(["git", "--git-dir", repo, "symbolic-ref", "--short", "HEAD"])
    with tempfile.TemporaryDirectory(prefix="container-ctl-rollback-") as name:
        co = Path(name) / "checkout"
        _run(["git", "clone", "-q", "--branch", branch, "--single-branch", repo, str(co)])
        path = co / ".deploy" / "containers" / f"{service}.json"
        path.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git = ["git", "-C", str(co), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        _run([*git, "add", "--", f".deploy/containers/{service}.json"])
        _run([*git, "commit", "-q", "-m", message])
        commit = _run(["git", "-C", str(co), "rev-parse", "HEAD"])
        _run(["git", "-C", str(co), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"])
    return commit


def prod_rollback(project: str, service: str, to_version: str, *, ref: str = "HEAD", yes: bool = False,
                  timeout: int = 180, db_path: str | None = None) -> int:
    from container_ctl.cli.prod_up import StepFailed, _container_name, _run_step, _wait_healthy
    me = f"container-ctl prod rollback {project} {service} --to {to_version}" + (" --yes" if yes else "")
    try:
        plan = plan_rollback(project, service, to_version, ref)
    except ImagePrepareError as exc:
        print(f"[FAIL] rollback 前置：{exc}")
        return 1
    print(f"[PLAN] {service}: {plan['from_version']} → {to_version}；image {str(plan['cur_image'])[-23:]} → {plan['old_image'][-23:]}")
    if plan["noop"]:
        print("[OK]   宣告已是目標版本，不需動作（若運行中容器不符，請 container-ctl deploy redeploy）")
        return 0
    if not yes:
        print(f"[INFO] 預覽結束。加 --yes 執行：{me} --yes")
        return 0
    try:
        commit = _run_step("commit", lambda: _commit_declaration(
            project, service, plan["new_cfg"], f"Rollback {service} to {to_version} (image pin restored)"),
            "檢查設定 repo 是否可 push 後重跑")
        print(f"[DONE] commit: {commit[:12]}")
        from container_ctl.cli.deploy import redeploy
        _run_step("redeploy", lambda: redeploy(project, "HEAD", yes=True, only=[service]),
                  f"container-ctl deploy redeploy {project} HEAD --service {service} --yes")
        print("[DONE] redeploy")
        container = _container_name(project, service, db_path)
        if not container:
            raise StepFailed("health", "state.db 找不到實例", f"container-ctl deploy verify {project} HEAD --service {service}")
        healthy, last = _wait_healthy(container, timeout)
        if not healthy:
            raise StepFailed("health", f"{container} 未 healthy（{last}）", f"podman logs {container}")
        print(f"[DONE] health: {container}: {last}")
        from container_ctl.smoke import run_smoke
        s_ok, s_msgs = run_smoke(container, plan["new_cfg"])
        for m in s_msgs:
            print(m)
        if not s_ok:
            raise StepFailed("smoke", "回退後 smoke 未通過", f"podman logs {container}")
        from container_ctl.prod_verify import verify_prod
        _run_step("verify", lambda: verify_prod(project, "HEAD", [service]), f"container-ctl deploy verify {project} HEAD --service {service}")
        from container_ctl.cli.image_prod import audit_prod_image
        a_ok, a_msgs = audit_prod_image(project, service, to_version, "HEAD")
        for m in a_msgs:
            print(m)
        if not a_ok:
            raise StepFailed("audit-prod", "錨定稽核未通過", f"container-ctl image audit-prod {project} {service} {to_version} HEAD")
        print(f"[OK]   {service} 已回到 {to_version}")
        return 0
    except StepFailed as exc:
        print(f"[FAIL] 步驟 {exc.step}：{exc.detail}")
        print(f"[NEXT] {exc.next_cmd}")
        return 1


def main_entry(args) -> None:
    sys.exit(prod_rollback(args.project, args.service, args.to, ref=args.ref, yes=args.yes, timeout=args.timeout))
