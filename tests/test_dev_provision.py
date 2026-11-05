from unittest.mock import patch

import pytest

from container_ctl import dev_provision as dpv
from container_ctl.host_guard import inside_container, refuse_in_container

DEV = {"name": "dev-x", "tier": "dev", "volumes": [], "env": {}}
PROD = {"name": "prod-x", "tier": "prod", "volumes": []}


def test_prod_only_is_noop():
    with patch("container_ctl.cli.host_prepare.plan_host_prepare") as p:
        assert dpv.run_dev_provision("P", "HEAD", [PROD], True)
    p.assert_not_called()


def test_preview_does_not_apply(capsys):
    acts = [{"op": "mkdir", "path": "/x", "service": "dev-x"}]
    with patch("container_ctl.cli.host_prepare.plan_host_prepare", return_value=(acts, [])), \
            patch("container_ctl.cli.host_prepare.apply_actions") as ap:
        assert dpv.run_dev_provision("P", "HEAD", [DEV], False)
    ap.assert_not_called()
    assert "--yes 時自動執行" in capsys.readouterr().err


def test_errors_abort():
    with patch("container_ctl.cli.host_prepare.plan_host_prepare", return_value=([], ["[ERROR] bad"])):
        assert not dpv.run_dev_provision("P", "HEAD", [DEV], True)


def test_no_pg_hub_declaration_skips_db(capsys):
    cfg = dict(DEV, env={"A_PG_PASSWORD_FILE": "/p"})
    with patch("container_ctl.cli.host_prepare.plan_host_prepare", return_value=([], [])), \
            patch("container_ctl.db_provision.has_declaration", return_value=False), \
            patch("container_ctl.db_provision.db_provision") as db:
        assert dpv.run_dev_provision("P", "HEAD", [cfg], True)
    db.assert_not_called()


def test_yes_runs_three_steps_in_order():
    order = []
    cfg = dict(DEV, env={"A_PG_PASSWORD_FILE": "/p"})
    acts = [{"op": "mkdir", "path": "/x", "service": "dev-x"}]
    sacts = [{"state": "missing", "service": "s", "env": "E", "path": "/p", "kind": "password", "uid": 1, "nbytes": 32}]
    with patch("container_ctl.cli.host_prepare.plan_host_prepare", return_value=(acts, [])), \
            patch("container_ctl.cli.host_prepare.apply_actions", side_effect=lambda a: order.append("host") or []), \
            patch("container_ctl.secret_provision.plan_secret_provision", return_value=(sacts, [])), \
            patch("container_ctl.secret_provision.apply_secret_provision", side_effect=lambda a: order.append("secret") or []), \
            patch("container_ctl.db_provision.has_declaration", return_value=True), \
            patch("container_ctl.db_provision.db_provision", side_effect=lambda *a: order.append(("db", a)) or 0):
        assert dpv.run_dev_provision("P", "HEAD", [cfg], True)
    assert order == ["host", "secret", ("db", ("P", "dev", "apply"))]


def test_step_failure_stops_later_steps():
    acts = [{"op": "mkdir", "path": "/x", "service": "dev-x"}]
    with patch("container_ctl.cli.host_prepare.plan_host_prepare", return_value=(acts, [])), \
            patch("container_ctl.cli.host_prepare.apply_actions", return_value=["[ERROR] chown"]), \
            patch("container_ctl.db_provision.has_declaration", return_value=True), \
            patch("container_ctl.db_provision.db_provision") as db:
        assert not dpv.run_dev_provision("P", "HEAD", [dict(DEV, env={"A_PG_PASSWORD_FILE": "/p"})], True)
    db.assert_not_called()


def test_host_guard(tmp_path):
    assert not inside_container(markers=(str(tmp_path / "none"),), environ={})
    m = tmp_path / ".containerenv"
    m.write_text("")
    assert inside_container(markers=(str(m),), environ={})
    assert inside_container(markers=(), environ={"container": "podman"})
    with pytest.raises(SystemExit, match="宿主 agent"):
        refuse_in_container(markers=(str(m),), environ={})
