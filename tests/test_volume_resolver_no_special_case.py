"""Podman socket 不再有特例放行：回到一般白名單規則。"""
import os
from pathlib import Path

from container_ctl import volume_resolver
from container_ctl.volume_resolver import resolve_volume_mount

SOCK = f"/run/user/{os.getuid()}/podman/podman.sock"


def test_no_temp_approval_table():
    assert not hasattr(volume_resolver, "TEMP_APPROVED_FILE_MOUNTS")


def test_podman_socket_rejected_by_whitelist(tmp_path):
    vol = {"host_path": SOCK, "container_path": "/run/podman/podman.sock", "options": "ro"}
    res, err = resolve_volume_mount(vol, "P", "svc", allowed_read_roots=(tmp_path.resolve(),))
    assert res is None
    assert "白名單" in err


def test_socket_like_file_under_whitelist_follows_general_rules(tmp_path):
    # 白名單內同名檔案：只能走一般單檔規則（ro 才放行；rw 被拒）
    sock = tmp_path / "podman.sock"
    sock.write_text("")
    roots = (tmp_path.resolve(),)
    ok, err = resolve_volume_mount({"host_path": str(sock), "container_path": "/s", "options": "ro"}, "P", "svc", allowed_read_roots=roots)
    assert err is None and ok.host_path == Path(sock).resolve()
    bad, err = resolve_volume_mount({"host_path": str(sock), "container_path": "/s", "options": "rw"}, "P", "svc", allowed_read_roots=roots)
    assert bad is None and err
