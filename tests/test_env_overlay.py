import json
import subprocess

import pytest

from container_ctl.cli.config import config_set
from container_ctl.cli.image import ImagePrepareError
from container_ctl.declaration_guard import check_declarations
from container_ctl.env_overlay import OverlayError, merge_overlay, read_profile
from container_ctl.repo_reader import list_container_configs, read_container_config

DIGEST = "@sha256:" + "a" * 64
BASE = {"name": "prod-s", "tier": "prod", "image": "localhost/x" + DIGEST, "version": "1.0.0",
        "ports": ["127.0.0.2:8000:8000"], "env": {"A": "1", "B": "base"}}


@pytest.fixture
def cfg_repo(tmp_path):
    work = tmp_path / "w"
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "ProjX", "version": "1.0.0", "dependencies": ["prod-s"]}))
    (work / ".deploy/containers/prod-s.json").write_text(json.dumps(BASE))
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
    g("init", "-b", "main"); g("config", "user.email", "t@x.invalid"); g("config", "user.name", "T")
    g("add", "."); g("commit", "-m", "base")
    bare = tmp_path / ".config_repos" / "ProjX.git"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return tmp_path, work, bare


def _commit(work, bare, files):
    for path, data in files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(json.dumps(data))
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "overlay"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", str(bare), "HEAD:refs/heads/main"], cwd=work, check=True)


def test_merge_semantics_dict_recursive_list_replaced_forbidden_rejected():
    out = merge_overlay(BASE, {"env": {"B": "x", "C": "3"}, "ports": ["127.0.0.2:9:9"]})
    assert out["env"] == {"A": "1", "B": "x", "C": "3"} and out["ports"] == ["127.0.0.2:9:9"] and out["image"] == BASE["image"]
    for key in ("image", "version", "name", "tier", "command", "user", "read_only"):
        with pytest.raises(OverlayError, match=key):
            merge_overlay(BASE, {key: "x"})
    assert merge_overlay(BASE, None) is BASE


def test_no_overlay_equals_base_and_overlay_applies_per_env(cfg_repo):
    tmp, work, bare = cfg_repo
    assert read_container_config(str(bare), "HEAD", "prod-s")["env"] == BASE["env"]
    _commit(work, bare, {".deploy/envs/dev/containers/prod-s.json": {"env": {"B": "dev"}},
                         ".deploy/envs/vps/containers/prod-s.json": {"env": {"B": "vps"}, "ports": ["10.0.0.5:80:8000"]}})
    assert read_container_config(str(bare), "HEAD", "prod-s")["env"] == BASE["env"]  # 沒有預設環境：只用 base
    assert read_container_config(str(bare), "HEAD", "prod-s", env="dev")["env"]["B"] == "dev"
    vps = list_container_configs(str(bare), "HEAD", env="vps")[0]
    assert vps["env"] == {"A": "1", "B": "vps"} and vps["ports"] == ["10.0.0.5:80:8000"] and vps["image"] == BASE["image"]


def test_forbidden_overlay_key_fails_read(cfg_repo):
    tmp, work, bare = cfg_repo
    _commit(work, bare, {".deploy/envs/dev/containers/prod-s.json": {"image": "evil" + DIGEST}})
    from container_ctl.repo_reader import DeployConfigNotFoundError
    with pytest.raises(DeployConfigNotFoundError, match="不得覆蓋 'image'"):
        read_container_config(str(bare), "HEAD", "prod-s", env="dev")


def test_profile_overrides_host_ip_rule(cfg_repo):
    tmp, work, bare = cfg_repo
    cfg = dict(BASE, ports=["10.0.0.5:80:8000"])
    assert not check_declarations([cfg])[0]
    _commit(work, bare, {".deploy/envs/vps/profile.json": {"host_ip": {"prod": "10.0.0.5"}}})
    profile = read_profile(str(bare), "HEAD", "vps")
    assert profile["host_ip"] == {"dev": "127.0.0.1", "prod": "10.0.0.5"}
    assert check_declarations([cfg], profile)[0]
    assert read_profile(str(bare), "HEAD", "none")["host_ip"]["prod"] == "127.0.0.2"


def test_config_set_env_writes_overlay_only_and_base_untouched(cfg_repo):
    tmp, work, bare = cfg_repo
    kw = dict(repos_dir=str(tmp / ".git_repos"), config_dir=str(tmp / ".config_repos"), audit_root=str(tmp / "audit"))
    show = lambda p: subprocess.run(["git", "--git-dir", str(bare), "show", f"HEAD:{p}"], capture_output=True, text=True)
    _, out = config_set("ProjX", "prod-s", {"B": "vps", "N": "1"}, [], env_name="vps", yes=True, **kw)
    assert any(".deploy/envs/vps/containers/prod-s.json" in l for l in out)
    assert json.loads(show(".deploy/envs/vps/containers/prod-s.json").stdout) == {"env": {"B": "vps", "N": "1"}}
    assert json.loads(show(".deploy/containers/prod-s.json").stdout) == BASE
    # 之後同環境的 config set（不帶 --env）預設環境無 overlay → 寫 base；有 overlay 的環境才續寫 overlay
    _, out = config_set("ProjX", "prod-s", {"N": "2"}, [], env_name="vps", yes=True, **kw)
    assert json.loads(show(".deploy/envs/vps/containers/prod-s.json").stdout)["env"]["N"] == "2"
    with pytest.raises(ImagePrepareError, match="來自 base"):
        config_set("ProjX", "prod-s", {}, ["A"], env_name="vps", **kw)
    config_set("ProjX", "prod-s", {}, ["N"], env_name="vps", yes=True, **kw)
    assert "N" not in json.loads(show(".deploy/envs/vps/containers/prod-s.json").stdout)["env"]
    rec = json.loads((tmp / "audit/ProjX/config-audit.jsonl").read_text().splitlines()[0])
    assert rec["env"] == "vps" and rec["path"].endswith("envs/vps/containers/prod-s.json")
    _, out = config_set("ProjX", "prod-s", {"B": "base2"}, [], yes=True, **kw)  # 沒有預設環境 → base
    assert json.loads(show(".deploy/containers/prod-s.json").stdout)["env"]["B"] == "base2"


def test_config_show_effective(cfg_repo):
    from container_ctl.cli.config import config_show
    tmp, work, bare = cfg_repo
    _commit(work, bare, {".deploy/envs/vps/containers/prod-s.json": {"env": {"B": "vps"}}})
    kw = dict(repos_dir=str(tmp / ".git_repos"), config_dir=str(tmp / ".config_repos"))
    assert json.loads(config_show("ProjX", "prod-s", "vps", **kw))["env"]["B"] == "vps"
    assert json.loads(config_show("ProjX", "prod-s", **kw))["env"]["B"] == "base"
