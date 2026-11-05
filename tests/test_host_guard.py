import json
import os
import socket
import threading

import pytest

from container_ctl.host_guard import ask_agent_principal, inside_container, refuse_in_container


@pytest.fixture
def marker(tmp_path):
    m = tmp_path / ".containerenv"; m.write_text("")
    return str(m)


def serve(path, reply: bytes | None, hang=False):
    srv = socket.socket(socket.AF_UNIX); srv.bind(path); srv.listen(1)
    seen = []
    def run():
        c, _ = srv.accept()
        seen.append(c.recv(4096))
        if hang:
            threading.Event().wait(5)
        elif reply is not None:
            c.sendall(reply)
        c.close(); srv.close()
    threading.Thread(target=run, daemon=True).start()
    return seen


def ok(who): return json.dumps({"ok": True, "rc": 0, "stdout": who + "\n", "stderr": "", "error": None}).encode() + b"\n"


def test_outside_container_never_consults_agent():
    called = []
    refuse_in_container(markers=("/nonexistent",), environ={}, ask=lambda p: called.append(p))
    assert called == []


def test_dev_container_without_agent_socket_is_refused(marker):
    with pytest.raises(SystemExit, match="REFUSE"):
        refuse_in_container(markers=(marker,), environ={})


def test_self_declared_flags_do_not_bypass(marker):
    for env in ({"CONTAINER_CTL_CONTROL_PLANE": "1"}, {"CONTAINER_CTL_ALLOW": "1"}, {"CONTAINER_CTL_HOST_AGENT": ""}):
        with pytest.raises(SystemExit):
            refuse_in_container(markers=(marker,), environ=env)


def test_control_plane_principal_is_allowed(marker, tmp_path):
    sock = str(tmp_path / "a.sock"); seen = serve(sock, ok("control-plane"))
    refuse_in_container(markers=(marker,), environ={"CONTAINER_CTL_HOST_AGENT": sock})
    assert json.loads(seen[0]) == {"verb": "auth.whoami", "params": {}}


@pytest.mark.parametrize("reply", [ok("host"), ok(""), ok("control-plane-x"), ok("Control-Plane"),
                                   json.dumps({"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "unauthorized"}).encode() + b"\n",
                                   json.dumps({"ok": True, "stdout": "control-plane\n", "error": "x"}).encode() + b"\n",
                                   json.dumps({"ok": 1, "stdout": "control-plane\n"}).encode() + b"\n",
                                   json.dumps({"ok": True, "stdout": ["control-plane"]}).encode() + b"\n",
                                   b"not json\n", b"[]\n", b"", b"\xff\xfe\n", b"x" * 10000])
def test_any_other_reply_is_refused(marker, tmp_path, reply):
    sock = str(tmp_path / "a.sock"); serve(sock, reply)
    with pytest.raises(SystemExit, match="REFUSE"):
        refuse_in_container(markers=(marker,), environ={"CONTAINER_CTL_HOST_AGENT": sock})


def test_hanging_agent_times_out_and_refuses(marker, tmp_path):
    sock = str(tmp_path / "a.sock"); serve(sock, None, hang=True)
    assert ask_agent_principal(sock, timeout=0.3) is None
    with pytest.raises(SystemExit):
        refuse_in_container(markers=(marker,), environ={"CONTAINER_CTL_HOST_AGENT": sock}, ask=lambda p: ask_agent_principal(p, timeout=0.3))


@pytest.mark.parametrize("make", ["missing", "regular_file", "relative", "dir"])
def test_bad_socket_path_is_refused(marker, tmp_path, make):
    path = str(tmp_path / "x")
    if make == "regular_file":
        open(path, "w").write("x")
    elif make == "relative":
        path = "a.sock"
    elif make == "dir":
        os.mkdir(path)
    with pytest.raises(SystemExit):
        refuse_in_container(markers=(marker,), environ={"CONTAINER_CTL_HOST_AGENT": path})


def test_ask_failure_modes_return_none(tmp_path):
    assert ask_agent_principal(str(tmp_path / "none.sock")) is None
    sock = str(tmp_path / "s.sock"); srv = socket.socket(socket.AF_UNIX); srv.bind(sock)   # 有檔但沒 listen → 連線被拒
    assert ask_agent_principal(sock) is None
    srv.close()


def test_inside_container_detection_unchanged(tmp_path):
    assert not inside_container(markers=(str(tmp_path / "none"),), environ={})
    assert inside_container(markers=(), environ={"container": "podman"})
