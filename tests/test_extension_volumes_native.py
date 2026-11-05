"""resolve_extension_volumes now asks the native `extscope plan`; fake the subprocess, check mapping, host rejection and loudness."""
import json
import subprocess

import pytest

from container_ctl import paths
from container_ctl import quadlet_gen as q


def _setup(tmp_path, monkeypatch, plan=None, rc=0, stderr="", origin_doc=None):
    svc = tmp_path / "services" / "ProjX"
    svc.mkdir(parents=True)
    (svc / "ext-default.json").write_text(json.dumps({"uuid": "u-1"}))
    scopes = tmp_path / "scopes"
    (scopes / "biz").mkdir(parents=True)
    origin = scopes / "biz" / "b.extscope.json"
    origin.write_text(json.dumps(origin_doc or {"uuid": "b"}))
    monkeypatch.setenv("CONTAINER_CTL_SCOPES_DIR", str(scopes))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = json.dumps({"items": [{"origin": str(origin), "item": it} for it in (plan or [])]})
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=stderr)
    monkeypatch.setattr(q.subprocess, "run", fake_run)
    return str(tmp_path / "services"), seen


def test_mounts_are_mapped_and_non_mounts_ignored(tmp_path, monkeypatch):
    services, seen = _setup(tmp_path, monkeypatch, plan=[
        {"type": "mount", "src": paths.DATA_ROOT + "/x", "dst_fhs": "/opt/ext/fhs/x", "mode": "ro"},
        {"type": "copy_inject", "src": paths.DATA_ROOT + "/y", "dst_fhs": "/opt/ext/fhs/y", "mode": "ro"}])
    vols = q.resolve_extension_volumes("ProjX", services, read_only=True)
    assert vols == [{"host_path": paths.DATA_ROOT + "/x", "container_path": "/opt/ext/fhs/x", "options": "ro", "adaptation_target": "container"}]
    assert seen["cmd"][:2] == ["extscope", "plan"] and "PROJECT_NAME=ProjX" in seen["cmd"] and seen["cmd"][-2:] == ["--entry", "u-1"]


def test_source_outside_the_allow_list_is_refused(tmp_path, monkeypatch):
    services, _ = _setup(tmp_path, monkeypatch, plan=[{"type": "mount", "src": "/etc/shadow", "dst_fhs": "/x", "mode": "ro"}])
    with pytest.raises(PermissionError):
        q.resolve_extension_volumes("ProjX", services, read_only=True)


def test_host_adaptation_scope_is_rejected(tmp_path, monkeypatch):
    services, _ = _setup(tmp_path, monkeypatch, origin_doc={"uuid": "b", "adaptation_target": "host"},
                         plan=[{"type": "mount", "src": paths.DATA_ROOT + "/a", "dst_fhs": "/x", "mode": "ro"}])
    with pytest.raises(q.HostAdaptationBindingError):
        q.resolve_extension_volumes("ProjX", services, read_only=True)


def test_extscope_failure_is_loud_not_silent(tmp_path, monkeypatch, capsys):
    services, _ = _setup(tmp_path, monkeypatch, rc=2, stderr='{"error":"operation_failed","detail":"boom"}')
    assert q.resolve_extension_volumes("ProjX", services, read_only=True) == []
    assert "WARNING extension volumes for 'ProjX' NOT resolved" in capsys.readouterr().err


def test_symlinked_ext_default_is_passed_as_its_real_file(tmp_path, monkeypatch):
    """Old layout: .services/<P>/ext-default.json is a link into .deploys; extscope refuses link scope files."""
    services, seen = _setup(tmp_path, monkeypatch, plan=[])
    link = tmp_path / "services" / "ProjX" / "ext-default.json"
    real = tmp_path / "deploys_ext-default.json"
    real.write_text(link.read_text())
    link.unlink()
    link.symlink_to(real)
    q.resolve_extension_volumes("ProjX", services, read_only=True)
    scopes = [seen["cmd"][i + 1] for i, a in enumerate(seen["cmd"]) if a == "--scope"]
    assert str(real.resolve()) in scopes and str(link) not in scopes
