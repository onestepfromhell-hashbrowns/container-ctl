"""
新 Tickets 的驗收測試（TICKET-015, 016, 017, 018, 019, 023, 024, 025, 026）
所有測試使用 tmp_path / MagicMock — 禁止依賴真實容器或 Bare Repo
"""
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from container_ctl.dag_resolver import DeployPlan, ServiceAction, resolve
from container_ctl.db import init_db


# ── TICKET-023: Quadlet network-online.target 防禦 ────────────────────────

class TestQuadletBootRace:
    """TICKET-023: 驗證生成的 Quadlet 單元包含 network-online.target。"""

    def _make_action(self, name="postgres", tier="prod"):
        return ServiceAction(
            name=name,
            action="NEW",
            config={"name": name, "scope": "dedicated",
                    "image": f"docker.io/{name}@sha256:abc123", "tier": tier},
        )

    def test_container_unit_has_network_online_after(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = self._make_action("postgres", "prod")
        unit_text = _render_container_unit(action, "docker.io/postgres@sha256:abc")
        assert "After=network-online.target" in unit_text

    def test_container_unit_has_network_online_wants(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = self._make_action("relay", "infra")
        unit_text = _render_container_unit(action, "docker.io/relay@sha256:abc")
        assert "Wants=network-online.target" in unit_text

    def test_infra_tier_described_in_unit(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = self._make_action("relay", "infra")
        unit_text = _render_container_unit(action, "docker.io/relay@sha256:abc")
        assert "infra" in unit_text

    def test_generate_units_calls_daemon_reload(self, tmp_path):
        from container_ctl.quadlet_gen import generate_units
        plan = DeployPlan(
            project_name="TestProj",
            version="v1.0",
            actions=[self._make_action("postgres", "prod")],
        )
        with patch("container_ctl.quadlet_gen.subprocess.run") as mock_run, \
             patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", tmp_path / "quadlet"):
            mock_run.return_value = MagicMock(returncode=0)
            generate_units(plan, services_dir=str(tmp_path / "services"))
        # 確認 daemon-reload 被呼叫
        reload_calls = [
            c for c in mock_run.call_args_list
            if "daemon-reload" in str(c)
        ]
        assert len(reload_calls) == 1

    def test_container_unit_has_network_alias(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = self._make_action("dev-webservice-postgres", "infra")
        unit_text = _render_container_unit(action, "docker.io/postgres@sha256:abc", project="TestProj")
        assert "Network=net-prod-TestProj.network" in unit_text
        assert "NetworkAlias=dev-webservice-postgres" in unit_text
        assert "NetworkAlias=postgres" in unit_text

    def test_container_unit_dev_tier_network(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = self._make_action("dev-webservice", "dev")
        unit_text = _render_container_unit(action, "docker.io/web@sha256:abc", project="TestProj")
        assert "Network=net-dev-TestProj.network" in unit_text

    def test_container_unit_custom_network_alias(self, tmp_path):
        from container_ctl.quadlet_gen import _render_container_unit
        action = ServiceAction(
            name="custom-svc",
            action="NEW",
            config={
                "name": "custom-svc",
                "scope": "dedicated",
                "image": "docker.io/custom@sha256:abc",
                "network_alias": ["my-alias", "db-alias"]
            }
        )
        unit_text = _render_container_unit(action, "docker.io/custom@sha256:abc", project="TestProj")
        assert "NetworkAlias=my-alias" in unit_text
        assert "NetworkAlias=db-alias" in unit_text


# ── TICKET-025: Tier 層級模型 ─────────────────────────────────────────────

class TestTierModel:
    """TICKET-025: DAG 依 tier 排序；instances 表有 tier 與 container_name 欄位。"""

    def _make_conn(self, tmp_path):
        return init_db(str(tmp_path / "state.db"))

    def test_infra_sorted_before_prod(self, tmp_path):
        conn = self._make_conn(tmp_path)
        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "webservice", "scope": "dedicated",
             "image": "docker.io/webservice@sha256:def", "tier": "prod"},
            {"name": "relay", "scope": "dedicated",
             "image": "docker.io/relay@sha256:abc", "tier": "infra"},
        ]
        plan = resolve(manifest, configs, conn)
        conn.close()
        tiers = [a.config["tier"] for a in plan.actions]
        assert tiers.index("infra") < tiers.index("prod")

    def test_dev_sorted_after_prod(self, tmp_path):
        conn = self._make_conn(tmp_path)
        manifest = {"project_name": "TestProj", "version": "v1.0"}
        configs = [
            {"name": "devtool", "scope": "dedicated",
             "image": "docker.io/devtool@sha256:xyz", "tier": "dev"},
            {"name": "postgres", "scope": "dedicated",
             "image": "docker.io/postgres@sha256:abc", "tier": "prod"},
        ]
        plan = resolve(manifest, configs, conn)
        conn.close()
        tiers = [a.config["tier"] for a in plan.actions]
        assert tiers.index("prod") < tiers.index("dev")

    def test_instances_table_has_tier_column(self, tmp_path):
        conn = self._make_conn(tmp_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
        assert "tier" in cols
        conn.close()

    def test_instances_table_has_container_name_column(self, tmp_path):
        conn = self._make_conn(tmp_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(instances)").fetchall()}
        assert "container_name" in cols
        conn.close()

    def test_status_list_tier_filter(self, tmp_path):
        """status list --tier 過濾只回傳指定 tier 的實例。"""
        from container_ctl.cli.status import list_instances
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("id-prod-1", "Proj", "postgres", "v1", "prod", "dedicated",
             "running", "{}", "/path/q", "svc-postgres@Proj-v1", now, now)
        )
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("id-infra-1", "Proj", "relay", "v1", "infra", "shared",
             "running", "{}", "/path/q2", "svc-relay@v1-shared", now, now)
        )
        conn.commit()
        conn.close()

        # 過濾 prod
        import io
        from unittest.mock import patch as p
        with p("sys.stdout", new_callable=io.StringIO) as mock_out:
            list_instances(tier="prod", db_path=db_path)
        output = mock_out.getvalue()
        assert "postgres" in output
        assert "relay" not in output


# ── TICKET-026: 方案 3 容器名稱查詢 ──────────────────────────────────────

class TestQueryContainerName:
    """TICKET-026: query container-name 指令驗收。"""

    def test_returns_container_name_when_exists(self, tmp_path):
        from container_ctl.cli.status import query_container_name
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("id-1", "MyProj", "postgres", "v1", "prod", "dedicated",
             "running", "{}", "/path/q", "svc-postgres@MyProj-v1", now, now)
        )
        conn.commit()
        conn.close()

        name = query_container_name("MyProj", "postgres", db_path=db_path)
        assert name == "svc-postgres@MyProj-v1"

    def test_returns_none_when_not_found(self, tmp_path):
        from container_ctl.cli.status import query_container_name
        db_path = str(tmp_path / "state.db")
        init_db(db_path).close()

        name = query_container_name("NonExistent", "postgres", db_path=db_path)
        assert name is None

    def test_returns_none_for_destroyed_instance(self, tmp_path):
        from container_ctl.cli.status import query_container_name
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("id-1", "MyProj", "postgres", "v1", "prod", "dedicated",
             "destroyed", "{}", "/path/q", "svc-postgres@MyProj-v1", now, now)
        )
        conn.commit()
        conn.close()

        name = query_container_name("MyProj", "postgres", db_path=db_path)
        assert name is None


class TestQueryWorkspaceContainer:
    """驗證 query_workspace_container 工作區開發容器名稱查詢邏輯。"""

    def test_prefers_workspace_mount_over_running_database(self, tmp_path):
        from container_ctl.cli.status import query_workspace_container
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-09-09T01:44:26+00:00"
        # 輔助 DB 容器：running，dev tier，無 /workspace 掛載
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("db-1", "pg-hub", "dev-postgres-biz", "v1.0.0", "dev", "shared",
             "running", '{"volumes": [{"container_path": "/var/lib/postgresql/data"}]}',
             "/q1", "svc-dev-postgres-biz-v1.0.0-shared", now, now)
        )
        # 主開發容器：stopped (manual)，dev tier，掛載 /workspace
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("app-1", "pg-hub", "dev-pghub", "v1.0.0", "dev", "shared",
             "stopped", '{"volumes": [{"container_path": "/workspace"}]}',
             "/q2", "svc-dev-pghub-v1.0.0-shared", now, now)
        )
        conn.commit()
        conn.close()

        res = query_workspace_container("pg-hub", db_path=db_path)
        assert res == "svc-dev-pghub-v1.0.0-shared"

    def test_returns_none_for_unknown_or_destroyed(self, tmp_path):
        from container_ctl.cli.status import query_workspace_container
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-09-09T01:44:26+00:00"
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("d-1", "OldProj", "dev-app", "v1.0.0", "dev", "dedicated",
             "destroyed", '{"volumes": [{"container_path": "/workspace"}]}',
             "/q", "svc-dev-app", now, now)
        )
        conn.commit()
        conn.close()

        assert query_workspace_container("Unknown", db_path=db_path) is None
        assert query_workspace_container("OldProj", db_path=db_path) is None



