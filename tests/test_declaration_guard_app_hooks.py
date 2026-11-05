from container_ctl.declaration_guard import check_declarations

_IMG = "localhost/x@sha256:" + "a" * 64


def _cfg(**kw):
    return {"name": "dev-x", "tier": "dev", "image": _IMG, **kw}


def test_dev_without_mode_warns():
    ok, msgs = check_declarations([_cfg()])
    assert ok and any("lifecycle_policy.mode" in m for m in msgs)


def test_dev_with_mode_no_warn():
    _, msgs = check_declarations([_cfg(lifecycle_policy={"mode": "manual"})])
    assert not any("lifecycle_policy.mode" in m for m in msgs)


def test_app_hooks_requires_start():
    ok, msgs = check_declarations([_cfg(app_hooks={"readiness_probe": "true"})])
    assert not ok and any("app_hooks" in m for m in msgs)


def test_app_hooks_valid_forms():
    ok, _ = check_declarations([_cfg(app_hooks={"start": "bash c.sh start", "readiness_probe": ["curl", "x"]})])
    assert ok


def test_unknown_key_warns():
    from container_ctl.declaration_guard import check_unknown_keys
    msgs = check_unknown_keys({"name": "x", "depends_on": ["a"], "bogus": 1, "notes": "ok", "image": "i"})
    assert any("depends_on" in m for m in msgs) and any("bogus" in m for m in msgs)
    assert not any("'notes'" in m or "'image'" in m for m in msgs)
