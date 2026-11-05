import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from container_ctl import rollout
from container_ctl.cli import rollout_cmd as rc
from container_ctl.cli.prod_up import StepFailed
from tests.test_env_overlay import BASE, DIGEST, _commit, cfg_repo  # noqa: F401


def _rec(db, version, result, image="sha256:a", cfg="c1", **kw):
    return rollout.record("P", "svc", "site", version, result, image_id=image, config_commit=cfg, db_path=db, **kw)


def test_record_history_and_plan_undo(tmp_path):
    db = str(tmp_path / "s.db")
    assert rollout.history("P", "svc", db_path=db) == []
    a = _rec(db, "1.0", "healthy", "sha256:a", "c1")
    b = _rec(db, "1.1", "healthy", "sha256:b", "c2")
    _rec(db, "1.2", "failed", "sha256:c", "c3")
    rows = rollout.history("P", "svc", db_path=db)
    assert [r["release_version"] for r in rows] == ["1.2", "1.1", "1.0"]
    plan = rollout.plan_undo("P", "svc", db_path=db)
    assert plan["current"]["id"] == b and plan["target"]["id"] == a  # 失敗的不算「目前」，也不當目標
    assert rollout.plan_undo("P", "svc", to=a, db_path=db)["target"]["id"] == a
    with pytest.raises(ValueError, match="不是 healthy"):
        rollout.plan_undo("P", "svc", to=3, db_path=db)


def test_plan_undo_same_state_skipped_and_empty(tmp_path):
    db = str(tmp_path / "s.db")
    with pytest.raises(ValueError, match="沒有任何 healthy"):
        rollout.plan_undo("P", "svc", db_path=db)
    _rec(db, "1.0", "healthy")
    _rec(db, "1.0", "healthy")  # 重複套用同狀態
    with pytest.raises(ValueError, match="沒有與目前不同"):
        rollout.plan_undo("P", "svc", db_path=db)


def test_undo_files_modes(cfg_repo):
    tmp, work, bare = cfg_repo
    first = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    new_img = "localhost/x@sha256:" + "b" * 64
    _commit(work, bare, {".deploy/containers/prod-s.json": dict(BASE, image=new_img, version="2.0.0", env={"A": "9", "B": "base"}),
                         ".deploy/envs/vps/containers/prod-s.json": {"env": {"B": "vps"}}})
    with patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=(str(bare), "config")):
        img = json.loads(rollout.undo_files("P", "prod-s", first, "image")[".deploy/containers/prod-s.json"])
        assert img["image"] == BASE["image"] and img["version"] == "1.0.0" and img["env"]["A"] == "9"
        cfgonly = json.loads(rollout.undo_files("P", "prod-s", first, "config")[".deploy/containers/prod-s.json"])
        assert cfgonly["image"] == new_img and cfgonly["version"] == "2.0.0" and cfgonly["env"]["A"] == "1"
        assert ".deploy/envs/vps/containers/prod-s.json" in rollout.undo_files("P", "prod-s", first, "config")
        both = rollout.undo_files("P", "prod-s", first, "both")
        assert json.loads(both[".deploy/containers/prod-s.json"]) == BASE and both[".deploy/envs/vps/containers/prod-s.json"] is None
        assert ".deploy/envs/vps/containers/prod-s.json" not in rollout.undo_files("P", "prod-s", first, "image")


