import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from container_ctl.cli.image import ImagePrepareError
from container_ctl.cli.image_prod import prepare_prod_image, publish_prod_image, read_release_record
from container_ctl.cli.main import build_parser

IMAGE_ID = "e" * 64


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _make_repo(tmp_path, tier="prod", image="localhost/prod-api:0.1.0"):
    work = tmp_path / "work"
    work.mkdir()
    _git("init", str(work))
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "test-proj", "version": "v1", "dependencies": ["prod-api"]}))
    (work / ".deploy/containers/prod-api.json").write_text(json.dumps({"name": "prod-api", "tier": tier, "image": image}))
    (work / "Dockerfile.prod").write_text("FROM scratch\nCOPY app.txt /app.txt\n")
    (work / "app.txt").write_text("logic")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "src", cwd=work)
    repos = tmp_path / "repos"
    repos.mkdir()
    _git("clone", "--bare", str(work), str(repos / "test-proj.git"))
    return repos


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


def _fake(commit, labels_override=None):
    actual = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["podman", "build"]:
            assert (Path(args[-1]) / "app.txt").read_text() == "logic"
            assert f"org.container-ctl.source-commit={commit}" in args
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            labels = {
                "org.container-ctl.source-commit": commit, "org.container-ctl.version": "1.0.0",
                "org.container-ctl.project": "test-proj", "org.container-ctl.service": "prod-api",
                "org.container-ctl.image-scope": "prod",
            }
            labels.update(labels_override or {})
            return subprocess.CompletedProcess(args, 0, json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": labels}), "")
        return actual(args, **kwargs)

    return fake_run


def test_prepare_then_publish_writes_release_tag(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        config = json.loads((artifact / "prod-api.json").read_text())
        assert config["image"] == "localhost/prod-api@sha256:" + IMAGE_ID
        assert read_release_record(bare, "prod-api", "1.0.0") is None  # prepare 不寫 tag
        assert publish_prod_image(str(artifact), repos_dir=str(repo), yes=True) == "release/prod-api/1.0.0"
    record = read_release_record(bare, "prod-api", "1.0.0")
    assert record == {"schema": 2, "service": "prod-api", "version": "1.0.0", "image_id": "sha256:" + IMAGE_ID, "source_commit": commit, "config_commit": commit,
                      "sbom": None, "signature": None,
                      "provenance": {"builder": "container-ctl", "source_commit": commit, "config_commit": commit}}


def test_existing_release_tag_blocks_prepare_and_publish(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
        with pytest.raises(ImagePrepareError, match="已存在"):
            publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
        with pytest.raises(ImagePrepareError, match="已凍結"):
            prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))


def test_publish_refuses_label_mismatch(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit, {"org.container-ctl.version": "9.9.9"})), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit, {"org.container-ctl.version": "9.9.9"})):
        with pytest.raises(ImagePrepareError, match="標籤"):
            publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
    assert read_release_record(bare, "prod-api", "1.0.0") is None


def test_prepare_rejects_non_prod_service_and_missing_dockerfile(repo, tmp_path):
    work = tmp_path / "w2"
    _git("clone", str(repo / "test-proj.git"), str(work))
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    _git("rm", "-q", "Dockerfile.prod", cwd=work)
    _git("commit", "-qm", "rm", cwd=work)
    _git("push", "-q", "origin", "HEAD", cwd=work)
    with pytest.raises(ImagePrepareError, match="Dockerfile.prod"):
        prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
    with pytest.raises(ImagePrepareError, match="version 無效"):
        prepare_prod_image("test-proj", "prod-api", "../x", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))


def test_cli_registers_prod_commands():
    args = build_parser().parse_args(["image", "prepare-prod", "P", "svc", "1.0.0", "HEAD", "--output-dir", "/tmp/x"])
    assert args.subcommand == "prepare-prod" and args.version == "1.0.0"
    assert build_parser().parse_args(["image", "publish-prod", "/tmp/a", "-y"]).yes is True


