import json
from types import SimpleNamespace

import pytest

from container_ctl import portfwd
from container_ctl.cli.main import build_parser
from container_ctl.db import init_db


def ok_run(calls):
    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def open_args(tmp_path, **over):
    a = dict(to="127.0.0.1:8081", listen=9443, reason="mobile test", bind="10.0.0.67",
             db_path=str(tmp_path / "s.db"))
    a.update(over)
    return a


def port_open_only_target(host, port):
    return (host, port) == ("127.0.0.1", 8081)


def test_rejects_prod_target(tmp_path):
    with pytest.raises(portfwd.PortfwdError, match="127.0.0.2"):
        portfwd.open_forward(**open_args(tmp_path, to="127.0.0.2:8081"))


@pytest.mark.parametrize("port", [80, 8081, 9500, 8443])
def test_rejects_listen_port_out_of_range(tmp_path, port):
    with pytest.raises(portfwd.PortfwdError, match="9000-9499"):
        portfwd.open_forward(**open_args(tmp_path, listen=port))


@pytest.mark.parametrize("bind", ["0.0.0.0", "127.0.0.1"])
def test_rejects_wildcard_or_loopback_bind(tmp_path, bind):
    with pytest.raises(portfwd.PortfwdError, match="禁止"):
        portfwd.open_forward(**open_args(tmp_path, bind=bind))


def test_requires_reason_and_valid_ttl(tmp_path):
    with pytest.raises(portfwd.PortfwdError, match="reason"):
        portfwd.open_forward(**open_args(tmp_path, reason=" "))
    with pytest.raises(portfwd.PortfwdError, match="TTL"):
        portfwd.open_forward(**open_args(tmp_path, ttl="2d"))
    with pytest.raises(portfwd.PortfwdError, match="24h"):
        portfwd.open_forward(**open_args(tmp_path, ttl="25h"))


def test_rejects_when_target_not_listening_or_listen_busy(tmp_path):
    with pytest.raises(portfwd.PortfwdError, match="沒有 listener"):
        portfwd.open_forward(**open_args(tmp_path), run=ok_run([]), port_open=lambda h, p: False)
    with pytest.raises(portfwd.PortfwdError, match="已被佔用"):
        portfwd.open_forward(**open_args(tmp_path), run=ok_run([]), port_open=lambda h, p: True)


def test_open_builds_systemd_run_and_records_event(tmp_path):
    calls = []
    d = portfwd.open_forward(**open_args(tmp_path, allow="192.168.1.0/24", ttl="30m"),
                             run=ok_run(calls), port_open=port_open_only_target)
    cmd = calls[0]
    assert cmd[:3] == ["systemd-run", "--user", "--unit=portfwd-9443"]
    assert "RuntimeMaxSec=1800" in cmd
    assert "TCP-LISTEN:9443,bind=10.0.0.67,fork,reuseaddr,range=192.168.1.0/24" in cmd
    assert cmd[-1] == "TCP:127.0.0.1:8081"
    assert not any("0.0.0.0" in c for c in cmd)
    conn = init_db(str(tmp_path / "s.db"))
    row = conn.execute("SELECT event_type, detail FROM lifecycle_events").fetchone()
    assert row["event_type"] == "portfwd_open"
    assert json.loads(row["detail"])["reason"] == "mobile test"
    assert d["ttl_sec"] == 1800


def test_stopped_hook_distinguishes_expire_from_close(tmp_path):
    db = str(tmp_path / "s.db")
    portfwd.record_stopped(9443, "host", "127.0.0.1:8081", "10.0.0.67", "timeout", db_path=db)
    portfwd.record_stopped(9443, "host", "127.0.0.1:8081", "10.0.0.67", "success", db_path=db)
    types = [r["event_type"] for r in init_db(db).execute("SELECT event_type FROM lifecycle_events ORDER BY id")]
    assert types == ["portfwd_expire", "portfwd_close"]


def test_cli_parser_wired():
    a = build_parser().parse_args(["portfwd", "open", "--to", "127.0.0.1:8081", "--listen", "9443", "--reason", "x"])
    assert (a.command, a.subcommand, a.ttl) == ("portfwd", "open", "2h")


def test_lan_only_shapes_windows_firewall_hint_not_socat(tmp_path):
    calls = []
    d = portfwd.open_forward(**open_args(tmp_path, lan="192.168.1.0/24"), run=ok_run(calls), port_open=port_open_only_target)
    assert not any("range=" in c for c in calls[0])
    assert d["lan"] == "192.168.1.0/24"
    assert "remoteip=192.168.1.0/24" in portfwd.windows_hint("10.0.0.67", 9443, d["lan"])
    assert "remoteip" not in portfwd.windows_hint("10.0.0.67", 9443)


def test_resolve_declaration_from_db_blueprint(tmp_path):
    db_file = str(tmp_path / "s.db")
    conn = init_db(db_file)
    bp = {
        "ports": ["127.0.0.1:8081:8081"],
        "portfwd": {"listen": 9443}
    }
    conn.execute(
        "INSERT INTO instances (id, project_name, service_name, version, tier, scope, status, blueprint, quadlet_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
        ("dev-app", "app-proj", "dev-app", "0.1.0", "dev", "dedicated", "running", json.dumps(bp), "/quadlet/path")
    )
    conn.commit()
    conn.close()

    to, listen, inst_id = portfwd.resolve_declaration("dev-app", db_path=db_file)
    assert (to, listen, inst_id) == ("127.0.0.1:8081", 9443, "dev-app")

    calls = []
    d = portfwd.open_forward(instance="dev-app", reason="auto test", bind="10.0.0.67", db_path=db_file, run=ok_run(calls), port_open=port_open_only_target)
    assert d["listen"] == 9443
    assert d["target"] == "127.0.0.1:8081"


def test_cli_parser_supports_positional_instance_and_ttl():
    p = build_parser()
    a = p.parse_args(["portfwd", "open", "dev-app", "--reason", "test", "--ttl", "8h"])
    assert a.instance == "dev-app"
    assert a.reason == "test"
    assert a.ttl == "8h"
    assert a.to is None
    assert a.listen is None



def _svc(root, proj, name, port):
    d = root / proj
    d.mkdir(parents=True, exist_ok=True)
    (d / f"svc-{name}.container").write_text(
        f"[Container]\nContainerName=svc-{name}\nLabel=container-ctl.portfwd.listen={port}\n")


def test_scan_and_next_port(tmp_path):
    _svc(tmp_path, "A", "dev-a", 9443)
    _svc(tmp_path, "B", "dev-b", 9444)
    assert portfwd.scan_declared_ports(tmp_path) == {9443: ["svc-dev-a"], 9444: ["svc-dev-b"]}
    nxt = portfwd.get_next_available_port(services_dir=tmp_path, port_open=lambda h, p: False)
    assert nxt == 9000
    nxt = portfwd.get_next_available_port((9443, 9499), tmp_path, port_open=lambda h, p: False)
    assert nxt == 9445


def test_conflict_detection(tmp_path):
    _svc(tmp_path, "A", "dev-a", 9443)
    _svc(tmp_path, "B", "dev-b", 9443)
    assert portfwd.find_listen_conflicts(tmp_path) == {9443: ["svc-dev-a", "svc-dev-b"]}
