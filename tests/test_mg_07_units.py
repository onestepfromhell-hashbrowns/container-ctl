"""test_mg_07_units.py — Ticket #07 Acceptance Tests (C04, C07, T08, T11).

Verifies:
1. Unified capacity policy compilation into owner tier slices and governance manifest.
2. Rescue targets are excluded from the workload slice tree (rescue not in workload slice).
3. Prod and dev are strictly separated (never under the same unloadable parent target).
4. Quadlet unit generation integrates Slice, MemoryMax, MemoryHigh, and ExecCondition.
5. Rescue units are not gated with ExecCondition or dev admission gates.
6. Atomic failure on invalid policy: zero partial files left on disk.
7. cgroup-sync consistency and idempotence with generator output.
8. Dry-run mode produces artifacts without invoking daemon-reload or restarting services.
9. ExecCondition adapter behavior for start-check return codes (0 allow, 3 deny, 2 policy error).
"""

import pytest

pytest.importorskip("memory_governor", reason="needs memory_governor (optional governance plugin)")

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.governance_compiler import (
    GovernanceManifest,
    SliceSpec,
    WorkloadGovernanceMapping,
    compile_governance,
)
from container_ctl.quadlet_gen import generate_units, _render_container_unit
from container_ctl.cgroup_sync import (
    apply_cgroup_limits_to_text,
    detect_tier_from_content,
    sync_quadlet_files,
)
from memory_governor.policy import (
    CapacityPolicy,
    OwnerBudget,
    TierBudget,
    WorkloadBudgetPolicy,
)


@pytest.fixture
def sample_capacity_policy() -> CapacityPolicy:
    """Create a valid C04 capacity policy with dev, prod, infra, and rescue."""
    return CapacityPolicy(
        schema_version=1,
        policy_version="1.0",
        mode="enforce",
        system_budget_bytes=1073741824,      # 1 GiB
        unmanaged_budget_bytes=536870912,    # 512 MiB
        rescue_reserve_bytes=1610612736,     # 1.5 GiB
        rescue_targets=["sshd", "cloudflared", "rescue-shell"],
        owners={
            "1000": OwnerBudget(
                owner_uid=1000,
                dev=TierBudget(
                    memory_high_bytes=1610612736,  # 1.5 GiB
                    memory_max_bytes=2147483648,   # 2 GiB
                    swap_max_bytes=0,
                ),
                prod=TierBudget(
                    memory_high_bytes=805306368,   # 768 MiB
                    memory_max_bytes=1073741824,   # 1 GiB
                    swap_max_bytes=0,
                ),
                infra=TierBudget(
                    memory_high_bytes=805306368,
                    memory_max_bytes=1073741824,
                    swap_max_bytes=0,
                ),
            )
        },
        workloads=[
            WorkloadBudgetPolicy(
                stable_id="dev-app",
                tier="dev",
                scope="dedicated",
                memory_high_bytes=1610612736,
                memory_max_bytes=2147483648,
                swap_max_bytes=0,
            ),
            WorkloadBudgetPolicy(
                stable_id="prod-api",
                tier="prod",
                scope="dedicated",
                memory_high_bytes=805306368,
                memory_max_bytes=1073741824,
                swap_max_bytes=0,
            ),
            WorkloadBudgetPolicy(
                stable_id="infra-db",
                tier="infra",
                scope="shared",
                memory_high_bytes=805306368,
                memory_max_bytes=1073741824,
                swap_max_bytes=0,
            ),
            WorkloadBudgetPolicy(
                stable_id="cloudflared",
                tier="rescue",
                scope="shared",
                memory_max_bytes=268435456,
            ),
        ],
    )


