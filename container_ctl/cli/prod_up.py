"""container-ctl prod up — 單一指令完成 prod 服務部署（SPEC 方案 P1）。

流程（皆重用既有工具函式；每步皆由「現況」判斷是否已完成，因此可重跑續做）：
  1 doctor        體檢（發布前允許 release tag／pin 尚未存在）
  2 host-prepare  建缺少的宿主目錄（只新建）
  2c db-provision   消費者宣告 *_PG_PASSWORD_FILE 時，委派 PG-Hub 工具發放 DB／角色／密碼檔
  2b secret-provision 依宣告 secrets[] 產生缺少的 password/token 檔（工具產生，內容不輸出，不覆寫）
  3 prepare       prepare-prod 建置並產出 artifact（release tag 已存在則略過）
  4 publish       publish-prod 提交 pin 並寫 release tag（已存在則略過）
  5 redeploy      redeploy --service
  6 health        等待容器 healthy（無 healthcheck 則等 Running）
  6b smoke        穩定性／TCP／選填 HTTP（見 smoke.py）；--rollback-on-fail 時失敗自動回到前一版
  7 verify        deploy verify ＋ audit-prod
  8 doctor-final  發布後嚴格體檢
未帶 --yes 只做 1–2 的預覽與步驟說明，不建置、不發布、不部署。
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.cli.host_prepare import apply_actions, plan_host_prepare
from container_ctl.cli.prod_doctor import run_prod_doctor
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import list_container_configs
from container_ctl import paths

REPORT_ROOT = Path(paths.DEPLOYS_DIR)


class StepFailed(Exception):
    def __init__(self, step: str, detail: str, next_cmd: str):
        super().__init__(detail)
        self.step, self.detail, self.next_cmd = step, detail, next_cmd


def _tag_exists(project: str, service: str, version: str) -> bool:
    from container_ctl.cli.image_prod import _tag_exists as tag_exists
    repo, _ = resolve_deploy_repo(project)
    return tag_exists(repo, service, version)


def _container_name(project: str, service: str, db_path: str | None = None) -> str | None:
    import sqlite3
    from container_ctl.db import DEFAULT_DB_PATH
    with sqlite3.connect(f"file:{db_path or DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT container_name FROM instances WHERE project_name=? AND service_name=? AND status!='destroyed' "
            "ORDER BY updated_at DESC LIMIT 1", (project, service)).fetchone()
    return row[0] if row else None


def _wait_healthy(container: str, timeout: int, poll=time.sleep) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = "unknown"
    while True:
        r = subprocess.run(["podman", "inspect", container, "--format",
                            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}"],
                           capture_output=True, text=True)
        last = r.stdout.strip() or (r.stderr.strip()[:100])
        parts = last.split()
        if len(parts) == 2 and parts[0] == "running" and parts[1] in ("healthy", "nohc"):
            return True, last
        if time.monotonic() >= deadline:
            return False, last
        poll(3)


_VERIFY_RACE = "requires active, Running and healthy"


def _verify_with_retry(project: str, ref: str, service: str, timeout: int = 60,
                       poll=time.sleep, clock=time.monotonic) -> None:
    """重啟後 systemd／healthcheck 狀態可能尚未穩定（rollout #1-#3 的 verify 競態）。
    只對「requires active, Running and healthy」重試；漂移、掛載、錨定等其他失敗立即丟出。"""
    from container_ctl.prod_verify import verify_prod
    deadline = clock() + timeout
    tries = 0
    while True:
        try:
            verify_prod(project, ref, [service])
        except Exception as exc:  # noqa: BLE001
            if _VERIFY_RACE not in str(exc) or clock() >= deadline:
                raise
            tries += 1
            poll(3)
            continue
        if tries:
            print(f"[INFO] verify 重試 {tries} 次後通過（重啟後狀態尚在穩定）")
        return


def _run_step(step: str, fn, next_cmd: str):
    try:
        return fn()
    except StepFailed:
        raise
    except SystemExit as exc:
        if exc.code in (0, None):
            return None
        raise StepFailed(step, f"步驟以非零狀態結束（{exc.code}）", next_cmd) from exc
    except Exception as exc:  # noqa: BLE001 — 轉成可行動的失敗訊息
        raise StepFailed(step, str(exc), next_cmd) from exc


def _current_version(project: str, service: str, db_path: str | None = None) -> str | None:
    """實際運行中的版本：running 容器的映像 ID 對上哪個 release tag 凍結記錄。

    不採 state.db 的 version 欄（redeploy 失敗也會留下新版本列）。對不上或有歧義回 None（呼叫端不得據此自動回滾）。
    """
    import sqlite3
    from container_ctl.cli.image_prod import read_release_record
    from container_ctl.cli.prod_rollback import list_versions
    from container_ctl.db import DEFAULT_DB_PATH
    try:
        with sqlite3.connect(f"file:{db_path or DEFAULT_DB_PATH}?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT container_name, version FROM instances WHERE project_name=? AND service_name=? "
                                "AND status!='destroyed' ORDER BY updated_at DESC", (project, service)).fetchall()
        repo, _ = resolve_deploy_repo(project)
        frozen = {v: (read_release_record(repo, service, v) or {}).get("image_id") for v in list_versions(repo, service)}
    except Exception:  # noqa: BLE001 — 查不到就當不知道
        return None
    running = set()
    for name, _ver in rows:
        r = subprocess.run(["podman", "inspect", name, "--format", "{{.State.Running}} {{.Image}}"], capture_output=True, text=True)
        parts = r.stdout.split()
        if r.returncode == 0 and len(parts) == 2 and parts[0] == "true":
            running.add("sha256:" + parts[1].removeprefix("sha256:"))
    matches = sorted(v for v, image_id in frozen.items() if image_id in running)
    if len(running) != 1 or not matches:
        return None
    hinted = {ver for _n, ver in rows}
    return next((v for v in matches if v in hinted), matches[-1])


def _confirm_rollback(project: str, service: str, previous: str, version: str) -> bool:
    """自動回滾會寫設定庫並重啟 prod，須人在場確認；非互動環境一律不回滾。"""
    if not sys.stdin.isatty():
        return False
    answer = input(f"回滾 {project}/{service}：{version} → {previous}（實際運行中的版本；將提交設定庫並 redeploy）？[y/N] ")
    return answer.strip().lower() == "y"


def _check_declaration_matches_tag(project: str, service: str, version: str, ref: str) -> None:
    """release tag 已存在而被略過 publish 時，設定庫宣告須與凍結記錄一致；不一致就直接報告，不空等容器。"""
    from container_ctl.cli.image import _source_commit, _source_config
    from container_ctl.cli.image_prod import read_release_record
    repo, _ = resolve_deploy_repo(project)
    record = read_release_record(repo, service, version) or {}
    cfg = _source_config(repo, _source_commit(repo, ref), service)
    declared_digest = "sha256:" + cfg["image"].rsplit("@sha256:", 1)[1] if "@sha256:" in str(cfg.get("image")) else None
    problems = []
    if str(cfg.get("version")) != version:
        problems.append(f"宣告 version={cfg.get('version')}（要部署 {version}）")
    if declared_digest != record.get("image_id"):
        problems.append(f"宣告 image={declared_digest}，release tag 凍結 {record.get('image_id')}")
    if problems:
        raise StepFailed("declaration", "設定庫宣告與 release tag 不一致：" + "；".join(problems),
                         f"確認後以 container-ctl prod rollback {project} {service} --to {version} 讓宣告回到該版（不空等容器）")


def _record_rollout(project: str, service: str, version: str, ref: str, result: str, detail: str, db_path: str | None) -> None:
    """寫 rollouts 紀錄（best effort，絕不讓紀錄失敗影響部署結果）。"""
    from container_ctl import rollout
    try:
        rollout.record_from_declaration(project, service, version, ref, result, detail, db_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] rollout 紀錄未寫入：{exc}")


def converge(project: str, service: str, version: str, ref: str, repo: str, timeout: int, db_path: str | None, done) -> None:
    """redeploy → health → smoke → verify／audit → 發布後 doctor。失敗丟 StepFailed。prod up 與 rollout apply 共用。"""
    # 5 redeploy
    from container_ctl.cli.deploy import redeploy
    _run_step("redeploy", lambda: redeploy(project, ref, yes=True, only=[service]),
              f"container-ctl deploy redeploy {project} {ref} --service {service} --yes")
    done("redeploy", f"--service {service}")

    # 6 health
    container = _container_name(project, service, db_path)
    if not container:
        raise StepFailed("health", "state.db 找不到實例", f"container-ctl deploy verify {project} {ref} --service {service}")
    healthy, last = _wait_healthy(container, timeout)
    if not healthy:
        raise StepFailed("health", f"{container} 未在 {timeout}s 內 healthy（最後狀態：{last}）",
                         f"podman logs {container}；若為設定漂移：container-ctl service restart {project} {service} --container-name {container} --yes")
    done("health", f"{container}: {last}")

    # 6b smoke（穩定性、TCP、選填 HTTP）
    from container_ctl.smoke import run_smoke
    cfg = next((c for c in list_container_configs(repo, ref) if c.get("name") == service), {})
    s_ok, s_msgs = run_smoke(container, cfg)
    for m in s_msgs:
        print(m)
    if not s_ok:
        raise StepFailed("smoke", "smoke 未通過", f"podman logs {container}；回到舊版：container-ctl prod rollback {project} {service} --to <舊版> --yes")
    done("smoke", "通過")

    # 7 verify / audit
    _run_step("verify", lambda: _verify_with_retry(project, ref, service),
              f"container-ctl deploy verify {project} {ref} --service {service}")
    done("verify", "deploy verify 通過")
    from container_ctl.cli.image_prod import audit_prod_image
    a_ok, a_msgs = audit_prod_image(project, service, version, ref)
    for m in a_msgs:
        print(m)
    if not a_ok:
        raise StepFailed("audit-prod", "錨定稽核未通過", f"container-ctl image audit-prod {project} {service} {version} {ref}")
    done("audit-prod", "通過")

    # 8 final doctor
    ok2, msgs2 = run_prod_doctor(project, ref, [service], anchor_strict=True)
    if not ok2:
        for m in msgs2:
            if m.startswith("[ERROR]"):
                print(m)
        raise StepFailed("doctor-final", "發布後體檢仍有阻塞", f"container-ctl prod doctor {project} {ref} --service {service}")
    done("doctor-final", "無阻塞")



def prod_up(project: str, service: str, version: str, code_ref: str = "HEAD", ref: str = "HEAD",
            yes: bool = False, timeout: int = 180, db_path: str | None = None, rollback_on_fail: bool = False,
            no_snapshot: bool = False) -> int:
    previous = None
    report: dict = {"project": project, "service": service, "version": version, "started": datetime.now(timezone.utc).isoformat(),
                    "steps": [], "yes": yes}
    me = f"container-ctl prod up {project} {service} --ver {version} --code-ref {code_ref}" + (" --yes" if yes else "")

    def done(step: str, detail: str = "ok"):
        report["steps"].append({"step": step, "detail": detail})
        print(f"[DONE] {step}: {detail}")

    try:
        # 1 doctor（發布前）
        ok, msgs = run_prod_doctor(project, ref, [service], anchor_strict=False)
        blockers = [m for m in msgs if m.startswith("[ERROR]")]
        # 宿主目錄缺少由下一步補，不算阻塞
        blockers = [m for m in blockers if "宿主目錄不存在" not in m and "宿主檔不存在" not in m
                    and "Volume 目錄不存在" not in m]  # 下一步 host prepare 會建；發布後 doctor-final 再嚴格檢查
        # 宣告 secrets[] 且 kind=password|token 的缺檔由 secret provision 補，不算阻塞
        from container_ctl.secret_provision import apply_secret_provision, plan_secret_provision
        _repo0, _ = resolve_deploy_repo(project)
        _cfgs0 = [c for c in list_container_configs(_repo0, ref) if c.get("name") == service]
        s_actions, _s_msgs = plan_secret_provision(_cfgs0)
        generatable = {a["path"] for a in s_actions if a["state"] == "missing"}
        from container_ctl.db_provision import has_declaration as _has_pg_decl
        db_made = _has_pg_decl(project)  # pg-hub.json 存在 → *_PG_PASSWORD_FILE 由 db provision 產生，不算阻塞
        secret_missing = [m for m in msgs if m.startswith("[ERROR]") and "宿主檔不存在" in m
                          and not any(p in m for p in generatable)
                          and not (db_made and "_PG_PASSWORD_FILE" in m)]
        if blockers or secret_missing:
            for m in blockers + secret_missing:
                print(m)
            raise StepFailed("doctor", f"{len(blockers) + len(secret_missing)} 項阻塞",
                             f"修正後重跑：{me}（password/token 由 secret／db provision 產生；憑證與私鑰須事先就位）")
        done("doctor", "無阻塞（發布前）")

        # 2 host prepare
        repo, _kind = resolve_deploy_repo(project)
        configs = [c for c in list_container_configs(repo, ref) if c.get("name") == service]
        actions, errors = plan_host_prepare(configs)
        if errors:
            for e in errors:
                print(e)
            raise StepFailed("host-prepare", "目錄計畫有錯誤", f"container-ctl host prepare {project} --service {service}")
        if actions:
            if not yes:
                for a in actions:
                    print(f"[PLAN] host prepare：{a['op']} {a['path']}")
            else:
                for line in apply_actions(actions):
                    print(line)
                    if line.startswith("[ERROR]"):
                        raise StepFailed("host-prepare", line, f"container-ctl host prepare {project} --service {service} --apply")
        done("host-prepare", f"{len(actions)} 項動作" + ("（預覽）" if actions and not yes else ""))

        # 2b secret provision（只建缺少的 password/token 檔；內容不輸出）
        pending = [a for a in s_actions if a["state"] == "missing"]
        if pending and not yes:
            for a in pending:
                print(f"[PLAN] secret provision：{a['env']} → {a['path']}")
        elif pending:
            for line in apply_secret_provision(s_actions):
                print(line)
                if line.startswith("[ERROR]"):
                    raise StepFailed("secret-provision", line, f"container-ctl secret provision {project} --service {service} --apply")
        done("secret-provision", f"{len(pending)} 個檔" + ("（預覽）" if pending and not yes else ""))

        # 2c db-provision（消費者宣告 *_PG_PASSWORD_FILE 時，由 PG-Hub 工具發放 DB／角色／密碼檔；已存在則略過）
        from container_ctl.db_provision import db_provision, has_declaration
        if any(k.endswith("_PG_PASSWORD_FILE") for c in configs for k in (c.get("env") or {})) and has_declaration(project):
            rc_db = db_provision(project, "prod", "apply" if yes else "dry-run")
            if rc_db and yes:
                raise StepFailed("db-provision", "資料庫發放失敗", f"container-ctl db provision {project} --env prod --mode dry-run")
            done("db-provision", "完成" if yes else "預覽")

        # 2d db-upgrade（宣告 .deploy/db-migrate.json 時：先快照備份，再套用待遷移 SQL；失敗即停，舊版仍在線）
        from container_ctl import db_upgrade as _dbu
        _mig_decl = _dbu.read_migrate_decl(project)
        if _mig_decl:
            if not yes:
                _run_step("db-upgrade", lambda: _dbu.migrate(project, "prod", False, code_ref), f"container-ctl db migrate {project} --env prod")
                done("db-upgrade", "預覽（只列計畫）")
            else:
                def _upgrade():
                    if not no_snapshot:
                        _dbu.snapshot(project, "prod", reason=f"pre-upgrade {service} {version}")
                    return _dbu.migrate(project, "prod", True, code_ref, snapshot_first=False)
                _run_step("db-upgrade", _upgrade, f"container-ctl db restore-drill {project} --env prod 後排除，再重跑：{me}")
                done("db-upgrade", "已備份" + ("（略過）" if no_snapshot else "") + "並套用 migration")

        if not yes:
            print("[INFO] 預覽結束。加 --yes 後依序執行：prepare-prod → publish-prod → redeploy --service → 等 healthy → verify")
            report["result"] = "preview"
            return 0

        # 3 prepare / 4 publish
        if _tag_exists(project, service, version):
            _check_declaration_matches_tag(project, service, version, ref)
            done("prepare", f"release/{service}/{version} 已存在，略過")
            done("publish", "已存在，略過")
        else:
            from container_ctl.cli.image_prod import prepare_prod_image, publish_prod_image
            out_dir = REPORT_ROOT / project / "prod-up" / f"{service}-{version}"
            out_dir.mkdir(parents=True, exist_ok=True)
            artifact = _run_step("prepare", lambda: prepare_prod_image(project, service, version, code_ref, str(out_dir)),
                                 f"修正建置問題後重跑：{me}")
            done("prepare", str(artifact))
            tag = _run_step("publish", lambda: publish_prod_image(str(artifact), yes=True),
                            f"檢查 receipt／設定 repo 後重跑：{me}")
            done("publish", str(tag))

        previous = _current_version(project, service, db_path)
        converge(project, service, version, ref, repo, timeout, db_path, done)
        _record_rollout(project, service, version, ref, "healthy", "prod up", db_path)
        report["result"] = "ok"
        return 0
    except StepFailed as exc:
        report["result"] = "failed"
        report["failed_step"] = exc.step
        if exc.step in ("redeploy", "health", "smoke", "verify", "audit-prod", "doctor-final"):
            _record_rollout(project, service, version, ref, "failed", f"{exc.step}: {exc.detail}"[:300], db_path)
        print(f"[FAIL] 步驟 {exc.step}：{exc.detail}")
        print(f"[NEXT] {exc.next_cmd}")
        if rollback_on_fail and exc.step in ("redeploy", "health", "smoke", "verify", "audit-prod") and previous and previous != version:
            from container_ctl.cli.prod_rollback import prod_rollback
            if _confirm_rollback(project, service, previous, version):
                print(f"[AUTO-ROLLBACK] 回到實際運行的 {previous}")
                rc = prod_rollback(project, service, previous, ref=ref, yes=True, timeout=timeout, db_path=db_path)
                report["rollback"] = {"to": previous, "rc": rc}
            else:
                print(f"[SKIP] 未自動回滾（未確認或非互動環境）；手動：container-ctl prod rollback {project} {service} --to {previous}")
        elif rollback_on_fail and exc.step in ("redeploy", "health", "smoke", "verify", "audit-prod") and not previous:
            print("[SKIP] 部署前找不到可驗證的運行版本（運行映像對不上任何 release tag），不自動回滾；請人工判斷")
        return 1
    finally:
        report["finished"] = datetime.now(timezone.utc).isoformat()
        try:
            rdir = REPORT_ROOT / project
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / f"prod-up-{service}-{version}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def main_entry(args) -> None:
    sys.exit(prod_up(args.project, args.service, args.ver, args.code_ref, args.ref, args.yes, args.timeout,
                     rollback_on_fail=getattr(args, "rollback_on_fail", False),
                     no_snapshot=getattr(args, "no_snapshot", False)))
