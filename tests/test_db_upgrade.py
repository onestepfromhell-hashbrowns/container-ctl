import subprocess
from datetime import datetime, timezone

import pytest

from container_ctl import db_upgrade as u


def _repo(tmp_path, files):
    r = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    for n, body in files.items():
        (r / n).parent.mkdir(parents=True, exist_ok=True)
        (r / n).write_text(body)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    return str(r / ".git")


RX = r"src/[^/]+/migrations/[^/]+\.sql"


def test_discover_sorted_skips_rollback_and_non_numbered(tmp_path):
    g = _repo(tmp_path, {"src/a/migrations/0002_b.sql": "B", "src/b/migrations/0001_a.sql": "A",
                         "src/a/migrations/0002_b_rollback.sql": "R", "src/a/migrations/role_grants.sql": "G"})
    assert [(v, s) for v, _n, s in u.discover(g, "HEAD", RX)] == [("0001", "A"), ("0002", "B")]


def test_discover_duplicate_version_is_error(tmp_path):
    g = _repo(tmp_path, {"src/a/migrations/0017_x.sql": "1", "src/b/migrations/0017_y.sql": "2"})
    with pytest.raises(u.UpgradeError, match="0017"):
        u.discover(g, "HEAD", RX)


def test_prune_keeps_last_and_recent(tmp_path):
    for i in range(1, 9):
        (tmp_path / f"2026090{i}T000000Z").mkdir()
        (tmp_path / f"2026090{i}T000000Z" / "f").write_text("x")
    gone = u.prune(tmp_path, now=datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert gone == ["20260901T000000Z", "20260902T000000Z", "20260903T000000Z"]
    assert len(list(tmp_path.iterdir())) == 5


def test_apply_inserts_ledger_in_same_transaction(monkeypatch):
    seen = []
    monkeypatch.setattr(u, "_psql", lambda c, db, sql, admin="infra_admin", stdin=None, single_tx=False: seen.append((stdin, single_tx)) or "")
    u.apply_pending("c", "db", {"ledger": "s.l", "search_path": "s, public"}, [("0020", "n.sql", "ALTER TABLE x;")])
    script, tx = seen[0]
    assert tx and "SET LOCAL search_path TO s, public" in script and "INSERT INTO s.l (version) VALUES ('0020')" in script


def test_drill_run_argv_pulls_never_and_keeps_isolation():
    """--pull=never 必須在映像名之前；其餘隔離旗標不得被改動（ADR-012：proxy 只放行 policy=never）。"""
    from container_ctl.db_upgrade import _drill_run_argv
    argv = _drill_run_argv("drill-x-1", "localhost/pg:15@sha256:" + "a" * 64)
    assert argv[:4] == ["podman", "run", "-d", "--pull=never"]
    assert argv.index("--pull=never") < argv.index("localhost/pg:15@sha256:" + "a" * 64)
    assert argv.count("--pull=never") == 1 and not any(a.startswith("--pull") and a != "--pull=never" for a in argv)
    assert ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
    assert ["--user", "70:70"] == argv[argv.index("--user"):argv.index("--user") + 2]
    assert argv[argv.index("--name") + 1] == "drill-x-1"
    assert argv[argv.index("--entrypoint") + 1] == "sh"
