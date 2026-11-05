import json
from unittest.mock import patch

import pytest

from container_ctl.cli.main import build_parser
from container_ctl.cli.service import operate_manual_instance
from container_ctl.db import init_db
from container_ctl.lifecycle_adapter import ServiceStartResult


@pytest.fixture(autouse=True)
def _no_real_systemd():
    with patch("container_ctl.cli.service._wait_active", return_value=True), patch(
        "container_ctl.cli.service._check_unit_drift"
    ):
        yield


@pytest.fixture
def manual_instance(tmp_path):
    db_path = str(tmp_path / "state.db")
    services_dir = tmp_path / "services"
    project_dir = services_dir / "test-proj"
    project_dir.mkdir(parents=True)
    quadlet_dir = tmp_path / "systemd"
    quadlet_dir.mkdir()
    unit = project_dir / "svc-dev-test.container"
    unit.write_text("[Container]\nImage=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    (quadlet_dir / unit.name).symlink_to(unit)

    config = {
        "name": "dev-test",
        "tier": "dev",
        "image": "localhost/test:dev@sha256:" + "a" * 64,
        "lifecycle_policy": {"mode": "manual"},
    }
    conn = init_db(db_path)
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status, blueprint,
            quadlet_path, container_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test-proj-dev-test-v1",
            "test-proj",
            "dev-test",
            "v1",
            "dev",
            "dedicated",
            "stopped",
            json.dumps(config),
            str(project_dir),
            "svc-dev-test",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    return db_path, services_dir, quadlet_dir


def _podman_pinned_image(*args, **kwargs):
    return type(
        "Result", (),
        {"returncode": 0, "stdout": json.dumps({"Id": "sha256:" + "a" * 64}), "stderr": ""},
    )()


def test_service_help_documents_scoped_manual_lifecycle():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["service", "start", "--help"])
    assert exc.value.code == 0


def test_start_one_manual_instance_and_record_request(manual_instance):
    db_path, services_dir, quadlet_dir = manual_instance
    with patch("container_ctl.cli.service.subprocess.run", side_effect=_podman_pinned_image), patch(
        "container_ctl.cli.service.precise_start_workload",
        return_value=(True, ServiceStartResult(job_submitted=True), "ok"),
    ) as start:
        operate_manual_instance(
            "start", "test-proj", "dev-test", "svc-dev-test", yes=True,
            db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
        )

    start.assert_called_once()
    conn = init_db(db_path)
    row = conn.execute("SELECT status FROM instances WHERE id = 'test-proj-dev-test-v1'").fetchone()
    event = conn.execute("SELECT event_type, triggered_by FROM lifecycle_events").fetchone()
    conn.close()
    assert row["status"] == "provisioned"
    assert tuple(event) == ("start_requested", "container_ctl_service_start")


def test_restart_uses_restart_admission_path(manual_instance):
    db_path, services_dir, quadlet_dir = manual_instance
    with patch("container_ctl.cli.service.subprocess.run", side_effect=_podman_pinned_image), patch(
        "container_ctl.cli.service.precise_restart_workload",
        return_value=(True, ServiceStartResult(job_submitted=True), "ok"),
    ) as restart:
        operate_manual_instance(
            "restart", "test-proj", "dev-test", "svc-dev-test", yes=True,
            db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
        )

    restart.assert_called_once()


def test_start_refuses_quadlet_image_that_does_not_match_pinned_blueprint(manual_instance, capsys):
    db_path, services_dir, quadlet_dir = manual_instance
    unit = services_dir / "test-proj" / "svc-dev-test.container"
    unit.write_text("[Container]\nImage=localhost/test:dev\n", encoding="utf-8")
    with patch("container_ctl.cli.service.precise_start_workload") as start:
        with pytest.raises(SystemExit):
            operate_manual_instance(
                "start", "test-proj", "dev-test", "svc-dev-test", yes=True,
                db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
            )
    start.assert_not_called()
    assert "Quadlet Image 與 state.db blueprint" in capsys.readouterr().err


