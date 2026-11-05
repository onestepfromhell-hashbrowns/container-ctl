import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from container_ctl.cli.image import ImagePrepareError, _dev_build_context, audit_dev_image, prepare_dev_image, publish_dev_image
from container_ctl.cli.main import build_parser
from container_ctl.db import init_db


IMAGE_ID = "d" * 64


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git("init", str(work))
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "test-proj", "version": "v1", "dependencies": ["dev-api"]}))
    (work / ".deploy/containers/dev-api.json").write_text(json.dumps({"name": "dev-api", "tier": "dev", "image": "localhost/dev-api:old@sha256:" + "a" * 64}))
    (work / "Dockerfile.dev").write_text("FROM scratch\n")
    (work / "source.txt").write_text("committed")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "source", cwd=work)
    repos = tmp_path / "repos"
    repos.mkdir()
    _git("clone", "--bare", str(work), str(repos / "test-proj.git"))
    (work / "source.txt").write_text("uncommitted")
    return repos


def test_prepare_dev_uses_clean_bare_commit_and_emits_pin(repo, tmp_path):
    actual_run = subprocess.run
    builds = []
    source_commit = _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD")
    context_hash = "blob:" + _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD:Dockerfile.dev")

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["podman", "build"]:
            assert not (Path(args[-1]) / "source.txt").exists()
            assert (Path(args[-1]) / "Dockerfile.dev").read_text() == "FROM scratch\n"
            builds.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            output = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": context_hash,
                "org.container-ctl.project": "test-proj",
                "org.container-ctl.service": "dev-api",
                "org.container-ctl.image-scope": "dev-infra",
            }}) if args[3].startswith("localhost/") else "sha256:" + IMAGE_ID
            return subprocess.CompletedProcess(args, 0, output, "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))

    assert len(builds) == 1
    assert "--pull=never" in builds[0]
    config = json.loads((artifact / "dev-api.json").read_text())
    receipt = json.loads((artifact / "receipt.json").read_text())
    assert config["image"] == "localhost/dev-api@sha256:" + IMAGE_ID
    assert receipt["source_commit"] == _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD")
    assert receipt["image_id"] == "sha256:" + IMAGE_ID
    assert f"org.container-ctl.context-hash={receipt['context_hash']}" in builds[0]
    assert not any(a.startswith("org.container-ctl.source-commit") for a in builds[0])
    assert receipt["context_hash"].startswith("blob:")


def test_prepare_dev_rejects_unlisted_service_before_build(repo, tmp_path):
    with pytest.raises(ImagePrepareError, match="manifest dependencies"):
        prepare_dev_image("test-proj", "other", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
    assert not (tmp_path / "out").exists()


def test_prepare_dev_handles_absolute_symlinks_in_archive(tmp_path):
    work = tmp_path / "work_sym"
    work.mkdir()
    _git("init", str(work))
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "sym-proj", "version": "v1", "dependencies": ["dev-api"]}))
    (work / ".deploy/containers/dev-api.json").write_text(json.dumps({"name": "dev-api", "tier": "dev", "image": "localhost/dev-api:old@sha256:" + "a" * 64}))
    (work / "Dockerfile.dev").write_text("FROM scratch\n")
    (work / ".agents/rules").mkdir(parents=True)
    (work / ".agents/rules/agent-frontmatter.md").symlink_to("/opt/container-ctl/dummy.md")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "add absolute symlink", cwd=work)
    repos = tmp_path / "repos_sym"
    repos.mkdir()
    _git("clone", "--bare", str(work), str(repos / "sym-proj.git"))

    source_commit = _git("--git-dir", str(repos / "sym-proj.git"), "rev-parse", "HEAD")
    context_hash = "blob:" + _git("--git-dir", str(repos / "sym-proj.git"), "rev-parse", "HEAD:Dockerfile.dev")
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["podman", "build"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            output = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": context_hash,
                "org.container-ctl.project": "sym-proj",
                "org.container-ctl.service": "dev-api",
                "org.container-ctl.image-scope": "dev-infra",
            }}) if args[3].startswith("localhost/") else "sha256:" + IMAGE_ID
            return subprocess.CompletedProcess(args, 0, output, "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        artifact = prepare_dev_image("sym-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repos))

    assert (artifact / "receipt.json").exists()


