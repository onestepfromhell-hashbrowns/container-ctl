"""manual redeploy repairs Quadlet drift without starting the instance."""

import json
from pathlib import Path
from unittest.mock import patch

import shutil

import pytest

from container_ctl.cli.deploy import redeploy
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.db import init_db
from container_ctl.quadlet_gen import (
    find_container_unit_drift, generate_units, _render_container_unit, _container_name,
)
from container_ctl.image_volume_guard import runtime_image_ref


@pytest.fixture
def manual_plan():
    config = {
        "name": "worker",
        "tier": "dev",
        "scope": "dedicated",
        "image": "example/worker:v1",
        "lifecycle_policy": {"mode": "manual"},
    }
    return DeployPlan("example", "v1", [ServiceAction("worker", "NEW", config)])


@pytest.fixture
def unit_paths(tmp_path, manual_plan):
    services = tmp_path / "services"
    scan_dir = tmp_path / "systemd"
    scan_dir.mkdir()
    action = manual_plan.actions[0]
    name = _container_name(action, manual_plan.project_name, manual_plan.version)
    unit = services / manual_plan.project_name / f"{name}.container"
    unit.parent.mkdir(parents=True)
    expected = _render_container_unit(
        action, runtime_image_ref(action.config["image"]),
        manual_plan.project_name, manual_plan.version, extra_volumes=[],
    )
    unit.write_text(expected, encoding="utf-8")
    link = scan_dir / unit.name
    link.symlink_to(unit)
    return services, scan_dir, unit, link


def test_matching_manual_unit_is_unchanged(manual_plan, unit_paths):
    services, scan_dir, _, _ = unit_paths
    assert find_container_unit_drift(manual_plan, str(services), scan_dir) == set()


@pytest.mark.parametrize("damage", ["missing_unit", "edited_unit", "missing_link"])
def test_manual_unit_drift_is_detected(manual_plan, unit_paths, damage):
    services, scan_dir, unit, link = unit_paths
    if damage == "missing_unit":
        unit.unlink()
    elif damage == "edited_unit":
        unit.write_text("[Container]\nImage=wrong\n", encoding="utf-8")
    else:
        link.unlink()
    assert find_container_unit_drift(manual_plan, str(services), scan_dir) == {"worker"}


def test_generate_units_repairs_manual_unit_without_start(tmp_path, manual_plan, unit_paths, monkeypatch):
    services, scan_dir, unit, link = unit_paths
    unit.write_text("[Container]\nImage=wrong\n", encoding="utf-8")
    link.unlink()
    monkeypatch.setattr("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", scan_dir)
    with patch("container_ctl.quadlet_gen.subprocess.run") as systemctl:
        generate_units(manual_plan, str(services))
    assert find_container_unit_drift(manual_plan, str(services), scan_dir) == set()
    assert link.is_symlink()
    systemctl.assert_called_once()
    assert systemctl.call_args.args[0] == ["systemctl", "--user", "daemon-reload"]


@pytest.mark.skipif(shutil.which("podman") is None, reason="requires podman")
def test_redeploy_repairs_drift_without_starting_manual_service(tmp_path, manual_plan):
    db_path = str(tmp_path / "state.db")
    conn = init_db(db_path)
    config = manual_plan.actions[0].config
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status,
            blueprint, quadlet_path, container_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("example-worker", "example", "worker", "v1", "dev", "dedicated", "stopped",
         json.dumps(config), "unit", "svc-worker", "now", "now"),
    )
    conn.commit()
    conn.close()

    with patch("container_ctl.cli.deploy.repo_path_for", return_value=Path("/unused")), \
         patch("container_ctl.cli.deploy.read_manifest", return_value={}), \
         patch("container_ctl.cli.deploy.list_container_configs", return_value=[]), \
         patch("container_ctl.cli.deploy.resolve", return_value=manual_plan), \
         patch("container_ctl.cli.deploy.generate_blueprint", return_value="blueprint"), \
         patch("container_ctl.cli.deploy.run_preflight", return_value=True), \
         patch("container_ctl.cli.deploy.find_container_unit_drift", return_value={"worker"}), \
         patch("container_ctl.cli.deploy.is_workload_paused", return_value=(False, None)), \
         patch("container_ctl.cli.deploy.validate_volumes", return_value=(True, [], [])), \
         patch("container_ctl.cli.deploy.ensure_volume_storage", return_value=(True, [])), \
         patch("container_ctl.cli.deploy.generate_units", return_value={"worker": "svc-worker"}) as generate, \
         patch("container_ctl.cli.deploy.precise_start_workload") as start:
        redeploy("example", "v1", yes=True, db_path=db_path, services_dir=str(tmp_path / "services"))

    generate.assert_called_once()
    start.assert_not_called()
    conn = init_db(db_path)
    row = conn.execute("SELECT status FROM instances WHERE id = 'example-worker'").fetchone()
    conn.close()
    assert row["status"] == "stopped"


