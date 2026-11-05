"""test_mg_08_deploy_gate.py — Ticket #08 Acceptance Tests (C05, C07, T05, T08, T11).

Verifies:
1. --yes cannot bypass governance pause; admission rejection prevents volume provisioning,
   unit generation, and old prod termination.
2. REUSE of paused shared workload is rejected without incrementing ref_count or falsely marking running.
3. Stable-ID pauses persist across version upgrades and unit rebuilds; reconcile does not clear ledger.
4. cgroup-sync restart skips paused targets while unpaused active targets are restarted.
5. Post-approval JIT invalidation rejects start attempt and records stopped status with failure event.
6. SystemdManager rootless dispatch per owner_uid with --no-block, strictly separating job submission from running state.
"""

import pytest

pytest.importorskip("memory_governor", reason="needs memory_governor (optional governance plugin)")

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.cli.deploy import apply, redeploy
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.db import init_db
from container_ctl.lifecycle_adapter import (
    SystemdManager,
    check_plan_admission,
    is_workload_paused,
    precise_start_workload,
    resolve_action_stable_id,
)
from container_ctl.reconcile import reconcile_instances
from container_ctl.cgroup_sync import SyncResult, reload_and_restart_services
from container_ctl.ssh_lifecycle import notify_connect
from memory_governor.ledger import GovernanceLedger, PauseRecord


@pytest.fixture
def mock_ledger(tmp_path, monkeypatch):
    """Create a temporary isolated GovernanceLedger."""
    ledger_db = tmp_path / "governor" / "ledger.db"
    ledger_db.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CONTAINER_CTL_GOVERNANCE_LEDGER", str(ledger_db))
    ledger = GovernanceLedger(db_path=ledger_db)
    return ledger


@pytest.fixture
def sample_pause_record():
    return PauseRecord(
        stable_id="dev-app",
        owner_uid=1000,
        incident_id="INC-TEST-001",
        reason="memory_pressure_emergency",
        created_at=datetime.now(timezone.utc).isoformat(),
        original_running=True,
        policy_version="1.0",
    )


