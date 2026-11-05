"""
TICKET-012: 優雅停止 DAG 倒序邏輯
依 DAG 拓撲倒序停止，確保資料完整性。
"""
import json
import subprocess

from container_ctl.lifecycle_adapter import get_default_manager
import sys
from datetime import datetime, timezone

from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl import paths

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

# 每個服務停止的超時秒數（超時後 kill -9）
STOP_TIMEOUT_SEC = 30

# Tier 停止優先順序（數字越小越先停）
# 倒序：dev=0 最先停，infra=2 最後停
_TIER_STOP_ORDER = {"dev": 0, "prod": 1, "infra": 2}


def _stop_service(container_name: str, timeout: int = STOP_TIMEOUT_SEC) -> bool:
    """
    systemctl --user stop <service>（阻塞等待）。
    超時後發送 SIGKILL。
    回傳 True 代表成功停止。
    """
    service_unit = f"{container_name}.service"
    try:
        result = get_default_manager().stop_unit_blocking(service_unit, timeout=timeout)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        get_default_manager().kill_unit(service_unit, timeout=5.0)
        return False


def _wal_checkpoint(db_path: str) -> None:
    """停止 prod tier 服務後執行 WAL TRUNCATE checkpoint。"""
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as e:
        print(f"  [WARN] WAL checkpoint 失敗: {e}", file=sys.stderr)


