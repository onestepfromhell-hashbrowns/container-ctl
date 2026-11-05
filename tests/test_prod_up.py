import contextlib
from unittest.mock import MagicMock, patch

import pytest

from container_ctl.cli import prod_up as pu


@pytest.fixture(autouse=True)
def _env(tmp_path):
    with patch("container_ctl.smoke.run_smoke", return_value=(True, [])), \
         patch.object(pu, "REPORT_ROOT", tmp_path), \
         patch.object(pu, "resolve_deploy_repo", return_value=("/nonexistent.git", "config")), \
         patch.object(pu, "list_container_configs", return_value=[{"name": "svc", "tier": "prod", "volumes": []}]):
        yield


@contextlib.contextmanager
def _doctor(ok=True, msgs=None):
    with patch.object(pu, "run_prod_doctor", return_value=(ok, msgs or [])), \
            patch.object(pu, "_check_declaration_matches_tag"):
        yield


def test_preview_without_yes_does_nothing_destructive():
    with _doctor(), patch("container_ctl.cli.image_prod.prepare_prod_image") as prep, \
            patch("container_ctl.cli.deploy.redeploy") as red:
        assert pu.prod_up("P", "svc", "1.0.0") == 0
    prep.assert_not_called()
    red.assert_not_called()


def test_doctor_blocker_stops_with_next_command(capsys):
    with _doctor(False, ["[ERROR] svc: env X 為空字串"]):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    out = capsys.readouterr().out
    assert "[FAIL] 步驟 doctor" in out and "[NEXT]" in out


def test_missing_host_dir_is_not_a_blocker_but_missing_secret_is(capsys):
    with _doctor(False, ["[ERROR] svc: 宿主目錄不存在 /x"]), \
            patch.object(pu, "_tag_exists", return_value=True), \
            patch("container_ctl.cli.deploy.redeploy"), patch.object(pu, "_container_name", return_value=None):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    assert "state.db 找不到實例" in capsys.readouterr().out
    with _doctor(False, ["[ERROR] svc: env T_FILE 指向的宿主檔不存在 /s"]):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    assert "[FAIL] 步驟 doctor" in capsys.readouterr().out


def test_full_flow_order_and_skip_when_tag_exists(capsys):
    calls = []
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch("container_ctl.cli.deploy.redeploy", side_effect=lambda *a, **k: calls.append("redeploy")), \
            patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "running healthy")), \
            patch("container_ctl.prod_verify.verify_prod", side_effect=lambda *a, **k: calls.append("verify")), \
            patch("container_ctl.cli.image_prod.audit_prod_image", return_value=(True, [])):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 0
    assert calls == ["redeploy", "verify"]
    assert "略過" in capsys.readouterr().out


def test_prepare_publish_run_when_no_tag(tmp_path):
    order = []
    with _doctor(), patch.object(pu, "_tag_exists", return_value=False), \
            patch("container_ctl.cli.image_prod.prepare_prod_image", side_effect=lambda *a, **k: order.append("prepare") or tmp_path), \
            patch("container_ctl.cli.image_prod.publish_prod_image", side_effect=lambda *a, **k: order.append("publish") or "release/svc/1.0.0"), \
            patch("container_ctl.cli.deploy.redeploy", side_effect=lambda *a, **k: order.append("redeploy")), \
            patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "running healthy")), \
            patch("container_ctl.prod_verify.verify_prod"), \
            patch("container_ctl.cli.image_prod.audit_prod_image", return_value=(True, [])):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 0
    assert order == ["prepare", "publish", "redeploy"]


def test_unhealthy_fails_with_restart_hint(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), patch("container_ctl.cli.deploy.redeploy"), \
            patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(False, "running unhealthy")):
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    assert "service restart" in capsys.readouterr().out


