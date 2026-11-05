from container_ctl.live_drift import find_live_drift

IMG = "sha256:" + "a" * 64


def _live(**over):
    live = {
        "Image": IMG,
        "Config": {"User": "1000:1000", "Env": ["A=1"], "Healthcheck": {"Test": ["CMD", "x"]}},
        "HostConfig": {"ReadonlyRootfs": True, "Init": True,
                       "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.2", "HostPort": "9443"}]}},
        "Mounts": [{"Destination": "/v", "Source": "/tmp", "RW": True}],
    }
    live.update(over)
    return live


def _cfg(**over):
    cfg = {"image": f"localhost/x@{IMG}", "user": "1000:1000", "read_only": True, "run_init": True,
           "ports": ["127.0.0.2:9443:8000"], "env": {"A": "1"}, "healthcheck": {"test": ["CMD", "x"]},
           "volumes": [{"host_path": "/tmp", "container_path": "/v", "options": "rw"}]}
    cfg.update(over)
    return cfg


def test_matching_has_no_drift():
    assert find_live_drift(_cfg(), _live()) == []


def test_run_init_missing_is_drift():
    live = _live()
    live["HostConfig"]["Init"] = False
    assert any("init" in r for r in find_live_drift(_cfg(), live))


def test_image_user_env_port_mount_healthcheck_drift():
    live = _live(Image="sha256:" + "b" * 64)
    live["Config"].update(User="0", Env=["A=2"], Healthcheck=None)
    live["HostConfig"]["PortBindings"] = {}
    live["Mounts"] = []
    reasons = " ".join(find_live_drift(_cfg(), live))
    for token in ("image", "user", "env A", "port", "volume /v", "healthcheck"):
        assert token in reasons


def test_port_ranges_skipped_and_undeclared_keys_ignored():
    assert find_live_drift({"ports": ["40000-40050:40000-40050/udp"]}, _live()) == []


def test_quadlet_dropins(tmp_path):
    from container_ctl.live_drift import find_quadlet_dropins
    unit = tmp_path / "svc.container"
    assert find_quadlet_dropins(unit) == []
    (tmp_path / "svc.container.d").mkdir()
    (tmp_path / "svc.container.d" / "10-x.conf").write_text("[Container]\n")
    assert len(find_quadlet_dropins(unit)) == 1
