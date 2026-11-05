"""
TICKET-021: Pod 殘留偵測 & Startup Reconcile（SPEC §15.1）
在每次 deploy apply 前執行，偵測 SQLite 記錄與實際 Podman 容器間的 DRIFT。
"""
import subprocess

from container_ctl.lifecycle_adapter import get_default_manager
import sys
from datetime import datetime, timezone

from container_ctl.db import DEFAULT_DB_PATH, init_db

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _container_exists(container_name: str) -> bool:
    """使用 Infra 容器 ID 驗證容器是否存在（對應 SPEC §15.1）。"""
    result = subprocess.run(
        ["podman", "container", "exists", container_name],
        capture_output=True,
    )
    return result.returncode == 0


def _reset_failed_unit(container_name: str) -> None:
    """偵測到殘留元資料時自動執行 systemctl --user reset-failed。"""
    get_default_manager().reset_failed(f"{container_name}.service", timeout=5.0)


def reconcile_instances(db_conn, verbose: bool = True) -> int:
    """
    對 SQLite instances 表中 status != 'destroyed' 的所有記錄，
    驗證實際 Podman 容器存在性。
    DRIFT 時更新 status='stopped' 並輸出 [WARN DRIFT]。

    回傳 DRIFT 數量（0 = 環境一致）。
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = db_conn.execute(
        """
        SELECT id, service_name, container_name, status
        FROM instances
        WHERE status NOT IN ('stopped', 'destroyed')
        """
    ).fetchall()

    drift_count = 0
    for row in rows:
        instance_id = row["id"]
        service_name = row["service_name"]
        container_name = row["container_name"]

        if container_name and not _container_exists(container_name):
            drift_count += 1
            if verbose:
                print(
                    f"  {_YELLOW}[WARN DRIFT]{_RESET} {service_name} "
                    f"({container_name}): SQLite 記錄存在但容器不存在，更新為 stopped。",
                    file=sys.stderr,
                )
            db_conn.execute(
                "UPDATE instances SET status = 'stopped', updated_at = ? WHERE id = ?",
                (now, instance_id),
            )
            db_conn.execute(
                """
                INSERT INTO lifecycle_events
                  (instance_id, event_type, triggered_by, detail, occurred_at)
                VALUES (?, 'drift_detected', 'reconcile', '{"reason": "container_not_found"}', ?)
                """,
                (instance_id, now),
            )
            _reset_failed_unit(container_name)

    # 同步更新 shared_refs 表狀態（若該版本無活躍實例則標記為 stopped）
    db_conn.execute(
        """
        UPDATE shared_refs
        SET status = 'stopped'
        WHERE (service_name, version) NOT IN (
            SELECT DISTINCT service_name, version FROM instances WHERE status NOT IN ('stopped', 'destroyed')
        )
        """
    )

    if drift_count == 0 and verbose:
        print(f"  {_GREEN}[OK] reconcile: 無 DRIFT 殘留。{_RESET}", file=sys.stderr)

    db_conn.commit()
    return drift_count