def _log_event(conn, instance_id: str, event_type: str, triggered_by: str,
               detail: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO lifecycle_events
          (instance_id, event_type, triggered_by, detail, occurred_at)
        VALUES (?,?,?,?,?)
        """,
        (instance_id, event_type, triggered_by,
         json.dumps(detail, ensure_ascii=False), now),
    )


def graceful_stop(
    project_name: str,
    scope: str = "project",
    db_path: str = DEFAULT_DB_PATH,
    dry_run: bool = False,
) -> bool:
    """
    優雅停止：依 DAG 拓撲倒序（dev→prod→infra）停止服務。

    scope="project"：只停止 dedicated 服務，shared 服務 ref_count -1，
                      ref_count 歸零且 auto_idle 才停止。
    scope="all"：停止所有（含 shared），忽略 ref_count。
    回傳 True 代表全部成功停止。
    """
    conn = init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        """
        SELECT id, service_name, tier, scope, container_name
        FROM instances
        WHERE project_name = ?
          AND status NOT IN ('stopped', 'destroyed')
        ORDER BY tier
        """,
        (project_name,),
    ).fetchall()

    if not rows:
        print(f"[INFO] 找不到 '{project_name}' 的 active 實例。", file=sys.stderr)
        conn.close()
        return True

    # 依 tier 倒序排（dev=0 先停，infra=2 最後）
    sorted_rows = sorted(
        rows,
        key=lambda r: _TIER_STOP_ORDER.get(r["tier"], 1),
    )

    all_ok = True
    prod_checkpoint_done = False
    step = 1

    for row in sorted_rows:
        instance_id = row["id"]
        service_name = row["service_name"]
        svc_scope = row["scope"]
        container_name = row["container_name"]
        tier = row["tier"]

        # shared 服務在 scope="project" 時只調整 ref_count
        if svc_scope == "shared" and scope == "project":
            shared_row = conn.execute(
                """
                SELECT service_id, ref_count, lifecycle_mode
                FROM shared_refs
                WHERE service_name = ? AND status NOT IN ('stopped', 'destroyed')
                ORDER BY rowid DESC LIMIT 1
                """,
                (service_name,),
            ).fetchone()

            if shared_row:
                new_count = max(0, shared_row["ref_count"] - 1)
                conn.execute(
                    "UPDATE shared_refs SET ref_count = ? WHERE service_id = ?",
                    (new_count, shared_row["service_id"]),
                )
                _log_event(conn, instance_id, "ref_decrement", "graceful_stop",
                           {"new_ref_count": new_count}, now)

                if new_count == 0 and shared_row["lifecycle_mode"] == "auto_idle":
                    print(
                        f"  [{step}] {_YELLOW}IDLE{_RESET} {service_name} "
                        f"(ref_count→0, auto_idle 計時器啟動)"
                    )
                    _log_event(conn, instance_id, "idle_start", "graceful_stop",
                               {"reason": "ref_count_zero"}, now)
                else:
                    print(
                        f"  [{step}] ↩  {service_name} ref_count→{new_count} (shared, 保持運行)"
                    )
            step += 1
            continue

        # dedicated（或 scope="all"）：直接停止
        print(f"  [{step}] 停止 {service_name} ({container_name}) ...", end=" ", flush=True)

        if dry_run:
            print(f"{_YELLOW}[dry-run]{_RESET}")
            step += 1
            continue

        ok = _stop_service(container_name)

        if ok:
            print(f"{_GREEN}OK{_RESET}")
        else:
            print(f"{_RED}FAILED{_RESET}")
            all_ok = False

        conn.execute(
            "UPDATE instances SET status = 'stopped', updated_at = ? WHERE id = ?",
            (now, instance_id),
        )
        _log_event(conn, instance_id, "stop", "graceful_stop",
                   {"ok": ok, "tier": tier}, now)

        # prod tier 停止後執行 WAL checkpoint（只做一次）
        if tier == "prod" and not prod_checkpoint_done:
            _wal_checkpoint(db_path)
            prod_checkpoint_done = True

        step += 1

    conn.commit()
    conn.close()

    if all_ok:
        print(f"\n{_GREEN}[OK] '{project_name}' 所有服務已停止。{_RESET}")
    else:
        print(
            f"\n{_YELLOW}[WARN] 部分服務停止失敗，請檢查 systemd journal。{_RESET}",
            file=sys.stderr,
        )

    return all_ok


def stop_workload_by_stable_id(
    stable_id: str,
    db_path: str = DEFAULT_DB_PATH,
    timeout: int = 5,
    dry_run: bool = False,
) -> bool:
    """Precise stop of a single workload by stable_id (Ticket #10, C06).
    
    Invariants:
    - Stops the exact instance matching stable_id.
    - Shared_refs table is completely untouched.
    - In dry-run mode, zero database mutations and zero process signals.
    - Updates instances status to 'stopped' upon confirmed stop evidence.
    - Zero WAL checkpoints executed on dev emergency shedding.
    """
    from pathlib import Path
    if not Path(db_path).exists():
        return False

    conn = init_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        """
        SELECT id, service_name, project_name, tier, scope, container_name, blueprint
        FROM instances
        WHERE status NOT IN ('stopped', 'destroyed')
        """
    ).fetchall()

    target_row = None
    for r in rows:
        cfg = {}
        if "blueprint" in r.keys() and r["blueprint"]:
            try:
                cfg = json.loads(r["blueprint"])
            except Exception:
                cfg = {}
        row_stable_id = (
            cfg.get("stable_id")
            or (cfg.get("governance", {}).get("id") if isinstance(cfg.get("governance"), dict) else None)
            or (cfg.get("labels", {}).get(f"{paths.LABEL_NS}.governance.id") if isinstance(cfg.get("labels"), dict) else None)
            or r["service_name"]
            or r["container_name"]
        )
        if row_stable_id == stable_id or r["container_name"] == stable_id or r["id"] == stable_id:
            target_row = r
            break

    if not target_row:
        conn.close()
        return False

    container_name = target_row["container_name"]
    instance_id = target_row["id"]
    tier = target_row["tier"]

    if dry_run:
        conn.close()
        return True

    ok = _stop_service(container_name, timeout=timeout)
    if ok:
        conn.execute(
            "UPDATE instances SET status = 'stopped', updated_at = ? WHERE id = ?",
            (now, instance_id),
        )
        _log_event(
            conn,
            instance_id,
            "stop_stable_id",
            "memory_governor",
            {"ok": True, "stable_id": stable_id, "tier": tier},
            now,
        )
        conn.commit()

    conn.close()
    return ok
