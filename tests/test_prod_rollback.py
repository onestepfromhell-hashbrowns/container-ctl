import json
from unittest.mock import patch

import pytest

from container_ctl.cli import prod_rollback as rb
from container_ctl.cli.image import ImagePrepareError

OLD = {"name": "s", "tier": "prod", "image": "localhost/x@sha256:" + "a" * 64, "version": "1.0.0", "env": {"A": "1"}}
CUR = {"name": "s", "tier": "prod", "image": "localhost/x@sha256:" + "b" * 64, "version": "1.1.0", "env": {"A": "2"}}
REC = {"image_id": "sha256:" + "a" * 64, "config_commit": "c0"}


def _patches(rec=REC, exists=True):
    def src_cfg(repo, commit, svc):
        return OLD if commit == "c0" else CUR
    return [patch.object(rb, "resolve_deploy_repo", return_value=("/r.git", "config")),
            patch.object(rb, "read_release_record", return_value=rec),
            patch.object(rb, "_source_config", side_effect=src_cfg),
            patch.object(rb, "_source_commit", return_value="head"),
            patch.object(rb, "list_versions", return_value=["1.0.0"])]


def _plan(**kw):
    ps = _patches(kw.get("rec", REC))
    for p in ps:
        p.start()
    try:
        return rb.plan_rollback("P", "s", "1.0.0", local_image_exists=lambda i: kw.get("exists", True))
    finally:
        for p in ps:
            p.stop()


def test_plan_restores_only_image_and_version():
    plan = _plan()
    assert plan["new_cfg"]["image"] == OLD["image"] and plan["new_cfg"]["version"] == "1.0.0"
    assert plan["new_cfg"]["env"] == {"A": "2"}  # 其餘維持現行
    assert not plan["noop"]


def test_missing_tag_lists_versions():
    with pytest.raises(ImagePrepareError, match="可用版本：1.0.0"):
        _plan(rec=None)


def test_missing_local_image_blocks():
    with pytest.raises(ImagePrepareError, match="映像"):
        _plan(exists=False)


def test_legacy_repo_rejected():
    with patch.object(rb, "resolve_deploy_repo", return_value=("/r.git", "legacy")):
        with pytest.raises(ImagePrepareError, match="設定 repo"):
            rb.plan_rollback("P", "s", "1.0.0")


def test_preview_does_not_commit(capsys):
    with patch.object(rb, "plan_rollback", return_value={"noop": False, "new_cfg": OLD, "old_image": OLD["image"],
                                                         "cur_image": CUR["image"], "from_version": "1.1.0", "to_version": "1.0.0"}), \
            patch.object(rb, "_commit_declaration") as commit:
        assert rb.prod_rollback("P", "s", "1.0.0") == 0
    commit.assert_not_called()
    assert "預覽結束" in capsys.readouterr().out


def test_bad_version_rejected(capsys):
    assert rb.prod_rollback("P", "s", "../x") == 1


def test_apply_runs_chain_in_order():
    calls = []
    plan = {"noop": False, "new_cfg": OLD, "old_image": OLD["image"], "cur_image": CUR["image"],
            "from_version": "1.1.0", "to_version": "1.0.0"}
    with patch.object(rb, "plan_rollback", return_value=plan), \
            patch.object(rb, "_commit_declaration", side_effect=lambda *a: calls.append("commit") or "abc123456789"), \
            patch("container_ctl.cli.deploy.redeploy", side_effect=lambda *a, **k: calls.append("redeploy")), \
            patch("container_ctl.cli.prod_up._container_name", return_value="c1"), \
            patch("container_ctl.cli.prod_up._wait_healthy", return_value=(True, "running healthy")), \
            patch("container_ctl.smoke.run_smoke", side_effect=lambda *a, **k: calls.append("smoke") or (True, [])), \
            patch("container_ctl.prod_verify.verify_prod", side_effect=lambda *a, **k: calls.append("verify")), \
            patch("container_ctl.cli.image_prod.audit_prod_image", return_value=(True, [])):
        assert rb.prod_rollback("P", "s", "1.0.0", yes=True) == 0
    assert calls == ["commit", "redeploy", "smoke", "verify"]
