import subprocess

from container_ctl.dockerfile_provenance import doctor_findings, from_findings, last_change

D = "sha256:" + "a" * 64


def test_pinned_ok_and_stage_alias_skipped():
    t = f"FROM python:3.11@{D} AS b\nFROM b AS r\nFROM scratch\n"
    assert from_findings(t) == []


def test_unpinned_is_error_with_fix():
    out = from_findings("FROM python:3.11-slim\n")
    assert out[0].startswith("[ERROR]") and "@sha256" in out[0] and ":1 " in out[0]


def test_variable_is_warn_and_platform_flag():
    assert from_findings("ARG B\nFROM ${B}\n")[0].startswith("[WARN]")
    assert from_findings("FROM --platform=linux/amd64 x:1\n")[0].startswith("[ERROR]")


def _repo(tmp_path, body):
    r = tmp_path / "r.git"
    w = tmp_path / "w"
    subprocess.run(["git", "init", "-q", "-b", "main", str(w)], check=True)
    (w / "Dockerfile.prod").write_text(body)
    g = ["git", "-C", str(w), "-c", "user.name=Codex", "-c", "user.email=codex@localhost"]
    subprocess.run([*g, "add", "."], check=True)
    subprocess.run([*g, "commit", "-qm", "x"], check=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(w), str(r)], check=True)
    return str(r)


def test_last_change_records_author(tmp_path):
    repo = _repo(tmp_path, "FROM scratch\n")
    info = last_change(repo, "HEAD")
    assert info["author"] == "Codex <codex@localhost>" and len(info["commit"]) == 40
    assert last_change(repo, "HEAD", "nope") is None


def test_doctor_findings_reads_repo_and_tolerates_missing(tmp_path):
    assert doctor_findings(_repo(tmp_path, "FROM python:3\n"))[0].startswith("[ERROR]")
    assert doctor_findings(str(tmp_path / "missing.git")) == []


def test_inject_wheelhouse(tmp_path):
    import hashlib
    from container_ctl.offline_deps import inject_wheelhouse
    import pytest
    src = tmp_path / "wh"
    src.mkdir()
    (src / "a.whl").write_bytes(b"x")
    (src / "SHA256SUMS").write_text(f"{hashlib.sha256(b'x').hexdigest()}  a.whl\n")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    assert inject_wheelhouse(ctx, "FROM x\n", src) is None
    info = inject_wheelhouse(ctx, "COPY wheelhouse/ /w\n", src)
    assert (ctx / "wheelhouse" / "a.whl").exists() and len(info["sha256sums"]) == 64
    assert inject_wheelhouse(ctx, "COPY wheelhouse/ /w\n", src) is None  # 已存在
    ctx2 = tmp_path / "ctx2"
    ctx2.mkdir()
    (src / "a.whl").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="驗證失敗"):
        inject_wheelhouse(ctx2, "COPY wheelhouse/ /w\n", src)


def test_build_flags_squash_and_offline(monkeypatch):
    import re
    squash = lambda t: bool(re.search(r"^#\s*container-ctl:\s*squash-all\s*$", t, re.M))
    assert squash("# container-ctl: squash-all\nFROM x@sha256:" + "a" * 64) and not squash("FROM x # container-ctl: squash-all")