def test_start_refuses_always_on_instance(manual_instance, capsys):
    db_path, services_dir, quadlet_dir = manual_instance
    conn = init_db(db_path)
    config = json.loads(conn.execute("SELECT blueprint FROM instances").fetchone()[0])
    config["lifecycle_policy"]["mode"] = "always_on"
    conn.execute("UPDATE instances SET blueprint = ?", (json.dumps(config),))
    conn.commit()
    conn.close()

    with patch("container_ctl.cli.service.precise_start_workload") as start:
        with pytest.raises(SystemExit):
            operate_manual_instance(
                "start", "test-proj", "dev-test", "svc-dev-test", yes=True,
                db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
            )
    start.assert_not_called()
    assert "只允許 restart" in capsys.readouterr().err


def test_restart_allows_always_on_instance(manual_instance):
    db_path, services_dir, quadlet_dir = manual_instance
    conn = init_db(db_path)
    config = json.loads(conn.execute("SELECT blueprint FROM instances").fetchone()[0])
    config["lifecycle_policy"]["mode"] = "always_on"
    conn.execute("UPDATE instances SET blueprint = ?", (json.dumps(config),))
    conn.commit()
    conn.close()

    with patch("container_ctl.cli.service.subprocess.run", side_effect=_podman_pinned_image), patch(
        "container_ctl.cli.service.precise_restart_workload", return_value=(True, "ok", "")
    ) as restart:
        operate_manual_instance(
            "restart", "test-proj", "dev-test", "svc-dev-test", yes=True,
            db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
        )
    restart.assert_called_once()


def test_start_fails_and_keeps_db_when_unit_never_active(manual_instance, capsys):
    db_path, services_dir, quadlet_dir = manual_instance
    with patch("container_ctl.cli.service.subprocess.run", side_effect=_podman_pinned_image), patch(
        "container_ctl.cli.service._check_unit_drift"
    ), patch("container_ctl.cli.service._wait_active", return_value=False), patch(
        "container_ctl.cli.service.precise_start_workload",
        return_value=(True, ServiceStartResult(job_submitted=True), "ok"),
    ):
        with pytest.raises(SystemExit):
            operate_manual_instance(
                "start", "test-proj", "dev-test", "svc-dev-test", yes=True,
                db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
            )
    conn = init_db(db_path)
    status = conn.execute("SELECT status FROM instances").fetchone()[0]
    event = conn.execute("SELECT event_type FROM lifecycle_events").fetchone()[0]
    conn.close()
    assert status == "stopped"
    assert event == "start_failed"
    assert "未在時限內" in capsys.readouterr().err


def test_with_app_requires_hook_declaration(manual_instance, capsys):
    db_path, services_dir, quadlet_dir = manual_instance
    with patch("container_ctl.cli.service.subprocess.run", side_effect=_podman_pinned_image), patch(
        "container_ctl.cli.service.precise_start_workload"
    ) as start:
        with pytest.raises(SystemExit):
            operate_manual_instance(
                "start", "test-proj", "dev-test", "svc-dev-test", yes=True, with_app=True,
                db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
            )
    start.assert_not_called()
    assert "app_hooks.start" in capsys.readouterr().err


def test_with_app_runs_start_hook_then_probe(manual_instance):
    db_path, services_dir, quadlet_dir = manual_instance
    conn = init_db(db_path)
    config = json.loads(conn.execute("SELECT blueprint FROM instances").fetchone()[0])
    config["app_hooks"] = {"start": "bash control.sh start", "readiness_probe": ["curl", "-f", "x"]}
    conn.execute("UPDATE instances SET blueprint = ?", (json.dumps(config),))
    conn.commit()
    conn.close()
    calls = []

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["podman", "exec"]:
            calls.append(cmd[3:])
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return _podman_pinned_image()

    with patch("container_ctl.cli.service.subprocess.run", side_effect=fake_run), patch(
        "container_ctl.cli.service.precise_restart_workload",
        return_value=(True, ServiceStartResult(job_submitted=True), "ok"),
    ):
        operate_manual_instance(
            "restart", "test-proj", "dev-test", "svc-dev-test", yes=True, with_app=True,
            db_path=db_path, services_dir=str(services_dir), quadlet_dir=quadlet_dir,
        )
    assert calls == [["sh", "-c", "bash control.sh start"], ["curl", "-f", "x"]]
