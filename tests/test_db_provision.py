from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from container_ctl import db_provision as dp

import pytest


@pytest.fixture(autouse=True)
def _stage(tmp_path_factory):
    with patch.object(dp, "STAGE_ROOT", str(tmp_path_factory.mktemp("stage"))):
        yield


DECL = {"service": "app", "migrator": True, "connections": [
    {"prefix": "DAILY", "schemas": ["daily"], "pool_max": 4,
     "env": {"prod": {"database": "app_prod", "role": "app_prod", "role_connlimit": 30}}},
    {"prefix": "TAG", "schemas": ["tag"], "pool_max": 4,
     "env": {"prod": {"database": "app_prod", "role": "tag_rt", "role_connlimit": 8}}}]}
SD_OK = SimpleNamespace(validate=lambda d: [])


def _cfg(tmp_path, with_tag=True):
    d = tmp_path / "sec"
    d.mkdir(exist_ok=True)
    env = {"DAILY_PG_PASSWORD_FILE": "/etc/opt/PJ/secrets/app_prod.pw"}
    if with_tag:
        env["TAG_PG_PASSWORD_FILE"] = "/etc/opt/PJ/secrets/tag_rt.pw"
    return {"name": "prod-app", "user": "1000:1000", "env": env,
            "volumes": [{"host_path": str(d), "container_path": "/etc/opt/PJ/secrets", "options": "ro"}]}, d


def test_derive_groups_by_database_and_maps_host_paths(tmp_path):
    cfg, d = _cfg(tmp_path)
    reqs, errs = dp.derive_requests(DECL, "prod", [cfg], SD_OK)
    assert not errs and len(reqs) == 1
    r = reqs[0]
    assert [x[0] for x in r["runtimes"]] == ["app_prod", "tag_rt"]
    assert r["runtimes"][0][1] == str(d / "app_prod.pw")
    assert r["schemas"] == [("daily", "app_prod"), ("tag", "tag_rt")]
    assert r["migrator"] == str(d / "prod_app_migrator.pw")


def test_derive_errors_on_missing_env_and_invalid_decl(tmp_path):
    cfg, _ = _cfg(tmp_path, with_tag=False)
    _, errs = dp.derive_requests(DECL, "prod", [cfg], SD_OK)
    assert "TAG_PG_PASSWORD_FILE" in errs[0]
    _, errs = dp.derive_requests(DECL, "prod", [cfg], SimpleNamespace(validate=lambda d: ["bad"]))
    assert "無效" in errs[0]
    _, errs = dp.derive_requests(DECL, "dev", [cfg], SD_OK)
    assert "沒有 dev" in errs[0]


def test_build_cmd_has_no_secret_values_and_uses_absolute_paths(tmp_path):
    cfg, d = _cfg(tmp_path)
    req = dp.derive_requests(DECL, "prod", [cfg], SD_OK)[0][0]
    cmd = dp.build_cmd(Path("/t/create-service-db"), "dry-run", "prod", "app", "pgc", req)
    assert cmd[:3] == ["python3", "/t/create-service-db", "dry-run"]
    assert "--migrator-secret" in cmd and f"app_prod:{d}/app_prod.pw:30" in cmd


def _run_provision(tmp_path, mode, existing=(), rc=0):
    cfg, d = _cfg(tmp_path)
    for n in existing:
        (d / n).write_text("x")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout="ok", stderr="boom")
    import json
    with patch.object(dp, "read_declaration_text", return_value=json.dumps(DECL)), \
            patch.object(dp, "extract_tooling", return_value=(Path("/t/csd"), SD_OK)), \
            patch("container_ctl.repo_reader.repo_path_for", return_value="/r.git"), \
            patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=("/c.git", "config")), \
            patch("container_ctl.repo_reader.list_container_configs", return_value=[cfg]):
        code = dp.db_provision("app-proj", "prod", mode, run=fake_run, container_lookup=lambda p, s: "pgc")
    return code, calls, d


def test_dry_run_calls_tool_once(tmp_path):
    code, calls, _ = _run_provision(tmp_path, "dry-run")
    assert code == 0 and len(calls) == 1 and calls[0][2] == "dry-run"


def test_apply_runs_tool_then_chowns(tmp_path):
    code, calls, d = _run_provision(tmp_path, "apply")
    assert code == 0 and calls[0][2] == "apply"
    # 假工具未實際產生暫存檔 → 不安裝
    assert not any("unshare" in c for c in calls)


