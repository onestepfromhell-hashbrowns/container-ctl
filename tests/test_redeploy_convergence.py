"""收斂性：記錄不能先於結果。啟動失敗的實例、延伸掛載缺漏、destroy 的生成物邊界、--tier 範圍。"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.cli.deploy import _plan_or_exit, destroy, redeploy
from container_ctl.cli.main import build_parser
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.db import init_db
from container_ctl.live_drift import find_live_drift
from container_ctl.repo_reader import PinnedDigestRequiredError


def _plan():
    config = {"name": "api", "tier": "dev", "scope": "dedicated", "image": "example/api:v1"}
    return DeployPlan("example", "v1", [ServiceAction("api", "NEW", config)])


def _seed(db_path, config, status):
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status,
            blueprint, quadlet_path, container_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("example-api", "example", "api", "v1", "dev", "dedicated", status,
         json.dumps(config), "unit", "svc-api", "now", "now"),
    )
    conn.commit()
    conn.close()


def _run_redeploy(tmp_path, plan, **kw):
    started = MagicMock(job_submitted=True)
    mgr = MagicMock()
    mgr.restart_unit.return_value = started
    with patch("container_ctl.cli.deploy.resolve_deploy_repo", return_value=(Path("/unused"), "bare")), \
         patch("container_ctl.cli.deploy.read_manifest", return_value={}), \
         patch("container_ctl.cli.deploy.list_container_configs", return_value=[]), \
         patch("container_ctl.cli.deploy.resolve", return_value=plan), \
         patch("container_ctl.cli.deploy.generate_blueprint", return_value="bp"), \
         patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
         patch("container_ctl.dev_provision.run_dev_provision", return_value=True), \
         patch("container_ctl.cli.deploy.find_container_unit_drift", return_value=set()), \
         patch("container_ctl.quadlet_gen.resolve_extension_volumes", return_value=[]), \
         patch("container_ctl.cli.deploy.inspect_container", return_value=None), \
         patch("container_ctl.cli.deploy.is_workload_paused", return_value=(False, None)), \
         patch("container_ctl.cli.deploy.validate_volumes", return_value=(True, [], [])), \
         patch("container_ctl.cli.deploy.ensure_volume_storage", return_value=(True, [])), \
         patch("container_ctl.cli.deploy.get_default_manager", return_value=mgr), \
         patch("container_ctl.cli.deploy.generate_units", return_value={"api": "svc-api"}) as generate, \
         patch("container_ctl.cli.deploy.precise_start_workload", return_value=(True, started, None)) as start:
        redeploy("example", "v1", yes=True, db_path=str(tmp_path / "state.db"),
                 services_dir=str(tmp_path / "services"), **kw)
    return generate, start


def test_errored_instance_is_retried_even_when_blueprint_is_identical(tmp_path):
    plan = _plan()
    _seed(str(tmp_path / "state.db"), plan.actions[0].config, "error")
    generate, start = _run_redeploy(tmp_path, plan)
    generate.assert_called_once()
    start.assert_called_once()


def test_healthy_identical_instance_stays_unchanged(tmp_path):
    plan = _plan()
    _seed(str(tmp_path / "state.db"), plan.actions[0].config, "provisioned")
    generate, start = _run_redeploy(tmp_path, plan)
    start.assert_not_called()


def test_tier_scope_does_not_mark_other_tiers_removed(tmp_path):
    plan = _plan()
    _seed(str(tmp_path / "state.db"), plan.actions[0].config, "provisioned")
    conn = init_db(str(tmp_path / "state.db"))
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status,
            blueprint, quadlet_path, container_name, created_at, updated_at)
           VALUES ('example-svc','example','svc','v1','prod','dedicated','provisioned','{}','u','svc-x','now','now')""")
    conn.commit(); conn.close()
    _run_redeploy(tmp_path, plan, tier="dev")
    conn = init_db(str(tmp_path / "state.db"))
    row = conn.execute("SELECT status FROM instances WHERE id='example-svc'").fetchone()
    conn.close()
    assert row["status"] == "provisioned"


def test_extension_volume_missing_in_running_container_is_drift():
    live = {"Mounts": [], "Config": {}, "HostConfig": {}}
    ext = [{"host_path": "/tmp", "container_path": "/ext/skills", "options": "ro"}]
    assert any("/ext/skills" in r for r in find_live_drift({}, live, ext))
    live["Mounts"] = [{"Destination": "/ext/skills", "Source": "/tmp", "RW": False}]
    assert find_live_drift({}, live, ext) == []


def test_destroy_keeps_ext_default_but_removes_generated_units(tmp_path):
    proj = tmp_path / "services" / "example"
    proj.mkdir(parents=True)
    (proj / "svc-api.container").write_text("x")
    (proj / "sub").mkdir()
    (proj / "sub" / "f").write_text("x")
    target = tmp_path / "ext-real.json"
    target.write_text("{}")
    (proj / "ext-default.json").symlink_to(target)
    with patch("container_ctl.graceful_stop.graceful_stop", return_value=True), \
         patch("container_ctl.cli.deploy.get_default_manager"), \
         patch("container_ctl.cli.deploy.init_db"):
        destroy("example", yes=True, db_path=str(tmp_path / "s.db"), services_dir=str(tmp_path / "services"))
    assert sorted(p.name for p in proj.iterdir()) == ["ext-default.json"]
    assert (proj / "ext-default.json").is_symlink()


def test_unpinned_declaration_is_a_clean_error_not_a_traceback(capsys):
    with patch("container_ctl.cli.deploy.plan", side_effect=PinnedDigestRequiredError("api 未錨定 digest")):
        with pytest.raises(SystemExit) as exc:
            _plan_or_exit("example", "v1")
    assert exc.value.code == 1
    assert "api 未錨定 digest" in capsys.readouterr().err


@pytest.mark.parametrize("sub", ["apply", "redeploy", "plan"])
def test_deploy_subcommands_accept_infra_tier(sub, monkeypatch):
    monkeypatch.setattr("sys.argv", ["container-ctl", "deploy", sub, "p", "v1", "--tier", "infra"])
    assert build_parser().parse_args().tier == "infra"


def test_service_tier_reads_infra_declaration():
    from container_ctl import repo_reader
    with patch.object(repo_reader, "_git_show", return_value=json.dumps({"tier": "infra"})):
        assert repo_reader._service_tier("/r", "HEAD", "service-monitor") == "infra"
