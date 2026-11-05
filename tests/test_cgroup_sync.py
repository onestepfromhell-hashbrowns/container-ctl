"""
測試 cgroup_sync 模組：驗證 dev/prod/infra/rescue tier 之 MemoryMax, MemorySwapMax 與 TasksMax 補全與冪等邏輯
"""
from pathlib import Path
import pytest

from container_ctl.cgroup_sync import (
    SyncResult,
    apply_cgroup_limits_to_text,
    detect_tier_from_content,
    sync_quadlet_files,
)


def test_detect_tier_from_ingress_label():
    text = "Label=ingress.tier=dev\n[Container]\n"
    assert detect_tier_from_content(text, Path("svc-something.container")) == "dev"

    text2 = "Label=ingress.tier=prod\n[Container]\n"
    assert detect_tier_from_content(text2, Path("svc-something.container")) == "prod"

    text3 = "Label=ingress.tier=rescue\n[Container]\n"
    assert detect_tier_from_content(text3, Path("svc-sshd.container")) == "rescue"


def test_detect_tier_from_infra_domain():
    text = "Label=platform.domain=infrastructure\n[Container]\n"
    assert detect_tier_from_content(text, Path("nginx-ingress.container")) == "infra"


def test_detect_tier_from_description_rescue():
    text = "[Unit]\nDescription=Container sshd (rescue)\n[Container]\n"
    assert detect_tier_from_content(text, Path("svc-sshd.container")) == "rescue"


def test_apply_cgroup_limits_dev_missing():
    raw = """[Unit]
Description=Dev app

[Container]
ContainerName=app-dev

[Service]
Restart=on-failure
RestartSec=10s
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "dev")
    assert modified is True
    assert mem == "2G"
    assert tasks == "2048"
    assert swap == "0"
    assert "MemoryMax=2G" in updated
    assert "TasksMax=2048" in updated
    assert "MemorySwapMax=0" in updated


def test_apply_cgroup_limits_upgrade_stale_128():
    raw = """[Service]
Restart=always
MemoryMax=2G
TasksMax=128
MemorySwapMax=0
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "dev")
    assert modified is True
    assert mem == "2G"
    assert tasks == "2048"
    assert swap == "0"
    assert "TasksMax=2048" in updated
    assert "TasksMax=128" not in updated


def test_apply_cgroup_limits_prod_missing():
    raw = """[Unit]
Description=Prod app

[Container]
ContainerName=app-prod

[Service]
Restart=always
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "prod")
    assert modified is True
    assert mem == "1G"
    assert tasks is None
    assert swap == "0"
    assert "MemoryMax=1G" in updated
    assert "MemorySwapMax=0" in updated
    assert "TasksMax" not in updated


def test_apply_cgroup_limits_already_set():
    raw = """[Service]
Restart=always
MemoryMax=4G
TasksMax=256
MemorySwapMax=0
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "dev")
    assert modified is False
    assert mem == "4G"
    assert tasks == "256"
    assert swap == "0"
    assert "MemoryMax=4G" in updated
    assert "TasksMax=256" in updated
    assert "MemorySwapMax=0" in updated


def test_swap_backfill_no_policy():
    """AC-07: 無 policy 的舊有 .container 補寫 MemorySwapMax=0"""
    raw = """[Container]
ContainerName=app-prod

[Service]
Restart=always
MemoryMax=1G
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "prod")
    assert modified is True
    assert swap == "0"
    assert "MemorySwapMax=0" in updated


def test_swap_idempotent():
    """AC-08: 重跑 cgroup_sync 冪等，不重複追加"""
    raw = """[Container]
ContainerName=app-prod

[Service]
Restart=always
MemoryMax=1G
MemorySwapMax=0
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "prod")
    assert modified is False
    assert swap == "0"
    assert updated.count("MemorySwapMax=0") == 1


def test_swap_rescue_exempt():
    """AC-09: rescue-tier 容器不補寫 MemorySwapMax"""
    raw = """[Container]
ContainerName=cloudflared
Label=ingress.tier=rescue

[Service]
Restart=always
MemoryMax=1G
"""
    updated, modified, mem, tasks, swap = apply_cgroup_limits_to_text(raw, "rescue")
    assert modified is False
    assert swap is None
    assert "MemorySwapMax" not in updated


def test_sync_result_swap_field(tmp_path):
    """AC-10: SyncResult.swap_max prod='0', rescue=None"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "myproj"
    proj_dir.mkdir(parents=True)

    prod_file = proj_dir / "svc-prod-app.container"
    prod_file.write_text(
        """[Container]
ContainerName=svc-prod-app
[Service]
Restart=always
"""
    )
    rescue_file = proj_dir / "svc-sshd.container"
    rescue_file.write_text(
        """[Container]
