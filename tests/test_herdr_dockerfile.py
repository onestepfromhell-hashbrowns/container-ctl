"""Static preflight for Dockerfile.herdr (no podman build, no container start)."""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

DF = Path(__file__).resolve().parent.parent / "dockerfiles" / "Dockerfile.herdr"
PKG = Path(os.environ.get("HERDR_PACKAGE_DIR", "/nonexistent/herdr"))  # site integration check: herdr package dir


def _lines():
    return [l.strip() for l in DF.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]


def test_minimal_nonroot_single_binary_image():
    ls = _lines()
    assert ls[0] == "FROM scratch"
    assert any(l.startswith("USER 1000") for l in ls)
    assert any("COPY" in l and "bin/herdr" in l for l in ls)
    assert not any(re.match(r"(RUN|ADD|VOLUME|EXPOSE)\b", l) for l in ls), "no build-time network/exec, no VOLUME"


def test_socket_env_matches_extscope_runtime_scope():
    text = DF.read_text()
    cand = os.environ.get("HERDR_RUNTIME_SCOPE")      # site integration check: path to biz-herdr-runtime.extscope.json
    if not cand or not Path(cand).is_file():
        pytest.skip("set HERDR_RUNTIME_SCOPE to a biz-herdr-runtime.extscope.json to run this site check")
    scope = json.loads(Path(cand).read_text())
    envs = {s["config"]["key"]: s["config"]["value"] for it in scope["include_items"] for s in it["pipeline"] if s["module"] == "env_inject"}
    for k, v in envs.items():
        assert f"{k}={v}" in text


def test_package_binary_matches_pinned_digest_if_present():
    if not PKG.exists():
        return
    ver = (PKG / "VERSION").read_text()
    want = re.search(r"sha256: ([0-9a-f]{64})", ver).group(1)
    assert hashlib.sha256((PKG / "bin" / "herdr").read_bytes()).hexdigest() == want


def test_config_disables_phone_home_and_dockerfile_points_at_it():
    import tomllib
    cfg = tomllib.loads((DF.parent / "herdr" / "config.toml").read_text())
    assert cfg["update"] == {"version_check": False, "manifest_check": False}
    assert "HERDR_CONFIG_PATH=/etc/herdr/config.toml" in DF.read_text()


def test_health_probe_is_a_real_herdr_subcommand():
    # `herdr health` does not exist (probe-verified on 0.9.3); `herdr api snapshot` exits 1 when no server runs.
    c = (Path(__file__).resolve().parent / "test_herdr_quadlet_contract.py").read_text()
    assert "herdr health" not in c and "herdr api snapshot" in c