def test_apply_skips_when_all_exist_and_errors_when_partial(tmp_path, capsys):
    names = ["app_prod.pw", "tag_rt.pw", "prod_app_migrator.pw"]
    code, calls, _ = _run_provision(tmp_path, "apply", existing=names)
    assert code == 0 and calls == [] and "略過" in capsys.readouterr().out
    (tmp_path / "b").mkdir()
    code, calls, _ = _run_provision(tmp_path / "b", "apply", existing=names[:1])
    assert code == 1 and calls == []


def test_tool_failure_returns_error(tmp_path, capsys):
    code, _, _ = _run_provision(tmp_path, "dry-run", rc=2)
    assert code == 1 and "失敗" in capsys.readouterr().out


def test_declaration_prefers_config_repo_then_code_repo():
    import subprocess as sp
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd[2])
        return SimpleNamespace(returncode=0 if cmd[2] == "/code.git" else 1, stdout="{}", stderr="")
    with patch.object(dp, "_declaration_repos", return_value=["/cfg.git", "/code.git"]), patch.object(sp, "run", side_effect=fake):
        assert dp.read_declaration_text("P") == "{}"
    assert calls == ["/cfg.git", "/code.git"]
    with patch.object(dp, "_declaration_repos", return_value=[]):
        assert dp.has_declaration("P") is False


def test_apply_stages_then_installs_via_unshare(tmp_path):
    cfg, d = _cfg(tmp_path)
    import json
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "python3":  # 假 create-service-db：把暫存檔寫出來
            for i, a in enumerate(cmd):
                if a in ("--runtime", "--migrator-secret"):
                    path = cmd[i + 1].split(":")[1] if a == "--runtime" else cmd[i + 1]
                    Path(path).write_text("pw")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch.object(dp, "read_declaration_text", return_value=json.dumps(DECL)), \
            patch.object(dp, "extract_tooling", return_value=(Path("/t/csd"), SD_OK)), \
            patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=("/c.git", "config")), \
            patch("container_ctl.repo_reader.list_container_configs", return_value=[cfg]):
        code = dp.db_provision("app-proj", "prod", "apply", run=fake_run, container_lookup=lambda p, s: "pgc")
    assert code == 0
    tool_cmd = calls[0]
    assert all(str(d) not in a for a in tool_cmd if a.startswith(("app_prod:", "tag_rt:")))  # 工具只看到暫存路徑
    installs = [c for c in calls if "unshare" in c]
    assert len(installs) == 3 and all(c[-3].startswith(str(d)) is False for c in installs) and all(c[-2].startswith(str(d)) for c in installs)


def test_install_never_overwrites_and_reports(tmp_path):
    staged = tmp_path / "s"
    staged.write_text("x")
    out = dp.install_staged([(str(staged), str(tmp_path / "real"))], "1000:1000",
                            run=lambda cmd, **kw: SimpleNamespace(returncode=3, stderr=""))
    assert out[0].startswith("[ERROR]") and "未覆寫" in out[0]
    assert dp.install_staged([], "x")[0].startswith("[ERROR]")


def test_env_without_connections_is_skipped(tmp_path, capsys):
    import json
    with patch.object(dp, "read_declaration_text", return_value=json.dumps(DECL)):
        assert dp.db_provision("P", "dev", "apply", container_lookup=lambda p, s: "pgc") == 0
    assert "略過" in capsys.readouterr().out


def test_apply_refuses_when_secret_parent_dir_missing_and_does_not_touch_pg(tmp_path, capsys):
    cfg, d = _cfg(tmp_path)
    import json
    import shutil
    shutil.rmtree(d, ignore_errors=True)  # 密碼檔父目錄不存在
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    with patch.object(dp, "read_declaration_text", return_value=json.dumps(DECL)), \
            patch.object(dp, "extract_tooling", return_value=(Path("/t/csd"), SD_OK)), \
            patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=("/c.git", "config")), \
            patch("container_ctl.repo_reader.list_container_configs", return_value=[cfg]):
        code = dp.db_provision("app-proj", "prod", "apply", run=fake_run, container_lookup=lambda p, s: "pgc")
    assert code == 1 and "父目錄不存在，未動 PG" in capsys.readouterr().out
    assert not any(c[0] == "python3" for c in calls)  # create-service-db 未被呼叫
