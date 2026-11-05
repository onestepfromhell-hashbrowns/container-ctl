import json
import subprocess

import pytest

from container_ctl.cli.config import config_init, config_status
from container_ctl.cli.image import ImagePrepareError
from container_ctl.cli.main import build_parser
from container_ctl.deploy_source import resolve_deploy_repo


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    g = lambda *a, cwd=work: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    g("init"); g("config", "user.email", "t@x.invalid"); g("config", "user.name", "T")
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "ProjX", "version": "v1", "dependencies": ["dev-s"]}))
    (work / ".deploy/containers/dev-s.json").write_text(json.dumps({"name": "dev-s", "tier": "dev"}))
    (work / "app.py").write_text("code")
    g("add", "."); g("commit", "-m", "c")
    repos = tmp_path / "repos"; repos.mkdir()
    subprocess.run(["git", "clone", "--bare", str(work), str(repos / "ProjX.git")], check=True, capture_output=True)
    return tmp_path, repos


def test_init_creates_config_repo_with_only_deploy(env):
    tmp, repos = env
    path = config_init("ProjX", repos_dir=str(repos))
    assert path == str(tmp / ".config_repos" / "ProjX.git")
    files = subprocess.run(["git", "--git-dir", path, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".deploy/containers/dev-s.json", ".deploy/manifest.json"]
    assert resolve_deploy_repo("ProjX", str(repos))[1] == "config"
    log = subprocess.run(["git", "--git-dir", path, "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert "from code repo" in log


def test_init_refuses_existing_and_missing_deploy(env):
    tmp, repos = env
    config_init("ProjX", repos_dir=str(repos))
    with pytest.raises(ImagePrepareError, match="已存在"):
        config_init("ProjX", repos_dir=str(repos))
    with pytest.raises(ImagePrepareError, match="不存在"):
        config_init("none-proj", repos_dir=str(repos))


def test_status_legacy_then_config_and_worktree_drift(env):
    tmp, repos = env
    wt_root = tmp / "wt"
    wt = wt_root / "ProjX"
    (wt / ".deploy/containers").mkdir(parents=True)
    (wt / ".deploy/manifest.json").write_text("{}")
    ok, msgs = config_status("ProjX", repos_dir=str(repos), worktrees_dir=str(wt_root))
    assert ok and any("尚未建立設定 repo" in m for m in msgs) and any("工作樹" in m and m.startswith("[WARN]") for m in msgs)
    config_init("ProjX", repos_dir=str(repos))
    ok, msgs = config_status("ProjX", repos_dir=str(repos), worktrees_dir=str(wt_root))
    assert any("權威來源" in m for m in msgs)
    assert not any("與舊程式碼 repo" in m for m in msgs)


def test_cli_registers_config():
    assert build_parser().parse_args(["config", "init", "ProjX"]).subcommand == "init"
    assert build_parser().parse_args(["config", "status", "ProjX"]).project == "ProjX"


def _prod_env(env):
    tmp, repos = env
    work = tmp / "w"
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True)
    digest = "@sha256:" + "a" * 64
    (work / ".deploy/containers/prod-s.json").write_text(json.dumps(
        {"name": "prod-s", "tier": "prod", "image": "localhost/x" + digest, "version": "0.1.0", "env": {"A": "1"}}))
    g("add", "."); g("commit", "-m", "prod")
    g("push", str(repos / "ProjX.git"), "HEAD:refs/heads/master") if False else None
    subprocess.run(["git", "--git-dir", str(repos / "ProjX.git"), "fetch", "-q", str(work), "HEAD:refs/heads/main"], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(repos / "ProjX.git"), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    config_init("ProjX", repos_dir=str(repos))
    return tmp, repos


def test_config_set_preview_commit_and_audit(env):
    from container_ctl.cli.config import config_set
    tmp, repos = _prod_env(env)
    audit = tmp / "audit"
    kw = dict(repos_dir=str(repos), audit_root=str(audit))
    head = lambda: subprocess.run(["git", "--git-dir", str(tmp / ".config_repos/ProjX.git"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    before = head()
    _, out = config_set("ProjX", "prod-s", {"B": "2", "A": "9"}, [], **kw)
    assert any("env.A" in l and "'1' → '9'" in l for l in out) and head() == before and not audit.exists()
    _, out = config_set("ProjX", "prod-s", {"B": "2", "A": "9"}, [], yes=True, reason="why", **kw)
    assert head() != before
    shown = json.loads(subprocess.run(["git", "--git-dir", str(tmp / ".config_repos/ProjX.git"), "show", "HEAD:.deploy/containers/prod-s.json"], capture_output=True, text=True).stdout)
    assert shown["env"] == {"A": "9", "B": "2"} and shown["image"].endswith("a" * 64) and shown["version"] == "0.1.0"
    rec = json.loads((audit / "ProjX/config-audit.jsonl").read_text().splitlines()[-1])
    assert rec["reason"] == "why" and rec["from"] == before
    _, out = config_set("ProjX", "prod-s", {"B": "2"}, [], yes=True, **kw)
    assert any("無變更" in l for l in out)
    config_set("ProjX", "prod-s", {}, ["B"], yes=True, **kw)


def test_config_set_rejects_plaintext_secret_unknown_unset_and_legacy(env):
    from container_ctl.cli.config import config_set
    tmp, repos = env
    with pytest.raises(ImagePrepareError, match="尚未建立設定 repo"):
        config_set("ProjX", "dev-s", {"A": "1"}, [], repos_dir=str(repos))
    tmp, repos = _prod_env(env)
    with pytest.raises(ImagePrepareError, match="明文機密"):
        config_set("ProjX", "prod-s", {"DB_PASSWORD": "hunter2"}, [], repos_dir=str(repos))
    with pytest.raises(ImagePrepareError, match="沒有 NOPE"):
        config_set("ProjX", "prod-s", {}, ["NOPE"], repos_dir=str(repos))


def test_ineffective_source_warns_on_legacy_diff_and_unpushed_worktree(env):
    from container_ctl.cli.config import config_set, ineffective_source_messages
    tmp, repos = _prod_env(env)
    assert ineffective_source_messages("ProjX", repos_dir=str(repos), worktrees_dir=str(tmp / "none")) == []
    config_set("ProjX", "prod-s", {"B": "2"}, [], yes=True, repos_dir=str(repos), audit_root=str(tmp / "a"))
    msgs = ineffective_source_messages("ProjX", repos_dir=str(repos), worktrees_dir=str(tmp / "none"))
    assert any("已失效" in m and "prod-s.json" in m for m in msgs)
    wt_root = tmp / "wt"
    subprocess.run(["git", "clone", "-q", str(repos / "ProjX.git"), str(wt_root / "ProjX")], check=True, capture_output=True)
    wt = wt_root / "ProjX"
    (wt / "fix.txt").write_text("x")
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@x.invalid", "commit", "-qm", "fix"], check=True)
    msgs = ineffective_source_messages("ProjX", repos_dir=str(repos), worktrees_dir=str(wt_root))
    assert any("工作樹領先" in m for m in msgs)


def test_no_legacy_deploy_means_no_ineffective_warning(env):
    from container_ctl.cli.config import config_status, ineffective_source_messages
    tmp, repos = _prod_env(env)
    work = tmp / "rm"
    subprocess.run(["git", "clone", "-q", str(repos / "ProjX.git"), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "rm", "-rq", ".deploy"], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@x.invalid", "commit", "-qm", "drop"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD"], check=True, capture_output=True)
    assert ineffective_source_messages("ProjX", repos_dir=str(repos), worktrees_dir=str(tmp / "none")) == []
    ok, msgs = config_status("ProjX", repos_dir=str(repos), worktrees_dir=str(tmp / "none"))
    assert ok and not any("[WARN] 設定 repo 與舊程式碼" in m for m in msgs) and any("已無 .deploy" in m for m in msgs)


# ── sync-image-context ──
def _code_commit(tmp, repos, files: dict, remove=(), symlinks=None):
    work = tmp / "w"
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True).stdout.strip()
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    for rel in remove:
        (work / rel).unlink()
    for rel, target in (symlinks or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).symlink_to(target)
    g("add", "-A"); g("commit", "-m", "ctx")
    g("push", "-q", str(repos / "ProjX.git"), "HEAD:refs/heads/ctx")
    return g("rev-parse", "HEAD")


def _cfg_files(repo):
    return subprocess.run(["git", "--git-dir", repo, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()


@pytest.fixture
def synced(env):
    from container_ctl.cli.config import sync_image_context
    tmp, repos = env
    _code_commit(tmp, repos, {".deploy/dev-images/dev-s/Dockerfile.dev": "FROM a\n"})
    config_init("ProjX", ref="ctx", repos_dir=str(repos))   # 初始設定 repo 已含舊 context
    return tmp, repos, str(tmp / ".config_repos" / "ProjX.git"), sync_image_context


def test_sync_preview_changes_nothing_then_yes_commits_only_context(synced):
    tmp, repos, cfg, sync = synced
    _code_commit(tmp, repos, {".deploy/dev-images/dev-s/Dockerfile.dev": "FROM a\nRUN echo new\n", ".deploy/dev-images/dev-s/extra.txt": "x"})
    before = subprocess.run(["git", "--git-dir", cfg, "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    ok, out = sync("ProjX", "dev-s", "ctx", repos_dir=str(repos), audit_root=str(tmp / "audit"))
    assert ok and any("RUN echo new" in l for l in out) and any(l.strip().startswith("A\t") and "extra.txt" in l for l in out)
    assert subprocess.run(["git", "--git-dir", cfg, "rev-parse", "HEAD"], capture_output=True, text=True).stdout == before
    ok, out = sync("ProjX", "dev-s", "ctx", yes=True, reason="加 podman-remote", repos_dir=str(repos), audit_root=str(tmp / "audit"))
    assert ok and any("已提交" in l for l in out)
    assert sorted(_cfg_files(cfg)) == sorted([".deploy/containers/dev-s.json", ".deploy/dev-images/dev-s/Dockerfile.dev", ".deploy/dev-images/dev-s/extra.txt", ".deploy/manifest.json"])
    assert "RUN echo new" in subprocess.run(["git", "--git-dir", cfg, "show", "HEAD:.deploy/dev-images/dev-s/Dockerfile.dev"], capture_output=True, text=True).stdout
    msg = subprocess.run(["git", "--git-dir", cfg, "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
    assert "sync-image-context dev-s" in msg and "加 podman-remote" in msg
    rec = json.loads((tmp / "audit" / "ProjX" / "config-audit.jsonl").read_text().splitlines()[-1])
    assert rec["action"] == "sync-image-context" and rec["reason"] == "加 podman-remote"
    # 宣告檔（image pin 等）不被動到
    changed = subprocess.run(["git", "--git-dir", cfg, "diff", "--name-only", "HEAD~1", "HEAD"], capture_output=True, text=True).stdout.split()
    assert all(p.startswith(".deploy/dev-images/dev-s/") for p in changed)


def test_sync_mirrors_deletions_and_is_idempotent(synced):
    tmp, repos, cfg, sync = synced
    _code_commit(tmp, repos, {".deploy/dev-images/dev-s/old.txt": "o"})
    sync("ProjX", "dev-s", "ctx", yes=True, repos_dir=str(repos), audit_root=str(tmp / "a"))
    _code_commit(tmp, repos, {}, remove=[".deploy/dev-images/dev-s/old.txt"])
    ok, out = sync("ProjX", "dev-s", "ctx", yes=True, repos_dir=str(repos), audit_root=str(tmp / "a"))
    assert ".deploy/dev-images/dev-s/old.txt" not in _cfg_files(cfg)
    ok, out = sync("ProjX", "dev-s", "ctx", yes=True, repos_dir=str(repos), audit_root=str(tmp / "a"))
    assert any("無變更" in l for l in out)


@pytest.mark.parametrize("kind", ["symlink", "src_dir", "no_dockerfile", "too_big"])
def test_sync_rejects_unsafe_context(synced, kind):
    tmp, repos, cfg, sync = synced
    base = ".deploy/dev-images/dev-s"
    if kind == "symlink":
        _code_commit(tmp, repos, {}, symlinks={f"{base}/evil": "/etc/passwd"})
    elif kind == "src_dir":
        _code_commit(tmp, repos, {f"{base}/src/app.py": "code"})
    elif kind == "no_dockerfile":
        _code_commit(tmp, repos, {f"{base}/x": "1"}, remove=[f"{base}/Dockerfile.dev"])
    else:
        _code_commit(tmp, repos, {f"{base}/big": "x" * (1024 * 1024 + 1)})
    head = subprocess.run(["git", "--git-dir", cfg, "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    with pytest.raises(ImagePrepareError):
        sync("ProjX", "dev-s", "ctx", yes=True, repos_dir=str(repos), audit_root=str(tmp / "a"))
    assert subprocess.run(["git", "--git-dir", cfg, "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head


def test_sync_guards_service_ref_and_config_repo(env, synced):
    tmp, repos, cfg, sync = synced
    with pytest.raises(ImagePrepareError):                       # 設定 repo 沒宣告的服務
        sync("ProjX", "dev-none", "ctx", repos_dir=str(repos))
    with pytest.raises(ImagePrepareError):                       # ref 不存在
        sync("ProjX", "dev-s", "no-such-ref", repos_dir=str(repos))
    with pytest.raises(ImagePrepareError):                       # 來源 ref 沒有該 context
        sync("ProjX", "dev-s", "master", repos_dir=str(repos))
    with pytest.raises(ImagePrepareError):
        sync("../x", "dev-s", "ctx", repos_dir=str(repos))


def test_sync_requires_config_repo(env):
    from container_ctl.cli.config import sync_image_context
    tmp, repos = env
    with pytest.raises(ImagePrepareError, match="設定 repo"):
        sync_image_context("ProjX", "dev-s", "HEAD", repos_dir=str(repos))


def test_sync_refuses_non_dev_service(synced, tmp_path):
    tmp, repos, cfg, sync = synced
    work = tmp / "w"
    (work / ".deploy/containers/prod-s.json").write_text(json.dumps({"name": "prod-s", "tier": "prod"}))
    # 直接在設定 repo 加一個 prod 服務宣告
    co = tmp / "co"; subprocess.run(["git", "clone", "-q", cfg, str(co)], check=True, capture_output=True)
    (co / ".deploy/containers/prod-s.json").write_text(json.dumps({"name": "prod-s", "tier": "prod"}))
    for c in (["add", "."], ["-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "p"], ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", "-C", str(co), *c], check=True, capture_output=True)
    _code_commit(tmp, repos, {".deploy/dev-images/prod-s/Dockerfile.dev": "FROM a\n"})
    with pytest.raises(ImagePrepareError, match="不是 dev"):
        sync("ProjX", "prod-s", "ctx", repos_dir=str(repos))


def test_parser_requires_from_ref():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["config", "sync-image-context", "ProjX", "dev-s"])
    a = p.parse_args(["config", "sync-image-context", "ProjX", "dev-s", "--from-ref", "main"])
    assert a.from_ref == "main" and a.yes is False


# ── config set --volume／--unset-volume ──
DEV_DECL = {"name": "dev-s", "tier": "dev", "image": "localhost/x@sha256:" + "a" * 64,
            "volumes": [{"host_path": "/srv/site/projects/old", "container_path": "/workspace", "options": "rw", "x": 1},
                        {"host_path": "/srv/site/projects/d", "container_path": "/data", "options": "ro"}]}


@pytest.fixture
def vol_repo(tmp_path):
    work = tmp_path / "w"
    (work / ".deploy/containers").mkdir(parents=True)
    (work / ".deploy/manifest.json").write_text(json.dumps({"project_name": "ProjV", "version": "1", "dependencies": ["dev-s"]}))
    (work / ".deploy/containers/dev-s.json").write_text(json.dumps(DEV_DECL))
    g = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True, text=True)
    g("init", "-b", "main"); g("config", "user.email", "t@x.invalid"); g("config", "user.name", "T"); g("add", "."); g("commit", "-m", "b")
    bare = tmp_path / ".config_repos" / "ProjV.git"; bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    kw = dict(repos_dir=str(tmp_path / ".git_repos"), config_dir=str(tmp_path / ".config_repos"), audit_root=str(tmp_path / "audit"))
    show = lambda p="dev-s": json.loads(subprocess.run(["git", "--git-dir", str(bare), "show", f"HEAD:.deploy/containers/{p}.json"], capture_output=True, text=True).stdout)
    return tmp_path, bare, kw, show


def test_volume_replaces_host_path_keeping_other_fields_and_only_that_entry(vol_repo):
    from container_ctl.cli.config import config_set
    tmp, bare, kw, show = vol_repo
    ok, out = config_set("ProjV", "dev-s", {}, [], vol_sets={"/workspace": ("/srv/site/projects/new", None)}, **kw)   # 預覽
    assert any("volume:/workspace" in l and "new" in l for l in out) and show() == DEV_DECL
    config_set("ProjV", "dev-s", {}, [], vol_sets={"/workspace": ("/srv/site/projects/new", None)}, yes=True, reason="修路徑", **kw)
    vols = show()["volumes"]
    assert vols[0] == {"host_path": "/srv/site/projects/new", "container_path": "/workspace", "options": "rw", "x": 1}
    assert vols[1] == DEV_DECL["volumes"][1]
    assert {k: v for k, v in show().items() if k != "volumes"} == {k: v for k, v in DEV_DECL.items() if k != "volumes"}
    rec = json.loads((tmp / "audit/ProjV/config-audit.jsonl").read_text().splitlines()[-1])
    assert rec["changes"][0]["key"] == "volume:/workspace" and "old" in rec["changes"][0]["before"]


def test_volume_options_add_and_unset(vol_repo):
    from container_ctl.cli.config import config_set
    tmp, bare, kw, show = vol_repo
    config_set("ProjV", "dev-s", {}, [], vol_sets={"/data": ("/srv/site/projects/d", "rw"), "/extra": ("/srv/site/projects/e", None)}, yes=True, **kw)
    vols = {v["container_path"]: v for v in show()["volumes"]}
    assert vols["/data"]["options"] == "rw" and vols["/extra"]["options"] == "rw" and len(vols) == 3
    config_set("ProjV", "dev-s", {}, [], vol_unsets=["/extra"], yes=True, **kw)
    assert [v["container_path"] for v in show()["volumes"]] == ["/workspace", "/data"]
    with pytest.raises(ImagePrepareError, match="無法移除"):
        config_set("ProjV", "dev-s", {}, [], vol_unsets=["/nope"], **kw)
    with pytest.raises(ImagePrepareError, match="同時"):
        config_set("ProjV", "dev-s", {}, [], vol_sets={"/data": ("/srv/site/projects/d", None)}, vol_unsets=["/data"], **kw)


def test_volume_noop_is_reported_and_commits_nothing(vol_repo):
    from container_ctl.cli.config import config_set
    tmp, bare, kw, show = vol_repo
    head = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    ok, out = config_set("ProjV", "dev-s", {}, [], vol_sets={"/workspace": ("/srv/site/projects/old", None)}, yes=True, **kw)
    assert any("無變更" in l for l in out)
    assert subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head


def test_volume_with_env_writes_whole_list_into_overlay_only(vol_repo):
    from container_ctl.cli.config import config_set
    tmp, bare, kw, show = vol_repo
    config_set("ProjV", "dev-s", {}, [], vol_sets={"/workspace": ("/srv/site/projects/vps", None)}, env_name="vps", yes=True, **kw)
    ov = json.loads(subprocess.run(["git", "--git-dir", str(bare), "show", "HEAD:.deploy/envs/vps/containers/dev-s.json"], capture_output=True, text=True).stdout)
    assert [v["container_path"] for v in ov["volumes"]] == ["/workspace", "/data"] and ov["volumes"][0]["host_path"].endswith("/vps")
    assert show() == DEV_DECL   # base 不動


@pytest.mark.parametrize("item", ["workspace=/a", "/w=rel/path", "/w=/a/../b", "/w=/a b", "/w=/a//b", "/w", "/w=/a:rw,evil!", "/w=/a:", "=/a", "/w=", "/w:x=/a"])
def test_parse_volume_rejects_bad_input(item):
    from container_ctl.cli.config import _parse_volume
    if item == "/w=/a:":
        assert _parse_volume(item) == ("/w", "/a", None)   # 空 options 視為未指定
        return
    with pytest.raises(ImagePrepareError):
        _parse_volume(item)


def test_parse_volume_ok_and_cli_parser():
    from container_ctl.cli.config import _parse_volume
    assert _parse_volume("/workspace=/srv/x:rw") == ("/workspace", "/srv/x", "rw")
    assert _parse_volume("/w=/srv/x:ro,Z") == ("/w", "/srv/x", "ro,Z")
    a = build_parser().parse_args(["config", "set", "P", "s", "--volume", "/w=/a", "--unset-volume", "/d"])
    assert a.volume == ["/w=/a"] and a.unset_volume == ["/d"]