def test_redeploy_only_limits_scope_to_selected_service(tmp_path):
    cfg_a = {"name": "dev-a", "tier": "dev", "scope": "dedicated", "image": "x/a:1",
             "lifecycle_policy": {"mode": "manual"}}
    cfg_b = {"name": "prod-b", "tier": "prod", "scope": "dedicated", "image": "x/b:1",
             "lifecycle_policy": {"mode": "manual"}}
    plan = DeployPlan("example", "v1", [ServiceAction("dev-a", "NEW", cfg_a), ServiceAction("prod-b", "NEW", cfg_b)])
    db_path = str(tmp_path / "state.db")
    init_db(db_path).close()
    seen = {}

    def fake_units(p, _services):
        seen["units"] = [a.name for a in p.actions]
        return {a.name: f"svc-{a.name}" for a in p.actions}

    def fake_preflight(_db, blueprints, project):
        seen["preflight"] = [b["name"] for b in blueprints]
        return True

    with patch("container_ctl.cli.deploy.resolve_deploy_repo", return_value=(Path("/unused"), "config")), \
         patch("container_ctl.cli.deploy.read_manifest", return_value={}), \
         patch("container_ctl.cli.deploy.list_container_configs", return_value=[cfg_a, cfg_b]), \
         patch("container_ctl.cli.deploy.resolve", return_value=plan), \
         patch("container_ctl.cli.deploy.generate_blueprint", return_value="blueprint"), \
         patch("container_ctl.cli.deploy.run_preflight", side_effect=fake_preflight), \
         patch("container_ctl.cli.deploy.find_container_unit_drift", return_value=set()), \
         patch("container_ctl.cli.deploy.check_release_anchor", return_value=(True, [])) as anchor, \
         patch("container_ctl.cli.deploy.is_workload_paused", return_value=(False, None)), \
         patch("container_ctl.cli.deploy.validate_volumes", return_value=(True, [], [])), \
         patch("container_ctl.cli.deploy.ensure_volume_storage", return_value=(True, [])), \
         patch("container_ctl.cli.deploy.generate_units", side_effect=fake_units), \
         patch("container_ctl.cli.deploy.precise_start_workload"):
        redeploy("example", "v1", yes=True, only=["dev-a"], db_path=db_path,
                 services_dir=str(tmp_path / "services"))

    assert seen["preflight"] == ["dev-a"]
    assert seen["units"] == ["dev-a"]
    assert anchor.call_args.args[1] == []  # prod-b 不在範圍，不受錨定閘門影響


def test_redeploy_only_rejects_unknown_service(tmp_path):
    cfg = {"name": "dev-a", "tier": "dev", "scope": "dedicated", "image": "x/a:1"}
    plan = DeployPlan("example", "v1", [ServiceAction("dev-a", "NEW", cfg)])
    db_path = str(tmp_path / "state.db")
    init_db(db_path).close()
    with patch("container_ctl.cli.deploy.resolve_deploy_repo", return_value=(Path("/unused"), "config")), \
         patch("container_ctl.cli.deploy.read_manifest", return_value={}), \
         patch("container_ctl.cli.deploy.list_container_configs", return_value=[cfg]), \
         patch("container_ctl.cli.deploy.resolve", return_value=plan), \
         patch("container_ctl.cli.deploy.generate_blueprint", return_value="blueprint"):
        with pytest.raises(SystemExit):
            redeploy("example", "v1", yes=True, only=["nope"], db_path=db_path,
                     services_dir=str(tmp_path / "services"))
