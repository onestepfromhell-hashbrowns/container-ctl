"""
TICKET-010: status list / show / query container-name
"""
import json
import sys

from container_ctl.db import DEFAULT_DB_PATH, init_db

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"

_STATUS_COLOR = {
    "running": _GREEN,
    "provisioned": _GREEN,
    "stopped": _YELLOW,
    "error": _RED,
    "destroyed": _RED,
}


def _colorize_status(s: str) -> str:
    return f"{_STATUS_COLOR.get(s, '')}{s}{_RESET}"


def list_instances(
    tier: str | None = None,
    output_json: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    conn = init_db(db_path)
    query = "SELECT * FROM instances"
    params: list = []
    if tier:
        query += " WHERE tier = ?"
        params.append(tier)
    query += " ORDER BY project_name, service_name"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if output_json:
        print(json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False))
        return

    if not rows:
        print("(無實例記錄)")
        return

    print(f"\n{'Project':<24} {'Service':<16} {'Tier':<8} {'Scope':<12} {'Status':<14} {'Version'}")
    print("─" * 90)
    for r in rows:
        print(
            f"{r['project_name']:<24} {r['service_name']:<16} {r['tier']:<8} "
            f"{r['scope']:<12} {_colorize_status(r['status']):<23} {r['version']}"
        )
    print()


def show_instance(project_name: str, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = init_db(db_path)

    instances = conn.execute(
        "SELECT * FROM instances WHERE project_name = ?", (project_name,)
    ).fetchall()

    if not instances:
        print(f"[INFO] 找不到專案 '{project_name}' 的實例。", file=sys.stderr)
        conn.close()
        return

    print(f"\n=== {project_name} ===")
    for r in instances:
        print(f"  {r['service_name']} [{_colorize_status(r['status'])}]")
        print(f"    container: {r['container_name']}")
        print(f"    tier/scope: {r['tier']}/{r['scope']}  ver: {r['version']}")

    # 最近 10 筆事件
    events = conn.execute(
        """
        SELECT event_type, triggered_by, occurred_at, detail
        FROM lifecycle_events
        WHERE instance_id LIKE ?
        ORDER BY id DESC
        LIMIT 10
        """,
        (f"{project_name}-%",),
    ).fetchall()

    if events:
        print("\n  最近事件:")
        for ev in events:
            print(f"    [{ev['occurred_at'][:19]}] {ev['event_type']} ({ev['triggered_by']})")

    conn.close()


def query_container_name(
    project_name: str,
    service_name: str,
    db_path: str = DEFAULT_DB_PATH,
) -> str | None:
    """供方案 3 (container_mounts.py) 查詢實際容器名稱。"""
    conn = init_db(db_path)
    row = conn.execute(
        """
        SELECT container_name FROM instances
        WHERE project_name = ? AND service_name = ?
          AND status NOT IN ('destroyed')
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (project_name, service_name),
    ).fetchone()
    conn.close()
    return row["container_name"] if row else None


def query_workspace_container(
    project_name: str,
    db_path: str = DEFAULT_DB_PATH,
) -> str | None:
    """
    查詢專案的主工作區 (Workspace) 開發容器名稱。
    供 remote-ssh / orchestrator 在未指定 service_name 時使用。

    優先權規則：
    1. 篩選 project_name 且 status IN ('running', 'provisioned', 'stopped')。
    2. 第一優先（工作區特徵）：Blueprint volumes 中明確掛載 container_path == '/workspace'
       或 Blueprint 包含 '/workspace'。
    3. 排除輔助 DB 干擾：若 service_name 包含 postgres/mysql/redis/db 等且未掛載 /workspace，降至最低優先級。
    4. Dev Tier 優先：tier == 'dev' 優於 prod/infra。
    5. 狀態優先：running > provisioned > stopped (manual)。
    6. 創建時間：created_at DESC (最新優先)。
    """
    conn = init_db(db_path)
    rows = conn.execute(
        """
        SELECT container_name, service_name, tier, status, blueprint, created_at
        FROM instances
        WHERE project_name = ?
          AND status IN ('running', 'provisioned', 'stopped')
        """,
        (project_name,),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    def _rank_key(row):
        service_name = (row["service_name"] or "").lower()
        tier = (row["tier"] or "").lower()
        status = (row["status"] or "").lower()
        blueprint_str = row["blueprint"] or ""

        has_workspace = False
        try:
            import json
            bp = json.loads(blueprint_str)
            if isinstance(bp, dict):
                for v in bp.get("volumes", []):
                    if isinstance(v, dict) and v.get("container_path") == "/workspace":
                        has_workspace = True
                        break
        except Exception:
            pass

        if not has_workspace and ('"/workspace"' in blueprint_str or ':/workspace' in blueprint_str):
            has_workspace = True

        is_auxiliary_db = any(
            kw in service_name for kw in ("postgres", "mysql", "redis", "mariadb", "mongo", "db")
        )

        if has_workspace:
            category = 0  # 明確掛載工作區容器
        elif not is_auxiliary_db:
            category = 1  # 一般非 DB 服務容器
        else:
            category = 2  # 輔助 DB 服務容器

        tier_score = 0 if tier == "dev" else 1

        if status == "running":
            status_score = 0
        elif status == "provisioned":
            status_score = 1
        else:
            status_score = 2

        return (category, tier_score, status_score)

    sorted_rows = sorted(rows, key=_rank_key)
    best_score = _rank_key(sorted_rows[0])
    candidates = [r for r in rows if _rank_key(r) == best_score]
    candidates.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return candidates[0]["container_name"]