def _live(image_id):
    actual = subprocess.run

    def run(args, **kwargs):
        if args[:2] == ["podman", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Image": image_id}), "")
        return actual(args, **kwargs)

    return run


def _db(tmp_path, image, status="provisioned"):
    from container_ctl.db import init_db
    path = tmp_path / "state.db"
    conn = init_db(str(path))
    conn.execute(
        "INSERT INTO instances (id, project_name, service_name, tier, scope, quadlet_path, version, container_name, blueprint, status, created_at, updated_at) "
        "VALUES (?,?,?,?,'dedicated','/x',?,?,?,?,datetime('now'),datetime('now'))",
        ("test-proj-prod-api-1.0.0", "test-proj", "prod-api", "prod", "1.0.0", "svc-prod-api-1.0.0", json.dumps({"image": image}), status),
    )
    conn.commit()
    conn.close()
    return str(path)


def _published(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
    return artifact


def _push_pin(repo, tmp_path, artifact):
    work = tmp_path / "w3"
    _git("clone", str(repo / "test-proj.git"), str(work))
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    (work / ".deploy/containers/prod-api.json").write_text((artifact / "prod-api.json").read_text())
    _git("commit", "-qam", "pin", cwd=work)
    _git("push", "-q", "origin", "HEAD", cwd=work)


def test_audit_prod_ok_and_detects_drift(repo, tmp_path):
    from container_ctl.cli.image_prod import audit_prod_image
    artifact = _published(repo, tmp_path)
    _push_pin(repo, tmp_path, artifact)
    pinned = "localhost/prod-api@sha256:" + IMAGE_ID
    with patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_live(IMAGE_ID)):
        ok, msgs = audit_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=_db(tmp_path, pinned))
    assert ok, msgs
    with patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_live("f" * 64)):
        ok, msgs = audit_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=str(tmp_path / "state.db"))
    assert not ok and any("運行容器" in m for m in msgs)


def test_audit_prod_flags_db_and_declaration_drift(repo, tmp_path):
    from container_ctl.cli.image_prod import audit_prod_image
    _published(repo, tmp_path)  # 宣告仍是未 pin 的原始 image
    stale = "localhost/prod-api@sha256:" + "a" * 64
    with patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_live(IMAGE_ID)):
        ok, msgs = audit_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=_db(tmp_path, stale))
    assert not ok
    assert any("Bare 宣告" in m for m in msgs) and any("state.db image" in m for m in msgs)


def test_audit_prod_requires_release_tag(repo, tmp_path):
    from container_ctl.cli.image_prod import audit_prod_image
    ok, msgs = audit_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=str(tmp_path / "x.db"))
    assert not ok and "發布記錄" in msgs[0]


