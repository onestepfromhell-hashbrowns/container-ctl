import os
import subprocess
import sys

from container_ctl import secret_provision as sp


def _cfg(tmp_path, **over):
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    cfg = {"name": "svc", "user": "1000:1000",
           "env": {"DB_PASSWORD_FILE": "/etc/opt/P/secrets/db"},
           "volumes": [{"host_path": str(d), "container_path": "/etc/opt/P/secrets", "options": "ro"}],
           "secrets": [{"env": "DB_PASSWORD_FILE", "kind": "password", "bytes": 32}]}
    cfg.update(over)
    return cfg, d


def test_plan_missing_then_exists(tmp_path):
    cfg, d = _cfg(tmp_path)
    actions, msgs = sp.plan_secret_provision([cfg])
    assert actions[0]["state"] == "missing" and actions[0]["path"] == str(d / "db") and not msgs
    (d / "db").write_text("x")
    assert sp.plan_secret_provision([cfg])[0][0]["state"] == "exists"


def test_cert_kind_is_never_generated(tmp_path):
    cfg, _ = _cfg(tmp_path, secrets=[{"env": "DB_PASSWORD_FILE", "kind": "cert"}])
    actions, msgs = sp.plan_secret_provision([cfg])
    assert actions[0]["state"] == "manual" and msgs[0].startswith("[ERROR]")


def test_invalid_declarations(tmp_path):
    cfg, _ = _cfg(tmp_path, secrets=[{"env": "PLAIN", "kind": "password"}])
    assert sp.plan_secret_provision([cfg])[1][0].startswith("[ERROR]")
    cfg, _ = _cfg(tmp_path, secrets=[{"env": "DB_PASSWORD_FILE", "kind": "password", "bytes": 4}])
    assert "bytes" in sp.plan_secret_provision([cfg])[1][0]
    cfg, _ = _cfg(tmp_path, env={"DB_PASSWORD_FILE": "/elsewhere/db"})
    assert "volumes" in sp.plan_secret_provision([cfg])[1][0]


def _fake_unshare(cmd, **kw):
    """以一般行程執行 _INNER（略過 podman unshare 與 chown 權限），驗證寫入語意。"""
    inner = cmd[cmd.index("-c") + 1]
    inner = inner.replace("os.chown(path, uid, -1)", "pass")
    return subprocess.run([sys.executable, "-c", inner, *cmd[cmd.index("-c") + 2:]], capture_output=True, text=True)


def test_apply_creates_0400_no_overwrite_no_leak(tmp_path):
    cfg, d = _cfg(tmp_path)
    actions, _ = sp.plan_secret_provision([cfg])
    calls = []

    def run(cmd, **kw):
        if "test" in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        calls.append(cmd)
        return _fake_unshare(cmd, **kw)

    lines = sp.apply_secret_provision(actions, run=run)
    f = d / "db"
    secret = f.read_text().strip()
    assert len(secret) >= 32 and oct(f.stat().st_mode & 0o777) == "0o400"
    assert all(secret not in l for l in lines) and all(secret not in " ".join(c) for c in calls)
    assert not [p for p in d.iterdir() if ".tmp-" in p.name]
    # 再跑：不覆寫
    actions2, _ = sp.plan_secret_provision([cfg])
    assert actions2[0]["state"] == "exists"
    forced = [dict(actions[0], state="missing")]
    sp.apply_secret_provision(forced, run=run)
    assert f.read_text().strip() == secret


def test_missing_parent_dir_error(tmp_path):
    a = [{"service": "s", "env": "E", "kind": "password", "path": str(tmp_path / "no" / "f"), "uid": 1, "nbytes": 32, "state": "missing"}]
    out = sp.apply_secret_provision(a, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert out[0].startswith("[ERROR]") and "host prepare" in out[0]
