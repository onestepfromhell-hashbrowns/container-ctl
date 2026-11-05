"""
test_volume_preflight_guardrails.py — 容器持久化 Volume 預檢與 Quadlet Exit 125 熔斷防禦加固測試
涵蓋:
- TICKET-VOL-01 + A-01..A-06: Preflight Volume 存在性、Canonical Containment、Auto-create 嚴格邊界與 CQS 解耦
- TICKET-VOL-02: Quadlet Service 注入 RestartPreventExitStatus=125 與 Pure Generator
- TICKET-VOL-03: Deploy CLI 整合、專案名一致性檢查與無副作用 Preview 驗證
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.preflight import (
    _check_image_digests,
    _check_volumes,
    run_preflight,
)
from container_ctl.quadlet_gen import (
    _render_container_unit,
    generate_units,
)
from container_ctl.volume_resolver import (
    ALLOWED_AUTO_CREATE_ROOTS,
    ALLOWED_MOUNT_READ_ROOTS,
    DEFAULT_VOLUMES_DIR,
    FORBIDDEN_WRITE_PATHS,
    ResolvedVolumeMount,
    ensure_volume_storage,
    resolve_volume_mount,
    validate_volumes,
)

SAMPLE_SHA256 = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ── TICKET-VOL-01 / A-01..A-06: Volume 解析器與安全預檢 ───────────────────────

class TestVolumeResolverAndGuardrails:
    """測試 Volume SSOT 解析器、Canonical Containment 與邊界防衛"""

    def test_canonical_containment_existing_dir(self, tmp_path):
        """已存在之目錄且位於允許讀取根目錄內應成功解析"""
        read_root = (tmp_path / "srv/site").resolve()
        vol_dir = read_root / "projects/.containers/storage/volumes/proj1/db"
        vol_dir.mkdir(parents=True, exist_ok=True)

        vol = {
            "host_path": str(vol_dir),
            "container_path": "/var/lib/postgresql/data",
            "options": "rw",
        }

        resolved, err = resolve_volume_mount(
            vol,
            project="proj1",
            service_name="db",
            allowed_read_roots=(read_root,),
            allowed_create_roots=((read_root / "projects/.containers/storage/volumes").resolve(),),
            forbidden_write_paths=(),
        )

        assert err is None
        assert resolved is not None
        assert resolved.host_path == vol_dir
        assert resolved.exists is True
        assert resolved.is_auto_creatable is False

    def test_path_traversal_attack_trapped(self, tmp_path):
        """路徑穿越（如 /opt/container-ctl/../../etc）即使字串符合前綴也必須被 Canonical Containment 阻斷"""
        read_root = (tmp_path / "opt/site").resolve()
        read_root.mkdir(parents=True, exist_ok=True)

        malicious_path = str(read_root) + "/../../etc/shadow"
        vol = {
            "host_path": malicious_path,
            "container_path": "/workspace/leak",
        }

        resolved, err = resolve_volume_mount(
            vol,
            project="proj1",
            service_name="evil",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(),
            forbidden_write_paths=(),
        )

        assert resolved is None
        assert err is not None
        assert "未在允許白名單中" in err

    def test_symlink_escape_trapped(self, tmp_path):
        """指向白名單外部之 Symlink 必須被 resolve() 辨識並拒絕"""
        read_root = (tmp_path / "srv/site").resolve()
        read_root.mkdir(parents=True, exist_ok=True)

        outside_target = (tmp_path / "outside_secret").resolve()
        outside_target.mkdir(parents=True, exist_ok=True)

        symlink_path = read_root / "link_to_outside"
        symlink_path.symlink_to(outside_target)

        vol = {
            "host_path": str(symlink_path),
            "container_path": "/workspace/secret",
        }

        resolved, err = resolve_volume_mount(
            vol,
            project="proj1",
            service_name="app",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(),
            forbidden_write_paths=(),
        )

        assert resolved is None
        assert err is not None
        assert "未在允許白名單中" in err

    def test_existing_regular_file_rejected(self, tmp_path):
        """掛載來源若為一般檔案（非目錄）必須報錯阻斷 (F-03)"""
        read_root = (tmp_path / "srv/site").resolve()
        read_root.mkdir(parents=True, exist_ok=True)

        file_path = read_root / "config.txt"
        file_path.write_text("not a directory")

        vol = {
            "host_path": str(file_path),
            "container_path": "/data",
        }

        resolved, err = resolve_volume_mount(
            vol,
            project="proj1",
            service_name="app",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(),
            forbidden_write_paths=(),
        )

        assert resolved is None
        assert err is not None
        assert "必須為目錄 (非一般檔案" in err

    def test_auto_create_allowed_only_in_volumes_root(self, tmp_path):
        """缺失目錄僅限在 ALLOWED_AUTO_CREATE_ROOTS 內才允許標記 is_auto_creatable (A-02)"""
        read_root = (tmp_path / "srv/site").resolve()
        create_root = (read_root / "projects/.containers/storage/volumes").resolve()
        forbidden_root = (read_root / "projects/.git_repos").resolve()

        # 1. 位於合法 auto-create 區域
        valid_missing = create_root / "proj1/redis"
        vol_valid = {"host_path": str(valid_missing), "container_path": "/data"}
        res1, err1 = resolve_volume_mount(
            vol_valid, project="proj1", service_name="redis",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(create_root,),
            forbidden_write_paths=(forbidden_root,),
        )
        assert err1 is None
        assert res1.is_auto_creatable is True
        assert res1.exists is False

        # 2. 位於 Bare Repo 或非 Volume 區，嘗試自動建立應被拒絕
        invalid_missing = forbidden_root / "repo.git/hooks"
        vol_invalid = {"host_path": str(invalid_missing), "container_path": "/hooks"}
        res2, err2 = resolve_volume_mount(
            vol_invalid, project="proj1", service_name="git",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(create_root,),
            forbidden_write_paths=(forbidden_root,),
        )
        assert res2 is None
        assert err2 is not None
        assert "非允許自動補建之專屬儲存區" in err2

    def test_cqs_preflight_has_zero_side_effects(self, tmp_path):
        """驗證 validate_volumes 純唯讀（不建立目錄），ensure_volume_storage 才建立目錄 (A-03)"""
        read_root = (tmp_path / "srv/site").resolve()
        create_root = (read_root / "projects/.containers/storage/volumes").resolve()
        target_dir = create_root / "proj1/cache"
        assert not target_dir.exists()

        blueprints = [
            {
                "name": "cache",
                "volumes": [{"host_path": str(target_dir), "container_path": "/data"}],
            }
        ]

        # 執行唯讀預檢
        ok, msgs, resolved = validate_volumes(
            blueprints,
            project="proj1",
            allowed_read_roots=(read_root,),
            allowed_create_roots=(create_root,),
            forbidden_write_paths=(),
        )

        assert ok is True
        assert not target_dir.exists(), "validate_volumes 絕不可擅自建立實體目錄！"
        assert len(resolved) == 1
        assert resolved[0].is_auto_creatable is True

        # 執行確認後的實體補建
        ensure_ok, ensure_msgs = ensure_volume_storage(resolved)
        assert ensure_ok is True
        assert target_dir.exists(), "ensure_volume_storage 應成功建立目錄"

    def test_pinned_digest_enforcement(self):
        """驗證 _check_image_digests 拒絕 :latest 與無 @sha256 雜湊的映像檔 (A-05)"""
        # 1. 違規案例：:latest
        bp_latest = [{"name": "app", "image": "docker.io/library/nginx:latest"}]
        ok1, msg1 = _check_image_digests(bp_latest)
        assert ok1 is False
        assert "禁止 :latest" in msg1

        # 2. 違規案例：一般 tag 未鎖定 digest
        bp_tag = [{"name": "app", "image": "docker.io/library/nginx:1.25-alpine"}]
        ok2, msg2 = _check_image_digests(bp_tag)
        assert ok2 is False

        # 3. 合規案例：包含 @sha256:[64hex]
        bp_digest = [{"name": "app", "image": f"docker.io/library/nginx@{SAMPLE_SHA256}"}]
        ok3, msg3 = _check_image_digests(bp_digest)
        assert ok3 is True
        assert "[OK]" in msg3

    def test_malformed_inputs_handled_gracefully(self):
        """傳入非 dict 或無效 blueprint / volume 時能優雅處理無 unhandled crash (A-06)"""
        bad_bps = [
            "not a dict",
            {"name": "app", "volumes": "not a list"},
            {"name": "app", "volumes": ["not a dict"]},
            {"name": "app", "volumes": [{"container_path": None}]},
        ]
        ok, msgs, resolved = validate_volumes(bad_bps)
        assert ok is False
        assert any("[ERROR]" in m for m in msgs)


# ── TICKET-VOL-02: Quadlet Service 注入與純 Generator ────────────────────────

class TestQuadletExit125Guardrail:
    """測試 Layer 2: Quadlet Service 注入 RestartPreventExitStatus=125 與純文本生成"""

    def _make_action(self, name="db", tier="prod", volumes=None):
        return ServiceAction(
            name=name,
            action="NEW",
            config={
                "name": name,
                "tier": tier,
                "image": f"docker.io/{name}@{SAMPLE_SHA256}",
                "volumes": volumes or [],
            },
        )

    def test_render_container_unit_has_restart_prevent_exit_status_125(self):
        """驗證生成的 Quadlet 單元 [Service] 區段包含 RestartPreventExitStatus=125"""
        action = self._make_action("postgres", "prod")
        unit_text = _render_container_unit(action, f"docker.io/postgres@{SAMPLE_SHA256}", project="demo")
        assert "RestartPreventExitStatus=125" in unit_text

    def test_render_container_unit_cgroup_limits_prod_default(self):
        """驗證 prod-tier 容器預設 MemoryMax=1G"""
        action = self._make_action("app", "prod")
        unit_text = _render_container_unit(action, f"docker.io/app@{SAMPLE_SHA256}", project="demo")
        assert "MemoryMax=1G" in unit_text

    def test_render_container_unit_cgroup_limits_dev_default(self):
        """驗證 dev-tier 容器預設 MemoryMax=2G 與 TasksMax=2048"""
        action = self._make_action("app", "dev")
        unit_text = _render_container_unit(action, f"docker.io/app@{SAMPLE_SHA256}", project="demo")
        assert "MemoryMax=2G" in unit_text
        assert "TasksMax=2048" in unit_text

    def test_render_container_unit_cgroup_limits_custom_override(self):
        """驗證 config 可自訂覆寫 memory_max 與 tasks_max"""
        action = ServiceAction(
            name="big-app",
            action="NEW",
            config={
                "name": "big-app",
                "tier": "dev",
                "image": f"docker.io/big-app@{SAMPLE_SHA256}",
                "memory_max": "4G",
                "tasks_max": "512",
            },
        )
        unit_text = _render_container_unit(action, f"docker.io/big-app@{SAMPLE_SHA256}", project="demo")
        assert "MemoryMax=4G" in unit_text
        assert "TasksMax=512" in unit_text

    def test_render_container_unit_does_not_mutate_filesystem(self, tmp_path):
        """Quadlet 生成器為純函數轉換，生成時不得建立缺失目錄 (A-04)"""
        missing_vol = tmp_path / "srv/site/projects/.containers/storage/volumes/demo/db"
        assert not missing_vol.exists()

        action = self._make_action(
            "db",
            "prod",
            volumes=[{"host_path": str(missing_vol), "container_path": "/var/lib/data"}],
        )

        with patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.volume_resolver.ALLOWED_AUTO_CREATE_ROOTS", (tmp_path.resolve(),)):
            unit_text = _render_container_unit(action, f"docker.io/db@{SAMPLE_SHA256}", project="demo")
            assert not missing_vol.exists(), "Quadlet 生成器不應暗中呼叫 mkdir！"
            assert f"Volume={missing_vol}:/var/lib/data:rw" in unit_text


# ── TICKET-VOL-03: Deploy CLI 整合測試 ────────────────────────────────────────

class TestDeployCLIIntegration:
    """測試 Layer 1 & 2 在 Deploy CLI 流程中的整合"""

    def test_deploy_apply_aborts_if_preflight_fails(self, tmp_path):
        """當 preflight 失敗時，deploy apply 應中斷退出"""
        from container_ctl.cli.deploy import apply

        with patch("container_ctl.cli.deploy.run_preflight", return_value=False), \
             pytest.raises(SystemExit) as exc_info:
            apply("testproj", "v1.0", yes=True, db_path=str(tmp_path / "test.db"))
        assert exc_info.value.code == 1

    def test_deploy_apply_aborts_if_project_name_mismatch(self, tmp_path):
        """CLI project_name 與 Manifest project_name 不一致時必須阻斷 (F-01)"""
        from container_ctl.cli.deploy import apply

        mock_plan = DeployPlan(
            project_name="manifest_proj",
            version="v1.0",
            actions=[],
        )

        with patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
             patch("container_ctl.reconcile.reconcile_instances"), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan), \
             pytest.raises(SystemExit) as exc_info:
            apply("cli_proj", "v1.0", yes=True, db_path=str(tmp_path / "test.db"))
        assert exc_info.value.code == 1

    def test_deploy_apply_no_side_effects_when_cancelled(self, tmp_path):
        """使用者在 apply 確認時輸入 'n' 取消，檔案系統不得建立任何 Volume 目錄 (A-03)"""
        from container_ctl.cli.deploy import apply
        db_p = str(tmp_path / "test.db")
        vol_p = tmp_path / "srv/site/projects/.containers/storage/volumes/testproj/db"

        mock_plan = DeployPlan(
            project_name="testproj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="db",
                    action="NEW",
                    config={
                        "name": "db",
                        "tier": "prod",
                        "image": f"docker.io/postgres@{SAMPLE_SHA256}",
                        "volumes": [{"host_path": str(vol_p), "container_path": "/var/lib/postgresql/data"}],
                    },
                )
            ],
        )

        with patch("container_ctl.cli.deploy.run_preflight", side_effect=[True, True]), \
             patch("container_ctl.reconcile.reconcile_instances"), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan), \
             patch("builtins.input", return_value="n"):
            apply("testproj", "v1.0", yes=False, db_path=db_p, services_dir=str(tmp_path / "services"))

        assert not vol_p.exists(), "取消部署後不應留下建立的 Volume 目錄！"

    def test_deploy_apply_volume_provision_success(self, tmp_path):
        """deploy apply 確認執行後，成功補建 Volume 目錄並生成 Quadlet"""
        from container_ctl.cli.deploy import apply
        db_p = str(tmp_path / "test.db")
        vol_p = tmp_path / "srv/site/projects/.containers/storage/volumes/testproj/db"

        mock_plan = DeployPlan(
            project_name="testproj",
            version="v1.0",
            actions=[
                ServiceAction(
                    name="db",
                    action="NEW",
                    config={
                        "name": "db",
                        "tier": "prod",
                        "image": f"docker.io/postgres@{SAMPLE_SHA256}",
                        "volumes": [{"host_path": str(vol_p), "container_path": "/var/lib/postgresql/data"}],
                    },
                )
            ],
        )

        with patch("container_ctl.cli.deploy.run_preflight", side_effect=[True, True]), \
             patch("container_ctl.reconcile.reconcile_instances"), \
             patch("container_ctl.cli.deploy.plan", return_value=mock_plan), \
             patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.volume_resolver.ALLOWED_AUTO_CREATE_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.cli.deploy.generate_units", return_value={"db": "svc-testproj-db"}), \
             patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0)
            apply("testproj", "v1.0", yes=True, db_path=db_p, services_dir=str(tmp_path / "services"))

        assert vol_p.exists(), "確認執行 apply 後應成功補建目錄！"


class TestSingleFileReadOnlyMount:
    def _resolve(self, tmp_path, name, options):
        root = (tmp_path / "srv/site").resolve()
        root.mkdir(parents=True, exist_ok=True)
        f = root / name
        f.write_text("x")
        return resolve_volume_mount(
            {"host_path": str(f), "container_path": "/etc/opt/P/secrets/x", "options": options},
            project="p", service_name="s",
            allowed_read_roots=(root,), allowed_create_roots=(), forbidden_write_paths=(),
        )

    def test_ro_regular_file_allowed(self, tmp_path):
        resolved, err = self._resolve(tmp_path, "pg_password", "ro")
        assert err is None and resolved is not None

    def test_rw_file_rejected(self, tmp_path):
        resolved, err = self._resolve(tmp_path, "pg_password", "rw")
        assert resolved is None and "必須為目錄" in err

    def test_private_key_file_rejected_even_ro(self, tmp_path):
        resolved, err = self._resolve(tmp_path, "server.key", "ro")
        assert resolved is None and "必須為目錄" in err