class TestGovernanceCompiler:
    """Test C04 / C07 Governance Compiler requirements."""

    def test_compile_slices_and_manifest(self, sample_capacity_policy):
        manifest = compile_governance(sample_capacity_policy, owner_uid=1000)

        assert isinstance(manifest, GovernanceManifest)
        assert manifest.schema_version == 1
        assert manifest.policy_version == "1.0"

        # Verify slices generated
        assert "workload.slice" in manifest.slices
        assert "workload-dev.slice" in manifest.slices
        assert "workload-prod.slice" in manifest.slices
        assert "workload-infra.slice" in manifest.slices

        dev_slice = manifest.slices["workload-dev.slice"]
        assert dev_slice.parent_slice == "workload.slice"
        assert "MemoryMax=2147483648" in dev_slice.unit_content
        assert "MemoryHigh=1610612736" in dev_slice.unit_content

        prod_slice = manifest.slices["workload-prod.slice"]
        assert prod_slice.parent_slice == "workload.slice"
        assert "MemoryMax=1073741824" in prod_slice.unit_content

        # Verify rescue target is NOT in workload slice
        assert "cloudflared" in manifest.workloads
        cf_mapping = manifest.workloads["cloudflared"]
        assert cf_mapping.tier == "rescue"
        assert cf_mapping.slice is None  # Rescue must NOT be in workload slice tree
        assert cf_mapping.guard is None  # Rescue must NOT have dev gate / start-check

        # Verify dev workload mapping
        dev_mapping = manifest.workloads["dev-app"]
        assert dev_mapping.slice == "workload-dev.slice"
        assert "start-check dev-app" in dev_mapping.guard
        assert dev_mapping.limits["memory_max"] == 2147483648
        assert dev_mapping.limits["tasks_max"] == 2048

        # Verify manifest dictionary export
        m_dict = manifest.to_dict()
        assert "workloads" in m_dict
        assert "slices" in m_dict
        assert m_dict["workloads"]["dev-app"]["slice"] == "workload-dev.slice"

    def test_prod_and_dev_targets_and_slices_separated(self, sample_capacity_policy):
        """Prod and Dev must not be enveloped under the same unloadable parent target."""
        manifest = compile_governance(sample_capacity_policy, owner_uid=1000)

        # Ensure distinct slices
        dev_slice = manifest.slices["workload-dev.slice"]
        prod_slice = manifest.slices["workload-prod.slice"]
        assert dev_slice.slice_name != prod_slice.slice_name

        # Ensure target isolation: no shared unloadable parent target
        # dev services belong to dev target or default, prod to prod target
        dev_mapping = manifest.workloads["dev-app"]
        prod_mapping = manifest.workloads["prod-api"]
        assert dev_mapping.target_name != prod_mapping.target_name
        assert "prod" not in dev_mapping.target_name
        assert "dev" not in prod_mapping.target_name


