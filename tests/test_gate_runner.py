import subprocess

from container_ctl.gate_runner import gate_findings

import json as _json

REPORT = _json.dumps({"ok": False, "violations": [
    {"file": ".deploy/containers/prod-a.json", "line": 7, "rule": "sensitive_assignment", "message": "x"},
    {"file": ".deploy/containers/dev-b.json", "line": 3, "rule": "url_credential", "message": "y"}], "waived": []})
OK = _json.dumps({"ok": True, "violations": [], "waived": []})


def _runner(code, out=""):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
    return run


def test_pass_returns_empty():
    assert gate_findings([{"name": "a", "tier": "prod"}], runner=_runner(0, OK), gate_bin="g") == []


def test_prod_error_dev_warn():
    cfgs = [{"name": "prod-a", "tier": "prod"}, {"name": "dev-b", "tier": "dev"}]
    msgs = gate_findings(cfgs, runner=_runner(1, REPORT), gate_bin="g")
    assert msgs[0].startswith("[ERROR] prod-a") and msgs[1].startswith("[WARN] dev-b")


def test_unparsable_failure_is_error():
    msgs = gate_findings([{"name": "a"}], runner=_runner(1, "boom"), gate_bin="g")
    assert msgs[0].startswith("[ERROR]")


def test_missing_tool_warns(monkeypatch):
    monkeypatch.setattr("container_ctl.gate_runner.shutil.which", lambda _n: None)
    assert gate_findings([{"name": "a"}])[0].startswith("[WARN]")
