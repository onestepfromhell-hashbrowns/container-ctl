import json

from container_ctl import env_overlay, host_check as hc

GOOD = """[Container]
ContainerName=x
PublishPort=127.0.0.1:8000:8000
Volume=/var/opt/x/etc:/etc/opt/x:ro
[Service]
MemoryMax=512M
CPUQuota=50%
MemorySwapMax=0
"""
BAD = """[Container]
Network=host
PublishPort=0.0.0.0:80:80
PublishPort=8081:8081
Volume=/home/someone/x:/data:rw
[Service]
MemoryMax=2G
MemorySwapMax=512M
"""


def _env(tmp_path, strength="advisory"):
    envdir = tmp_path / "envs" / "t"
    envdir.mkdir(parents=True)
    units = tmp_path / "units"
    units.mkdir()
    (envdir / "profile.json").write_text(json.dumps({"host": {"quadlet_dir": str(units)}, "paths": {"data_root": str(tmp_path)}}))
    cg = {"memory_max": "1G", "cpu_quota": "85%", "memory_swap_max": "0"}
    rules = [{"id": i, "params": {"allow_network_host_units": ["ing"], **(cg if i == "cgroup-guard" else {})}}
             for i in ("loopback-publish", "cgroup-guard", "fhs-paths", "image-pinned")]
    (envdir / "policy.json").write_text(json.dumps({"default_strength": strength, "rules": rules}))
    return units


def _run(monkeypatch, tmp_path):
    monkeypatch.setattr(env_overlay, "ENVIRONMENTS_DIR", str(tmp_path / "envs"))
    monkeypatch.setattr(hc, "environment_dir", env_overlay.environment_dir)
    return hc.run_check("t")


def test_parse_repeated_keys():
    u = hc.parse_unit(BAD)
    assert u["Container"]["PublishPort"] == ["0.0.0.0:80:80", "8081:8081"]


def test_good_unit_passes(tmp_path, monkeypatch):
    (_env(tmp_path) / "good.container").write_text(GOOD)
    r = _run(monkeypatch, tmp_path)
    st = {x["rule"]: x["status"] for x in r["results"]}
    assert st == {"loopback-publish": "ok", "cgroup-guard": "ok", "fhs-paths": "ok", "image-pinned": "skipped"}


def test_bad_unit_flags_all(tmp_path, monkeypatch):
    (_env(tmp_path) / "bad.container").write_text(BAD)
    f = {x["rule"]: x["findings"] for x in _run(monkeypatch, tmp_path)["results"]}
    assert any("Network=host" in i for i in f["loopback-publish"]["bad"])
    assert any("0.0.0.0" in i for i in f["loopback-publish"]["bad"])
    assert any("8081" in i for i in f["loopback-publish"]["bad"])
    assert {"MemoryMax=2G > 1G", "CPUQuota 未設"} <= set(f["cgroup-guard"]["bad"])
    assert f["fhs-paths"]["bad"]


def test_none_param_skips_and_swap_cap(tmp_path, monkeypatch):
    envdir = tmp_path / "envs" / "t"
    (_env(tmp_path) / "bad.container").write_text(BAD)
    pol = json.loads((envdir / "policy.json").read_text())
    for r in pol["rules"]:
        if r["id"] == "cgroup-guard":
            r["params"] = {"memory_max": "2G", "cpu_quota": None, "memory_swap_max": "512M"}
    (envdir / "policy.json").write_text(json.dumps(pol))
    f = {x["rule"]: x["findings"] for x in _run(monkeypatch, tmp_path)["results"]}
    assert "cgroup-guard" in f and f["cgroup-guard"] == {}


def test_deferred_rendered(tmp_path, monkeypatch):
    envdir = tmp_path / "envs" / "t"
    _env(tmp_path)
    pol = json.loads((envdir / "policy.json").read_text())
    pol["deferred"] = [{"id": "x", "note": "n", "expires": "2000-01-01"}]
    (envdir / "policy.json").write_text(json.dumps(pol))
    assert "暫緩已過期" in hc.render(_run(monkeypatch, tmp_path))


def test_network_host_exception(tmp_path, monkeypatch):
    (_env(tmp_path) / "ing.container").write_text("[Container]\nNetwork=host\n")
    r = _run(monkeypatch, tmp_path)
    assert r["results"][0]["status"] == "ok"


def test_exit_codes(tmp_path, monkeypatch, capsys):
    (_env(tmp_path) / "bad.container").write_text(BAD)
    monkeypatch.setattr(env_overlay, "ENVIRONMENTS_DIR", str(tmp_path / "envs"))
    monkeypatch.setattr(hc, "environment_dir", env_overlay.environment_dir)
    assert hc.host_check("t", None, "table", strict=False) == 0
    assert hc.host_check("t", None, "table", strict=True) == 1
    assert hc.host_check("missing", None, "table", strict=False) == 2


def test_read_profile_layers(tmp_path, monkeypatch):
    envdir = tmp_path / "envs" / "t"
    envdir.mkdir(parents=True)
    (envdir / "profile.json").write_text(json.dumps({"host_ip": {"prod": "127.0.0.9"}}))
    monkeypatch.setattr(env_overlay, "ENVIRONMENTS_DIR", str(tmp_path / "envs"))
    monkeypatch.setattr(env_overlay, "_read_json", lambda *a: None)
    p = env_overlay.read_profile("r", "HEAD", "t")
    assert p["host_ip"] == {"dev": "127.0.0.1", "prod": "127.0.0.9"}
    monkeypatch.setattr(env_overlay, "ENVIRONMENTS_DIR", str(tmp_path / "none"))
    assert env_overlay.read_profile("r", "HEAD", "t")["host_ip"]["prod"] == "127.0.0.2"