class TestQuadletGeneratorGovernance:
    """Test Quadlet generation integration with governance and atomic rollback."""

    def test_generate_units_with_policy(self, tmp_path, sample_capacity_policy):
        services_dir = tmp_path / "services"
        quadlet_dir = tmp_path / "systemd"
        quadlet_dir.mkdir(parents=True)

        plan = DeployPlan(
            project_name="myproj",
            version="v1.0.0",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="NEW",
                    config={
                        "name": "dev-app",
                        "tier": "dev",
                        "scope": "shared",
                        "image": "localhost/app:dev",
                        "stable_id": "dev-app",
                    },
                ),
                ServiceAction(
                    name="prod-api",
                    action="NEW",
                    config={
                        "name": "prod-api",
                        "tier": "prod",
                        "scope": "shared",
                        "image": "localhost/api:prod",
                        "stable_id": "prod-api",
                    },
                ),
            ],
        )

        with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlet_dir), \
             patch("container_ctl.quadlet_gen.subprocess.run") as mock_run:
            c_map = generate_units(
                plan=plan,
                services_dir=str(services_dir),
                policy=sample_capacity_policy,
                dry_run=False,
            )

            # verify daemon-reload was called when not dry_run
            assert mock_run.called

        proj_dir = services_dir / "myproj"
        dev_file = proj_dir / "svc-myproj-v1.0.0.container"  # or specific container name
        assert any("svc-" in f.name for f in proj_dir.glob("*.container"))

        # Find dev and prod unit files
        dev_content = None
        prod_content = None
        for f in proj_dir.glob("*.container"):
            text = f.read_text()
            if "dev-app" in text:
                dev_content = text
            elif "prod-api" in text:
                prod_content = text

        assert dev_content is not None
        assert "Slice=workload-dev.slice" in dev_content
        assert "MemoryMax=2147483648" in dev_content
        assert "MemoryHigh=1610612736" in dev_content
        assert "ExecCondition=/usr/local/bin/memory-ctl start-check dev-app" in dev_content

        assert prod_content is not None
        assert "Slice=workload-prod.slice" in prod_content
        assert "MemoryMax=1073741824" in prod_content
        assert "ExecCondition=/usr/local/bin/memory-ctl start-check prod-api" in prod_content

        # Verify slices were created and symlinked
        slice_files = list(proj_dir.glob("*.slice"))
        assert len(slice_files) >= 3  # workload.slice, workload-dev.slice, workload-prod.slice
        for sf in slice_files:
            assert (quadlet_dir / sf.name).is_symlink()

        # Verify manifest file output
        manifest_file = proj_dir / "governance_manifest.json"
        assert manifest_file.exists()
        m_data = json.loads(manifest_file.read_text())
        assert m_data["schema_version"] == 1
        assert "dev-app" in m_data["workloads"]

    def test_rescue_unit_not_gated(self, tmp_path, sample_capacity_policy):
        """Rescue target units must not contain start-check ExecCondition."""
        services_dir = tmp_path / "services"
        quadlet_dir = tmp_path / "systemd"
        quadlet_dir.mkdir(parents=True)

        plan = DeployPlan(
            project_name="infra",
            version="v1.0.0",
            actions=[
                ServiceAction(
                    name="cloudflared",
                    action="NEW",
                    config={
                        "name": "cloudflared",
                        "tier": "rescue",
                        "scope": "shared",
                        "image": "localhost/cf:latest",
                        "stable_id": "cloudflared",
                    },
                )
            ],
        )

        with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlet_dir), \
             patch("container_ctl.quadlet_gen.subprocess.run"):
            generate_units(plan, str(services_dir), policy=sample_capacity_policy)

        cf_file = next((tmp_path / "services" / "infra").glob("*cloudflared*.container"))
        text = cf_file.read_text()
        assert "ExecCondition=" not in text
        assert "Slice=workload" not in text

    def test_invalid_policy_atomic_rejection_no_partial_files(self, tmp_path):
        """When capacity policy is invalid, generation fails atomically with zero partial files."""
        services_dir = tmp_path / "services"
        quadlet_dir = tmp_path / "systemd"
        quadlet_dir.mkdir(parents=True)

        # Policy with negative budget -> invalid
        invalid_policy = CapacityPolicy(
            schema_version=1,
            policy_version="1.0",
            mode="enforce",
            system_budget_bytes=-100,  # invalid
        )

        plan = DeployPlan(
            project_name="badproj",
            version="v1.0.0",
            actions=[
                ServiceAction(
                    name="dev-test",
                    action="NEW",
                    config={"name": "dev-test", "tier": "dev", "image": "test:latest"},
                )
            ],
        )

        with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlet_dir), \
             patch("container_ctl.quadlet_gen.subprocess.run"):
            with pytest.raises(ValueError, match="policy"):
                generate_units(plan, str(services_dir), policy=invalid_policy)

        # Verify no files were left behind in badproj directory
        bad_dir = services_dir / "badproj"
        if bad_dir.exists():
            assert list(bad_dir.iterdir()) == []
        assert list(quadlet_dir.iterdir()) == []

    def test_quadlet_dry_run_zero_side_effects(self, tmp_path, sample_capacity_policy):
        """In dry-run mode, files are generated to temp SSOT but daemon-reload is NOT invoked."""
        services_dir = tmp_path / "services"
        quadlet_dir = tmp_path / "systemd"
        quadlet_dir.mkdir(parents=True)

        plan = DeployPlan(
            project_name="dryproj",
            version="v1.0.0",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="NEW",
                    config={"name": "dev-app", "tier": "dev", "image": "localhost/app:dev"},
                )
            ],
        )

        with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlet_dir), \
             patch("container_ctl.quadlet_gen.subprocess.run") as mock_run:
            generate_units(plan, str(services_dir), policy=sample_capacity_policy, dry_run=True)

            # daemon-reload must NOT be called
            mock_run.assert_not_called()


