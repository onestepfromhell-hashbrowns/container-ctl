import json
import subprocess
from unittest.mock import patch

from container_ctl.cli.validate import run_validation
from container_ctl.deploy_source import config_repo_path, resolve_deploy_repo

HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _bare(path, version):
    work = path.parent / (path.name + "_w")
    work.mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True)
    g("init"); g("config", "user.email", "t@x.invalid"); g("config", "user.name", "T")
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "ProjX", "version": version, "dependencies": ["dev-svc"]}))
    (work / ".deploy/containers/dev-svc.json").write_text(json.dumps(
        {"name": "dev-svc", "tier": "dev", "image": f"localhost/x@sha256:{HEX}", "ports": [], "lifecycle_policy": {"mode": "manual"}}))
    g("add", "."); g("commit", "-m", "c")
    subprocess.run(["git", "clone", "--bare", str(work), str(path)], check=True, capture_output=True)


def test_legacy_when_no_config_repo(tmp_path):
    repos = tmp_path / "repos"; repos.mkdir()
    _bare(repos / "ProjX.git", "legacy")
    repo, kind = resolve_deploy_repo("ProjX", str(repos))
    assert kind == "legacy" and repo.endswith("ProjX.git") and "repos" in repo


def test_config_repo_wins_when_present(tmp_path):
    repos = tmp_path / "repos"; repos.mkdir()
    _bare(repos / "ProjX.git", "legacy")
    (tmp_path / ".config_repos").mkdir()
    _bare(tmp_path / ".config_repos" / "ProjX.git", "from-config")
    repo, kind = resolve_deploy_repo("ProjX", str(repos))
    assert kind == "config" and repo == config_repo_path("ProjX", str(repos))
    m = json.loads(subprocess.run(["git", "--git-dir", repo, "show", "HEAD:.deploy/manifest.json"], capture_output=True, text=True).stdout)
    assert m["version"] == "from-config"


def test_explicit_config_dir_overrides(tmp_path):
    cfg = tmp_path / "cfg"; cfg.mkdir()
    _bare(cfg / "ProjX.git", "c")
    assert resolve_deploy_repo("ProjX", None, str(cfg))[1] == "config"


def test_validate_reads_config_repo_not_legacy(tmp_path):
    repos = tmp_path / "repos"; repos.mkdir()
    _bare(repos / "ProjX.git", "legacy")
    (tmp_path / ".config_repos").mkdir()
    _bare(tmp_path / ".config_repos" / "ProjX.git", "from-config")
    seen = {}
    real = __import__("container_ctl.cli.validate", fromlist=["read_manifest"]).read_manifest

    def spy(repo, ref):
        seen["repo"] = repo
        return real(repo, ref)

    with patch("container_ctl.cli.validate.read_manifest", side_effect=spy), \
         patch("container_ctl.cli.validate._check_image_digests", return_value=(True, "")), \
         patch("container_ctl.cli.validate.validate_volumes", return_value=(True, [], [])), \
         patch("container_ctl.cli.validate.check_image_volumes", return_value=(True, [])):
        ok, _ = run_validation("ProjX", repos_dir=str(repos))
    assert ok and ".config_repos" in seen["repo"]


def test_validate_warns_when_legacy_and_not_when_config(tmp_path):
    repos = tmp_path / "repos"; repos.mkdir()
    _bare(repos / "ProjX.git", "legacy")
    patches = [
        patch("container_ctl.cli.validate._check_image_digests", return_value=(True, "")),
        patch("container_ctl.cli.validate.validate_volumes", return_value=(True, [], [])),
        patch("container_ctl.cli.validate.check_image_volumes", return_value=(True, [])),
    ]
    for p in patches:
        p.start()
    try:
        ok, msgs = run_validation("ProjX", repos_dir=str(repos))
        assert ok and msgs[0].startswith("[OK]") and any("legacy 模式" in m for m in msgs)
        (tmp_path / ".config_repos").mkdir()
        _bare(tmp_path / ".config_repos" / "ProjX.git", "c")
        ok, msgs = run_validation("ProjX", repos_dir=str(repos))
        assert ok and not any("legacy 模式" in m for m in msgs)
    finally:
        for p in patches:
            p.stop()