# ── TICKET-016: SSH 生命週期 Hooks ────────────────────────────────────────

class TestSshLifecycle:
    """TICKET-016: SSH connect/disconnect 事件寫入 lifecycle_events。"""

    def _setup_db(self, tmp_path):
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("inst-1", "TestProj", "postgres", "v1", "prod", "dedicated",
             "provisioned", "{}", "/path/q", "svc-postgres@TestProj-v1", now, now)
        )
        conn.commit()
        conn.close()
        return db_path

    def test_notify_connect_writes_event(self, tmp_path):
        from container_ctl.ssh_lifecycle import notify_connect
        db_path = self._setup_db(tmp_path)
        notify_connect("TestProj", db_path=db_path)

        conn = sqlite3.connect(db_path)
        events = conn.execute(
            "SELECT event_type FROM lifecycle_events WHERE instance_id='inst-1'"
        ).fetchall()
        conn.close()
        assert any(e[0] == "ssh_connect" for e in events)

    def test_notify_connect_sets_status_running(self, tmp_path):
        from container_ctl.ssh_lifecycle import notify_connect
        db_path = self._setup_db(tmp_path)
        notify_connect("TestProj", db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status FROM instances WHERE id='inst-1'").fetchone()
        conn.close()
        assert row[0] == "running"

    def test_notify_disconnect_sync_writes_event(self, tmp_path):
        """使用同步版本測試（避免 subprocess spawn）。"""
        from container_ctl.ssh_lifecycle import _write_disconnect_sync
        db_path = self._setup_db(tmp_path)
        _write_disconnect_sync("TestProj", db_path=db_path)

        conn = sqlite3.connect(db_path)
        events = conn.execute(
            "SELECT event_type FROM lifecycle_events WHERE instance_id='inst-1'"
        ).fetchall()
        conn.close()
        assert any(e[0] == "ssh_disconnect" for e in events)

    def test_notify_disconnect_uses_popen(self, tmp_path):
        """notify_disconnect 應使用非阻塞 Popen，不直接修改 DB。"""
        from container_ctl.ssh_lifecycle import notify_disconnect
        db_path = self._setup_db(tmp_path)
        with patch("container_ctl.ssh_lifecycle.subprocess.Popen") as mock_popen:
            notify_disconnect("TestProj", db_path=db_path)
        assert mock_popen.called
        # 確認 start_new_session=True（脫離 SSH session）
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True


# ── TICKET-015: Network Broker ────────────────────────────────────────────

class TestNetworkBroker:
    """TICKET-015: net_broker 模組功能驗收（mock subprocess）。"""

    def test_load_config_returns_defaults_if_missing(self, tmp_path):
        from broker.net_broker import load_config
        cfg = load_config(str(tmp_path / "nonexistent.json"))
        assert "passive_attach" in cfg
        assert "network_prefix" in cfg

    def test_load_config_reads_file(self, tmp_path):
        from broker.net_broker import load_config
        config_data = {
            "passive_attach": [{"container": "my-relay", "comment": "test"}],
            "network_prefix": "net-prod-",
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(config_data))
        cfg = load_config(str(cfg_file))
        assert cfg["passive_attach"][0]["container"] == "my-relay"

    def test_attach_container_success(self):
        from broker.net_broker import attach_container_to_network
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("broker.net_broker.subprocess.run", return_value=mock_result):
            result = attach_container_to_network("my-relay", "net-prod-testproj")
        assert result is True

    def test_attach_container_already_connected(self):
        from broker.net_broker import attach_container_to_network
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "already connected to network"
        with patch("broker.net_broker.subprocess.run", return_value=mock_result):
            result = attach_container_to_network("my-relay", "net-prod-testproj")
        # already connected 視為成功
        assert result is True

    def test_reconcile_connects_missing_networks(self):
        from broker.net_broker import reconcile
        config = {
            "passive_attach": [{"container": "my-relay"}],
            "network_prefix": "net-prod-",
        }

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "ls" in cmd:
                m.stdout = "net-prod-proj1\nnet-prod-proj2\n"
            elif "ps" in cmd:
                m.stdout = "my-relay\n"
            elif "inspect" in cmd:
                # 假設 my-relay 只在 proj1
                m.stdout = "net-prod-proj1\n"
            elif "connect" in cmd:
                m.stdout = ""
            return m

        with patch("broker.net_broker.subprocess.run", side_effect=fake_run):
            reconcile(config)  # 不應拋出


# ── TICKET-024: Interactive Menu ──────────────────────────────────────────

class TestInteractiveMenu:
    """TICKET-024: 互動式選單驗收（非 TTY 模式）。"""

    def test_non_tty_prints_usage_hint(self, capsys):
        from container_ctl.cli.menu import run_menu
        with patch("container_ctl.cli.menu._is_tty", return_value=False):
            run_menu()
        captured = capsys.readouterr()
        assert "非互動式環境" in captured.out or "子指令模式" in captured.out

    def test_menu_items_defined(self):
        from container_ctl.cli.menu import _MENU_ITEMS
        keys = [item[0] for item in _MENU_ITEMS]
        assert "q" in keys
        # 必要操作
        actions = [item[1] for item in _MENU_ITEMS]
        for required in ("status", "stop", "wsl-shutdown", "health"):
            assert required in actions, f"選單缺少 {required}"

    def test_confirm_batch_non_tty_auto_confirms(self):
        from container_ctl.cli.menu import _confirm_batch
        with patch("container_ctl.cli.menu._is_tty", return_value=False):
            result = _confirm_batch("test-action")
        assert result is True

    def test_select_project_non_tty_returns_empty(self):
        from container_ctl.cli.menu import _select_project
        with patch("container_ctl.cli.menu._is_tty", return_value=False):
            # 非 TTY 時 _prompt 回傳空字串
            result = _select_project()
        # 非 TTY 環境應回傳 None 或空
        assert result is None or result == ""


class TestReconcileSharedRefs:
    """測試 reconcile_instances 正確依 (service_name, version) 同步 shared_refs 狀態。"""

    def test_reconcile_stops_inactive_versions_in_shared_refs(self, tmp_path):
        from container_ctl.db import init_db
        from container_ctl.reconcile import reconcile_instances

        db_file = str(tmp_path / "state.db")
        conn = init_db(db_file)

        # 插入多個版本的 shared_refs
        conn.execute(
            """
            INSERT INTO shared_refs (service_id, service_name, version, ref_count, lifecycle_mode, status)
            VALUES
                ('svc-v1.4.0-shared', 'test-svc', 'v1.4.0', 1, 'always_on', 'running'),
                ('svc-v1.6.0-shared', 'test-svc', 'v1.6.0', 1, 'always_on', 'running')
            """
        )
        # instances 中只有 v1.6.0 是 active (provisioned)，v1.4.0 是 stopped
        conn.execute(
            """
            INSERT INTO instances (id, project_name, service_name, version, tier, scope, status, blueprint, quadlet_path, created_at, updated_at)
            VALUES
                ('p-test-svc-v1.4.0', 'p', 'test-svc', 'v1.4.0', 'infra', 'shared', 'stopped', '', '', '', ''),
                ('p-test-svc-v1.6.0', 'p', 'test-svc', 'v1.6.0', 'infra', 'shared', 'provisioned', '', '', '', '')
            """
        )
        conn.commit()

        # 執行 reconcile（mock container 存在以避免 DRIFT）
        with patch("container_ctl.reconcile._container_exists", return_value=True):
            reconcile_instances(conn, verbose=False)

        # 驗證 v1.4.0 被標記為 stopped，而 v1.6.0 維持 running
        v14 = conn.execute("SELECT status FROM shared_refs WHERE version = 'v1.4.0'").fetchone()["status"]
        v16 = conn.execute("SELECT status FROM shared_refs WHERE version = 'v1.6.0'").fetchone()["status"]

        assert v14 == "stopped"
        assert v16 == "running"
        conn.close()


class TestQuadletLabels:
    """驗證 Quadlet 單元生成器支援 Label 標籤渲染（如 Ingress OCI Labels）。"""

    def test_container_unit_renders_labels_dict(self):
        from container_ctl.quadlet_gen import _render_container_unit
        action = ServiceAction(
            name="web",
            action="NEW",
            config={
                "name": "web",
                "image": "docker.io/web:latest",
                "labels": {
                    "ingress.enable": "true",
                    "ingress.path": "/api/v1/web",
                    "ingress.port": "8080",
                    "ingress.tier": "dev",
                },
            },
        )
        unit = _render_container_unit(action, "docker.io/web:latest", project="TestProj", version="v1.0")
        assert "Label=ingress.enable=true" in unit
        assert "Label=ingress.path=/api/v1/web" in unit
        assert "Label=ingress.port=8080" in unit
        assert "Label=ingress.tier=dev" in unit

    def test_container_unit_renders_labels_list(self):
        from container_ctl.quadlet_gen import _render_container_unit
        action = ServiceAction(
            name="web",
            action="NEW",
            config={
                "name": "web",
                "image": "docker.io/web:latest",
                "labels": [
                    "ingress.enable=true",
                    "ingress.tier=production",
                ],
            },
        )
        unit = _render_container_unit(action, "docker.io/web:latest", project="TestProj", version="v1.0")
        assert "Label=ingress.enable=true" in unit

# ── Manifest Schema Validation & Fallback Tests ─────────────────────────

class TestManifestValidation:
    """驗證 manifest.json 的 Schema 驗證、容錯與正規化。"""

    def test_read_manifest_with_project_name(self, tmp_path):
        from container_ctl.repo_reader import read_manifest
        manifest_data = {
            "schema_version": 1,
            "project_name": "TestProj",
            "version": "v1.0.0",
            "dependencies": ["dev-test"],
        }
        with patch("container_ctl.repo_reader._git_show", return_value=json.dumps(manifest_data)):
            data = read_manifest("/fake/repo", "HEAD")
            assert data["project_name"] == "TestProj"
            assert data["dependencies"] == ["dev-test"]

    def test_read_manifest_fallback_to_name(self, tmp_path):
        from container_ctl.repo_reader import read_manifest
        manifest_data = {
            "name": "FallbackProj",
            "version": "v1.0.0",
            "containers": [{"name": "dev-fallback"}],
        }
        with patch("container_ctl.repo_reader._git_show", return_value=json.dumps(manifest_data)):
            data = read_manifest("/fake/repo", "HEAD")
            assert data["project_name"] == "FallbackProj"
            assert data["dependencies"] == ["dev-fallback"]

    def test_read_manifest_missing_project_name_raises(self, tmp_path):
        from container_ctl.repo_reader import read_manifest, DeployConfigNotFoundError
        manifest_data = {
            "version": "v1.0.0",
        }
        with patch("container_ctl.repo_reader._git_show", return_value=json.dumps(manifest_data)):
            with pytest.raises(DeployConfigNotFoundError, match="缺少必要欄位 'project_name'"):
                read_manifest("/fake/repo", "HEAD")

    def test_read_manifest_missing_version_raises(self, tmp_path):
        from container_ctl.repo_reader import read_manifest, DeployConfigNotFoundError
        manifest_data = {
            "project_name": "TestProj",
        }
        with patch("container_ctl.repo_reader._git_show", return_value=json.dumps(manifest_data)):
            with pytest.raises(DeployConfigNotFoundError, match="缺少必要欄位 'version'"):
                read_manifest("/fake/repo", "HEAD")

    def test_resolve_with_name_only_does_not_keyerror(self, tmp_path):
        conn = init_db(str(tmp_path / "state.db"))
        manifest = {
            "name": "ServiceD",
            "version": "v1.0.0",
        }
        configs = [{
            "name": "dev-pis",
            "tier": "dev",
            "scope": "dedicated",
            "image": "docker.io/python@sha256:abc",
        }]
        plan = resolve(manifest, configs, conn)
        assert plan.project_name == "ServiceD"
        assert len(plan.actions) == 1
        conn.close()



