"""declaration_guard: digest 格式與 tier 對應 Host IP 的靜態檢查。"""
from unittest.mock import patch

import pytest

from container_ctl.cli.validate import run_validation
from container_ctl.declaration_guard import check_declarations

HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def cfg(**kw):
    base = {"name": "svc", "image": f"localhost/x:1@sha256:{HEX}", "tier": "dev", "ports": [], "lifecycle_policy": {"mode": "manual"}}
    base.update(kw)
    return base


@pytest.mark.parametrize("c", [
    cfg(ports=["127.0.0.1:8080:8080"]),
    cfg(ports=["127.0.0.1:8080:8080/udp"]),
    cfg(tier="prod", ports=["127.0.0.2:8080:8080/tcp"]),
    cfg(tier=None, name="prod-api", ports=["127.0.0.2:80:80"]),
    cfg(),
])
def test_ok(c):
    ok, msgs = check_declarations([c])
    assert ok and not msgs


@pytest.mark.parametrize("ports,tier", [
    (["8080:8080"], "dev"),
    (["8080"], "dev"),
    ([8080], "dev"),
    (["0.0.0.0:8080:8080"], "dev"),
    (["127.0.0.2:8080:8080"], "dev"),
    (["127.0.0.1:8080:8080"], "prod"),
    (["8080:8080/udp"], "prod"),
    (["[::1]:8080:8080"], "dev"),
])
def test_bad_host_ip(ports, tier):
    ok, msgs = check_declarations([cfg(tier=tier, ports=ports)])
    assert not ok and "Host IP" in msgs[0]


@pytest.mark.parametrize("image", [
    "localhost/x@sha256:placeholder_fill_pinned_digest",
    f"localhost/x@sha256:{HEX[:-1]}",
    f"localhost/x@sha256:{HEX.upper()}",
    f"localhost/x@sha256:{HEX}extra",
    "localhost/x:latest",
])
def test_bad_digest(image):
    ok, msgs = check_declarations([cfg(image=image)])
    assert not ok and "sha256" in msgs[0]


def test_other_tier_with_ports_warns_only():
    ok, msgs = check_declarations([cfg(tier="infra", ports=["8080:8080"])])
    assert ok and msgs[0].startswith("[WARN]")


def test_run_validation_blocks_bad_port(tmp_path):
    repo = tmp_path / "ProjX.git"
    repo.mkdir()
    manifest = {"project_name": "ProjX", "version": "v1", "dependencies": ["svc"]}
    with patch("container_ctl.cli.validate.read_manifest", return_value=manifest), \
         patch("container_ctl.cli.validate.list_container_configs", return_value=[cfg(ports=["8080:8080"])]):
        ok, msgs = run_validation("ProjX", repos_dir=str(tmp_path))
    assert not ok and any("Host IP" in m for m in msgs)


def test_preflight_blocks_bad_port_in_apply_path(monkeypatch, capsys):
    from container_ctl import preflight as pf
    for name in ("_check_mount", "_check_podman_socket", "_check_network_tier_conformance", "_check_portfwd_conflicts"):
        monkeypatch.setattr(pf, name, lambda *a, **k: (True, "[OK]"))
    monkeypatch.setattr(pf, "_check_db", lambda *a, **k: (True, "[OK]"))
    monkeypatch.setattr(pf, "_check_volumes", lambda *a, **k: (True, []))
    monkeypatch.setattr(pf, "check_image_volumes", lambda *a, **k: (True, []))
    assert pf.run_preflight(blueprints=[cfg()]) is True
    assert pf.run_preflight(blueprints=[cfg(ports=["8080:8080"])]) is False


@pytest.mark.parametrize("vol", [
    {"container_path": "/workspace"},
    {"host_path": "/srv/site/projects/.worktrees/proj-x/main", "container_path": "/app"},
    {"host_path": "/srv/site/projects/.git_repos/proj-x.git", "container_path": "/repo"},
])
def test_prod_rejects_source_mounts(vol):
    ok, msgs = check_declarations([cfg(tier="prod", volumes=[vol])])
    assert not ok and "不得掛載專案工作樹" in msgs[0]


def test_prod_data_volume_and_dev_workspace_allowed():
    assert check_declarations([cfg(tier="prod", volumes=[{"container_path": "/var/lib/data"}])]) == (True, [])
    assert check_declarations([cfg(tier="dev", volumes=[{"container_path": "/workspace"}])]) == (True, [])


@pytest.mark.parametrize("tier", ["dev", "prod", "infra"])
def test_any_tier_rejects_config_repo_mount(tier):
    vol = {"host_path": "/srv/site/projects/.config_repos/proj-x.git", "container_path": "/cfg"}
    ok, msgs = check_declarations([cfg(tier=tier, volumes=[vol])])
    assert not ok and any("設定 repo" in m for m in msgs)


UDP_EXC = {"ports": "40051-40100:40051-40100/udp", "reason": "WebRTC ICE media"}


def test_publish_exception_allows_listed_udp_range_only():
    ok, msgs = check_declarations([cfg(
        ports=["127.0.0.1:8031:8031/tcp", "40051-40100:40051-40100/udp"],
        publish_exceptions=[UDP_EXC])])
    assert ok and any("publish_exceptions" in m for m in msgs)
    ok, msgs = check_declarations([cfg(
        ports=["8031:8031/tcp", "40051-40100:40051-40100/udp"],
        publish_exceptions=[UDP_EXC])])
    assert not ok and any("8031:8031/tcp" in m for m in msgs)


def test_publish_exception_matches_with_explicit_host_ip():
    ok, _ = check_declarations([cfg(
        ports=["0.0.0.0:40051-40100:40051-40100/udp"], publish_exceptions=[UDP_EXC])])
    assert ok


@pytest.mark.parametrize("exc", [
    {"ports": "8031:8031/tcp", "reason": "x"},
    {"ports": "40051-40100:40051-40100/udp", "reason": ""},
    {"ports": "40051-40100:40051-40100/udp"},
])
def test_publish_exception_rejects_tcp_or_missing_reason(exc):
    ok, msgs = check_declarations([cfg(ports=["40051-40100:40051-40100/udp"], publish_exceptions=[exc])])
    assert not ok and any("publish_exceptions" in m for m in msgs)


def test_publish_exception_does_not_cover_other_range():
    ok, _ = check_declarations([cfg(
        ports=["40000-40050:40000-40050/udp"], publish_exceptions=[UDP_EXC])])
    assert not ok
