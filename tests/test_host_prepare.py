from pathlib import Path
from unittest.mock import MagicMock

from container_ctl.cli.host_prepare import apply_actions, plan_host_prepare


def _cfg(root: Path):
    return {
        "name": "prod-x", "tier": "prod", "user": "1000:1000",
        "volumes": [
            {"host_path": str(root / "etc"), "container_path": "/etc/opt/x", "options": "ro"},
            {"host_path": str(root / "var"), "container_path": "/var/opt/x", "options": "rw"},
            {"host_path": str(root / "sec"), "container_path": "/etc/opt/x/secrets", "options": "ro"},
        ],
    }


def test_plan_creates_missing_dirs_and_nested_mountpoint(tmp_path):
    root = tmp_path / "proj"
    actions, errors = plan_host_prepare([_cfg(root)], allowed_roots=(tmp_path,), forbidden=())
    assert errors == []
    paths = [(a["op"], a["path"]) for a in actions]
    assert ("mkdir", str(root)) in paths and ("mkdir", str(root / "var")) in paths
    # 巢狀掛載點 etc/secrets 預建在父層 etc 內
    assert ("mkdir", str(root / "etc" / "secrets")) in paths
    # mkdir 先於 chown
    assert paths.index(("mkdir", str(root / "var"))) < paths.index(("chown", str(root / "var")))


def test_existing_dirs_are_not_touched(tmp_path):
    root = tmp_path / "proj"
    for d in ("etc", "var", "sec", "etc/secrets"):
        (root / d).mkdir(parents=True)
    actions, errors = plan_host_prepare([_cfg(root)], allowed_roots=(tmp_path,), forbidden=())
    assert actions == [] and errors == []


def test_refuses_outside_allowed_root_and_forbidden(tmp_path):
    cfg = _cfg(tmp_path / "proj")
    actions, errors = plan_host_prepare([cfg], allowed_roots=(tmp_path / "other",), forbidden=())
    assert actions == [] and len(errors) == 3
    actions, errors = plan_host_prepare([cfg], allowed_roots=(tmp_path,), forbidden=(tmp_path / "proj",))
    assert actions == [] and errors


def test_existing_file_as_volume_is_error(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "var").write_text("x")  # rw 掛載點是檔案 → 錯誤（單檔只允許 ro）
    _, errors = plan_host_prepare([_cfg(root)], allowed_roots=(tmp_path,), forbidden=())
    assert any("不是目錄" in e for e in errors)


def test_apply_runs_mkdir_then_chown(tmp_path):
    root = tmp_path / "proj"
    actions, _ = plan_host_prepare([_cfg(root)], allowed_roots=(tmp_path,), forbidden=())
    run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    log = apply_actions(actions, run=run)
    assert (root / "var").is_dir() and (root / "etc" / "secrets").is_dir()
    assert all(l.startswith("[DONE]") for l in log)
    assert run.call_args_list[0].args[0][:4] == ["podman", "--remote=false", "unshare", "chown"]


def test_mkdir_falls_back_to_unshare_when_parent_is_mapped(tmp_path):
    from unittest.mock import patch
    calls = []
    run = lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0)
    with patch("pathlib.Path.mkdir", side_effect=PermissionError):
        log = apply_actions([{"op": "mkdir", "path": str(tmp_path / "x"), "service": "s"}], run=run)
    assert calls[0][:5] == ["podman", "--remote=false", "unshare", "mkdir", "-p"] and log == [f"[DONE] mkdir {tmp_path / 'x'}"]