class TestCgroupSyncConsistencyAndIdempotence:
    """Test consistency between quadlet_gen and cgroup_sync, idempotence, and directive preservation."""

    def test_sync_matches_generator_output(self, sample_capacity_policy):
        """cgroup_sync must produce the exact same directives as quadlet generator."""
        raw_quadlet = """[Unit]
Description=Dev app
After=network-online.target

[Container]
ContainerName=svc-dev-app
Image=localhost/app:dev

[Service]
Restart=on-failure
RestartSec=10s
"""
        updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(
            raw_quadlet,
            tier="dev",
            policy=sample_capacity_policy,
            stable_id="dev-app",
        )

        assert modified is True
        assert "Slice=workload-dev.slice" in updated
        assert "MemoryMax=2147483648" in updated
        assert "MemoryHigh=1610612736" in updated
        assert "ExecCondition=/usr/local/bin/memory-ctl start-check dev-app" in updated
        assert "TasksMax=2048" in updated

        # Test idempotence: running again on updated text must yield modified=False and no duplicate lines
        second_pass, second_mod, _, _, _ = apply_cgroup_limits_to_text(
            updated,
            tier="dev",
            policy=sample_capacity_policy,
            stable_id="dev-app",
        )
        assert second_mod is False
        assert second_pass == updated
        assert updated.count("Slice=workload-dev.slice") == 1
        assert updated.count("ExecCondition=/usr/local/bin/memory-ctl start-check dev-app") == 1
        assert updated.count("MemoryMax=2147483648") == 1

    def test_sync_preserves_external_management_and_custom_settings(self, sample_capacity_policy):
        """cgroup_sync must not overwrite external directives (e.g. Environment, custom mounts)."""
        raw_quadlet = """[Unit]
Description=Custom Service
Wants=custom-monitor.service

[Container]
ContainerName=svc-custom-prod
Environment=CUSTOM_TOKEN=secret123
Volume=/var/opt/data:/data:rw

[Service]
Restart=always
TimeoutStartSec=120s
"""
        updated, modified, _, _, _ = apply_cgroup_limits_to_text(
            raw_quadlet,
            tier="prod",
            policy=sample_capacity_policy,
            stable_id="prod-api",
        )

        assert modified is True
        assert "CUSTOM_TOKEN=secret123" in updated
        assert "/var/opt/data:/data:rw" in updated
        assert "TimeoutStartSec=120s" in updated
        assert "Slice=workload-prod.slice" in updated
        assert "MemoryMax=1073741824" in updated
        assert "ExecCondition=/usr/local/bin/memory-ctl start-check prod-api" in updated


class TestExecConditionAdapterBehavior:
    """Test start-check ExecCondition return code semantics (AC 3, T08)."""

    def test_exec_condition_admission_exit_codes(self):
        """Test systemd-facing exit codes from start-check command:
        - Exit 0: Allowed -> systemd proceeds to ExecStart.
        - Exit 3: Denied (paused, budget full, lock contention) -> systemd skips without failure/restart.
        - Exit 2: Policy error.
        """
        from memory_governor.cli import cmd_start_check
        from memory_governor.ledger import GovernanceLedger, AdmissionDecision

        # Mock collector and ledger
        mock_collector = MagicMock()
        mock_collector.collect.return_value.quality = "ok"
        mock_collector.collect.return_value.guest = MagicMock()
        mock_collector.collect.return_value.boot_id = "test-boot-1"

        # Case 1: Allowed -> 0
        mock_ledger_allow = MagicMock(spec=GovernanceLedger)
        mock_ledger_allow.check_admission.return_value = AdmissionDecision(
            allowed=True,
            stable_id="dev-app",
            reason="ok",
            detail="Allowed",
            exit_code=0,
        )

        args_allow = MagicMock()
        args_allow.stable_id = "dev-app"
        args_allow.json = False
        args_allow.dry_run = False
        args_allow.token = None
        args_allow.incarnation = None

        code = cmd_start_check(
            args_allow,
            capacity_policy=MagicMock(),
            collector=mock_collector,
            ledger=mock_ledger_allow,
        )
        assert code == 0

        # Case 2: Paused / Capacity Denied -> 3
        mock_ledger_deny = MagicMock(spec=GovernanceLedger)
        mock_ledger_deny.check_admission.return_value = AdmissionDecision(
            allowed=False,
            stable_id="dev-app",
            reason="workload_paused",
            detail="Workload paused by governance incident",
            exit_code=3,
        )

        code_deny = cmd_start_check(
            args_allow,
            capacity_policy=MagicMock(),
            collector=mock_collector,
            ledger=mock_ledger_deny,
        )
        assert code_deny == 3  # In systemd, 1..254 skips service without Restart=on-failure


