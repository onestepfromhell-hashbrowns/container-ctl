"""agent_exec：SystemdManager 在控制面容器內改經 host-agent 執行；預設路徑（無 CONTAINER_CTL_HOST_AGENT）零行為變更。"""
import json
import socket
import subprocess
import threading

import pytest

from container_ctl import agent_exec
from container_ctl import lifecycle_adapter as la


class FakeAgent:
    """單執行緒 UDS 假 agent：記錄每個請求並回預設回覆。"""

    def __init__(self, path, reply=None, hang=False):
        self.path, self.seen, self.hang = path, [], hang
        self.replies = {}  # verb → 該動詞的專屬回覆（優先於 self.reply）
        self.reply = reply or {"ok": True, "rc": 0, "stdout": "", "stderr": "", "error": None}
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(path)
        self.srv.listen(8)
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            data = b""
            while b"\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.split(b"\n", 1)[0]) if data else None
            if req:
                self.seen.append(req)
            if self.hang:
                threading.Event().wait(3)
            else:
                c.sendall(json.dumps(self.replies.get(req["verb"], self.reply) if req else self.reply).encode() + b"\n")
            c.close()

    def close(self):
        self.srv.close()


@pytest.fixture
def agent(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "agent.sock"))
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    yield a
    a.close()


S = ["systemctl", "--user"]


@pytest.mark.parametrize("argv,verb,params", [
    (S + ["start", "--no-block", "a.service"], "systemd.start", {"unit": "a.service"}),
    (S + ["restart", "--no-block", "a.service"], "systemd.restart", {"unit": "a.service"}),
    (S + ["stop", "a.service"], "systemd.stop", {"unit": "a.service"}),
    (S + ["is-active", "a.service"], "systemd.is_active", {"unit": "a.service"}),
    (S + ["is-active", "--quiet", "a.service"], "systemd.is_active", {"unit": "a.service"}),
    (S + ["daemon-reload"], "systemd.daemon_reload", {}),
    (S + ["list-units", "svc-*.service", "--no-legend", "--plain"], "systemd.list_units", {"pattern": "svc-*.service"}),
    (S + ["kill", "--signal=SIGKILL", "a.service"], "systemd.kill", {"unit": "a.service", "signal": "SIGKILL"}),
    (S + ["reset-failed", "a.service"], "systemd.reset_failed", {"unit": "a.service"}),
])
def test_translation(agent, argv, verb, params):
    cp = agent_exec.run_via_agent(argv)
    assert cp.returncode == 0
    assert agent.seen == [{"verb": verb, "params": params}]


@pytest.mark.parametrize("argv", [
    S + ["start", "a.service"],                       # 阻塞式 start：agent 一律 --no-block，語意不符
    S + ["mask", "a.service"],
    ["systemd-run", "--user", "--unit=x", "sleep", "1"],
    ["systemctl", "start", "--no-block", "a.service"],  # 缺 --user
    S + ["restart", "--no-block", "a.service", "b.service"],
    [],
])
def test_unsupported_argv_never_reaches_agent(agent, argv):
    with pytest.raises(agent_exec.AgentError):
        agent_exec.run_via_agent(argv)
    assert agent.seen == []


def test_nonzero_rc_is_returned_not_raised(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {"ok": False, "rc": 3, "stdout": "inactive\n", "stderr": "", "error": None})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    cp = agent_exec.run_via_agent(S + ["is-active", "a.service"])
    assert (cp.returncode, cp.stdout) == (3, "inactive\n")
    a.close()


@pytest.mark.parametrize("reply", [
    {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "unauthorized"},
    {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "不得操作 control-plane.service"},
])
def test_agent_error_reply_raises(tmp_path, monkeypatch, reply):
    a = FakeAgent(str(tmp_path / "s"), reply)
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    with pytest.raises(agent_exec.AgentError, match=reply["error"]):
        agent_exec.run_via_agent(S + ["stop", "a.service"])
    a.close()


def test_no_socket_raises_instead_of_falling_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", str(tmp_path / "missing.sock"))
    with pytest.raises(agent_exec.AgentError):
        agent_exec.run_via_agent(S + ["daemon-reload"])


def test_relative_path_rejected(monkeypatch):
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", "agent.sock")
    with pytest.raises(agent_exec.AgentError):
        agent_exec.run_via_agent(S + ["daemon-reload"])


