"""
Unit tests for domain checkers.
"""

from pathlib import Path
import tempfile
from unittest.mock import patch
import pytest

from container_doctor.checkers.mount_checker import MountChecker
from container_doctor.checkers.boundary_checker import BoundaryChecker
from container_doctor.checkers.network_checker import NetworkChecker
from container_doctor.checkers.resource_checker import ResourceChecker
from container_doctor.checkers.ssh_checker import SshChecker
from container_doctor.core.context import ExecutionContext, ExecutionMode
from container_doctor.core.models import CheckStatus
from container_doctor.core.reconcile import (
    DeclaredConfig,
    DeclaredMount,
    DeclaredResourceLimits,
)


def test_mount_checker_with_mock_proc_mounts(tmp_path):
    mounts_file = tmp_path / "mounts"
    mounts_content = (
        "overlay / ext4 rw,relatime 0 0\n"
        "/dev/sda1 /opt/container-ctl ext4 ro,relatime 0 0\n"
        "/dev/sda2 /workspace ext4 rw,relatime 0 0\n"
    )
    mounts_file.write_text(mounts_content)

    checker = MountChecker(mounts_file=mounts_file)
    ctx = ExecutionContext(
        mode=ExecutionMode.INSIDE,
        is_container=True,
        workspace_path=tmp_path / "workspace",
        opt_lab_path=tmp_path / "opt_lab",
    )
    # create folders
    (tmp_path / "workspace").mkdir()
    (tmp_path / "opt_lab").mkdir()

    results = checker.check(ctx)
    statuses = [r.status for r in results]
    assert CheckStatus.FAIL not in statuses


def test_mount_checker_declared_reconciliation(tmp_path):
    mounts_file = tmp_path / "mounts"
    mounts_content = (
        "overlay / ext4 rw,relatime 0 0\n"
        "/dev/sda1 /opt/container-ctl ext4 ro,relatime 0 0\n"
        "/dev/sda2 /workspace ext4 rw,relatime 0 0\n"
        "/dev/sda3 /opt/ext/fhs/tools ext4 rw,relatime 0 0\n"
    )
    mounts_file.write_text(mounts_content)

    decl_mount = DeclaredMount(
        src="/host/tools",
        dst="/opt/ext/fhs/tools",
        mode="ro",
        origin_file="scope.json",
    )
    # Create target dir
    (tmp_path / "opt_ext").mkdir(parents=True, exist_ok=True)

    declared_cfg = DeclaredConfig(declared_mounts=[decl_mount])

    checker = MountChecker(mounts_file=mounts_file)
    ctx = ExecutionContext(
        mode=ExecutionMode.INSIDE,
        is_container=True,
        workspace_path=tmp_path / "workspace",
        opt_lab_path=tmp_path / "opt_lab",
        declared_config=declared_cfg,
    )
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "opt_lab").mkdir(exist_ok=True)

    results = checker.check(ctx)
    decl_results = [r for r in results if r.name == "declared_mounts_reconciliation"]
    assert len(decl_results) == 1


def test_boundary_checker_leaks():
    checker = BoundaryChecker()
    leak_path = "/" + "home/someuser/secret/code.py"
    ctx_leak = ExecutionContext(
        mode=ExecutionMode.INSIDE,
        is_container=True,
        env={"TEST_VAR": leak_path, "SAFE_VAR": "/opt/container-ctl/bin"},
    )
    results = checker.check(ctx_leak)
    fail_results = [r for r in results if r.status == CheckStatus.FAIL]
    assert len(fail_results) >= 1
    assert "host_env_leaks" in fail_results[0].name


def test_boundary_checker_safe_and_mandatory(tmp_path):
    checker = BoundaryChecker()
    src_dir = tmp_path / "src"
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    src_dir.mkdir()
    data_dir.mkdir()
    cache_dir.mkdir()

    ctx_safe = ExecutionContext(
        mode=ExecutionMode.INSIDE,
        is_container=True,
        env={
            "APP_SRC_DIR": str(src_dir),
            "APP_DATA_DIR": str(data_dir),
            "APP_CACHE_DIR": str(cache_dir),
        },
    )
    results = checker.check(ctx_safe)
    statuses = [r.status for r in results]
    assert CheckStatus.FAIL not in statuses


def test_boundary_checker_missing_mandatory():
    checker = BoundaryChecker()
    ctx_missing = ExecutionContext(
        mode=ExecutionMode.INSIDE,
        is_container=True,
        env={"RANDOM_VAR": "val"},
    )
    results = checker.check(ctx_missing)
    warn_results = [r for r in results if r.status == CheckStatus.WARN and r.name == "app_path_resolver"]
    assert len(warn_results) == 1
    assert "Missing mandatory" in warn_results[0].message


def test_network_checker():
    checker = NetworkChecker()
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    results = checker.check(ctx)
    assert len(results) >= 1
    assert any(r.name == "loopback_interface" for r in results)


def test_resource_checker_unlimited_warn(tmp_path):
    cgroup_dir = tmp_path / "cgroup"
    cgroup_dir.mkdir()
    (cgroup_dir / "memory.max").write_text("max\n")

    checker = ResourceChecker(cgroup_root=cgroup_dir)
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    results = checker.check(ctx)
    mem_result = [r for r in results if r.name == "cgroup_memory_bound"][0]
    assert mem_result.status == CheckStatus.WARN
    assert "unlimited" in mem_result.message


def test_resource_checker_bounded_pass(tmp_path):
    cgroup_dir = tmp_path / "cgroup"
    cgroup_dir.mkdir()
    (cgroup_dir / "memory.max").write_text("104857600\n")  # 100MB
    (cgroup_dir / "memory.current").write_text("52428800\n")  # 50MB (50%)
    (cgroup_dir / "cpu.max").write_text("200000 100000\n")  # 2.0 cores

    checker = ResourceChecker(cgroup_root=cgroup_dir)
    ctx = ExecutionContext(mode=ExecutionMode.INSIDE, is_container=True)
    results = checker.check(ctx)
    mem_result = [r for r in results if r.name == "cgroup_memory_bound"][0]
    assert mem_result.status == CheckStatus.PASS
    assert "100MB" in mem_result.message

    cpu_result = [r for r in results if r.name == "cgroup_cpu_quota"][0]
    assert cpu_result.status == CheckStatus.PASS
    assert "2.0 cores" in cpu_result.message


def test_ssh_checker(tmp_path):
    checker = SshChecker()
    ctx = ExecutionContext(mode=ExecutionMode.HOST, is_container=False)
    results = checker.check(ctx)
    assert len(results) >= 1
    assert any(r.name == "ssh_key_permissions" for r in results)
