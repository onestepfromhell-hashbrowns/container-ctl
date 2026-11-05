"""
單元測試：Tracer Bullet 主軸驗證
db → dag_resolver → blueprint_gen → quadlet_gen → deploy pipeline
"""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.dag_resolver import DeployPlan, ServiceAction, resolve
from container_ctl.db import init_db


# ── TICKET-001: SQLite Init ─────────────────────────────────────────

class TestInitDb:
    def test_creates_all_tables(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "instances" in tables
        assert "shared_refs" in tables
        assert "lifecycle_events" in tables
        conn.close()

    def test_wal_mode_enabled(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_idempotent(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        conn1 = init_db(db_path)
        conn1.close()
        # 第二次呼叫不應拋出
        conn2 = init_db(db_path)
        conn2.close()

    def test_instances_has_tier_and_container_name_columns(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(instances)").fetchall()
        }
        assert "tier" in cols
        assert "container_name" in cols
        conn.close()


# ── TICKET-004: DAG Resolver ────────────────────────────────────────

class TestDagResolver:
    def _make_conn(self, tmp_path) -> sqlite3.Connection:
        return init_db(str(tmp_path / "state.db"))

    def test_dedicated_always_new(self, tmp_path):
        conn = self._make_conn(tmp_path)
        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "postgres", "scope": "dedicated",
             "image": "docker.io/postgres@sha256:abc", "tier": "prod"}
        ]
        plan = resolve(manifest, configs, conn)
        assert len(plan.actions) == 1
        assert plan.actions[0].action == "NEW"
        assert plan.actions[0].name == "postgres"
        conn.close()

    def test_shared_reuse_when_running(self, tmp_path):
        conn = self._make_conn(tmp_path)
        # 預插入 shared_refs
        conn.execute(
            """INSERT INTO shared_refs
               (service_id, service_name, version, ref_count, lifecycle_mode, status)
               VALUES ('relay-v1.0-shared', 'relay', 'v1.0', 1, 'auto_idle', 'running')"""
        )
        conn.commit()

        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "relay", "scope": "shared", "version": "v1.0",
             "image": "docker.io/relay@sha256:abc", "tier": "infra"}
        ]
        plan = resolve(manifest, configs, conn)
        assert plan.actions[0].action == "REUSE"
        assert plan.actions[0].existing_instance_id == "relay-v1.0-shared"
        conn.close()

    def test_shared_conflict_when_version_mismatch(self, tmp_path):
        conn = self._make_conn(tmp_path)
        conn.execute(
            """INSERT INTO shared_refs
               (service_id, service_name, version, ref_count, lifecycle_mode, status)
               VALUES ('relay-v0.9-shared', 'relay', 'v0.9', 1, 'auto_idle', 'running')"""
        )
        conn.commit()

        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "relay", "scope": "shared", "version": "v1.0",
             "image": "docker.io/relay@sha256:abc", "tier": "infra"}
        ]
        plan = resolve(manifest, configs, conn)
        assert plan.actions[0].action == "CONFLICT"
        assert plan.has_conflict is True
        conn.close()

    def test_plan_preserves_dependency_order(self, tmp_path):
        conn = self._make_conn(tmp_path)
        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "postgres", "scope": "dedicated",
             "image": "docker.io/postgres@sha256:abc", "tier": "prod"},
            {"name": "webservice", "scope": "dedicated",
             "image": "docker.io/webservice@sha256:def", "tier": "prod"},
        ]
        plan = resolve(manifest, configs, conn)
        assert [a.name for a in plan.actions] == ["postgres", "webservice"]
        conn.close()


# ── TICKET-003: Repo Reader 基本驗證 ────────────────────────────────

class TestRepoReader:
    def test_pinned_digest_required(self):
        from container_ctl.repo_reader import PinnedDigestRequiredError, read_container_config

        with patch("container_ctl.repo_reader._git_show") as mock_show:
            mock_show.return_value = json.dumps({
                "name": "postgres",
                "image": "docker.io/postgres:15-alpine",  # 無 @sha256
                "scope": "dedicated",
            })
            with pytest.raises(PinnedDigestRequiredError):
                read_container_config("/fake/repo", "v1.0", "postgres")

    def test_valid_pinned_digest_passes(self):
        from container_ctl.repo_reader import read_container_config

        with patch("container_ctl.repo_reader._git_show") as mock_show:
            mock_show.return_value = json.dumps({
                "name": "postgres",
                "image": "docker.io/postgres:15@sha256:abc123",
                "scope": "dedicated",
            })
            config = read_container_config("/fake/repo", "v1.0", "postgres")
            assert config["name"] == "postgres"


# ── TICKET-005: Blueprint Generator ────────────────────────────────

class TestBlueprintGen:
    def test_generates_valid_blueprint_json(self, tmp_path):
        from container_ctl.blueprint_gen import generate_blueprint

        plan = DeployPlan(
            project_name="TestProj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="postgres",
                    action="NEW",
                    config={"name": "postgres", "scope": "dedicated",
                            "image": "docker.io/postgres@sha256:abc"},
                )
            ],
        )
        path = generate_blueprint(plan, output_dir=str(tmp_path))
        assert Path(path).exists()
        data = json.loads(Path(path).read_text())
        assert data["schema_version"] == 1
        assert data["project_name"] == "TestProj"
        assert len(data["services"]) == 1

    def test_idempotent_overwrite(self, tmp_path):
        from container_ctl.blueprint_gen import generate_blueprint

        plan = DeployPlan(project_name="TestProj", version="v1.0", actions=[])
        path1 = generate_blueprint(plan, output_dir=str(tmp_path))
        path2 = generate_blueprint(plan, output_dir=str(tmp_path))
        assert path1 == path2
