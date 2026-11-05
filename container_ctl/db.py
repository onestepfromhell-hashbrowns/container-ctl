"""
TICKET-001: SQLite 狀態 DB 初始化
路徑：paths.STATE_DB（預設 <SERVICES_DIR>/state.db）
"""
import sqlite3
from pathlib import Path
from container_ctl import paths

DEFAULT_DB_PATH = paths.STATE_DB

_DDL = """
CREATE TABLE IF NOT EXISTS instances (
    id            TEXT PRIMARY KEY,
    project_name  TEXT NOT NULL,
    service_name  TEXT NOT NULL,
    version       TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT 'prod',
    scope         TEXT NOT NULL,
    status        TEXT NOT NULL,
    blueprint     TEXT NOT NULL,
    quadlet_path  TEXT NOT NULL,
    container_name TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_refs (
    service_id       TEXT PRIMARY KEY,
    service_name     TEXT NOT NULL,
    version          TEXT NOT NULL,
    ref_count        INTEGER NOT NULL DEFAULT 0,
    lifecycle_mode   TEXT NOT NULL,
    idle_timeout_sec INTEGER,
    idle_since       TEXT,
    status           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    triggered_by TEXT,
    detail       TEXT,
    occurred_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name    TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    env             TEXT NOT NULL,
    release_version TEXT NOT NULL,
    image_id        TEXT,
    manifest_digest TEXT,
    config_commit   TEXT,
    result          TEXT NOT NULL,
    detail          TEXT,
    actor           TEXT,
    occurred_at     TEXT NOT NULL
);
"""


def init_db(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建立或連線 state.db，啟用 WAL 模式，建立三張核心表（冪等）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_DDL)
    conn.commit()
    return conn