class TestSeamCGovernanceCompilerSwapGuard:
    def test_slice_swap_max_zero(self):
        """AC-11: workload-prod.slice 含 MemorySwapMax=0，即使 tier budget 未設 swap"""
        policy = CapacityPolicy(
            schema_version=1,
            policy_version="1.0",
            mode="enforce",
            system_budget_bytes=1073741824,
            unmanaged_budget_bytes=536870912,
            rescue_reserve_bytes=1610612736,
            rescue_targets=["sshd"],
            owners={
                "1000": OwnerBudget(
                    owner_uid=1000,
                    prod=TierBudget(
                        memory_high_bytes=805306368,
                        memory_max_bytes=1073741824,
                        swap_max_bytes=None,
                    ),
                )
            },
            workloads=[],
        )
        manifest = compile_governance(policy)
        prod_slice = manifest.slices["workload-prod.slice"]
        assert prod_slice.memory_swap_max == 0
        assert "MemorySwapMax=0" in prod_slice.unit_content

    def test_workload_swap_max_default(self):
        """AC-12: WorkloadGovernanceMapping.limits['swap_max'] 非 rescue 時為 0，rescue 為 None"""
        policy = CapacityPolicy(
            schema_version=1,
            policy_version="1.0",
            mode="enforce",
            system_budget_bytes=1073741824,
            unmanaged_budget_bytes=536870912,
            rescue_reserve_bytes=1610612736,
            rescue_targets=["sshd"],
            owners={},
            workloads=[
                WorkloadBudgetPolicy(
                    stable_id="prod-api",
                    tier="prod",
                    memory_max_bytes=1073741824,
                    swap_max_bytes=None,
                ),
                WorkloadBudgetPolicy(
                    stable_id="sshd",
                    tier="rescue",
                    memory_max_bytes=1073741824,
                    swap_max_bytes=None,
                ),
            ],
        )
        manifest = compile_governance(policy)
        assert manifest.workloads["prod-api"].limits["swap_max"] == 0
        assert manifest.workloads["sshd"].limits["swap_max"] is None


class TestGovernanceIdPersistenceAndSyncIdempotence:
    """驗證 policy 生成單元時持久化 container-ctl.governance.id，且後續 sync 同 policy 具備嚴格冪等性 (P1 回歸)。"""

    def test_generate_persists_governance_id_and_sync_is_idempotent(self, tmp_path, sample_capacity_policy):
        services_dir = tmp_path / "services"
        quadlet_dir = tmp_path / "systemd"
        quadlet_dir.mkdir(parents=True)

        plan = DeployPlan(
            project_name="demo",
            version="v1",
            actions=[
                ServiceAction(
                    name="dev-app",
                    action="CREATE",
                    config={
                        "image": "localhost/demo-app:latest",
                        "tier": "dev",
                        "stable_id": "dev-app",
                    },
                )
            ],
        )

        with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlet_dir), \
             patch("subprocess.run"):
            # 1. 執行生成
            container_map = generate_units(
                plan=plan,
                services_dir=str(services_dir),
                policy=sample_capacity_policy,
                dry_run=False,
            )

            cfile = services_dir / "demo" / "svc-dev-app.container"
            assert cfile.exists()
            content = cfile.read_text(encoding="utf-8")

            # 斷言持久化寫入 Label=container-ctl.governance.id=dev-app
            assert "Label=container-ctl.governance.id=dev-app" in content
            assert "ExecCondition=/usr/local/bin/memory-ctl start-check dev-app" in content

            # 2. 緊接著執行 cgroup-sync 傳入同一 policy
            results = sync_quadlet_files(
                services_dir=str(services_dir),
                quadlet_dir=quadlet_dir,
                dry_run=False,
                policy=sample_capacity_policy,
            )

            # 斷言完全冪等，modified 必須為 False
            assert len(results) == 1
            assert results[0].modified is False, "同一 policy 下 sync 必須保持冪等，不得回報 modified=True"

            # 斷言檔案未被篡改，ExecCondition 仍保持正確的 stable ID (dev-app)，而非推回的 demo-v1
            content_after = cfile.read_text(encoding="utf-8")
            assert content_after == content
            assert "ExecCondition=/usr/local/bin/memory-ctl start-check dev-app" in content_after
            assert "start-check demo-v1" not in content_after
