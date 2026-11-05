"""
TICKET-013: WSL 安全關閉 / 重啟
呼叫 TICKET-012 優雅停止（全部專案）+ TICKET-014 SQLite 備份，
再輸出提示讓使用者於 Windows 執行 wsl.exe --shutdown。
"""
import sys
from pathlib import Path

from container_ctl.backup import backup_state as _backup_state
from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl.graceful_stop import graceful_stop


def _get_all_projects(db_path: str) -> list[str]:
    """回傳所有 active 專案名稱（去重）。"""
    conn = init_db(db_path)
    rows = conn.execute(
        """
        SELECT DISTINCT project_name FROM instances
        WHERE status NOT IN ('stopped', 'destroyed')
        """
    ).fetchall()
    conn.close()
    return [r["project_name"] for r in rows]


def _is_tty() -> bool:
    return sys.stdin.isatty()


def wsl_shutdown(
    yes: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """
    container-ctl wsl-shutdown：
    1. 確認（非 TTY / --yes 時自動跳過）
    2. 優雅停止所有專案（scope="all"）
    3. SQLite 備份
    4. 輸出 Windows 關機提示
    """
    _run_shutdown_sequence(yes=yes, db_path=db_path, restart=False)


def wsl_restart(
    yes: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    """
    container-ctl wsl-restart：
    同 wsl_shutdown + 輸出重啟提示。
    """
    _run_shutdown_sequence(yes=yes, db_path=db_path, restart=True)


def _run_shutdown_sequence(
    yes: bool,
    db_path: str,
    restart: bool,
) -> None:
    action = "重啟" if restart else "關閉"

    # ── 1. 確認 ──────────────────────────────────────────────────
    auto_confirm = yes or not _is_tty()
    if auto_confirm:
        print(f"[AUTO] 非互動模式，自動確認 WSL {action}。")
    else:
        try:
            answer = input(f"確認執行 WSL {action}？所有容器將被停止。(y/N): ").strip().lower()
        except EOFError:
            answer = "y"  # EOF 同等非 TTY，自動確認
        if answer != "y":
            print(f"[CANCELLED] 已取消 wsl-{'restart' if restart else 'shutdown'}。")
            return

    # ── 2. 優雅停止所有專案 ──────────────────────────────────────
    print("\n[1/3] 優雅停止所有專案容器...")
    projects = _get_all_projects(db_path)
    if projects:
        for proj in projects:
            print(f"  → 停止 {proj}")
            graceful_stop(proj, scope="all", db_path=db_path)
    else:
        print("  [INFO] 沒有 active 專案，跳過停止步驟。")

    # ── 3. SQLite 備份 ────────────────────────────────────────────
    print("\n[2/3] 備份 SQLite state.db...")
    if Path(db_path).exists():
        _backup_state(db_path)
    else:
        print(f"  [SKIP] {db_path} 不存在，跳過備份。")

    # ── 4. 輸出提示 ───────────────────────────────────────────────
    print()
    if restart:
        print("[3/3] 準備完成。請於 Windows 執行：")
        print()
        print("      wsl.exe --shutdown")
        print("      wsl.exe -d <你的發行版名稱>  # 重新啟動")
    else:
        print("[3/3] 準備完成。請於 Windows 執行：")
        print()
        print("      wsl.exe --shutdown")
