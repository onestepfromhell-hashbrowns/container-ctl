"""
TICKET-014: `backup state / volumes` 指令
使用 SQLite .backup API 備份 state.db；tar 打包容器 volume。
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl import paths

_VOLUMES_ROOT = paths.VOLUMES_DIR
_BACKUPS_ROOT = paths.VOLUME_BACKUPS_DIR


def backup_state(db_path: str = DEFAULT_DB_PATH) -> str:
    """
    使用 SQLite .backup API 備份 state.db。
    回傳備份路徑。
    """
    import sqlite3

    if not Path(db_path).exists():
        print(f"[ERROR] {db_path} 不存在，無法備份。", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak.{ts}"

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()

    size = Path(backup_path).stat().st_size
    print(f"[OK] 備份完成: {backup_path} ({size} bytes)")
    return backup_path


def backup_volumes(project: str, db_path: str = DEFAULT_DB_PATH) -> None:
    """
    tar 打包指定專案（或 'all'）的 container volume 目錄。
    """
    volumes_root = Path(_VOLUMES_ROOT)
    backups_root = Path(_BACKUPS_ROOT)
    backups_root.mkdir(parents=True, exist_ok=True)

    if project == "all":
        # 查出所有有 active 實例的專案
        conn = init_db(db_path)
        rows = conn.execute(
            "SELECT DISTINCT project_name FROM instances WHERE status NOT IN ('stopped', 'destroyed')"
        ).fetchall()
        conn.close()
        projects = [r["project_name"] for r in rows]
        if not projects:
            print("[INFO] 沒有 active 專案，無 volume 可備份。")
            return
    else:
        projects = [project]

    for proj in projects:
        vol_dir = volumes_root / proj
        if not vol_dir.exists():
            print(f"  [SKIP] {vol_dir} 不存在，跳過。", file=sys.stderr)
            continue

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_file = backups_root / f"{proj}-{ts}.tar.gz"

        print(f"  打包 {vol_dir} → {out_file} ...", end=" ", flush=True)
        result = subprocess.run(
            ["tar", "-czf", str(out_file), "-C", str(volumes_root), proj],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("FAILED")
            print(f"  [ERROR] {result.stderr.strip()}", file=sys.stderr)
            continue

        size = out_file.stat().st_size
        print(f"OK ({size} bytes)")
        print(f"  [OK] {out_file}")
