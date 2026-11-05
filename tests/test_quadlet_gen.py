import pytest
from container_ctl.dag_resolver import ServiceAction
from container_ctl.governance_compiler import WorkloadGovernanceMapping
from container_ctl.quadlet_gen import _render_container_unit


def test_swap_guard_prod():
    """AC-01: prod-tier .container 含 MemorySwapMax=0"""
    action = ServiceAction(
        name="web",
        config={"tier": "prod"},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "MemorySwapMax=0" in unit


def test_swap_guard_dev():
    """AC-02: dev-tier .container 含 MemorySwapMax=0"""
    action = ServiceAction(
        name="web-dev",
        config={"tier": "dev"},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "MemorySwapMax=0" in unit


def test_swap_guard_rescue_exempt():
    """AC-03: rescue-tier .container 不含 MemorySwapMax"""
    action = ServiceAction(
        name="sshd",
        config={"tier": "rescue"},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "MemorySwapMax" not in unit


def test_swap_guard_explicit_config():
    """Blueprint 顯式宣告 memory_swap_max 或 mapping limits"""
    # 1. Blueprint 覆蓋
    action_custom = ServiceAction(
        name="ml-prod",
        config={"tier": "prod", "memory_swap_max": "2G"},
        action="NEW",
    )
    unit_custom = _render_container_unit(action=action_custom, image="alpine:latest", project="demo", version="1.0")
    assert "MemorySwapMax=2G" in unit_custom

    # 2. WorkloadGovernanceMapping 優先
    gov = WorkloadGovernanceMapping(
        stable_id="ml-prod",
        unit_name="svc-demo-1.0.service",
        tier="prod",
        slice="workload-prod.slice",
        guard=None,
        limits={"swap_max": "512M"},
    )
    unit_gov = _render_container_unit(
        action=action_custom,
        image="alpine:latest",
        project="demo",
        version="1.0",
        governance_mapping=gov,
    )
    assert "MemorySwapMax=512M" in unit_gov


def test_tmpfs_quota_auto_inject():
    """AC-04: tmpfs:['/tmp'] prod 自動補注 Tmpfs=/tmp:size=64m,mode=1777"""
    action = ServiceAction(
        name="web",
        config={"tier": "prod", "tmpfs": ["/tmp"]},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "Tmpfs=/tmp:size=64m,mode=1777" in unit


def test_tmpfs_quota_dev():
    """AC-05: tmpfs:['/tmp'] dev 補注 size=128m"""
    action = ServiceAction(
        name="web-dev",
        config={"tier": "dev", "tmpfs": ["/tmp"]},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "Tmpfs=/tmp:size=128m,mode=1777" in unit


def test_tmpfs_quota_preserve():
    """AC-06: 已含 size= 的 tmpfs 項目不被覆蓋"""
    action = ServiceAction(
        name="web",
        config={
            "tier": "prod",
            "tmpfs": [
                "/run/secrets:size=16m,mode=0700",
                "/custom:size=256m",
            ],
        },
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "Tmpfs=/run/secrets:size=16m,mode=0700" in unit
    assert "Tmpfs=/custom:size=256m" in unit
    assert "size=64m" not in unit


def test_sysctls_rendered_as_quadlet_sysctl():
    action = ServiceAction(
        name="relay",
        config={"tier": "dev", "sysctls": {"net.ipv4.ip_local_port_range": "40051 40100"}},
        action="NEW",
    )
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert 'Sysctl="net.ipv4.ip_local_port_range=40051 40100"' in unit


def test_no_sysctl_line_when_not_declared():
    action = ServiceAction(name="web", config={"tier": "dev"}, action="NEW")
    unit = _render_container_unit(action=action, image="alpine:latest", project="demo", version="1.0")
    assert "Sysctl=" not in unit


def test_prod_security_and_health_contract():
    action = ServiceAction("api", "NEW", {
        "tier": "prod", "user": "1000:1000", "read_only": True,
        "entrypoint": ["/opt/api/entrypoint.sh"],
        "healthcheck": {"test": ["CMD", "python3", "-c", "print('ok')"],
                        "interval": "10s", "timeout": "3s", "retries": 5},
    })
    unit = _render_container_unit(action, "localhost/api@sha256:" + "a" * 64, "demo", "1")
    assert "User=1000:1000" in unit
    assert "ReadOnly=true" in unit
    assert 'Entrypoint=["/opt/api/entrypoint.sh"]' in unit
    assert "HealthCmd=python3 -c" in unit
    assert "HealthInterval=10s" in unit
    assert "HealthRetries=5" in unit


def test_shell_healthcheck_does_not_include_docker_prefix():
    action = ServiceAction("pg", "NEW", {"tier": "prod", "health_cmd": ["CMD-SHELL", "pg_isready -U infra_admin || exit 1"]})
    unit = _render_container_unit(action, "pg:15", "demo", "1")
    assert "HealthCmd=pg_isready -U infra_admin || exit 1" in unit
    assert "HealthCmd=CMD-SHELL" not in unit


def test_userns_keep_id_emitted_after_user():
    action = ServiceAction("herdr", "NEW", {"tier": "infra", "user": "1000", "userns": "keep-id"})
    unit = _render_container_unit(action, "img@sha256:" + "a" * 64, "demo", "1")
    assert "User=1000\nUserNS=keep-id" in unit


def test_userns_absent_by_default_and_rejects_other_modes():
    import pytest
    plain = _render_container_unit(ServiceAction("w", "NEW", {"tier": "dev"}), "alpine:latest", "demo", "1")
    assert "UserNS=" not in plain
    with pytest.raises(ValueError):
        _render_container_unit(ServiceAction("w", "NEW", {"tier": "dev", "userns": "host"}), "alpine:latest", "demo", "1")


def test_stable_unit_naming_is_opt_in_and_labels_carry_release():
    from container_ctl.dag_resolver import ServiceAction
    from container_ctl.quadlet_gen import _container_name, _db_instance_id, _inst_id
    base = {"name": "prod-x", "tier": "prod", "version": "1.2.3"}
    legacy = ServiceAction("prod-x", "NEW", base)
    stable = ServiceAction("prod-x", "NEW", dict(base, unit_naming="stable"))
    assert _container_name(legacy, "P", "9") == "svc-prod-x-1.2.3" and _db_instance_id(legacy, "P", "9") == "P-prod-x-1.2.3"
    assert _container_name(stable, "P", "9") == "svc-prod-x" and _db_instance_id(stable, "P", "9") == "P-prod-x"
    shared = ServiceAction("prod-x", "NEW", dict(base, unit_naming="stable", scope="shared"))
    assert _inst_id(shared, "P", "9") == "prod-x-1.2.3-shared"  # shared 單例仍帶版本