def test_timeout_maps_to_timeout_expired(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), hang=True)
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    with pytest.raises(subprocess.TimeoutExpired):
        agent_exec.run_via_agent(S + ["is-active", "a.service"], timeout=0.3)
    a.close()


def test_agent_socket_reads_env(monkeypatch):
    monkeypatch.delenv("CONTAINER_CTL_HOST_AGENT", raising=False)
    assert agent_exec.agent_socket() is None
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", "")
    assert agent_exec.agent_socket() is None
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", "/x/y.sock")
    assert agent_exec.agent_socket() == "/x/y.sock"


# --- SystemdManager 整合 ---

def test_default_path_still_uses_subprocess_run(monkeypatch):
    """零行為變更：沒有 CONTAINER_CTL_HOST_AGENT 時，所有呼叫照舊走 subprocess.run（既有測試的 patch 點）。"""
    monkeypatch.delenv("CONTAINER_CTL_HOST_AGENT", raising=False)
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(la.subprocess, "run", fake)
    m = la.SystemdManager()
    assert m.start_unit("a.service").job_submitted
    assert m.restart_unit("a.service").job_submitted
    assert m.is_active("a.service") and m.stop_unit("a.service")
    assert m.daemon_reload() and m.reset_failed("a.service") and m.kill_unit("a.service")
    assert m.unit_state("a.service") == ""
    assert calls[0] == ["systemctl", "--user", "start", "--no-block", "a.service"]
    assert len(calls) == 8


