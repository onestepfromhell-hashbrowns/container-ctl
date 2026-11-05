"""
container-ctl 互動式主選單
- 非 TTY 環境自動降級為 --yes 模式
- 防呆確認：手動輸入 yes 確認批次操作
"""
import sys


_MENU_ITEMS = [
    ("1", "status",    "查詢所有實例狀態"),
    ("2", "start",     "啟動（部署）專案"),
    ("3", "stop",      "優雅停止專案"),
    ("4", "restart",   "重啟專案服務"),
    ("5", "redeploy",  "重新部署（熱重載）"),
    ("6", "wsl-restart", "安全重啟 WSL"),
    ("7", "wsl-shutdown", "安全關閉 WSL"),
    ("8", "health",    "健康診斷（Preflight）"),
    ("q", "quit",      "離開"),
]


def _is_tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(msg: str, default: str = "") -> str:
    """安全的 input()，非 TTY 時回傳 default。"""
    if not _is_tty():
        return default
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        return default


def _confirm_batch(action: str) -> bool:
    """防呆批次確認：手動輸入 'yes'。"""
    if not _is_tty():
        return True
    ans = _prompt(f"確認執行 [{action}]？輸入 'yes' 以確認：")
    return ans == "yes"


def _print_menu() -> None:
    print("\n╔══════════════════════════════════════╗")
    print("║      container-ctl 管理選單          ║")
    print("╠══════════════════════════════════════╣")
    for key, _, desc in _MENU_ITEMS:
        print(f"║  [{key}] {desc:<30}║")
    print("╚══════════════════════════════════════╝")


def _select_project() -> str | None:
    """列出現有實例，讓使用者選擇或輸入專案名稱。"""
    try:
        from container_ctl.db import DEFAULT_DB_PATH, init_db
        conn = init_db(DEFAULT_DB_PATH)
        rows = conn.execute(
            "SELECT DISTINCT project_name FROM instances WHERE status NOT IN ('destroyed')"
        ).fetchall()
        conn.close()
        projects = [r["project_name"] for r in rows]
    except Exception:
        projects = []

    if projects:
        print("\n現有專案：")
        for i, p in enumerate(projects, 1):
            print(f"  [{i}] {p}")
        choice = _prompt("選擇編號或輸入專案名稱：")
        if choice.isdigit() and 1 <= int(choice) <= len(projects):
            return projects[int(choice) - 1]
        return choice or None
    else:
        return _prompt("輸入專案名稱：") or None


def run_menu() -> None:
    """執行互動式主選單。非 TTY 環境輸出提示後結束。"""
    if not _is_tty():
        print("[INFO] 非互動式環境，請使用子指令模式（如 container-ctl deploy apply <project> <ref> --yes）")
        return

    while True:
        _print_menu()
        choice = _prompt("\n選擇操作：").lower()

        if choice == "q":
            print("再見。")
            break

        elif choice == "1":
            # status
            from container_ctl.cli.status import list_instances
            list_instances()

        elif choice == "2":
            # start / deploy apply
            project = _select_project()
            if not project:
                print("[WARN] 未輸入專案名稱。")
                continue
            ref = _prompt("輸入 Git ref（tag / commit）：")
            if not ref:
                print("[WARN] 未輸入 ref。")
                continue
            if _confirm_batch(f"deploy apply {project} {ref}"):
                from container_ctl.cli.deploy import apply
                apply(project, ref, yes=True)

        elif choice == "3":
            # stop
            project = _select_project()
            if not project:
                continue
            if _confirm_batch(f"stop {project}"):
                from container_ctl.cli.deploy import stop
                stop(project, yes=True)

        elif choice == "4":
            # restart（stop + apply 上次版本）
            project = _select_project()
            if not project:
                continue
            if not _confirm_batch(f"restart {project}"):
                continue
            try:
                from container_ctl.db import DEFAULT_DB_PATH, init_db
                conn = init_db(DEFAULT_DB_PATH)
                row = conn.execute(
                    "SELECT version FROM instances WHERE project_name=? ORDER BY updated_at DESC LIMIT 1",
                    (project,),
                ).fetchone()
                conn.close()
                ref = row["version"] if row else None
            except Exception:
                ref = None
            if not ref:
                ref = _prompt("找不到上次版本，請輸入 ref：")
            if ref and _confirm_batch(f"restart {project} @ {ref}"):
                from container_ctl.cli.deploy import stop, apply
                stop(project, yes=True)
                apply(project, ref, yes=True)

        elif choice == "5":
            # redeploy
            project = _select_project()
            if not project:
                continue
            ref = _prompt("輸入新 Git ref：")
            if not ref:
                continue
            if _confirm_batch(f"redeploy {project} {ref}"):
                from container_ctl.cli.deploy import redeploy
                redeploy(project, ref, yes=True)

        elif choice == "6":
            # wsl-restart
            if _confirm_batch("wsl-restart"):
                from container_ctl.wsl_shutdown import wsl_restart
                wsl_restart(yes=True)

        elif choice == "7":
            # wsl-shutdown
            if _confirm_batch("wsl-shutdown"):
                from container_ctl.wsl_shutdown import wsl_shutdown
                wsl_shutdown(yes=True)

        elif choice == "8":
            # health / preflight
            from container_ctl.preflight import run_preflight
            run_preflight()

        else:
            print(f"[WARN] 未知選項：{choice!r}，請重新選擇。")
