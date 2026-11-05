import io
import json

from container_ctl import exec_run as x


def test_cli_roundtrip_reports_invalid_spec_as_a_result_not_a_crash():
    out = io.StringIO()
    assert x.main(stdin=io.StringIO('{"v":2}'), stdout=out) == 0
    assert json.loads(out.getvalue())["error_code"] == "SPEC_INVALID"
    out = io.StringIO()
    assert x.main(stdin=io.StringIO("not json"), stdout=out) == 0
    assert json.loads(out.getvalue())["state"] == "failed"


def test_main_passes_scratch_root_from_env(monkeypatch, tmp_path):
    import io, json
    import container_ctl.exec_run as er
    seen = {}
    monkeypatch.setattr(er, "run_exec", lambda spec, **kw: seen.update(kw) or {"v": 1})
    d = tmp_path / "scr"
    monkeypatch.setenv("CONTAINER_CTL_EXEC_SCRATCH", str(d))
    er.main(stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seen["scratch_root"] == str(d) and d.is_dir() and oct(d.stat().st_mode & 0o777) == "0o700"
    monkeypatch.delenv("CONTAINER_CTL_EXEC_SCRATCH")
    er.main(stdin=io.StringIO("{}"), stdout=io.StringIO())
    assert seen["scratch_root"] is None
