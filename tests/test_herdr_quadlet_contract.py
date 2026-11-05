from container_ctl.dag_resolver import ServiceAction
from container_ctl import paths
from container_ctl.quadlet_gen import _render_container_unit


def _herdr_action(**overrides):
    config = {
        "name": "herdr",
        "tier": "infra",
        "herdr_control_plane": True,
        "podman_socket": {
            "host_path": "/run/user/1000/podman/podman.sock",
            "container_path": "/run/podman/podman.sock",
            "uid": 1000,
        },
        "herdr_socket_dir": paths.HERDR_SOCKET_ROOT + "/demo",
        "read_only_rootfs": True,
        "tmpfs": ["/run/herdr:size=32m,mode=0700"],
        "health_cmd": ["herdr", "api", "snapshot"],
        "memory_max": "512M",
        "memory_swap_max": "512M",
    }
    config.update(overrides)
    return ServiceAction(name="herdr", action="NEW", config=config)


def test_herdr_gets_rootless_socket_and_no_capabilities():
    unit = _render_container_unit(
        _herdr_action(),
        image="registry.example/herdr@sha256:" + "a" * 64,
        project="demo",
        version="v1",
    )
    assert "Volume=/run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro" in unit
    assert f"Volume={paths.HERDR_SOCKET_ROOT}/demo:/run/herdr:rw" in unit
    assert "User=1000" in unit
    assert "DropCapability=ALL" in unit
    assert "ReadOnly=true" in unit
    assert "HealthCmd=herdr api snapshot" in unit
    assert "SYS_ADMIN" not in unit
    assert "SYS_PTRACE" not in unit


def test_herdr_rejects_non_rootless_socket_path():
    action = _herdr_action(
        podman_socket={
            "host_path": "/run/podman/podman.sock",
            "container_path": "/run/podman/podman.sock",
            "uid": 0,
        },
        herdr_socket_dir=paths.HERDR_SOCKET_ROOT + "/demo",
    )
    try:
        _render_container_unit(action, image="registry.example/herdr@sha256:" + "b" * 64, project="demo", version="v1")
    except ValueError as exc:
        assert "host_path" in str(exc)
    else:
        raise AssertionError("non-rootless Podman socket was accepted")


def test_herdr_rejects_added_capabilities():
    action = _herdr_action(add_capabilities=["SYS_ADMIN"])
    try:
        _render_container_unit(action, image="registry.example/herdr@sha256:" + "c" * 64, project="demo", version="v1")
    except ValueError as exc:
        assert "must not add" in str(exc)
    else:
        raise AssertionError("Herdr capability escalation was accepted")