def test_adopt_prod_writes_adopted_tag_only_when_live_matches(repo, tmp_path):
    from container_ctl.cli.image_prod import adopt_prod_release
    bare = str(repo / "test-proj.git")
    work = tmp_path / "w4"
    _git("clone", str(repo / "test-proj.git"), str(work))
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    pinned = "localhost/prod-api@sha256:" + IMAGE_ID
    (work / ".deploy/containers/prod-api.json").write_text(json.dumps({"name": "prod-api", "tier": "prod", "image": pinned}))
    _git("commit", "-qam", "pin", cwd=work)
    _git("push", "-q", "origin", "HEAD", cwd=work)
    db = _db(tmp_path, pinned)
    with patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_live("f" * 64)):
        with pytest.raises(ImagePrepareError, match="不符"):
            adopt_prod_release("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=db, yes=True)
    assert read_release_record(bare, "prod-api", "1.0.0") is None
    with patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_live(IMAGE_ID)):
        adopt_prod_release("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=db, yes=True)
        with pytest.raises(ImagePrepareError, match="已存在"):
            adopt_prod_release("test-proj", "prod-api", "1.0.0", "HEAD", repos_dir=str(repo), db_path=db, yes=True)
    record = read_release_record(bare, "prod-api", "1.0.0")
    assert record["adopted"] is True and record["image_id"] == "sha256:" + IMAGE_ID


def test_publish_rejects_option_like_image_id_in_receipt(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
    receipt = json.loads((artifact / "receipt.json").read_text())
    receipt["image_id"] = "--format=x"
    (artifact / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ImagePrepareError, match="image_id 格式無效"):
        publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)


def _config_repo(tmp_path, repo):
    from container_ctl.cli.config import config_init
    return config_init("test-proj", repos_dir=str(repo))


def test_config_mode_publish_prod_pins_into_config_repo_and_tags_there(repo, tmp_path):
    code = str(repo / "test-proj.git")
    code_head = _git("--git-dir", code, "rev-parse", "HEAD")
    cfg = _config_repo(tmp_path, repo)
    cfg_head = _git("--git-dir", cfg, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(code_head)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(code_head)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        receipt = json.loads((artifact / "receipt.json").read_text())
        assert receipt["deploy_kind"] == "config" and receipt["config_commit"] == cfg_head
        publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
    # 程式碼 repo 沒有 tag、沒有新 commit；設定 repo 有 pin commit 與 tag
    assert read_release_record(code, "prod-api", "1.0.0") is None
    assert _git("--git-dir", code, "rev-parse", "HEAD") == code_head
    record = read_release_record(cfg, "prod-api", "1.0.0")
    new_head = _git("--git-dir", cfg, "rev-parse", "HEAD")
    assert record["config_commit"] == new_head != cfg_head and record["source_commit"] == code_head
    pinned = json.loads(_git("--git-dir", cfg, "show", "HEAD:.deploy/containers/prod-api.json"))
    assert pinned["image"] == "localhost/prod-api@sha256:" + IMAGE_ID


def test_config_mode_publish_prod_refuses_when_config_repo_advanced(repo, tmp_path):
    code = str(repo / "test-proj.git")
    code_head = _git("--git-dir", code, "rev-parse", "HEAD")
    cfg = _config_repo(tmp_path, repo)
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(code_head)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(code_head)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        work = tmp_path / "adv"
        _git("clone", cfg, str(work))
        _git("config", "user.email", "t@example.invalid", cwd=work)
        _git("config", "user.name", "T", cwd=work)
        (work / "note").write_text("x")
        _git("add", ".", cwd=work)
        _git("commit", "-qm", "advance", cwd=work)
        _git("push", "-q", "origin", "HEAD", cwd=work)
        with pytest.raises(ImagePrepareError, match="已前進"):
            publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
    assert read_release_record(cfg, "prod-api", "1.0.0") is None


def test_store_failure_aborts_publish_before_pin_and_tag(repo, tmp_path, monkeypatch):
    from container_ctl import artifact_store as st
    bare = str(repo / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    order = []
    monkeypatch.setattr(st, "enabled", lambda: True)
    monkeypatch.setattr(st, "discard", lambda *a, **k: order.append("discard"))

    def boom(*a, **k):
        order.append("put")
        raise st.ArtifactStoreError("push failed")
    monkeypatch.setattr(st, "put", boom)
    monkeypatch.setattr("container_ctl.cli.image_prod._commit_pin_to_config_repo",
                        lambda *a, **k: order.append("pin") or commit)
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        artifact = prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        with pytest.raises(ImagePrepareError, match="未提交 pin"):
            publish_prod_image(str(artifact), repos_dir=str(repo), yes=True)
    assert order == ["discard", "put"] and read_release_record(bare, "prod-api", "1.0.0") is None


def _prepare(repos, tmp_path):
    bare = str(repos / "test-proj.git")
    commit = _git("--git-dir", bare, "rev-parse", "HEAD")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        return prepare_prod_image("test-proj", "prod-api", "1.0.0", "HEAD", str(tmp_path / "out"), repos_dir=str(repos)), commit


def test_infra_tier_builds_and_publishes_like_prod(tmp_path):
    """Reproducible image provenance does not depend on the role: infra goes through the same path as prod."""
    repos = _make_repo(tmp_path, tier="infra")
    artifact, commit = _prepare(repos, tmp_path)
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake(commit)), \
         patch("container_ctl.cli.image_prod.subprocess.run", side_effect=_fake(commit)):
        assert publish_prod_image(str(artifact), repos_dir=str(repos), yes=True) == "release/prod-api/1.0.0"
    assert json.loads((artifact / "prod-api.json").read_text())["tier"] == "infra"   # role is preserved, not rewritten
    assert read_release_record(str(repos / "test-proj.git"), "prod-api", "1.0.0")["image_id"] == "sha256:" + IMAGE_ID


def test_dev_tier_is_still_refused_by_prepare_prod(tmp_path):
    repos = _make_repo(tmp_path, tier="dev")
    with pytest.raises(ImagePrepareError, match="不是 prod／infra"):
        _prepare(repos, tmp_path)


def test_infra_tier_still_requires_a_local_image(tmp_path):
    repos = _make_repo(tmp_path, tier="infra", image="docker.io/library/nginx:alpine")
    with pytest.raises(ImagePrepareError, match="localhost/"):
        _prepare(repos, tmp_path)
