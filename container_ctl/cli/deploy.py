"""
TICKET-007, TICKET-008, TICKET-009: deploy plan / apply / stop / destroy
Tracer Bullet 終點：端對端 Bare Repo → Blueprint → Quadlet → systemd → SQLite
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from container_ctl.blueprint_gen import generate_blueprint
from container_ctl.dag_resolver import DeployPlan, ServiceAction, resolve
from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl.preflight import run_preflight
from container_ctl.quadlet_gen import DEFAULT_SERVICES_DIR, generate_units, find_container_unit_drift, _inst_id, _db_instance_id
from container_ctl.release_anchor import check_release_anchor
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import list_container_configs, read_manifest, repo_path_for
from container_ctl.live_drift import find_live_drift, find_quadlet_dropins, inspect_container
from container_ctl.volume_resolver import ensure_volume_storage, validate_volumes
from container_ctl.lifecycle_adapter import (
    get_default_manager,
    check_plan_admission,
    is_workload_paused,
    precise_start_workload,
    resolve_action_stable_id,
)

# ─── ANSI 顏色 ────────────────────────────────────────────────
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"

_ACTION_COLOR = {
    "NEW": _GREEN,
    "REUSE": _CYAN,
    "CONFLICT": _RED,
}


def _fmt_action(action: str) -> str:
    return f"{_ACTION_COLOR.get(action, '')}{action:<10}{_RESET}"


# ─── Plan Preview ─────────────────────────────────────────────

def plan(
    project_name: str,
    ref: str,
    repos_dir: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    output_json: bool = False,
    tier: str | None = None,
) -> DeployPlan:
    """
    讀取宣告、解析依賴、生成 Blueprint，輸出 Preview。
    不執行任何 systemd 或 Podman 操作。
    """
    repo, _kind = resolve_deploy_repo(project_name, repos_dir)

    manifest = read_manifest(repo, ref)
    configs = list_container_configs(repo, ref, tier=tier)

    conn = init_db(db_path)
    from container_ctl.reconcile import reconcile_instances
    reconcile_instances(conn, verbose=False)
    deploy_plan = resolve(manifest, configs, conn)
    conn.close()

    blueprint_path = generate_blueprint(deploy_plan)

    if output_json:
        print(json.dumps({
            "project": deploy_plan.project_name,
            "version": deploy_plan.version,
            "blueprint": blueprint_path,
            "actions": [
                {
                    "name": a.name,
                    "action": a.action,
                    "scope": a.config.get("scope", "dedicated"),
                    "existing_instance_id": a.existing_instance_id,
                }
                for a in deploy_plan.actions
            ],
        }, indent=2, ensure_ascii=False))
    else:
        _print_plan_preview(deploy_plan, blueprint_path)

    return deploy_plan


def _print_plan_preview(plan: DeployPlan, blueprint_path: str) -> None:
    print(f"\n{_CYAN}Project:{_RESET} {plan.project_name}  ref: {plan.version}")
    print(f"{_CYAN}Blueprint:{_RESET} {blueprint_path}\n")
    print(f"{'Service':<20} {'Tier':<8} {'Scope':<12} {'Action':<12} Detail")
    print("─" * 72)
    for a in plan.actions:
        tier = a.config.get("tier", "prod")
        scope = a.config.get("scope", "dedicated")
        detail = ""
        if a.action == "REUSE":
            detail = f"複用: {a.existing_instance_id}"
        elif a.action == "CONFLICT":
            detail = f"{_RED}⚠ 版本衝突: 現有={a.existing_version}{_RESET}"
        print(f"{a.name:<20} {tier:<8} {scope:<12} {_fmt_action(a.action)} {detail}")
    print()
    if plan.has_conflict:
        print(f"{_RED}⚠ 存在版本衝突，apply 時需要明確確認。{_RESET}")


def _plan_or_exit(*args, **kwargs) -> DeployPlan:
    """宣告不合格（缺檔、未錨定 digest）是使用者可修的輸入問題：給一行錯誤，不丟 traceback。"""
    from container_ctl.repo_reader import DeployConfigNotFoundError, PinnedDigestRequiredError
    try:
        return plan(*args, **kwargs)
    except (DeployConfigNotFoundError, PinnedDigestRequiredError) as exc:
        print(f"{_RED}[ERROR] {exc}{_RESET}\n  提示：用 --tier 只處理要部署的 tier。", file=sys.stderr)
        sys.exit(1)


# ─── Apply ────────────────────────────────────────────────────

def apply(
    project_name: str,
    ref: str,
    yes: bool = False,
    repos_dir: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    services_dir: str = DEFAULT_SERVICES_DIR,
    tier: str | None = None,
) -> None:
    """
    Tracer Bullet 主流程：
    Preflight → Plan → 確認 → Quadlet 生成 → 非阻塞啟動 → SQLite 記錄
    """
    # 1. Preflight
    print("[1/5] 執行環境預檢...", file=sys.stderr)
    if not run_preflight(db_path):
        print(f"{_RED}[ABORT] Preflight 失敗，請修復後重試。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 1b. TICKET-021: Reconcile — 偵測並修復 SQLite vs 實際容器的 DRIFT
    print("[1b] Pod 殘留偵測 (reconcile)...", file=sys.stderr)
    from container_ctl.reconcile import reconcile_instances
    _rc = init_db(db_path)
    reconcile_instances(_rc, verbose=True)
    _rc.close()

    # 2. Plan
    print("[2/5] 解析依賴並生成 Blueprint...", file=sys.stderr)
    deploy_plan = _plan_or_exit(project_name, ref, repos_dir=repos_dir, db_path=db_path, tier=tier)

    if deploy_plan.project_name != project_name:
        print(f"{_RED}[ERROR] CLI 專案名稱 ({project_name}) 與 Manifest 宣告名稱 ({deploy_plan.project_name}) 不一致！{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 2b. TICKET-VOL-01: Blueprint Volume 預檢 (純查詢無副作用)
    print("[2b] 執行 Volume 預檢...", file=sys.stderr)
    if not run_preflight(db_path, blueprints=[a.config for a in deploy_plan.actions], project=deploy_plan.project_name):
        print(f"{_RED}[ABORT] Volume 預檢失敗，請檢查配置後重試。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 2c0. dev 前置：host prepare／secret provision／db provision（--yes 時；只處理 tier=dev）
    from container_ctl.dev_provision import run_dev_provision
    if not run_dev_provision(project_name, ref, [a.config for a in deploy_plan.actions], yes):
        sys.exit(1)

    # 2c. 治理准入與暫停門禁 (Pre-mutation Gate: 必須在任何檔案/目錄寫入前核驗)
    admitted, deny_reasons = check_plan_admission(deploy_plan)
    if not admitted:
        for r in deny_reasons:
            print(f"{_RED}[ABORT 治理拒絕] {r}{_RESET}", file=sys.stderr)
        print(f"{_RED}[ABORT] 計畫中存在處於治理暫停狀態之工作負載，即使 --yes 亦中止 apply。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 固定 dev 名稱不能由 apply 疊加在既有實例上；改版應走 redeploy。
    conn = init_db(db_path)
    active_dev = conn.execute(
        """SELECT service_name FROM instances
           WHERE project_name = ? AND tier = 'dev'
             AND status NOT IN ('stopped', 'destroyed')""",
        (deploy_plan.project_name,),
    ).fetchall()
    conn.close()
    active_names = {row["service_name"] for row in active_dev}
    if any(a.config.get("tier") == "dev" and a.name in active_names
           and a.action != "REUSE" for a in deploy_plan.actions):
        print(f"{_RED}[ABORT] 既有 dev 實例仍在運行，請使用 deploy redeploy 遷移固定名稱。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 3. 確認
    if deploy_plan.has_conflict and yes:
        print(f"{_RED}[ERROR] 存在版本衝突，即使 --yes 也不允許自動繼續。請手動確認。{_RESET}", file=sys.stderr)
        sys.exit(1)

    if not yes:
        try:
            answer = input("確認執行 apply？(y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("[CANCELLED] 已取消 apply。")
            return

    # 3b. 實體建立 Volume 儲存目錄 (Post-confirmation Provisioning)
    _, _, resolved_vols = validate_volumes([a.config for a in deploy_plan.actions], project=deploy_plan.project_name)
    ensure_ok, ensure_msgs = ensure_volume_storage(resolved_vols)
    if not ensure_ok:
        for msg in ensure_msgs:
            print(msg, file=sys.stderr)
        print(f"{_RED}[ABORT] Volume 目錄補建失敗，中止 apply。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 4. Quadlet 生成
    print("[3/5] 生成 Quadlet 單元...", file=sys.stderr)
    container_map = generate_units(deploy_plan, services_dir)

    # 5. 非阻塞啟動（DAG 拓撲順序）
    print("[4/5] 非阻塞啟動服務...", file=sys.stderr)
    conn = init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    for action in deploy_plan.actions:
        inst = _inst_id(action, deploy_plan.project_name, deploy_plan.version)
        cname = container_map.get(action.name, f"svc-{inst}")
        service_unit = f"{cname}.service"

        stable_id = resolve_action_stable_id(action)

        if action.action == "REUSE":
            paused, pause_rec = is_workload_paused(stable_id)
            if paused and pause_rec is not None:
                print(f"{_RED}[ABORT] 共享服務 '{stable_id}' 處於治理暫停狀態 (incident: {pause_rec.incident_id})，拒絕 REUSE 復用！{_RESET}", file=sys.stderr)
                sys.exit(1)
            _update_shared_ref(conn, action, deploy_plan.project_name, +1, now)
            _log_event(conn, action.existing_instance_id, "reuse",
                       "deploy_apply", {"project": deploy_plan.project_name}, now)
            print(f"  ↩ REUSE {action.name} ({cname})")
            continue

        lifecycle_mode = action.config.get("lifecycle_policy", {}).get("mode", "always_on")
        started = False
        denied_reason = None
        if service_unit and lifecycle_mode != "manual":
            owner_uid = action.config.get("owner_uid", 1000)
            allowed, start_res, reason = precise_start_workload(stable_id, service_unit, owner_uid=owner_uid)
            if not allowed:
                denied_reason = reason
                started = False
            else:
                started = start_res.job_submitted
        else:
            started = False

        instance_id = _db_instance_id(action, deploy_plan.project_name, deploy_plan.version)
        if denied_reason:
            inst_status = "stopped"
        else:
            inst_status = "provisioned" if started else ("stopped" if lifecycle_mode == "manual" else "error")
        _write_instance(conn, instance_id, deploy_plan, action, cname,
                        services_dir, inst_status, now)
        _log_event(conn, instance_id, "provision", "deploy_apply",
                   {
                       "container_name": cname,
                       "started": started,
                       "mode": lifecycle_mode,
                       "denied_reason": denied_reason,
                   }, now)

        if action.config.get("scope") == "shared":
            _upsert_shared_ref(conn, action, deploy_plan.project_name, deploy_plan.version, now)

        if lifecycle_mode == "manual":
            status_icon = f"{_YELLOW}⏸{_RESET}"
            print(f"  {status_icon} {action.name} → {cname} (manual, 未自動啟動)")
        else:
            status_icon = f"{_GREEN}✓{_RESET}" if started else f"{_RED}✗{_RESET}"
            print(f"  {status_icon} {action.name} → {cname}")

    conn.commit()
    conn.close()

    print(f"[5/5] {_GREEN}deploy apply 完成。{_RESET}")


# ─── Stop ─────────────────────────────────────────────────────

def stop(
    project_name: str,
    yes: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """
    TICKET-009: 優雅停止 <project> 的所有服務。
    dedicated 服務停止；shared 服務 ref_count -1。
    """
    from container_ctl.graceful_stop import graceful_stop

    if not yes:
        try:
            answer = input(f"確認停止 '{project_name}' 所有服務？(y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("[CANCELLED] 已取消 stop。")
            return

    ok = graceful_stop(project_name, scope="project", db_path=db_path)
    if not ok:
        sys.exit(1)


# ─── Destroy ──────────────────────────────────────────────────

def destroy(
    project_name: str,
    yes: bool = False,
    db_path: str = DEFAULT_DB_PATH,
    services_dir: str = DEFAULT_SERVICES_DIR,
) -> None:
    """
    TICKET-009: stop + 移除 Quadlet 單元 + 標記 destroyed（不刪除 Volume 資料）。
    """
    import shutil
    from pathlib import Path
    from container_ctl.graceful_stop import graceful_stop

    if not yes:
        try:
            answer = input(
                f"確認銷毀 '{project_name}'？Quadlet 單元將被移除（Volume 保留）。(y/N): "
            ).strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("[CANCELLED] 已取消 destroy。")
            return

    # 1. 優雅停止
    print("[1/3] 優雅停止服務...", file=sys.stderr)
    graceful_stop(project_name, scope="project", db_path=db_path)

    # 2. 移除 Quadlet 單元目錄
    print("[2/3] 移除 Quadlet 單元...", file=sys.stderr)
    project_services_dir = Path(services_dir) / project_name
    if project_services_dir.exists():
        # ext-default.json 是人宣告的專案延伸入口，不是生成物；銷毀容器不該連它一起刪。
        for item in project_services_dir.iterdir():
            if item.name == "ext-default.json":
                continue
            shutil.rmtree(item) if item.is_dir() and not item.is_symlink() else item.unlink()
        print(f"  [OK] 已移除 {project_services_dir} 內的生成單元（保留 ext-default.json）")
    else:
        print(f"  [INFO] {project_services_dir} 不存在，跳過。")

    get_default_manager().daemon_reload()

    # 3. SQLite 更新 status=destroyed
    print("[3/3] 更新 SQLite 狀態...", file=sys.stderr)
    conn = init_db(db_path)
    now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT id FROM instances WHERE project_name = ? AND status != 'destroyed'",
        (project_name,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE instances SET status = 'destroyed', updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.execute(
            """
            INSERT INTO lifecycle_events
              (instance_id, event_type, triggered_by, detail, occurred_at)
            VALUES (?,?,?,?,?)
            """,
            (row["id"], "destroy", "deploy_destroy",
             '{"note": "quadlet_units_removed"}', now),
        )
    conn.commit()
    conn.close()

    print(f"\n{_GREEN}[OK] '{project_name}' 已銷毀（Volume 資料保留）。{_RESET}")


# ─── SQLite 寫入輔助 ───────────────────────────────────────────

def _write_instance(conn, instance_id, plan, action, cname, services_dir, status, now):
    import json as _json
    # Correct legacy rows that used the manifest version for an explicitly
    # versioned service. A container must have one current instance record.
    conn.execute(
        "UPDATE instances SET status='destroyed', updated_at=? "
        "WHERE project_name=? AND service_name=? AND container_name=? AND id!=?",
        (now, plan.project_name, action.name, cname, instance_id),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO instances
          (id, project_name, service_name, version, tier, scope,
           status, blueprint, quadlet_path, container_name, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            instance_id,
            plan.project_name,
            action.name,
            action.config.get("version") or plan.version,
            action.config.get("tier", "prod"),
            action.config.get("scope", "dedicated"),
            status,
            _json.dumps(action.config, ensure_ascii=False),
            f"{services_dir}/{plan.project_name}",
            cname,
            now,
            now,
        ),
    )


def _upsert_shared_ref(conn, action, project, version, now):
    lp = action.config.get("lifecycle_policy", {})
    conn.execute(
        """
        INSERT INTO shared_refs
          (service_id, service_name, version, ref_count,
           lifecycle_mode, idle_timeout_sec, status)
        VALUES (?,?,?,1,?,?,?)
        ON CONFLICT(service_id) DO UPDATE SET
          ref_count = ref_count + 1,
          version = excluded.version,
          status = 'running'
        """,
        (
            _inst_id(action, project, version),
            action.name,
            action.config.get("version") or version,
            lp.get("mode", "auto_idle"),
            lp.get("idle_timeout_sec", 300),
            "running",
        ),
    )


def _update_shared_ref(conn, action, project_name, delta, now):
    sid = action.existing_instance_id
    if sid:
        conn.execute(
            "UPDATE shared_refs SET ref_count = MAX(0, ref_count + ?) WHERE service_id = ?",
            (delta, sid),
        )


def _log_event(conn, instance_id, event_type, triggered_by, detail, now):
    import json as _json
    conn.execute(
        """
        INSERT INTO lifecycle_events
          (instance_id, event_type, triggered_by, detail, occurred_at)
        VALUES (?,?,?,?,?)
        """,
        (instance_id, event_type, triggered_by,
         _json.dumps(detail, ensure_ascii=False), now),
    )


# ─── Redeploy ─────────────────────────────────────────────────

def redeploy(
    project_name: str,
    new_ref: str,
    yes: bool = False,
    only: list[str] | None = None,
    repos_dir: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    services_dir: str = DEFAULT_SERVICES_DIR,
    tier: str | None = None,
) -> None:
    """
    TICKET-011: 熱重載 — 讀取新版 Blueprint，差異比對，只重啟有變更的服務。
    支援回滾（傳入舊 ref 即可）。
    only: 只處理指定服務（其餘服務視為不在本次範圍，不比對、不生成、不重啟）。
    """
    # 1. 讀取新版 Blueprint
    print(f"[1/4] 解析新版 Blueprint ({new_ref})...", file=sys.stderr)
    repo, _kind = resolve_deploy_repo(project_name, repos_dir)
    manifest = read_manifest(repo, new_ref)
    from container_ctl.repo_reader import DeployConfigNotFoundError, PinnedDigestRequiredError
    try:
        configs = list_container_configs(repo, new_ref, tier=tier)
    except (DeployConfigNotFoundError, PinnedDigestRequiredError) as exc:
        print(f"{_RED}[ERROR] {exc}{_RESET}\n  提示：用 --tier 或 --service 只處理要部署的範圍。", file=sys.stderr)
        sys.exit(1)

    conn = init_db(db_path)
    new_plan = resolve(manifest, configs, conn)
    conn.close()

    if new_plan.project_name != project_name:
        print(f"{_RED}[ERROR] CLI 專案名稱 ({project_name}) 與 Manifest 宣告名稱 ({new_plan.project_name}) 不一致！{_RESET}", file=sys.stderr)
        sys.exit(1)

    new_blueprint_path = generate_blueprint(new_plan)

    if only:
        unknown = sorted(set(only) - {a.name for a in new_plan.actions})
        if unknown:
            print(f"{_RED}[ERROR] --service 指定的服務不在宣告中：{', '.join(unknown)}{_RESET}", file=sys.stderr)
            sys.exit(1)
        new_plan = DeployPlan(new_plan.project_name, new_plan.version,
                              [a for a in new_plan.actions if a.name in set(only)])

    # 1b. TICKET-VOL-01: Blueprint Volume 預檢 (純查詢無副作用)
    if not run_preflight(db_path, blueprints=[a.config for a in new_plan.actions], project=new_plan.project_name):
        print(f"{_RED}[ABORT] Volume 預檢失敗，請檢查配置後重試。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 1c. dev 前置（--yes 時自動執行；prod 由 prod up 負責）
    from container_ctl.dev_provision import run_dev_provision
    if not run_dev_provision(project_name, new_ref, [a.config for a in new_plan.actions], yes):
        sys.exit(1)

    # 2. 讀取現有實例（舊版）作為對比基礎
    conn = init_db(db_path)
    old_rows = conn.execute(
        """
        SELECT service_name, version, blueprint, container_name, status, tier
        FROM instances
        WHERE project_name = ?
          AND status != 'destroyed'
        ORDER BY updated_at
        """,
        (project_name,),
    ).fetchall()
    conn.close()

    old_services: dict[str, dict] = {}
    for r in old_rows:
        try:
            cfg = json.loads(r["blueprint"]) if r["blueprint"] else {}
        except Exception:
            cfg = {}
        old_services[r["service_name"]] = {
            "version": r["version"],
            "config": cfg,
            "container_name": r["container_name"],
            "status": r["status"],
        }
        if tier and r["tier"] != tier:      # 範圍外的 tier 不比對，否則會被誤判 REMOVED
            del old_services[r["service_name"]]

    if only:
        old_services = {k: v for k, v in old_services.items() if k in set(only)}

    # 3. 差異比對
    diff: list[tuple[str, str]] = []  # (name, "NEW"|"CHANGED"|"UNCHANGED"|"REMOVED")
    new_names = {a.name for a in new_plan.actions}

    for action in new_plan.actions:
        if action.name not in old_services:
            diff.append((action.name, "NEW"))
        elif (
            json.dumps(action.config, sort_keys=True) !=
            json.dumps(old_services[action.name]["config"], sort_keys=True)
            or (action.config.get("version") or new_plan.version) != old_services[action.name]["version"]
        ):
            diff.append((action.name, "CHANGED"))
        elif (old_services[action.name].get("status") == "error"
              and action.config.get("lifecycle_policy", {}).get("mode", "always_on") != "manual"):
            # 記錄先於收斂：上次啟動失敗的實例還沒達到宣告狀態，不能因 blueprint 相同就當作完成。
            diff.append((action.name, "CHANGED"))
        else:
            diff.append((action.name, "UNCHANGED"))

    for old_name in old_services:
        if old_name not in new_names:
            diff.append((old_name, "REMOVED"))

    # Blueprint equality alone cannot detect a missing or edited Quadlet.
    drifted = find_container_unit_drift(new_plan, services_dir)
    diff = [(name, "DRIFT" if status == "UNCHANGED" and name in drifted else status)
            for name, status in diff]

    # 運行中容器 vs 宣告：state.db 已更新但容器沒被重啟過時，blueprint 比對看不出差異
    live_reasons: dict[str, list[str]] = {}
    # 延伸掛載由 ext-default.json 在生成 Quadlet 時展開，不在宣告的 volumes 裡；容器只在建立時取得掛載，
    # 所以要與運行中的容器對一次，否則「Quadlet 已含掛載、容器沒有」會被判成 UNCHANGED。
    from container_ctl.quadlet_gen import resolve_extension_volumes
    ext_volumes = resolve_extension_volumes(project_name, services_dir, read_only=True)
    for action in new_plan.actions:
        status = dict(diff).get(action.name)
        cname_live = old_services.get(action.name, {}).get("container_name", "")
        if status not in ("UNCHANGED", "DRIFT") or not cname_live:
            continue
        live = inspect_container(cname_live)
        if not live or not (live.get("State") or {}).get("Running"):
            continue
        reasons = find_live_drift(action.config, live, ext_volumes)
        if reasons:
            live_reasons[action.name] = reasons
    for action in new_plan.actions:
        cn = old_services.get(action.name, {}).get("container_name")
        for conf in (find_quadlet_dropins(f"{services_dir}/{project_name}/{cn}.container")
                     + find_quadlet_dropins(f"{os.path.expanduser('~/.config/containers/systemd')}/{cn}.container") if cn else []):
            print(f"[WARN] {action.name}: 受管 Quadlet 有手工 drop-in {conf}（繞過宣告，SPEC §23.4）→ 把內容遷入宣告後移除")
    manual_live = {n for n in live_reasons
                   if next(a for a in new_plan.actions if a.name == n).config.get("lifecycle_policy", {}).get("mode", "always_on") == "manual"}
    diff = [(n, "LIVE-DRIFT" if (s in ("UNCHANGED", "DRIFT") and n in live_reasons and n not in manual_live) else s)
            for n, s in diff]

    # 3b. 宿主埠衝突（舊實例名稱對不上時不會被停，新單元會 address already in use）
    from container_ctl.port_conflict import find_port_conflicts, podman_ps, precheck_own_names
    from container_ctl.quadlet_gen import _container_name as _new_unit_name
    port_conflicts: dict[str, list[str]] = {}
    ps_now = None
    for action in new_plan.actions:
        if dict(diff).get(action.name) in (None, "UNCHANGED") or not action.config.get("ports"):
            continue
        ps_now = podman_ps() if ps_now is None else ps_now
        own, stop_notice = precheck_own_names(old_services.get(action.name, {}).get("container_name", ""),
                                              _new_unit_name(action, project_name, new_plan.version), action.config)
        if stop_notice:
            print(f"[STOP-FIRST] {action.name}: {stop_notice}")
        found = find_port_conflicts(action.config["ports"], own, ps_now)
        if found:
            port_conflicts[action.name] = found
    for name, found in port_conflicts.items():
        print(f"{_RED}[PORT-CONFLICT]{_RESET} {name}: " + "；".join(found)
              + "；佔用者不是本服務的舊單元，工具不會代停：請先確認它是誰、由其負責人處理後再 redeploy", file=sys.stderr)
    if port_conflicts and yes:
        print(f"{_RED}[ABORT] 宿主埠衝突，未做任何變更。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 4. 顯示 diff preview
    print(f"\n{_CYAN}Redeploy:{_RESET} {project_name}  new-ref: {new_ref}")
    print(f"{_CYAN}Blueprint:{_RESET} {new_blueprint_path}\n")
    print(f"{'Service':<20} {'Diff':<12} Detail")
    print("─" * 56)
    diff_color = {
        "NEW": _GREEN, "CHANGED": _YELLOW,
        "UNCHANGED": _RESET, "DRIFT": _YELLOW, "LIVE-DRIFT": _YELLOW, "REMOVED": _RED,
    }
    for name, status in diff:
        color = diff_color.get(status, _RESET)
        print(f"{name:<20} {color}{status:<12}{_RESET}")
    print()
    for name, reasons in live_reasons.items():
        tag = "僅回報（manual 不自動重啟，避免中斷手動啟動的應用）" if name in manual_live else "將重啟以套用宣告"
        print(f"[LIVE-DRIFT] {name}（{tag}）：" + "；".join(reasons[:4]))
    if live_reasons:
        print()

    changed = [name for name, s in diff if s in ("NEW", "CHANGED", "LIVE-DRIFT")]
    repair = [name for name, s in diff if s == "DRIFT"]
    removed = [name for name, s in diff if s == "REMOVED"]

    # prod 版本錨定（嚴格）：新增／變更的 prod 服務須有 release tag，且 digest 與凍結記錄一致
    gated = [c for c in configs if c.get("name") in changed and c.get("tier") == "prod"]
    anchor_ok, anchor_msgs = check_release_anchor(repo, gated, str(new_plan.version), strict=True)
    for msg in anchor_msgs:
        print(msg, file=sys.stderr)
    if not anchor_ok:
        print(f"{_RED}[ABORT] prod 版本錨定檢查失敗；請先 image prepare-prod／publish-prod（或 adopt-prod）。{_RESET}", file=sys.stderr)
        sys.exit(1)

    if not changed and not repair and not removed:
        print("[INFO] 無任何變更，redeploy 跳過。")
        return

    # 4b. 治理准入與暫停門禁 (Pre-mutation Gate)
    changed_actions = [a for a in new_plan.actions if a.name in changed or a.name in repair]
    for action in changed_actions:
        stable_id = resolve_action_stable_id(action)
        paused, pause_rec = is_workload_paused(stable_id)
        if paused and pause_rec is not None:
            print(
                f"{_RED}[ABORT 治理拒絕] 工作負載 '{stable_id}' 處於治理暫停狀態 "
                f"(incident: {pause_rec.incident_id}, reason: {pause_rec.reason})，即使 --yes 亦中止 redeploy。{_RESET}",
                file=sys.stderr,
            )
            sys.exit(1)

    # 5. 確認
    if not yes:
        try:
            answer = input("確認執行 redeploy？(y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("[CANCELLED] 已取消 redeploy。")
            return

    # 5b. 實體建立 Volume 儲存目錄 (Post-confirmation Provisioning)
    _, _, resolved_vols = validate_volumes([a.config for a in new_plan.actions], project=new_plan.project_name)
    ensure_ok, ensure_msgs = ensure_volume_storage(resolved_vols)
    if not ensure_ok:
        for msg in ensure_msgs:
            print(msg, file=sys.stderr)
        print(f"{_RED}[ABORT] Volume 目錄補建失敗，中止 redeploy。{_RESET}", file=sys.stderr)
        sys.exit(1)

    # 6. 生成新 Quadlet 單元（只針對 CHANGED / NEW）
    print("[3/4] 生成 Quadlet 單元...", file=sys.stderr)
    container_map = generate_units(new_plan, services_dir)

    # 7. 重啟有變更的服務
    print("[4/4] 重啟有變更的服務...", file=sys.stderr)
    conn = init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    for action in new_plan.actions:
        if action.name not in changed:
            if action.name in repair:
                print(f"  ✓ {action.name} (DRIFT, Quadlet 已修復；未變更服務狀態)")
            else:
                print(f"  — {action.name} (UNCHANGED, 跳過)")
            continue

        cname = container_map.get(action.name, "")
        service_unit = f"{cname}.service" if cname else ""

        stable_id = resolve_action_stable_id(action)
        lifecycle_mode = action.config.get("lifecycle_policy", {}).get("mode", "always_on")
        started = False
        denied_reason = None
        old_cname = old_services.get(action.name, {}).get("container_name", "")

        if service_unit and lifecycle_mode != "manual":
            owner_uid = action.config.get("owner_uid", 1000)
            from container_ctl.port_conflict import needs_stop_first
            if needs_stop_first(old_cname, cname, action.config):
                print(f"  ⏸ {action.name}: 先停舊單元 {old_cname}（固定宿主埠，新舊不可並存；此期間服務中斷）")
                get_default_manager().stop_unit_blocking(f"{old_cname}.service")
            allowed, start_res, reason = precise_start_workload(stable_id, service_unit, owner_uid=owner_uid)
            if not allowed:
                denied_reason = reason
                started = False
            else:
                # 固定名稱的 dev 單元需要顯式重啟；start 對已運行單元不會套用新配置。
                if old_cname and old_cname != cname:
                    get_default_manager().stop_unit_blocking(f"{old_cname}.service")
                # 同名單元（dev，或 prod 同版本改設定）：start 對已運行單元不會套用新配置，需顯式重啟
                if old_cname and (action.config.get("tier") == "dev" or old_cname == cname):
                    started = get_default_manager().restart_unit(service_unit, owner_uid=owner_uid).job_submitted
                else:
                    started = start_res.job_submitted
        else:
            started = False

        if action.config.get("tier") == "dev" and lifecycle_mode == "manual" and old_cname and old_cname != cname:
            get_default_manager().stop_unit_blocking(f"{old_cname}.service")

        instance_id = _db_instance_id(action, project_name, new_plan.version)
        if denied_reason:
            inst_status = "stopped"
        else:
            inst_status = "provisioned" if started else ("stopped" if lifecycle_mode == "manual" else "error")
        _write_instance(conn, instance_id, new_plan, action, cname,
                        services_dir, inst_status, now)
        if action.config.get("scope") == "shared" and started:
            sid = _inst_id(action, project_name, new_plan.version)
            existing_ref = conn.execute("SELECT 1 FROM shared_refs WHERE service_id=?", (sid,)).fetchone()
            if not existing_ref:
                _upsert_shared_ref(conn, action, project_name, new_plan.version, now)
        if action.config.get("tier") == "dev" and not denied_reason and (started or lifecycle_mode == "manual"):
            conn.execute(
                """UPDATE instances SET status = 'stopped', updated_at = ?
                   WHERE project_name = ? AND service_name = ? AND id != ?
                     AND status != 'destroyed'""",
                (now, project_name, action.name, instance_id),
            )
            if old_cname and old_cname != cname:
                from pathlib import Path
                from container_ctl.quadlet_gen import SYSTEMD_QUADLET_DIR

                old_unit = Path(services_dir) / project_name / f"{old_cname}.container"
                old_link = SYSTEMD_QUADLET_DIR / old_unit.name
                if old_link.is_symlink() and old_link.resolve() == old_unit.resolve():
                    old_link.unlink()
                old_unit.unlink(missing_ok=True)
                get_default_manager().daemon_reload()
        _log_event(conn, instance_id, "redeploy", "deploy_redeploy",
                   {
                       "new_ref": new_ref,
                       "container_name": cname,
                       "started": started,
                       "mode": lifecycle_mode,
                       "denied_reason": denied_reason,
                   }, now)

        if lifecycle_mode == "manual":
            icon = f"{_YELLOW}⏸{_RESET}"
            print(f"  {icon} {action.name} → {cname} (manual, 未自動啟動)")
        else:
            icon = f"{_GREEN}✓{_RESET}" if started else f"{_RED}✗{_RESET}"
            print(f"  {icon} {action.name} → {cname}")

    # 停止並標記 REMOVED 服務
    for name in removed:
        old_info = old_services.get(name, {})
        old_cname = old_info.get("container_name", "")
        if old_cname:
            get_default_manager().stop_unit_blocking(f"{old_cname}.service")
        conn.execute(
            """
            UPDATE instances SET status = 'stopped', updated_at = ?
            WHERE project_name = ? AND service_name = ?
            """,
            (now, project_name, name),
        )
        _log_event(conn, f"{project_name}-{name}-{new_plan.version}",
                   "remove", "deploy_redeploy", {"reason": "removed_from_manifest"}, now)
        print(f"  {_RED}✗{_RESET} {name} (REMOVED, 已停止)")

    conn.commit()
    conn.close()
    print(f"\n{_GREEN}[OK] redeploy 完成。{_RESET}")
