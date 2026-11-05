"""session_sample: watch-only egress sampling of a live session."""
import json
import os

from container_ctl import exec_run as x

PROC_TEXT = (
    "  sl  local_address rem_address   st\n"
    "   0: 0100007F:0035 0100007F:1F90 0A 00000000:00000000\n"  # loopback, LISTEN: never a remote
    "   1: 0100000A:A1B2 0100000A:01BB 01 00000000:00000000\n"  # established to a non-loopback peer
)


class SampleRunner:
    def __init__(self, rc=0, text=PROC_TEXT):
        self.rc, self.text, self.calls = rc, text, []

    def __call__(self, argv, stdin, timeout):
        self.calls.append(argv)
        return self.rc, self.text.encode(), b""


def _open_state(state_root, exec_id="sess-exec-01", **extra):
    d = os.path.join(state_root, exec_id)
    os.makedirs(d, exist_ok=True)
    state = {"container_name": f"ctr-{exec_id}", "run_id": "run-1", "egress": "default", **extra}
    with open(os.path.join(d, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)
    return d


def _log_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_sample_appends_one_line_with_same_format_as_close(tmp_path):
    state_root = str(tmp_path / "sessions")
    log = str(tmp_path / "egress.jsonl")
    _open_state(state_root)
    runner = SampleRunner()

    res = x.session_sample("sess-exec-01", runner=runner, state_root=state_root, log_path=log)

    assert res["ok"] is True
    lines = _log_lines(log)
    assert len(lines) == 1
    assert lines[0]["exec_id"] == "sess-exec-01"
    assert lines[0]["run_id"] == "run-1"
    assert lines[0]["profile"] == "default"
    assert lines[0]["remotes"] == sorted(x.parse_remotes(PROC_TEXT))
    assert res["remotes"] == len(lines[0]["remotes"])


def test_sample_reads_the_session_container_only(tmp_path):
    state_root = str(tmp_path / "sessions")
    log = str(tmp_path / "egress.jsonl")
    _open_state(state_root)
    runner = SampleRunner()

    x.session_sample("sess-exec-01", runner=runner, state_root=state_root, log_path=log)

    assert runner.calls, "sampler must run a command in the container"
    assert all(argv[:2] == ["exec", "ctr-sess-exec-01"] for argv in runner.calls)
    assert all(argv[0] != "rm" for argv in runner.calls), "watch only: never remove or modify"


def test_sample_appends_across_calls(tmp_path):
    state_root = str(tmp_path / "sessions")
    log = str(tmp_path / "egress.jsonl")
    _open_state(state_root)

    x.session_sample("sess-exec-01", runner=SampleRunner(), state_root=state_root, log_path=log)
    x.session_sample("sess-exec-01", runner=SampleRunner(), state_root=state_root, log_path=log)

    assert len(_log_lines(log)) == 2


def test_sample_without_log_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTAINER_CTL_EXEC_EGRESS_LOG", raising=False)
    state_root = str(tmp_path / "sessions")
    _open_state(state_root)

    res = x.session_sample("sess-exec-01", runner=SampleRunner(), state_root=state_root)

    assert res == {"ok": False, "exec_id": "sess-exec-01", "error_code": "EGRESS_LOG_UNSET"}


def test_sample_unknown_session_is_refused(tmp_path):
    log = str(tmp_path / "egress.jsonl")

    res = x.session_sample("no-such", runner=SampleRunner(), state_root=str(tmp_path), log_path=log)

    assert res["ok"] is False and res["error_code"] == "SESSION_NOT_FOUND"
    assert not os.path.exists(log)


def test_sample_failed_exec_still_records_an_empty_line_without_raising(tmp_path):
    state_root = str(tmp_path / "sessions")
    log = str(tmp_path / "egress.jsonl")
    _open_state(state_root)

    res = x.session_sample("sess-exec-01", runner=SampleRunner(rc=1, text=""), state_root=state_root, log_path=log)

    assert res["ok"] is True and res["remotes"] == 0
    assert _log_lines(log)[0]["remotes"] == []