def test_dev_build_context_rejects_workspace_copy(tmp_path):
    (tmp_path / "Dockerfile.dev").write_text("FROM scratch\nCOPY src/ /workspace/src/\n")
    with pytest.raises(ImagePrepareError, match="workspace"):
        _dev_build_context(tmp_path, "dev-api", tmp_path / "isolated")


def test_dev_build_context_uses_only_dedicated_infra_assets(tmp_path):
    dedicated = tmp_path / ".deploy/dev-images/dev-api"
    dedicated.mkdir(parents=True)
    (dedicated / "Dockerfile.dev").write_text("FROM scratch\nCOPY wheels/ /opt/dev-wheels/\n")
    (dedicated / "wheels").mkdir()
    (dedicated / "wheels" / "tool.whl").write_text("fixture")
    (tmp_path / "src").mkdir()
    selected = _dev_build_context(tmp_path, "dev-api", tmp_path / "isolated")
    assert selected == dedicated
    assert not (selected / "src").exists()


def test_dev_build_context_rejects_symlink_to_project_source(tmp_path):
    dedicated = tmp_path / ".deploy/dev-images/dev-api"
    dedicated.mkdir(parents=True)
    (dedicated / "Dockerfile.dev").write_text("FROM scratch\n")
    source = tmp_path / "source.txt"
    source.write_text("app code")
    (dedicated / "source-link").symlink_to(source)
    with pytest.raises(ImagePrepareError, match="symlink"):
        _dev_build_context(tmp_path, "dev-api", tmp_path / "isolated")


def test_prepare_dev_help_explains_scope(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["image", "prepare-dev", "--help"])
    assert exc.value.code == 0
    assert "不修改 Bare Repo" in capsys.readouterr().out


def test_publish_dev_commits_only_pin_after_candidate_validation(repo, tmp_path, monkeypatch):
    actual_run = subprocess.run
    source_commit = _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD")
    context_hash = "blob:" + _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD:Dockerfile.dev")
    branch = _git("--git-dir", str(repo / "test-proj.git"), "symbolic-ref", "--short", "HEAD")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["podman", "build"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            output = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": context_hash,
                "org.container-ctl.project": "test-proj",
                "org.container-ctl.service": "dev-api",
                "org.container-ctl.image-scope": "dev-infra",
            }})
            return subprocess.CompletedProcess(args, 0, output, "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run), patch(
        "container_ctl.cli.validate.run_validation", return_value=(True, ["[OK] test"]),
    ) as validation:
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        new_commit = publish_dev_image(str(artifact), branch, yes=True, repos_dir=str(repo))

    validation.assert_called_once()
    assert new_commit != source_commit
    assert _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", branch) == new_commit
    change = _git("--git-dir", str(repo / "test-proj.git"), "diff-tree", "--no-commit-id", "--name-only", "-r", new_commit)
    assert change == ".deploy/containers/dev-api.json"


def test_publish_dev_refuses_stale_branch_before_mutation(repo, tmp_path):
    source_commit = _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD")
    branch = _git("--git-dir", str(repo / "test-proj.git"), "symbolic-ref", "--short", "HEAD")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "receipt.json").write_text(json.dumps({
        "project": "test-proj", "service": "dev-api", "source_commit": "b" * 40, "context_hash": "blob:" + "c" * 40,
        "image_id": "sha256:" + IMAGE_ID, "pinned_image": "localhost/dev-api@sha256:" + IMAGE_ID,
    }))
    (artifact / "dev-api.json").write_text("{}")
    with pytest.raises(ImagePrepareError, match="已變更"):
        publish_dev_image(str(artifact), branch, yes=True, repos_dir=str(repo))
    assert _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD") == source_commit


