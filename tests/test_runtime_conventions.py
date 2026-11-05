from container_ctl.declaration_guard import check_runtime_conventions


def _cfg(**over):
    cfg = {"name": "svc", "tier": "prod", "env": {"PROJECT_NAME": "MyApp"}, "volumes": []}
    cfg.update(over)
    return cfg


def test_plaintext_password_prod_error_dev_warn():
    ok, msgs = check_runtime_conventions(_cfg(env={"DB_PASSWORD": "x"}))
    assert not ok and any("[ERROR]" in m and "DB_PASSWORD" in m for m in msgs)
    ok, msgs = check_runtime_conventions(_cfg(tier="dev", env={"DB_PASSWORD": "x"}))
    assert ok and any("[WARN]" in m for m in msgs)


def test_file_locator_and_url_userinfo():
    ok, msgs = check_runtime_conventions(_cfg(env={"DB_PASSWORD_FILE": "/etc/opt/MyApp/secrets/p"}))
    assert ok and not msgs
    ok, msgs = check_runtime_conventions(_cfg(env={"DB_URL": "postgresql://u:p@h/db"}))
    assert not ok and any("URL" in m for m in msgs)


def test_prod_workspace_and_app_paths():
    ok, msgs = check_runtime_conventions(_cfg(env={"APP_ROOT": "/workspace"}))
    assert not ok and any("/workspace" in m for m in msgs)
    ok, msgs = check_runtime_conventions(_cfg(env={"APP_DATA_DIR": "/data"}))
    assert not ok
    ok, msgs = check_runtime_conventions(_cfg(env={"APP_DATA_DIR": "/var/opt/MyApp"}))
    assert ok and not msgs
    ok, msgs = check_runtime_conventions(_cfg(tier="dev", env={"APP_ROOT": "/workspace"}))
    assert ok and not msgs


def test_volume_templates_secrets_path_and_case():
    cfg = _cfg(volume_templates=[{"host_path": "x"}],
               volumes=[{"container_path": "/var/opt/myapp/secrets", "host_path": "/h"},
                        {"container_path": "/var/log/myapp", "host_path": "/h2"}])
    ok, msgs = check_runtime_conventions(cfg)
    text = "\n".join(msgs)
    assert ok and "volume_templates" in text and "/etc/opt/" in text and "大小寫" in text