def test_wait_healthy_times_out_and_succeeds():
    seq = iter([MagicMock(stdout="running starting\n", stderr=""), MagicMock(stdout="running healthy\n", stderr="")])
    with patch.object(pu.subprocess, "run", side_effect=lambda *a, **k: next(seq)):
        ok, last = pu._wait_healthy("c", 30, poll=lambda s: None)
    assert ok and last == "running healthy"
    with patch.object(pu.subprocess, "run", return_value=MagicMock(stdout="running starting\n", stderr="")):
        ok, _ = pu._wait_healthy("c", 0, poll=lambda s: None)
    assert not ok


def test_smoke_failure_stops_and_auto_rolls_back(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch.object(pu, "_current_version", return_value="0.9.0"), \
            patch("container_ctl.cli.deploy.redeploy"), patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "running healthy")), \
            patch("container_ctl.smoke.run_smoke", return_value=(False, ["[ERROR] smoke TCP x"])), \
            patch.object(pu, "_confirm_rollback", return_value=True), \
            patch("container_ctl.cli.prod_rollback.prod_rollback", return_value=0) as rb:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True, rollback_on_fail=True) == 1
    rb.assert_called_once()
    assert rb.call_args.args[2] == "0.9.0"
    assert "[FAIL] 步驟 smoke" in capsys.readouterr().out


def test_smoke_failure_without_flag_does_not_roll_back():
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch.object(pu, "_current_version", return_value="0.9.0"), \
            patch("container_ctl.cli.deploy.redeploy"), patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "ok")), \
            patch("container_ctl.smoke.run_smoke", return_value=(False, [])), \
            patch("container_ctl.cli.prod_rollback.prod_rollback") as rb:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    rb.assert_not_called()


def test_db_provision_runs_only_when_pg_password_file_declared():
    cfgs = [{"name": "svc", "tier": "prod", "volumes": [], "env": {"X_PG_PASSWORD_FILE": "/etc/p"}}]
    with _doctor(), patch.object(pu, "list_container_configs", return_value=cfgs), \
            patch("container_ctl.db_provision.has_declaration", return_value=True), \
            patch("container_ctl.db_provision.db_provision", return_value=1) as dbp:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    dbp.assert_called_once_with("P", "prod", "apply")
    with _doctor(), patch.object(pu, "list_container_configs", return_value=cfgs), \
            patch("container_ctl.db_provision.has_declaration", return_value=True), \
            patch("container_ctl.db_provision.db_provision", return_value=1) as dbp:
        assert pu.prod_up("P", "svc", "1.0.0") == 0
    dbp.assert_called_once_with("P", "prod", "dry-run")


def test_auto_rollback_refused_without_confirmation(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch.object(pu, "_current_version", return_value="0.9.0"), \
            patch("container_ctl.cli.deploy.redeploy"), patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "ok")), \
            patch("container_ctl.smoke.run_smoke", return_value=(False, [])), \
            patch.object(pu, "_confirm_rollback", return_value=False), \
            patch("container_ctl.cli.prod_rollback.prod_rollback") as rb:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True, rollback_on_fail=True) == 1
    rb.assert_not_called()
    assert "未自動回滾" in capsys.readouterr().out


def test_no_verifiable_running_version_means_no_rollback(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch.object(pu, "_current_version", return_value=None), \
            patch("container_ctl.cli.deploy.redeploy"), patch.object(pu, "_container_name", return_value="c1"), \
            patch.object(pu, "_wait_healthy", return_value=(True, "ok")), \
            patch("container_ctl.smoke.run_smoke", return_value=(False, [])), \
            patch("container_ctl.cli.prod_rollback.prod_rollback") as rb:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True, rollback_on_fail=True) == 1
    rb.assert_not_called()
    assert "找不到可驗證的運行版本" in capsys.readouterr().out


def test_existing_tag_with_mismatched_declaration_fails_fast_without_waiting(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), \
            patch.object(pu, "_check_declaration_matches_tag", side_effect=pu.StepFailed("declaration", "不一致", "x")), \
            patch("container_ctl.cli.deploy.redeploy") as rd, patch.object(pu, "_wait_healthy") as wh:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    rd.assert_not_called(); wh.assert_not_called()
    assert "[FAIL] 步驟 declaration" in capsys.readouterr().out