def test_rollout_apply_preview_runs_nothing_and_yes_converges_and_records(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    cfg = {"name": "svc", "tier": "prod", "version": "1.0.0"}
    with patch.object(rc, "resolve_deploy_repo", return_value=("r", "config")), \
            patch.object(rc, "list_container_configs", return_value=[cfg]), \
            patch.object(rc, "_check_declaration_matches_tag"), \
            patch.object(rc, "_source_commit", return_value="c" * 40), \
            patch("container_ctl.cli.prod_doctor.run_prod_doctor", return_value=(True, [])), \
            patch.object(rollout, "reconcile_report", return_value=(True, ["[DRIFT] env X"])), \
            patch.object(rc, "converge") as conv, \
            patch.object(rollout, "record_from_declaration", return_value=7) as recd:
        assert rc.rollout_apply("P", "svc", db_path=db) == 0
        conv.assert_not_called()
        assert rc.rollout_apply("P", "svc", yes=True, db_path=db) == 0
        conv.assert_called_once()
        assert recd.call_args.args[4] == "healthy"
        conv.side_effect = StepFailed("health", "bad", "x")
        assert rc.rollout_apply("P", "svc", yes=True, db_path=db) == 1
        assert recd.call_args.args[4] == "failed"
    assert "預覽結束" in capsys.readouterr().out


def test_rollout_apply_refuses_non_prod_and_doctor_blockers():
    with patch.object(rc, "resolve_deploy_repo", return_value=("r", "config")), \
            patch.object(rc, "list_container_configs", return_value=[{"name": "svc", "tier": "dev"}]):
        assert rc.rollout_apply("P", "svc", yes=True) == 1
    with patch.object(rc, "resolve_deploy_repo", return_value=("r", "config")), \
            patch.object(rc, "list_container_configs", return_value=[{"name": "svc", "tier": "prod", "version": "1"}]), \
            patch.object(rc, "_check_declaration_matches_tag"), \
            patch("container_ctl.cli.prod_doctor.run_prod_doctor", return_value=(False, ["[ERROR] x"])), \
            patch.object(rc, "converge") as conv:
        assert rc.rollout_apply("P", "svc", yes=True) == 1
        conv.assert_not_called()


def test_reconcile_report_flags_drift_orphans_and_stale_rollout(tmp_path):
    db = str(tmp_path / "s.db")
    import sqlite3
    from container_ctl.db import init_db
    conn = init_db(db)
    for n in ("svc-a", "svc-b"):
        conn.execute("INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (n, "P", "svc", "1", "prod", "s", "provisioned", "{}", "q", n, "t", "t"))
    conn.commit(); conn.close()
    _rec(db, "1.0.0", "healthy", cfg="old")
    live = {"State": {"Running": True}}
    with patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=("r", "config")), \
            patch("container_ctl.repo_reader.list_container_configs", return_value=[{"name": "svc", "version": "1.0.0"}]), \
            patch("container_ctl.cli.image._source_commit", return_value="n" * 40), \
            patch("container_ctl.cli.image_prod.read_release_record", return_value={"image_id": "sha256:a"}), \
            patch("container_ctl.live_drift.inspect_container", return_value=live), \
            patch("container_ctl.live_drift.find_live_drift", side_effect=[["env X 運行值與宣告不同"], []]):
        ok, out = rollout.reconcile_report("P", "svc", db_path=db)
    text = "\n".join(out)
    assert not ok and "[DRIFT]" in text and "同時有 2 個運行實例" in text and "設定 repo 已前進" in text


def test_reconcile_dev_service_does_not_require_release(tmp_path):
    db = str(tmp_path / "s.db")
    from container_ctl.db import init_db
    conn = init_db(db)
    conn.execute("INSERT INTO instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("svc-dev", "P", "dev", "1", "dev", "s", "provisioned", "{}", "q", "svc-dev", "t", "t"))
    conn.commit(); conn.close()
    with patch("container_ctl.deploy_source.resolve_deploy_repo", return_value=("r", "config")), \
            patch("container_ctl.repo_reader.list_container_configs", return_value=[{"name": "dev", "tier": "dev"}]), \
            patch("container_ctl.cli.image._source_commit", return_value="n" * 40), \
            patch("container_ctl.cli.image_prod.read_release_record", return_value=None), \
            patch("container_ctl.live_drift.inspect_container", return_value={"State": {"Running": True}}), \
            patch("container_ctl.live_drift.find_live_drift", return_value=[]):
        ok, out = rollout.reconcile_report("P", "dev", db_path=db)
    assert ok and not any("[ERROR]" in m for m in out)
