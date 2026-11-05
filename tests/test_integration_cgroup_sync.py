import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from container_ctl.cgroup_sync import sync_quadlet_files
from container_ctl.dag_resolver import ServiceAction
from container_ctl.quadlet_gen import _render_container_unit
from container_ctl.cli.main import build_parser, main


def test_ac13_cli_dry_run_swap_max_observability(tmp_path, capsys):
    """AC-13: container-ctl cgroup-sync --dry-run 輸出含 SWAP_MAX 欄位，prod/dev 為 0，rescue 為 -"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "myproj"
    proj_dir.mkdir(parents=True)

    # 1. prod unit
    (proj_dir / "svc-prod-web.container").write_text(
        "[Container]\nContainerName=svc-prod-web\n[Service]\nRestart=always\n"
    )
    # 2. dev unit
    (proj_dir / "svc-dev-api.container").write_text(
        "[Container]\nContainerName=svc-dev-api\n[Service]\nRestart=always\n"
    )
    # 3. rescue unit
    (proj_dir / "svc-sshd.container").write_text(
        "[Container]\nContainerName=svc-sshd\nLabel=ingress.tier=rescue\n[Service]\nRestart=always\n"
    )

    quadlet_dir = tmp_path / "systemd"

    test_args = ["container-ctl", "cgroup-sync", "--dry-run", "--services-dir", str(services_dir), "--quadlet-dir", str(quadlet_dir)]
    with patch.object(sys, "argv", test_args):
        main()

    captured = capsys.readouterr().out
    assert "SWAP_MAX" in captured
    assert "CONTAINER" in captured
    assert "TIER" in captured

    lines = [l for l in captured.splitlines() if "svc-" in l]
    prod_line = next(l for l in lines if "svc-prod-web" in l)
    dev_line = next(l for l in lines if "svc-dev-api" in l)
    rescue_line = next(l for l in lines if "svc-sshd" in l)

    # 斷言 prod/dev 為 0，rescue 為 -，且不含 'None'
    assert "0" in prod_line
    assert "0" in dev_line
    assert "-" in rescue_line
    assert "None" not in rescue_line


def test_ac14_convergence_and_idempotence_with_mixed_fixtures(tmp_path):
    """AC-14:
    混合新生成單元 (Ticket 02) 與舊格式單元，非 rescue 收斂為恰一條 MemorySwapMax=0，
    rescue 不含該條目；第二輪執行全數 modified=False。
    """
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "demo"
    proj_dir.mkdir(parents=True)
    quadlet_dir = tmp_path / "systemd"

    # 新生成單元 (via quadlet_gen)
    act_new = ServiceAction(name="new-worker", action="NEW", config={"tier": "prod"})
    new_rendered = _render_container_unit(act_new, image="worker:latest", project="demo", version="1.0")
    assert "MemorySwapMax=0" in new_rendered
    (proj_dir / "svc-new-worker.container").write_text(new_rendered)

    # 舊格式單元 (缺 MemorySwapMax)
    old_prod = """[Container]
ContainerName=svc-old-prod
[Service]
Restart=always
MemoryMax=1G
"""
    (proj_dir / "svc-old-prod.container").write_text(old_prod)

    # 舊格式 rescue 單元 (不應被注入 MemorySwapMax)
    old_rescue = """[Container]
ContainerName=svc-rescue-tool
Label=ingress.tier=rescue
[Service]
Restart=always
MemoryMax=1G
"""
    (proj_dir / "svc-rescue-tool.container").write_text(old_rescue)

    # 第一次 sync
    round1 = sync_quadlet_files(str(services_dir), quadlet_dir=quadlet_dir, dry_run=False)
    r1_map = {r.file_path.name: r for r in round1}

    assert r1_map["svc-new-worker.container"].modified is False
    assert r1_map["svc-old-prod.container"].modified is True
    assert r1_map["svc-rescue-tool.container"].modified is False

    # 驗證檔案內容
    old_prod_content = (proj_dir / "svc-old-prod.container").read_text()
    assert old_prod_content.count("MemorySwapMax=0") == 1

    new_worker_content = (proj_dir / "svc-new-worker.container").read_text()
    assert new_worker_content.count("MemorySwapMax=0") == 1

    rescue_content = (proj_dir / "svc-rescue-tool.container").read_text()
    assert "MemorySwapMax" not in rescue_content

    # 第二次 sync（冪等驗證）
    round2 = sync_quadlet_files(str(services_dir), quadlet_dir=quadlet_dir, dry_run=False)
    assert all(r.modified is False for r in round2)


def test_dry_run_safety_invariants(tmp_path):
    """--dry-run 不修改任何檔案、不建立 symlink、不呼叫 systemctl"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "proj"
    proj_dir.mkdir(parents=True)
    quadlet_dir = tmp_path / "systemd"

    cfile = proj_dir / "svc-target.container"
    orig_content = "[Container]\nContainerName=svc-target\n[Service]\nRestart=always\n"
    cfile.write_text(orig_content)

    results = sync_quadlet_files(str(services_dir), quadlet_dir=quadlet_dir, dry_run=True)
    assert len(results) == 1
    assert results[0].modified is True

    # 檔案內容必須完全未受修改
    assert cfile.read_text() == orig_content

    # symlink 目錄不得產生 symlink
    assert not (quadlet_dir / "svc-target.container").exists()


def test_cgroup_sync_cli_fails_closed_on_invalid_policy(tmp_path, capsys):
    """container-ctl cgroup-sync --policy <invalid> 必須 Fail Closed，以非零碼 (1) 中止退出。"""
    services_dir = tmp_path / "services"
    proj_dir = services_dir / "proj"
    proj_dir.mkdir(parents=True)
    cfile = proj_dir / "svc-target.container"
    orig_content = "[Container]\nContainerName=svc-target\n[Service]\nRestart=always\n"
    cfile.write_text(orig_content)

    invalid_policy_file = tmp_path / "invalid_policy.json"
    invalid_policy_file.write_text('{"schema_version": 1, "system_budget_bytes": -1}')

    test_args = [
        "container-ctl",
        "cgroup-sync",
        "--policy",
        str(invalid_policy_file),
        "--services-dir",
        str(services_dir),
    ]

    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    err_captured = capsys.readouterr().err
    assert "[ERROR] cgroup-sync 執行失敗 (Fail-Closed)" in err_captured
    # 檔案必須完全未被修改
    assert cfile.read_text() == orig_content