def _db(tmp_path, rows):
    import sqlite3
    path = tmp_path / "s.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE instances (project_name, service_name, container_name, version, status, updated_at)")
        c.executemany("INSERT INTO instances VALUES ('P','svc',?,?,'running',?)", rows)
    return str(path)


def test_current_version_uses_running_image_not_state_db(tmp_path):
    # state.db 最新列是 0.3.4（失敗的 redeploy 留下），實際在跑的是 0.3.2 的映像
    db = _db(tmp_path, [("svc-0.3.4", "0.3.4", 2), ("svc-0.3.2", "0.3.2", 1)])
    frozen = {"0.3.2": {"image_id": "sha256:" + "2" * 64}, "0.3.4": {"image_id": "sha256:" + "4" * 64}}
    inspect = {"svc-0.3.4": MagicMock(returncode=0, stdout="false sha256:" + "4" * 64),
               "svc-0.3.2": MagicMock(returncode=0, stdout="true " + "2" * 64)}
    with patch.object(pu, "resolve_deploy_repo", return_value=("r", "config")), \
            patch("container_ctl.cli.prod_rollback.list_versions", return_value=["0.3.2", "0.3.4"]), \
            patch("container_ctl.cli.image_prod.read_release_record", side_effect=lambda r, s, v: frozen[v]), \
            patch.object(pu.subprocess, "run", side_effect=lambda cmd, **k: inspect[cmd[2]]):
        assert pu._current_version("P", "svc", db) == "0.3.2"
        inspect["svc-0.3.4"] = MagicMock(returncode=0, stdout="true " + "4" * 64)  # 兩個都在跑 → 有歧義
        assert pu._current_version("P", "svc", db) is None
        inspect["svc-0.3.4"] = MagicMock(returncode=1, stdout="")
        inspect["svc-0.3.2"] = MagicMock(returncode=0, stdout="true " + "9" * 64)  # 對不上任何 tag
        assert pu._current_version("P", "svc", db) is None


def test_prod_up_records_rollout_on_failure(capsys):
    with _doctor(), patch.object(pu, "_tag_exists", return_value=True), patch("container_ctl.cli.deploy.redeploy"), \
            patch.object(pu, "_container_name", return_value="c1"), patch.object(pu, "_current_version", return_value=None), \
            patch.object(pu, "_wait_healthy", return_value=(False, "running unhealthy")), \
            patch.object(pu, "_record_rollout") as rec:
        assert pu.prod_up("P", "svc", "1.0.0", yes=True) == 1
    assert rec.call_args.args[4] == "failed" and "health" in rec.call_args.args[5]


def _vclock():
    now = [0.0]
    return (lambda: now[0]), (lambda s: now.__setitem__(0, now[0] + s))


def test_verify_retry_passes_after_race(capsys):
    clock, poll = _vclock()
    seq = [Exception("svc: requires active, Running and healthy")] * 2 + [None]
    with patch("container_ctl.prod_verify.verify_prod", side_effect=seq) as v:
        pu._verify_with_retry("P", "HEAD", "svc", timeout=60, poll=poll, clock=clock)
    assert v.call_count == 3
    assert "重試 2 次" in capsys.readouterr().out


def test_verify_retry_does_not_mask_other_failures():
    clock, poll = _vclock()
    with patch("container_ctl.prod_verify.verify_prod", side_effect=Exception("Quadlet declaration drift: svc")) as v:
        with pytest.raises(Exception, match="drift"):
            pu._verify_with_retry("P", "HEAD", "svc", timeout=60, poll=poll, clock=clock)
    assert v.call_count == 1


def test_verify_retry_times_out_and_raises():
    clock, poll = _vclock()
    with patch("container_ctl.prod_verify.verify_prod", side_effect=Exception("svc: requires active, Running and healthy")) as v:
        with pytest.raises(Exception, match="requires active"):
            pu._verify_with_retry("P", "HEAD", "svc", timeout=10, poll=poll, clock=clock)
    assert 3 <= v.call_count <= 5

