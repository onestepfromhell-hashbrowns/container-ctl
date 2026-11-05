"""
TICKET-016: SSH 生命週期 Hooks
在 SSH 登入/登出時通知生命週期管理器，寫入 lifecycle_events。
- 登出通知非同步（不阻塞 SSH 登出流程）
- 不直接停止外部依賴服務（交由 Ref Count + lifecycle_policy 決定）
"""
import subprocess
import sys
from datetime import datetime, timezone

from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl import paths


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def notify_connect(project_name: str, db_path: str = DEFAULT_DB_PATH, ledger: any = None) -> None:
    """
    SSH 登入時通知：寫入 ssh_connect 事件，更新實例狀態為 active（嚴格跳過 paused 實例，不假稱 running）。
    同步執行（登入時允許短暫等待）。
    """
    import json
    from container_ctl.governance_port import paused_stable_ids
    paused_ids: set[str] = paused_stable_ids(ledger)

    conn = init_db(db_path)
    try:
        now = _now_iso()
        # 取得該專案所有實例
        instances = conn.execute(
            """
            SELECT id, service_name, blueprint FROM instances
            WHERE project_name = ? AND status NOT IN ('destroyed')
            """,
            (project_name,),
        ).fetchall()

        for inst in instances:
            cfg = {}
            if "blueprint" in inst.keys() and inst["blueprint"]:
                try:
                    cfg = json.loads(inst["blueprint"])
                except Exception:
                    cfg = {}
            stable_id = (
                cfg.get("stable_id")
                or (cfg.get("governance", {}).get("id") if isinstance(cfg.get("governance"), dict) else None)
                or (cfg.get("labels", {}).get(f"{paths.LABEL_NS}.governance.id") if isinstance(cfg.get("labels"), dict) else None)
                or inst["service_name"]
            )

            is_paused = (stable_id in paused_ids)

            conn.execute(
                """
                INSERT INTO lifecycle_events
                    (instance_id, event_type, triggered_by, detail, occurred_at)
                VALUES (?, 'ssh_connect', 'ssh_lifecycle', ?, ?)
                """,
                (inst["id"], f"SSH connect to {project_name}" + (" (paused, preserved)" if is_paused else ""), now),
            )

            if not is_paused:
                conn.execute(
                    "UPDATE instances SET status='running', updated_at=? WHERE id=?",
                    (now, inst["id"]),
                )

        conn.commit()
    finally:
        conn.close()


def notify_disconnect(
    project_name: str,
    db_path: str = DEFAULT_DB_PATH,
    idle_timeout_sec: int | None = None,
) -> None:
    """
    SSH 登出時通知：非同步寫入 ssh_disconnect 事件，啟動 idle 計時器。
    使用 subprocess.Popen（detach）確保不阻塞 SSH 登出流程。
    """
    # 使用非同步 subprocess 執行實際寫入，避免阻塞
    cmd = [
        sys.executable, "-c",
        _disconnect_script(project_name, db_path, idle_timeout_sec),
    ]
    subprocess.Popen(
        cmd,
        start_new_session=True,   # 脫離 SSH session
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _disconnect_script(
    project_name: str,
    db_path: str,
    idle_timeout_sec: int | None,
) -> str:
    """生成非同步執行的 Python 腳本字串。"""
    return f"""
import sqlite3, time
from datetime import datetime, timezone

db_path = {db_path!r}
project_name = {project_name!r}
idle_timeout_sec = {idle_timeout_sec!r}

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
now = datetime.now(timezone.utc).isoformat()

instances = conn.execute(
    "SELECT id, blueprint FROM instances WHERE project_name=? AND status NOT IN ('destroyed')",
    (project_name,)
).fetchall()

import json
for inst in instances:
    conn.execute(
        "INSERT INTO lifecycle_events (instance_id, event_type, triggered_by, detail, occurred_at) VALUES (?, 'ssh_disconnect', 'ssh_lifecycle', ?, ?)",
        (inst['id'], f"SSH disconnect from {project_name!r}", now)
    )
    # 若 lifecycle_policy.mode = auto_idle，啟動 idle 計時
    try:
        bp = json.loads(inst['blueprint']) if inst['blueprint'] else {{}}
    except Exception:
        bp = {{}}
    lp = bp.get('lifecycle_policy', {{}})
    mode = lp.get('mode', 'manual')
    timeout = idle_timeout_sec or lp.get('idle_timeout_sec')
    if mode == 'auto_idle' and timeout:
        conn.execute(
            "UPDATE instances SET status='idle', updated_at=? WHERE id=?",
            (now, inst['id'])
        )
        conn.commit()
        time.sleep(timeout)
        # Re-check：若 idle timeout 後仍為 idle，更新為 stopped（交由外部策略決定是否停止）
        still_idle = conn.execute(
            "SELECT status FROM instances WHERE id=?", (inst['id'],)
        ).fetchone()
        if still_idle and still_idle['status'] == 'idle':
            conn.execute(
                "INSERT INTO lifecycle_events (instance_id, event_type, triggered_by, detail, occurred_at) VALUES (?, 'idle_timeout', 'ssh_lifecycle', 'auto_idle timeout expired', ?)",
                (inst['id'], datetime.now(timezone.utc).isoformat())
            )

conn.commit()
conn.close()
"""


def _write_disconnect_sync(project_name: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """
    同步版本的 disconnect 通知（供測試使用）。
    """
    conn = init_db(db_path)
    try:
        now = _now_iso()
        instances = conn.execute(
            """
            SELECT id FROM instances
            WHERE project_name = ? AND status NOT IN ('destroyed')
            """,
            (project_name,),
        ).fetchall()

        for inst in instances:
            conn.execute(
                """
                INSERT INTO lifecycle_events
                    (instance_id, event_type, triggered_by, detail, occurred_at)
                VALUES (?, 'ssh_disconnect', 'ssh_lifecycle', ?, ?)
                """,
                (inst["id"], f"SSH disconnect from {project_name}", now),
            )

        conn.commit()
    finally:
        conn.close()