def test_manager_routes_through_agent_and_never_spawns(agent, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("agent 模式不得本機 spawn")
    monkeypatch.setattr(la.subprocess, "run", boom)
    m = la.SystemdManager()
    r = m.start_unit("svc-x.service")
    assert r.job_submitted and r.returncode == 0
    assert m.restart_unit("svc-x.service").job_submitted
    assert m.is_active("svc-x.service") and m.stop_unit("svc-x.service")
    assert m.daemon_reload() and m.reset_failed("svc-x.service") and m.kill_unit("svc-x.service")
    assert m.stop_unit_blocking("svc-x.service").returncode == 0
    assert m.list_units("svc-*.service") == ""
    assert [s["verb"] for s in agent.seen] == [
        "systemd.start", "systemd.restart", "systemd.is_active", "systemd.stop", "systemd.daemon_reload",
        "systemd.reset_failed", "systemd.kill", "systemd.stop", "systemd.list_units"]


def test_manager_start_reports_agent_denial_as_error_result(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "unauthorized"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    r = la.SystemdManager().start_unit("svc-x.service")
    assert not r.job_submitted and "unauthorized" in (r.error or "")
    a.close()


def test_run_transient_unsupported_in_agent_mode(agent):
    with pytest.raises(agent_exec.AgentError):
        la.SystemdManager().run_transient("u", "d", [], ["sleep", "1"])
    assert agent.seen == []


# --- unshare.*：db_provision／secret_provision 在 agent 模式改走型別化動詞 ---
from types import SimpleNamespace  # noqa: E402

from container_ctl import db_provision, secret_provision  # noqa: E402


def test_unshare_helpers_send_typed_params(agent):
    agent_exec.unshare_install_file("/s/a", "/d/a", "1000")
    agent_exec.unshare_write_secret("/d/p", 1001, 32)
    assert agent.seen == [
        {"verb": "unshare.install_file", "params": {"src": "/s/a", "dst": "/d/a", "uid": "1000"}},
        {"verb": "unshare.write_secret", "params": {"path": "/d/p", "uid": 1001, "nbytes": 32}}]


def _boom(*a, **k):
    raise AssertionError("agent 模式不得本機 podman unshare")


def test_install_staged_agent_mode_ok_and_exists(tmp_path, monkeypatch):
    staged = tmp_path / "0-pw"; staged.write_text("x")
    a = FakeAgent(str(tmp_path / "s"))
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    seq = [{"ok": True, "rc": 0, "stdout": "", "stderr": "", "error": None},
           {"ok": False, "rc": 3, "stdout": "", "stderr": "", "error": None}]
    a.reply = seq[0]
    out1 = db_provision.install_staged([(str(staged), "/srv/real/pw")], "1000:1000", run=subprocess.run)
    assert out1 and out1[0].startswith("[OK]")
    a.reply = seq[1]
    out2 = db_provision.install_staged([(str(staged), "/srv/real/pw")], "1000:1000", run=subprocess.run)
    assert "3＝目標已存在" in out2[0] and out2[0].startswith("[ERROR]")
    assert [s["verb"] for s in a.seen] == ["unshare.install_file"] * 2
    assert a.seen[0]["params"] == {"src": str(staged), "dst": "/srv/real/pw", "uid": "1000"}
    a.close()


def test_install_staged_agent_denial_is_error_line(tmp_path, monkeypatch):
    staged = tmp_path / "0-pw"; staged.write_text("x")
    a = FakeAgent(str(tmp_path / "s"), {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "unauthorized"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    out = db_provision.install_staged([(str(staged), "/srv/real/pw")], "1000", run=subprocess.run)
    assert out[0].startswith("[ERROR]") and "unauthorized" in out[0]
    a.close()


def test_install_staged_injected_run_wins_over_agent(tmp_path, monkeypatch, agent):
    staged = tmp_path / "0-pw"; staged.write_text("x")
    calls = []
    def fake(cmd, **kw):
        calls.append(cmd); return SimpleNamespace(returncode=0, stdout="", stderr="")
    out = db_provision.install_staged([(str(staged), "/srv/real/pw")], "1000", run=fake)
    assert calls and out[0].startswith("[OK]") and agent.seen == []


def test_apply_secret_provision_agent_mode(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {"ok": True, "rc": 0, "stdout": "CREATED\n", "stderr": "", "error": None})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    monkeypatch.setattr(secret_provision.subprocess, "run", _boom)
    act = {"service": "svc", "env": "PW_FILE", "kind": "random", "path": "/nonexistent-parent/pw", "uid": 1000, "nbytes": 32, "state": "missing"}
    out = secret_provision.apply_secret_provision([act], run=subprocess.run)
    assert out[0].startswith("[OK]") and "已產生" in out[0]
    assert a.seen == [{"verb": "unshare.write_secret", "params": {"path": "/nonexistent-parent/pw", "uid": 1000, "nbytes": 32}}]
    a.reply = {"ok": True, "rc": 0, "stdout": "EXISTS\n", "stderr": "", "error": None}
    assert "已存在" in secret_provision.apply_secret_provision([act], run=subprocess.run)[0]
    a.reply = {"ok": False, "rc": None, "stdout": "", "stderr": "", "error": "unauthorized"}
    out = secret_provision.apply_secret_provision([act], run=subprocess.run)
    assert out[0].startswith("[ERROR]") and "unauthorized" in out[0]
    a.close()


# --- portfwd：控制面容器內的探測／開關改走 agent ---
from container_ctl import portfwd  # noqa: E402

OKR = {"ok": True, "rc": 0, "stdout": "", "stderr": "", "error": None}


def test_port_open_agent_mode_maps_rc(agent):
    agent.replies["net.port_open"] = {**OKR, "ok": False, "rc": 1}
    assert portfwd._port_open("127.0.0.1", 8081) is False
    agent.replies["net.port_open"] = OKR
    assert portfwd._port_open("127.0.0.1", 8081) is True
    assert agent.seen[-1] == {"verb": "net.port_open", "params": {"host": "127.0.0.1", "port": 8081}}


def test_port_open_default_mode_probes_locally(monkeypatch):
    monkeypatch.delenv("CONTAINER_CTL_HOST_AGENT", raising=False)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    port = s.getsockname()[1]
    assert portfwd._port_open("127.0.0.1", port) is True
    s.close()
    # WSL2 的 loopback 在 close 後第一次 connect 可能仍成功；輪詢到拒絕為止
    assert any(portfwd._port_open("127.0.0.1", port) is False for _ in range(5))


def test_detect_bind_ip_agent_mode(agent):
    agent.replies["net.addr_show"] = {**OKR, "stdout": "2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global eth0\n"}
    assert portfwd.detect_bind_ip("eth0") == "192.168.1.50"
    assert agent.seen[-1] == {"verb": "net.addr_show", "params": {"iface": "eth0"}}


def _open_args(tmp_path, **over):
    a = dict(to="127.0.0.1:8081", listen=9443, reason="mobile test", bind="10.0.0.67", db_path=str(tmp_path / "s.db"))
    a.update(over)
    return a


def test_open_forward_agent_mode_uses_typed_verb(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(la.subprocess, "run", _boom_run)
    agent.replies["net.port_open"] = OKR
    # target 要有 listener、bind:listen 要沒被佔：依 host 回不同答案
    answers = {("127.0.0.1", 8081): True, ("10.0.0.67", 9443): False}
    monkeypatch.setattr(portfwd, "_port_open", lambda h, p: answers[(h, p)])
    d = portfwd.open_forward(**_open_args(tmp_path, allow="192.168.1.0/24", ttl="30m"), port_open=portfwd._port_open)
    assert d["unit"] == "portfwd-9443" and d["ttl_sec"] == 1800
    opens = [s for s in agent.seen if s["verb"] == "portfwd.open"]
    assert len(opens) == 1
    p = opens[0]["params"]
    assert (p["listen"], p["target_port"], p["bind"], p["ttl_sec"], p["allow"]) == (9443, 8081, "10.0.0.67", 1800, "192.168.1.0/24")
    assert isinstance(p["instance"], str) and p["instance"]


def _boom_run(*a, **k):
    raise AssertionError("agent 模式不得本機 spawn")


def test_open_forward_agent_denial_becomes_portfwd_error(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {**OKR, "ok": False, "rc": None, "error": "portfwd 未設定（缺 ctl_path）"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    answers = {("127.0.0.1", 8081): True, ("10.0.0.67", 9443): False}
    with pytest.raises(portfwd.PortfwdError, match="未設定"):
        portfwd.open_forward(**_open_args(tmp_path), port_open=lambda h, p: answers[(h, p)])
    a.close()


def test_close_and_list_route_through_agent(agent, monkeypatch):
    monkeypatch.setattr(la.subprocess, "run", _boom_run)
    agent.replies["systemd.list_units"] = {**OKR, "stdout": "portfwd-9443.service loaded active running x\n"}
    assert portfwd.close_forward(listen=9443) == 9443
    assert "portfwd-9443" in portfwd.list_forwards()
    assert [s["verb"] for s in agent.seen] == ["systemd.stop", "systemd.list_units"]
    assert agent.seen[0]["params"] == {"unit": "portfwd-9443.service"}


# --- artifact_store：控制面模式改走 cp_core.artifact_api ---
import sys  # noqa: E402
import types  # noqa: E402

from container_ctl import artifact_store as ast_  # noqa: E402


def _fake_cp_core(monkeypatch, calls):
    api = types.ModuleType("cp_core.artifact_api")
    api.put = lambda *a, **k: calls.append(("put", a, k)) or "sha256:" + "a" * 64
    api.load = lambda *a, **k: calls.append(("load", a, k)) or "sha256:" + "b" * 64
    pkg = types.ModuleType("cp_core"); pkg.artifact_api = api
    monkeypatch.setitem(sys.modules, "cp_core", pkg)
    monkeypatch.setitem(sys.modules, "cp_core.artifact_api", api)


def test_artifact_store_agent_mode_delegates(agent, monkeypatch):
    calls = []
    _fake_cp_core(monkeypatch, calls)
    img = "sha256:" + "1" * 64
    assert ast_.put("P", "s", "1.0", img, root="/r") == "sha256:" + "a" * 64
    assert ast_.load("P", "s", "1.0", root="/r") == "sha256:" + "b" * 64
    assert calls == [("put", ("P", "s", "1.0", img), {"root": "/r"}), ("load", ("P", "s", "1.0"), {"root": "/r"})]


def test_artifact_store_agent_mode_missing_cp_core_errors(agent, monkeypatch):
    monkeypatch.setitem(sys.modules, "cp_core", None)
    with pytest.raises(ast_.ArtifactStoreError, match="cp_core"):
        ast_.put("P", "s", "1.0", "sha256:" + "1" * 64)


def test_artifact_store_default_mode_never_imports_cp_core(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTAINER_CTL_HOST_AGENT", raising=False)
    monkeypatch.setitem(sys.modules, "cp_core", None)  # 若被匯入會 ImportError
    with pytest.raises(ast_.ArtifactStoreError, match="image_id"):
        ast_.put("P", "s", "1.0", "bad", root=str(tmp_path))


# --- prod_doctor._stat_path：PermissionError 時改走 unshare.stat ---
from pathlib import Path as _P  # noqa: E402
from container_ctl.cli import prod_doctor as pd  # noqa: E402


def _deny(monkeypatch):
    def boom(self, *a, **k):
        raise PermissionError
    monkeypatch.setattr(_P, "stat", boom)


def test_stat_path_agent_mode(agent, monkeypatch):
    _deny(monkeypatch)
    monkeypatch.setattr(la.subprocess, "run", _boom_run)
    agent.replies["unshare.stat"] = {**OKR, "stdout": "70 700\n"}
    assert pd._stat_path(_P("/srv/site/x"), None, 70) == (True, True, 0o700)
    assert agent.seen[-1] == {"verb": "unshare.stat", "params": {"path": "/srv/site/x"}}
    agent.replies["unshare.stat"] = {**OKR, "ok": False, "rc": 1}
    assert pd._stat_path(_P("/srv/site/x"), None, 70) == (False, None, None)


def test_stat_path_agent_denied_is_unknown(tmp_path, monkeypatch):
    _deny(monkeypatch)
    a = FakeAgent(str(tmp_path / "s"), {**OKR, "ok": False, "rc": None, "error": "unauthorized"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    assert pd._stat_path(_P("/srv/site/x"), None, 70) == (False, None, None)
    a.close()


# --- host_prepare.apply_actions：agent 模式 mkdir／chown 走型別化動詞 ---
from container_ctl.cli import host_prepare as hp  # noqa: E402


def test_host_prepare_agent_mode(agent, monkeypatch, tmp_path):
    monkeypatch.setattr(la.subprocess, "run", _boom_run)
    d = str(tmp_path / "never-created-locally")
    log = hp.apply_actions([{"op": "mkdir", "path": d, "service": "s"}, {"op": "chown", "path": d, "user": "70:70", "service": "s"}],
                           run=hp.subprocess.run)
    assert log == [f"[DONE] mkdir {d}", f"[DONE] chown 70:70 {d}"]
    assert [x["verb"] for x in agent.seen] == ["unshare.mkdir", "unshare.chown"]
    assert agent.seen[1]["params"] == {"path": d, "user": "70:70"}
    assert not (tmp_path / "never-created-locally").exists()


def test_host_prepare_agent_denial_logged(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {**OKR, "ok": False, "rc": None, "error": "路徑不在允許的根目錄內"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    log = hp.apply_actions([{"op": "mkdir", "path": "/x", "service": "s"}])
    assert log[0].startswith("[ERROR] mkdir /x 失敗") and "允許的根目錄" in log[0]
    a.close()


# --- public_files.publish：agent 模式整件交給 unshare.publish_ca ---
from container_ctl import public_files as pf  # noqa: E402


def test_publish_agent_mode_maps_results(agent, monkeypatch):
    monkeypatch.setattr(la.subprocess, "run", _boom_run)
    agent.replies["unshare.publish_ca"] = {**OKR, "stdout": "PUBLISHED\n"}
    assert pf.publish("/srv/site/a.crt", "/srv/site/projects/.containers/storage/volumes/x/ca.crt", run=pf.subprocess.run).startswith("[OK]   已發布")
    assert agent.seen[-1] == {"verb": "unshare.publish_ca",
                              "params": {"src": "/srv/site/a.crt", "dst": "/srv/site/projects/.containers/storage/volumes/x/ca.crt"}}
    agent.replies["unshare.publish_ca"] = {**OKR, "stdout": "UNCHANGED\n"}
    assert "已是最新" in pf.publish("/srv/site/a", "/srv/site/b", run=pf.subprocess.run)
    agent.replies["unshare.publish_ca"] = {**OKR, "ok": False, "rc": 4, "stderr": "拒絕發布：含 PRIVATE KEY 區塊\n"}
    out = pf.publish("/srv/site/a", "/srv/site/b", run=pf.subprocess.run)
    assert out.startswith("[ERROR]") and "PRIVATE KEY" in out


def test_publish_agent_denial_is_error_line(tmp_path, monkeypatch):
    a = FakeAgent(str(tmp_path / "s"), {**OKR, "ok": False, "rc": None, "error": "src 不在允許的根目錄內"})
    monkeypatch.setenv("CONTAINER_CTL_HOST_AGENT", a.path)
    assert pf.publish("/etc/x", "/srv/site/b").startswith("[ERROR]")
    a.close()
