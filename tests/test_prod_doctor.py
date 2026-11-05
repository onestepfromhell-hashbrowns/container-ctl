import os

from container_ctl.cli.prod_doctor import prod_findings


def _cfg(tmp_path, **over):
    d = tmp_path / "d"
    d.mkdir(exist_ok=True)
    cfg = {
        "name": "prod-x", "tier": "prod", "user": "0:0", "read_only": True, "tmpfs": ["/tmp"],
        "healthcheck": {"test": ["CMD", "true"]},
        "lifecycle_policy": {"mode": "always_on"},
        "volumes": [{"host_path": str(d), "container_path": "/var/opt/x", "options": "rw"}],
        "env": {},
    }
    cfg.update(over)
    return cfg


def test_clean_declaration_has_no_findings(tmp_path):
    assert prod_findings(_cfg(tmp_path), subuid_start=100000) == []


def test_empty_env_value_is_error(tmp_path):
    out = prod_findings(_cfg(tmp_path, env={"ALLOWED_ORIGINS": ""}), subuid_start=100000)
    assert any("[ERROR]" in m and "ALLOWED_ORIGINS" in m for m in out)


def test_plain_http_warns_but_loopback_does_not(tmp_path):
    out = prod_findings(_cfg(tmp_path, env={"RESOURCE_GATEWAY_URL": "http://gw:8082", "X_URL": "http://127.0.0.1:1"}), subuid_start=100000)
    assert any("RESOURCE_GATEWAY_URL" in m and "[WARN]" in m for m in out)
    assert not any("X_URL" in m for m in out)


def test_relative_path_under_read_only_is_error(tmp_path):
    out = prod_findings(_cfg(tmp_path, env={"APP_LOG_DIR": "logs"}), subuid_start=100000)
    assert any("[ERROR]" in m and "APP_LOG_DIR" in m for m in out)


def test_missing_host_dir_is_error(tmp_path):
    cfg = _cfg(tmp_path, volumes=[{"host_path": str(tmp_path / "nope"), "container_path": "/v", "options": "rw"}])
    out = prod_findings(cfg, subuid_start=100000)
    assert any("宿主目錄不存在" in m for m in out)


def test_missing_secret_file_is_error(tmp_path):
    cfg = _cfg(tmp_path, env={"TOKEN_FILE": "/var/opt/x/token"})
    out = prod_findings(cfg, subuid_start=100000)
    assert any("TOKEN_FILE" in m and "不存在" in m for m in out)


def test_secret_file_loose_mode_warns(tmp_path):
    cfg = _cfg(tmp_path, env={"TOKEN_FILE": "/var/opt/x/token"})
    f = tmp_path / "d" / "token"
    f.write_text("x")
    f.chmod(0o644)
    out = prod_findings(cfg, subuid_start=100000)
    assert any("過寬" in m for m in out)


def test_manual_mode_and_missing_healthcheck_warn(tmp_path):
    cfg = _cfg(tmp_path, lifecycle_policy={"mode": "manual"})
    del cfg["healthcheck"]
    out = prod_findings(cfg, subuid_start=100000)
    assert any("manual" in m for m in out) and any("healthcheck" in m for m in out)


def test_wrong_owner_of_rw_mount_is_error(tmp_path):
    cfg = _cfg(tmp_path, user="1000:1000")
    out = prod_findings(cfg, subuid_start=os.getuid() + 500000)
    assert any("podman unshare chown" in m for m in out)


def test_read_only_without_tmpfs_is_not_flagged():
    """podman 在 --read-only 下自動掛載 /tmp、/run tmpfs（prod-app 實測），不需宣告。"""
    from container_ctl.cli.prod_doctor import prod_findings
    base = {"name": "p", "read_only": True, "healthcheck": {"test": ["CMD", "x"]}, "volumes": [], "env": {}}
    assert not any("tmpfs" in m for m in prod_findings(base, subuid_start=100000))
