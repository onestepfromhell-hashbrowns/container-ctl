from pathlib import Path
import pytest
from container_params.paths import FHSPathResolver, resolve_app_paths


def test_pure_prod_fhs_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # resolver also probes cwd; keep it free of dev markers
    # 沒有任何 Dev 環境變數與特徵目錄
    resolver = FHSPathResolver(
        project_name="shipping-status",
        environ={},
        base_dir=tmp_path,
    )
    paths = resolver.resolve()

    assert paths.app_root == Path("/opt/shipping-status")
    assert paths.config_dir == Path("/etc/opt/shipping-status")
    assert paths.runtime_dir == Path("/run/shipping-status")
    assert paths.log_dir == Path("/var/log/shipping-status")
    assert paths.data_dir == Path("/var/lib/shipping-status")
    assert paths.frontend_dir == Path("/opt/shipping-status/frontend")
    assert paths.static_vendors_dir == Path("/opt/shipping-status/static_vendors")

    # 驗證 read-only rootfs 劃分
    writable = paths.get_writable_directories()
    assert Path("/run/shipping-status") in writable
    assert Path("/var/log/shipping-status") in writable
    assert Path("/var/lib/shipping-status") in writable


def test_dev_container_workspace_override():
    environ = {
        "APP_ROOT": "/workspace",
    }
    resolver = FHSPathResolver(
        project_name="shipping-status",
        environ=environ,
    )
    paths = resolver.resolve()

    assert paths.app_root == Path("/workspace")
    assert paths.config_dir == Path("/workspace/app/configs")
    assert paths.runtime_dir == Path("/workspace/run")
    assert paths.log_dir == Path("/workspace/logs")
    assert paths.data_dir == Path("/workspace/data")
    assert paths.frontend_dir == Path("/workspace/frontend")


def test_specific_prefix_override():
    environ = {
        "SHIPPING_ROOT": "/opt/custom-shipping",
        "SHIPPING_CONFIG_DIR": "/etc/custom-config",
    }
    resolver = FHSPathResolver(
        project_name="shipping-status",
        prefix="SHIPPING",
        environ=environ,
    )
    paths = resolver.resolve()

    assert paths.app_root == Path("/opt/custom-shipping")
    assert paths.config_dir == Path("/etc/custom-config")


def test_local_repo_auto_detection(tmp_path):
    # 建立 pyproject.toml 模擬本地開發 repo
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'demo'")
    (tmp_path / "config").mkdir()

    resolver = FHSPathResolver(
        project_name="demo-service",
        environ={},
        base_dir=tmp_path,
    )
    paths = resolver.resolve()

    assert paths.app_root == tmp_path.resolve()
    assert paths.runtime_dir == tmp_path.resolve() / "run"
    assert paths.log_dir == tmp_path.resolve() / "logs"