def test_audit_dev_detects_runtime_image_drift(repo, tmp_path):
    db = tmp_path / "state.db"
    services = tmp_path / "services"
    unit = services / "test-proj" / "svc-dev-api.container"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Container]\nImage=localhost/dev-api:old\n")
    quadlets = tmp_path / "quadlets"
    quadlets.mkdir()
    (quadlets / unit.name).symlink_to(unit)
    config = {"name": "dev-api", "tier": "dev", "image": "localhost/dev-api:old@sha256:" + "a" * 64}
    conn = init_db(str(db))
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status, blueprint,
            quadlet_path, container_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("test-proj-dev-api", "test-proj", "dev-api", "v1", "dev", "dedicated", "stopped",
         json.dumps(config), str(unit), "svc-dev-api", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:3] == ["podman", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 1, "", "image not known")
        if args[:2] == ["podman", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Image": "b" * 64, "State": {"Running": True}}), "")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(args, 0, "active\n", "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        ok, messages = audit_dev_image(
            "test-proj", "dev-api", "HEAD", repos_dir=str(repo), db_path=str(db),
            services_dir=str(services), quadlet_dir=quadlets,
        )
    assert not ok
    assert any("pinned 映像不在本機" in msg for msg in messages)
    assert any("state.db 記為 stopped" in msg for msg in messages)


def _insert_instance(conn, instance_id, status, container, config):
    conn.execute(
        """INSERT INTO instances
           (id, project_name, service_name, version, tier, scope, status, blueprint,
            quadlet_path, container_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (instance_id, "test-proj", "dev-api", "v1", "dev", "dedicated", status,
         json.dumps(config), "q", container, "2026-01-01", "2026-01-01"),
    )


def _audit_with_rows(repo, tmp_path, rows, pin_labels=None, mode=None):
    db = tmp_path / "state.db"
    services = tmp_path / "services"
    (services / "test-proj").mkdir(parents=True)
    config = {"name": "dev-api", "tier": "dev", "image": "localhost/dev-api:old@sha256:" + "a" * 64}
    if mode:
        config["lifecycle_policy"] = {"mode": mode}
    conn = init_db(str(db))
    for instance_id, status, container in rows:
        _insert_instance(conn, instance_id, status, container, config)
    conn.commit()
    conn.close()
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if pin_labels is not None and args[:3] == ["podman", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, json.dumps({"Id": "sha256:" + "a" * 64, "Labels": pin_labels}), "")
        if args[0] in ("podman", "systemctl"):
            return subprocess.CompletedProcess(args, 1, "", "x")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        return audit_dev_image(
            "test-proj", "dev-api", "HEAD", repos_dir=str(repo), db_path=str(db),
            services_dir=str(services), quadlet_dir=tmp_path / "quadlets",
        )


def test_audit_dev_ignores_retired_legacy_id_rows(repo, tmp_path):
    ok, messages = _audit_with_rows(repo, tmp_path, [
        ("test-proj-dev-api-v1", "stopped", "svc-dev-api-v1"),
        ("test-proj-dev-api", "provisioned", "svc-dev-api"),
    ])
    assert any("忽略 1 筆已退役" in msg for msg in messages)
    assert not any("實例數應為 1" in msg for msg in messages)
    assert any("Quadlet" in msg for msg in messages)  # 後續檢查有執行


def test_audit_dev_still_rejects_ambiguous_active_rows(repo, tmp_path):
    ok, messages = _audit_with_rows(repo, tmp_path, [
        ("test-proj-dev-api-v1", "provisioned", "svc-dev-api-v1"),
        ("test-proj-dev-api", "provisioned", "svc-dev-api"),
    ])
    assert not ok
    assert any("實例數應為 1，實際 2" in msg for msg in messages)


def _fake_podman(context_hash):
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] in (["podman", "build"], ["podman", "tag"]):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            output = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": context_hash,
                "org.container-ctl.project": "test-proj",
                "org.container-ctl.service": "dev-api",
                "org.container-ctl.image-scope": "dev-infra",
            }}) if args[3].startswith("localhost/") or args[3].startswith("sha256:") and "--format" in args and "{{json .}}" in args else "sha256:" + IMAGE_ID
            return subprocess.CompletedProcess(args, 0, output, "")
        return actual_run(args, **kwargs)
    return fake_run


def _advance_branch(repo, files):
    work = repo.parent / "advance"
    _git("clone", str(repo / "test-proj.git"), str(work))
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    for name, text in files.items():
        (work / name).write_text(text)
    _git("add", ".", cwd=work)
    _git("commit", "-m", "advance", cwd=work)
    _git("push", "origin", "HEAD", cwd=work)


def test_publish_dev_accepts_advanced_branch_when_context_unchanged(repo, tmp_path, monkeypatch):
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "test@example.invalid")
    bare = str(repo / "test-proj.git")
    branch = _git("--git-dir", bare, "symbolic-ref", "--short", "HEAD")
    context_hash = "blob:" + _git("--git-dir", bare, "rev-parse", "HEAD:Dockerfile.dev")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake_podman(context_hash)), patch(
        "container_ctl.cli.validate.run_validation", return_value=(True, ["[OK]"]),
    ):
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        _advance_branch(repo, {"unrelated.txt": "x"})  # context 未變
        advanced = _git("--git-dir", bare, "rev-parse", branch)
        new_commit = publish_dev_image(str(artifact), branch, yes=True, repos_dir=str(repo))
    assert _git("--git-dir", bare, "rev-parse", f"{new_commit}^") == advanced
    assert _git("--git-dir", bare, "show", f"{new_commit}:unrelated.txt") == "x"


def test_publish_dev_refuses_when_context_changed(repo, tmp_path):
    bare = str(repo / "test-proj.git")
    branch = _git("--git-dir", bare, "symbolic-ref", "--short", "HEAD")
    context_hash = "blob:" + _git("--git-dir", bare, "rev-parse", "HEAD:Dockerfile.dev")
    with patch("container_ctl.cli.image.subprocess.run", side_effect=_fake_podman(context_hash)):
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        _advance_branch(repo, {"Dockerfile.dev": "FROM scratch\nRUN true\n"})
        before = _git("--git-dir", bare, "rev-parse", branch)
        with pytest.raises(ImagePrepareError, match="build context"):
            publish_dev_image(str(artifact), branch, yes=True, repos_dir=str(repo))
    assert _git("--git-dir", bare, "rev-parse", branch) == before


def test_prepare_dev_missing_base_image_hint(repo, tmp_path):
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "build"]:
            return subprocess.CompletedProcess(args, 125, "", "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        with pytest.raises(ImagePrepareError, match="--pull-policy missing"):
            prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))


def test_audit_dev_flags_stale_context_hash(repo, tmp_path):
    ok, messages = _audit_with_rows(repo, tmp_path, [("test-proj-dev-api", "provisioned", "svc-dev-api")], pin_labels={
        "org.container-ctl.image-scope": "dev-infra", "org.container-ctl.context-hash": "blob:" + "d" * 40,
    })
    assert not ok
    assert any("映像已過時" in m for m in messages)


def test_audit_dev_accepts_matching_context_hash_and_warns_on_legacy(repo, tmp_path):
    good = "blob:" + _git("--git-dir", str(repo / "test-proj.git"), "rev-parse", "HEAD:Dockerfile.dev")
    _, messages = _audit_with_rows(repo, tmp_path, [("test-proj-dev-api", "provisioned", "svc-dev-api")], pin_labels={
        "org.container-ctl.image-scope": "dev-infra", "org.container-ctl.context-hash": good,
    })
    assert not any("過時" in m or "context-hash" in m for m in messages)
    _, legacy = _audit_with_rows(repo, tmp_path / "legacy", [("test-proj-dev-api", "provisioned", "svc-dev-api")], pin_labels={
        "org.container-ctl.image-scope": "dev-infra", "org.container-ctl.source-commit": "e" * 40,
    })
    assert any("[WARN]" in m and "context-hash" in m for m in legacy)


def _cfg_with_context(repo, tmp_path, with_context):
    from container_ctl.cli.config import config_init
    cfg = config_init("test-proj", repos_dir=str(repo))
    if with_context:
        work = tmp_path / "cfgwork"
        _git("clone", cfg, str(work))
        _git("config", "user.email", "t@example.invalid", cwd=work)
        _git("config", "user.name", "T", cwd=work)
        (work / ".deploy/dev-images/dev-api").mkdir(parents=True)
        (work / ".deploy/dev-images/dev-api/Dockerfile.dev").write_text("FROM scratch\n")
        _git("add", ".", cwd=work)
        _git("commit", "-qm", "ctx", cwd=work)
        _git("push", "-q", "origin", "HEAD", cwd=work)
    return cfg


def test_config_mode_prepare_dev_requires_dedicated_context(repo, tmp_path):
    _cfg_with_context(repo, tmp_path, with_context=False)
    with pytest.raises(ImagePrepareError, match="dev-images"):
        prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))


def test_config_mode_prepare_dev_builds_from_config_repo_context(repo, tmp_path):
    cfg = _cfg_with_context(repo, tmp_path, with_context=True)
    actual_run = subprocess.run
    tree = _git("--git-dir", cfg, "rev-parse", "HEAD:.deploy/dev-images/dev-api")
    builds = []

    def fake_run(args, **kwargs):
        if args[:2] == ["podman", "tag"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["podman", "build"]:
            builds.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            out = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": "tree:" + tree, "org.container-ctl.project": "test-proj",
                "org.container-ctl.service": "dev-api", "org.container-ctl.image-scope": "dev-infra"}}) if args[3].startswith("localhost/") else "sha256:" + IMAGE_ID
            return subprocess.CompletedProcess(args, 0, out, "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run):
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
    receipt = json.loads((artifact / "receipt.json").read_text())
    assert receipt["context_hash"] == "tree:" + tree and receipt["source_commit"] == _git("--git-dir", cfg, "rev-parse", "HEAD")
    assert len(builds) == 1


def test_config_mode_publish_dev_pins_into_config_repo_only(repo, tmp_path, monkeypatch):
    cfg = _cfg_with_context(repo, tmp_path, with_context=True)
    code = str(repo / "test-proj.git")
    code_head = _git("--git-dir", code, "rev-parse", "HEAD")
    cfg_head = _git("--git-dir", cfg, "rev-parse", "HEAD")
    tree = _git("--git-dir", cfg, "rev-parse", "HEAD:.deploy/dev-images/dev-api")
    branch = _git("--git-dir", cfg, "symbolic-ref", "--short", "HEAD")
    for k, v in (("NAME", "Test"), ("EMAIL", "test@example.invalid")):
        monkeypatch.setenv(f"GIT_AUTHOR_{k}", v)
        monkeypatch.setenv(f"GIT_COMMITTER_{k}", v)
    actual_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[:2] in (["podman", "tag"], ["podman", "build"]):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:3] == ["podman", "image", "inspect"]:
            out = json.dumps({"Id": "sha256:" + IMAGE_ID, "Labels": {
                "org.container-ctl.context-hash": "tree:" + tree, "org.container-ctl.project": "test-proj",
                "org.container-ctl.service": "dev-api", "org.container-ctl.image-scope": "dev-infra"}})
            return subprocess.CompletedProcess(args, 0, out, "")
        return actual_run(args, **kwargs)

    with patch("container_ctl.cli.image.subprocess.run", side_effect=fake_run), patch(
        "container_ctl.cli.validate.run_validation", return_value=(True, ["[OK] test"]),
    ):
        artifact = prepare_dev_image("test-proj", "dev-api", "HEAD", str(tmp_path / "out"), repos_dir=str(repo))
        new_commit = publish_dev_image(str(artifact), branch, yes=True, repos_dir=str(repo))
    assert _git("--git-dir", code, "rev-parse", "HEAD") == code_head  # 程式碼 repo 不變
    assert _git("--git-dir", cfg, "rev-parse", branch) == new_commit != cfg_head
    assert _git("--git-dir", cfg, "diff-tree", "--no-commit-id", "--name-only", "-r", new_commit) == ".deploy/containers/dev-api.json"


def test_audit_dev_manual_not_started_is_info_not_error(repo, tmp_path):
    ok, messages = _audit_with_rows(
        repo, tmp_path, [("test-proj-dev-api", "provisioned", "svc-dev-api")],
        pin_labels={"org.container-ctl.image-scope": "dev-infra"}, mode="manual",
    )
    assert any("manual 實例尚未啟動" in m for m in messages)
    assert not any("容器不存在" in m or "systemd 狀態" in m for m in messages)


def test_audit_dev_always_on_not_started_is_error(repo, tmp_path):
    ok, messages = _audit_with_rows(
        repo, tmp_path, [("test-proj-dev-api", "provisioned", "svc-dev-api")],
        pin_labels={"org.container-ctl.image-scope": "dev-infra"}, mode="always_on",
    )
    assert not ok
    assert any("容器不存在" in m for m in messages)
