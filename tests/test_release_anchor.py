import json
import subprocess

from container_ctl.release_anchor import check_release_anchor


def _bare_with_tag(tmp_path, record):
    work = tmp_path / "w"
    work.mkdir()
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
    g("init")
    g("config", "user.email", "t@x.invalid")
    g("config", "user.name", "T")
    (work / "f").write_text("x")
    g("add", ".")
    g("commit", "-m", "c")
    if record:
        g("tag", "-a", "release/svc/1.0.0", "-m", json.dumps(record))
    return str(work / ".git")


CFG = {"name": "svc", "tier": "prod", "image": "localhost/svc@sha256:" + "a" * 64}


def test_matching_digest_passes(tmp_path):
    repo = _bare_with_tag(tmp_path, {"image_id": "sha256:" + "a" * 64})
    assert check_release_anchor(repo, [CFG], "1.0.0", strict=True) == (True, [])


def test_digest_mismatch_is_always_error(tmp_path):
    repo = _bare_with_tag(tmp_path, {"image_id": "sha256:" + "b" * 64})
    for strict in (False, True):
        ok, msgs = check_release_anchor(repo, [CFG], "1.0.0", strict=strict)
        assert not ok and "已凍結" in msgs[0]


def test_missing_tag_warns_then_errors_when_strict(tmp_path):
    repo = _bare_with_tag(tmp_path, None)
    ok, msgs = check_release_anchor(repo, [CFG], "1.0.0")
    assert ok and msgs[0].startswith("[WARN]")
    ok, msgs = check_release_anchor(repo, [CFG], "1.0.0", strict=True)
    assert not ok and msgs[0].startswith("[ERROR]")


def test_mutable_tag_and_non_prod(tmp_path):
    repo = _bare_with_tag(tmp_path, None)
    mutable = dict(CFG, image="localhost/svc:0.2.0")
    assert check_release_anchor(repo, [mutable], "1.0.0")[0] is True
    assert check_release_anchor(repo, [mutable], "1.0.0", strict=True)[0] is False
    assert check_release_anchor(repo, [dict(CFG, tier="dev", image="x")], "1.0.0") == (True, [])


def test_run_validation_wired_to_anchor(tmp_path):
    from unittest.mock import patch
    from container_ctl.cli.validate import run_validation

    (tmp_path / "ProjX.git").mkdir()
    manifest = {"project_name": "ProjX", "version": "1.0.0", "dependencies": ["svc"]}
    cfg = dict(CFG, ports=[])
    patches = [
        patch("container_ctl.cli.validate.read_manifest", return_value=manifest),
        patch("container_ctl.cli.validate.list_container_configs", return_value=[cfg]),
        patch("container_ctl.cli.validate._check_image_digests", return_value=(True, "")),
        patch("container_ctl.cli.validate.validate_volumes", return_value=(True, [], [])),
        patch("container_ctl.cli.validate.check_image_volumes", return_value=(True, [])),
    ]
    for p in patches:
        p.start()
    try:
        with patch("container_ctl.release_anchor.read_release_record", return_value={"image_id": "sha256:" + "b" * 64}):
            ok, msgs = run_validation("ProjX", repos_dir=str(tmp_path))
        assert not ok and any("已凍結" in m for m in msgs)
        with patch("container_ctl.release_anchor.read_release_record", return_value=None):
            ok, msgs = run_validation("ProjX", repos_dir=str(tmp_path))
        assert ok and any(m.startswith("[WARN]") for m in msgs)
    finally:
        for p in patches:
            p.stop()