ContainerName=svc-sshd
Label=ingress.tier=rescue
[Service]
Restart=always
"""
    )

    quadlet_dir = tmp_path / "systemd"
    results = sync_quadlet_files(str(services_dir), quadlet_dir=quadlet_dir)
    res_map = {r.container_name: r for r in results}

    assert res_map["svc-prod-app"].swap_max == "0"
    assert res_map["svc-sshd"].swap_max is None


def test_sync_quadlet_files_creates_symlinks(tmp_path):
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "myproj"
    proj_dir.mkdir(parents=True)

    cfile = proj_dir / "svc-dev-test.container"
    cfile.write_text(
        """[Container]
ContainerName=svc-dev-test

[Service]
Restart=on-failure
"""
    )

    quadlet_dir = tmp_path / "systemd"
    results = sync_quadlet_files(str(services_dir), quadlet_dir=quadlet_dir)

    assert len(results) == 1
    assert results[0].modified is True
    assert results[0].swap_max == "0"
    assert "MemoryMax=2G" in cfile.read_text()
    assert "MemorySwapMax=0" in cfile.read_text()
    assert (quadlet_dir / "svc-dev-test.container").is_symlink()


def test_detect_tier_strict_rescue():
    """Rescue 偵測必須嚴格，不可因檔名包含 sshd 或 rescue 子字串就放寬治理。"""
    dev_content = "[Container]\nContainerName=my-sshd-proxy\n[Service]\nRestart=on-failure\n"
    # 名稱碰巧包含 sshd 但無 rescue 標籤，應回退為 dev，絕不判定為 rescue
    assert detect_tier_from_content(dev_content, Path("my-sshd-proxy.container")) == "dev"
    assert detect_tier_from_content(dev_content, Path("workload-rescue-data.container")) == "dev"

    # 受控的精確 legacy 單元名稱允許辨識為 rescue
    assert detect_tier_from_content(dev_content, Path("svc-sshd.container")) == "rescue"
    assert detect_tier_from_content(dev_content, Path("sshd.container")) == "rescue"

    # 顯式標籤優先精確辨識
    rescue_labeled = "[Container]\nContainerName=my-tool\nLabel=ingress.tier=rescue\n"
    assert detect_tier_from_content(rescue_labeled, Path("my-tool.container")) == "rescue"


def test_sync_quadlet_files_dry_run_does_not_create_directory(tmp_path):
    """--dry-run 模式下嚴禁建立 quadlet_dir 目錄（零磁碟副作用）。"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "testproj"
    proj_dir.mkdir(parents=True)

    cfile = proj_dir / "svc-dev-app.container"
    cfile.write_text("[Container]\nContainerName=svc-dev-app\n[Service]\nRestart=on-failure\n")

    non_existent_quadlet_dir = tmp_path / "systemd_never_created"
    assert not non_existent_quadlet_dir.exists()

    results = sync_quadlet_files(
        services_dir=str(services_dir),
        quadlet_dir=non_existent_quadlet_dir,
        dry_run=True,
    )

    assert len(results) == 1
    assert results[0].modified is True
    # 驗證目錄絕對沒有被建立
    assert not non_existent_quadlet_dir.exists()


@pytest.mark.skipif(not __import__("container_ctl.governance_port", fromlist=["x"]).AVAILABLE, reason="policy validation needs memory_governor")
def test_sync_quadlet_files_fails_closed_on_invalid_policy(tmp_path):
    """明確傳入 --policy 時，若 policy 無效必須拋出異常中止，嚴禁修改任何檔案。"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "testproj"
    proj_dir.mkdir(parents=True)

    cfile = proj_dir / "svc-dev-app.container"
    initial_content = "[Container]\nContainerName=svc-dev-app\n[Service]\nRestart=on-failure\n"
    cfile.write_text(initial_content)

    quadlet_dir = tmp_path / "systemd"

    # 傳入非法 policy（負數預算）
    invalid_policy = {
        "schema_version": 1,
        "policy_version": "1.0",
        "system_budget_bytes": -1,  # 非法數值
        "unmanaged_budget_bytes": 100,
        "rescue_reserve_bytes": 100,
    }

    with pytest.raises((ValueError, Exception)) as exc_info:
        sync_quadlet_files(
            services_dir=str(services_dir),
            quadlet_dir=quadlet_dir,
            dry_run=False,
            policy=invalid_policy,
        )

    assert "system_budget_bytes must be a mandatory non-negative integer" in str(exc_info.value)
    # 斷言檔案完全沒有被更動（Fail Closed）
    assert cfile.read_text() == initial_content