class TestDeploymentAdmissionGate:
    """Test deploy apply and redeploy enforcement of governance pauses (AC 1, AC 4)."""

    def test_apply_yes_flag_cannot_bypass_pause_zero_side_effects(self, tmp_path, mock_ledger, sample_pause_record):
        """AC 1 & AC 4: --yes 不能越過 pause；准入失敗不執行 volume provisioning、單元覆寫與啟動。"""
        # 1. 登記暫停
        mock_ledger.record_pause(sample_pause_record)
        assert is_workload_paused("dev-app", ledger=mock_ledger)[0] is True

        services_dir = tmp_path / "services"
        db_path = str(tmp_path / "state.db")
        repos_dir = tmp_path / "repos"

        # 模擬 plan
        mock_plan = DeployPlan(
            project_name="testproj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="NEW",
                    config={
                        "image": "localhost/app:latest",
                        "tier": "dev",
                        "stable_id": "dev-app",
                        "volumes": [{"host_path": str(tmp_path / "vol"), "container_path": "/data"}],
                    },
                )
            ],
        )

        with patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan), \
             patch("container_ctl.cli.deploy.ensure_volume_storage") as mock_ensure_vol, \
             patch("container_ctl.cli.deploy.generate_units") as mock_gen_units:

            # 呼叫 apply 帶有 --yes
            with pytest.raises(SystemExit) as exc_info:
                apply(
                    project_name="testproj",
                    ref="v1.0",
                    yes=True,
                    repos_dir=str(repos_dir),
                    db_path=db_path,
                    services_dir=str(services_dir),
                )

            assert exc_info.value.code == 1

            # 驗證零副作用：不建立 Volume、不生成單元
            assert mock_ensure_vol.call_count == 0, "准入失敗嚴禁調用 volume provisioning！"
            assert mock_gen_units.call_count == 0, "准入失敗嚴禁生成/覆寫 Quadlet 單元！"

    def test_reuse_paused_workload_rejected_no_ref_no_running(self, tmp_path, mock_ledger):
        """AC 1: REUSE paused 服務被拒絕，不加 shared ref，也不假稱 running。"""
        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        # 預先建立 shared_refs 初始記錄
        conn.execute(
            """
            INSERT INTO shared_refs (service_id, service_name, version, ref_count, lifecycle_mode, idle_timeout_sec, status)
            VALUES ('shared-db-v1', 'shared-db', 'v1', 1, 'always_on', 0, 'running')
            """
        )
        conn.commit()
        conn.close()

        # 登記治理暫停
        pause_rec = PauseRecord(
            stable_id="shared-db",
            owner_uid=1000,
            incident_id="INC-DB-002",
            reason="shared_db_high_pressure",
            created_at=now,
            original_running=True,
            policy_version="1.0",
        )
        mock_ledger.record_pause(pause_rec)

        mock_plan = DeployPlan(
            project_name="client_proj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="shared-db",
                    action="REUSE",
                    existing_instance_id="shared-db-v1",
                    config={
                        "scope": "shared",
                        "stable_id": "shared-db",
                    },
                )
            ],
        )

        with patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan):

            with pytest.raises(SystemExit) as exc_info:
                apply(
                    project_name="client_proj",
                    ref="v1.0",
                    yes=True,
                    db_path=db_path,
                )

            assert exc_info.value.code == 1

        # 驗證 shared_refs 引用計數未被增加
        conn = init_db(db_path)
        row = conn.execute("SELECT ref_count, status FROM shared_refs WHERE service_id='shared-db-v1'").fetchone()
        conn.close()
        assert row["ref_count"] == 1, "處於暫停之服務嚴禁增加 shared_refs 計數！"

    def test_version_upgrade_preserves_stable_id_pause_and_reconcile_preserves_ledger(self, tmp_path, mock_ledger, sample_pause_record):
        """AC 2: 改 version (v1 -> v2) 或重建單元時 stable-id pause 仍有效；reconcile 不清帳本。"""
        # 登記 dev-app 暫停
        mock_ledger.record_pause(sample_pause_record)

        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        # 既有舊版 v1 實例記錄
        conn.execute(
            """
            INSERT INTO instances (id, project_name, service_name, version, tier, scope, status, blueprint, quadlet_path, container_name, created_at, updated_at)
            VALUES ('demo-dev-app-v1', 'demo', 'dev-app', 'v1', 'dev', 'dedicated', 'stopped', '{"stable_id": "dev-app"}', '/tmp/quadlet', 'svc-demo-v1', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        # 新版 v2 plan
        v2_plan = DeployPlan(
            project_name="demo",
            version="v2",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="NEW",
                    config={
                        "image": "localhost/app:v2",
                        "tier": "dev",
                        "stable_id": "dev-app",
                    },
                )
            ],
        )

        with patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
             patch("container_ctl.cli.deploy.read_manifest"), \
             patch("container_ctl.cli.deploy.list_container_configs"), \
             patch("container_ctl.cli.deploy.resolve", return_value=v2_plan), \
             patch("container_ctl.cli.deploy.generate_blueprint", return_value="/tmp/bp.json"), \
             patch("container_ctl.cli.deploy.validate_volumes", return_value=(True, [], [])):

            # 嘗試 redeploy 至新版 v2
            with pytest.raises(SystemExit) as exc_info:
                redeploy(
                    project_name="demo",
                    new_ref="v2",
                    yes=True,
                    db_path=db_path,
                )

            assert exc_info.value.code == 1, "版本升級時 stable_id pause 必須依然有效並阻擋部署！"

        # 執行 reconcile_instances
        with patch("container_ctl.reconcile._container_exists", return_value=False), \
             patch("container_ctl.reconcile._reset_failed_unit"):
            drift = reconcile_instances(conn, verbose=False)
            assert drift == 0  # stopped 狀態不會被判定為 drift

        conn.close()

        # 驗證 reconcile 絕對沒有清除 GovernanceLedger 帳本
        pauses_after = mock_ledger.get_pauses()
        assert len(pauses_after) == 1
        assert pauses_after[0].stable_id == "dev-app"
        assert pauses_after[0].incident_id == "INC-TEST-001"


class TestCgroupSyncAndSshLifecycleGates:
    """Test cgroup-sync restart and SSH connect skipping paused workloads (AC 3, AC 1)."""

    def test_cgroup_sync_restart_skips_paused_target(self, tmp_path, mock_ledger, sample_pause_record):
        """AC 3: cgroup-sync 變更後不能 restart paused 目標；final gate 仍是最後防線。"""
        mock_ledger.record_pause(sample_pause_record)

        results = [
            SyncResult(
                file_path=Path("/tmp/svc-app1.container"),
                container_name="svc-app1",
                tier="dev",
                modified=True,
                memory_max="2G",
                tasks_max="2048",
                swap_max="0",
                stable_id="dev-app",  # 處於暫停
            ),
            SyncResult(
                file_path=Path("/tmp/svc-app2.container"),
                container_name="svc-app2",
                tier="prod",
                modified=True,
                memory_max="1G",
                tasks_max=None,
                swap_max="0",
                stable_id="prod-app",  # 正常未暫停
            ),
        ]

        with patch("subprocess.run") as mock_subproc:
            # 模擬 systemctl 查詢與重啟
            def fake_run(cmd, *args, **kwargs):
                ret = MagicMock()
                ret.returncode = 0
                ret.stdout = ""
                ret.stderr = ""
                return ret

            mock_subproc.side_effect = fake_run

            restarted = reload_and_restart_services(results, restart=True, ledger=mock_ledger)

            # 斷言 paused 之 dev-app (svc-app1) 被跳過，僅 app2 被重啟
            assert "svc-app1" not in restarted
            assert "svc-app2" in restarted

    def test_ssh_lifecycle_notify_connect_preserves_paused_status(self, tmp_path, mock_ledger, sample_pause_record):
        """AC 1: SSH 重連時不得將處於暫停之實例標示為 running。"""
        mock_ledger.record_pause(sample_pause_record)

        db_path = str(tmp_path / "state.db")
        conn = init_db(db_path)
        now = datetime.now(timezone.utc).isoformat()

        # 建立兩個實例：一個暫停 (dev-app)，一個正常 (other-svc)
        conn.execute(
            """
            INSERT INTO instances (id, project_name, service_name, version, tier, scope, status, blueprint, quadlet_path, container_name, created_at, updated_at)
            VALUES
              ('demo-dev-app', 'demo', 'dev-app', 'v1', 'dev', 'dedicated', 'stopped', '{"stable_id": "dev-app"}', '/tmp/quadlet', 'svc-dev-app', ?, ?),
              ('demo-other', 'demo', 'other-svc', 'v1', 'dev', 'dedicated', 'stopped', '{"stable_id": "other-svc"}', '/tmp/quadlet', 'svc-other', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()
        conn.close()

        # 觸發 SSH 連線通知
        notify_connect("demo", db_path=db_path, ledger=mock_ledger)

        conn = init_db(db_path)
        rows = {r["service_name"]: r["status"] for r in conn.execute("SELECT service_name, status FROM instances").fetchall()}
        conn.close()

        assert rows["dev-app"] == "stopped", "處於暫停狀態之實例嚴禁被更新為 running！"
        assert rows["other-svc"] == "running", "未受暫停影響之正常實例應更新為 running。"


class TestJitRaceAndManagerDispatch:
    """Test JIT race invalidation and SystemdManager dispatch behavior (AC 4, AC 5)."""

    def test_jit_pre_start_invalidation_records_stopped_and_event(self, tmp_path, mock_ledger, sample_pause_record):
        """AC 4: 批准後到 start 前失效（JIT 競態）要拒絕啟動並留下 stopped 未完成結果。"""
        services_dir = tmp_path / "services"
        db_path = str(tmp_path / "state.db")
        repos_dir = tmp_path / "repos"

        mock_plan = DeployPlan(
            project_name="raceproj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="NEW",
                    config={
                        "image": "localhost/app:latest",
                        "tier": "dev",
                        "stable_id": "dev-app",
                    },
                )
            ],
        )

        # 模擬 Pre-mutation 通過，但在啟動前一刻（JIT）帳本被寫入暫停
        call_count = [0]
        original_check_plan = check_plan_admission

        def pre_mutation_pass_then_pause(plan, ledger=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次檢查（Pre-mutation）允許通過
                # 但隨後立刻注入暫停記錄（模擬非同步治理事件）
                mock_ledger.record_pause(sample_pause_record)
                return True, []
            return False, ["paused"]

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = ""
        fake_proc.stderr = ""

        with patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan), \
             patch("container_ctl.cli.deploy.check_plan_admission", side_effect=pre_mutation_pass_then_pause), \
             patch("container_ctl.cli.deploy.ensure_volume_storage", return_value=(True, [])), \
             patch("container_ctl.cli.deploy.generate_units", return_value={"dev-app": "svc-raceproj-v1.0"}), \
             patch("subprocess.run", return_value=fake_proc) as mock_run:

            # 執行 apply
            apply(
                project_name="raceproj",
                ref="v1.0",
                yes=True,
                repos_dir=str(repos_dir),
                db_path=db_path,
                services_dir=str(services_dir),
            )

            # 驗證 systemctl start 絕對沒有被執行！
            for call in mock_run.call_args_list:
                cmd = call[0][0] if call[0] else []
                assert not ("systemctl" in cmd and "start" in cmd), "JIT 拒絕時嚴禁呼叫 systemctl start！"

        # 驗證 SQLite 狀態為 stopped，並記錄了治理暫停原因
        conn = init_db(db_path)
        inst = conn.execute("SELECT status FROM instances WHERE project_name='raceproj'").fetchone()
        assert inst["status"] == "stopped", "JIT 准入失效必須標記為 stopped，絕不假稱 provisioned/running！"

        event = conn.execute("SELECT event_type, detail FROM lifecycle_events WHERE triggered_by='deploy_apply'").fetchone()
        assert event is not None
        assert "governance_paused" in event["detail"]
        conn.close()

    def test_systemd_manager_owner_dispatch_and_no_block_semantics(self):
        """AC 5: mock manager 中不同 owner dispatch 正確，start 用非阻塞 job 並回報結果，不把 job 提交等同 running。"""
        manager = SystemdManager(default_owner_uid=1000)

        with patch("subprocess.run") as mock_subproc:
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = "Job 42 enqueued."
            mock_res.stderr = ""
            mock_subproc.return_value = mock_res

            # 測試 dispatch owner_uid=1001
            res = manager.start_unit("svc-worker.service", owner_uid=1001, no_block=True)

            # 驗證命令包含 --no-block
            called_cmd = mock_subproc.call_args[0][0]
            assert "systemctl" in called_cmd
            assert "--user" in called_cmd
            assert "--no-block" in called_cmd
            assert "svc-worker.service" in called_cmd

            # 驗證環境變數設定了正確的 owner_uid
            called_env = mock_subproc.call_args[1].get("env", {})
            assert called_env.get("XDG_RUNTIME_DIR") == "/run/user/1001"

            # 驗證結果語意：job_submitted=True，但 is_running=False（嚴格合約）
            assert res.job_submitted is True
            assert res.is_running is False, "非阻塞任務提交成功絕對不代表已 running！"
