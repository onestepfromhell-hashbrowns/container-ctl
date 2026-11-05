import subprocess
from pathlib import Path

from container_ctl import public_files as pf
from container_ctl.cli.host_prepare import apply_actions, plan_host_prepare

CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"


def test_validate_ca_pem():
    assert pf.validate_ca_pem(CERT) is None
    assert pf.validate_ca_pem(CERT + CERT) is None
    assert "PRIVATE KEY" in pf.validate_ca_pem(CERT + KEY)
    assert "PRIVATE KEY" in pf.validate_ca_pem("-----BEGIN RSA PRIVATE KEY-----\nx\n")
    assert pf.validate_ca_pem("hello") == "不是 PEM 憑證"
    assert "非 CERTIFICATE" in pf.validate_ca_pem("-----BEGIN PUBLIC KEY-----\nx\n")
    assert "64KB" in pf.validate_ca_pem(CERT + "a" * 70000)


def test_check_declaration(tmp_path):
    roots, forb = [tmp_path], [tmp_path / "protected"]
    ok = {"src": "/s/ca.crt", "dest": str(tmp_path / "c" / "ca.crt"), "kind": "ca-cert"}
    assert pf.check_declaration("s", ok, roots, forb) == []
    assert pf.check_declaration("s", dict(ok, kind="key"), roots, forb)
    assert pf.check_declaration("s", dict(ok, src="rel"), roots, forb)
    assert pf.check_declaration("s", dict(ok, dest=str(tmp_path / "c" / "server.key")), roots, forb)
    assert pf.check_declaration("s", dict(ok, dest="/etc/x/ca.crt"), roots, forb)
    assert pf.check_declaration("s", dict(ok, dest=str(tmp_path / "protected" / "ca.crt")), roots, forb)


def _fake_unshare(src_text):
    def run(cmd, input=None, **kw):
        args = cmd[3:]
        if args[0] == "cat":
            p = Path(args[1])
            if args[1].endswith("src.crt"):
                return subprocess.CompletedProcess(cmd, 0, stdout=src_text.encode(), stderr=b"")
            return subprocess.CompletedProcess(cmd, 0 if p.exists() else 1, stdout=p.read_bytes() if p.exists() else b"", stderr=b"")
        r = subprocess.run(["sh", "-c", args[2], *args[3:]], input=input, capture_output=True)
        return r
    return run


def test_publish_writes_0644_and_is_idempotent(tmp_path):
    dest = tmp_path / "certs" / "ca.crt"
    dest.parent.mkdir()
    run = _fake_unshare(CERT)
    assert "已發布" in pf.publish(str(tmp_path / "src.crt"), str(dest), run=run)
    assert dest.read_text() == CERT and oct(dest.stat().st_mode & 0o777) == "0o644"
    assert "未改動" in pf.publish(str(tmp_path / "src.crt"), str(dest), run=run)
    assert not [p for p in dest.parent.iterdir() if ".tmp." in p.name]


def test_publish_refuses_private_key_and_writes_nothing(tmp_path):
    dest = tmp_path / "certs" / "ca.crt"
    dest.parent.mkdir()
    msg = pf.publish(str(tmp_path / "src.crt"), str(dest), run=_fake_unshare(CERT + KEY))
    assert msg.startswith("[ERROR]") and "PRIVATE KEY" in msg and not dest.exists()


def test_plan_and_apply_via_host_prepare(tmp_path):
    cfg = {"name": "d", "tier": "dev", "volumes": [],
           "public_files": [{"src": "/s/src.crt", "dest": str(tmp_path / "a" / "b" / "ca.crt"), "kind": "ca-cert"}]}
    actions, errs = plan_host_prepare([cfg], allowed_roots=[tmp_path], forbidden=[])
    assert not errs
    assert [a["op"] for a in actions] == ["mkdir", "mkdir", "publish"]
    log = apply_actions(actions, run=_fake_unshare(CERT))
    assert (tmp_path / "a" / "b" / "ca.crt").read_text() == CERT and "已發布" in log[-1]


def test_plan_rejects_bad_dest(tmp_path):
    cfg = {"name": "d", "volumes": [], "public_files": [{"src": "/s/x", "dest": "/etc/ca.crt", "kind": "ca-cert"}]}
    _, errs = plan_host_prepare([cfg], allowed_roots=[tmp_path], forbidden=[])
    assert errs


def test_single_file_ro_mount_is_not_a_host_prepare_error(tmp_path):
    f = tmp_path / "ca.crt"
    f.write_text("x")
    cfg = {"name": "d", "volumes": [{"host_path": str(f), "container_path": "/etc/ca.crt", "options": "ro"}]}
    actions, errs = plan_host_prepare([cfg], allowed_roots=[tmp_path], forbidden=[])
    assert not errs and not actions
