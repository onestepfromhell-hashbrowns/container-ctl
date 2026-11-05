"""tests/test_exec_spec_v2_session.py — Tests for ExecSpec v2 and session mode.

Verifies ADR-028 ExecSpec v2:
- T-C-2: Dual-version validation (v1 and v2, mode=oneshot and mode=session, v1 behavior unchanged)
- T-CC-1: session_open idempotency by exec_id (no duplicate container)
- T-CC-2: session_close cleanup (runtime_dirs removed, container force-removed, container-ctl.session label cleared)
- T-CC-3: argv secret string and regex rejection in session mode
- T-S-5: session container timeout (--timeout timeout_s in podman args) and labels
- T-H-6: Herdr binary SHA256 verification
- session_status & session_mask (including refreshed token regex scrubbing)
- CLI subcommand dispatch
"""
import hashlib
import io
import json
import os
import pathlib
import pytest
import time

from container_ctl import exec_run as x


class FakeRunner:
    def __init__(self, exists=False, running=True, run_rc=0, exec_rc=0, out=b"hello", rss="32.0MB / 900MB"):
        self.calls = []
        self.exists = exists
        self.running = running
        self.run_rc = run_rc
        self.exec_rc = exec_rc
        self.out = out
        self.rss = rss

    def __call__(self, argv, stdin, timeout):
        self.calls.append((list(argv), stdin))
        if argv[:2] == ["container", "exists"]:
            return (0 if self.exists else 1), b"", b""
        if argv[:2] == ["container", "inspect"]:
            if "--format" in argv:
                fmt = argv[argv.index("--format") + 1]
                if "{{.State.Running}}" in fmt:
                    return 0, b"true\n" if self.running else b"false\n", b""
                if "{{.State.StartedAt}}" in fmt:
                    return 0, b"2026-11-03T18:00:00Z\n", b""
            return 0, b"{}", b""
        if argv[:2] == ["container", "stats"]:
            return 0, self.rss.encode(), b""
        if argv[0] == "run":
            return self.run_rc, b"cid-session-12345", (b"boom" if self.run_rc else b"")
        if argv[0] == "exec":
            return self.exec_rc, self.out, b"err"
        if argv[0] == "ps":
            return 0, b"cctl-exec-e1\n", b""
        if argv[0] == "rm":
            return 0, b"", b""
        return 0, b"", b""


SESSION_SPEC = {
    "v": 2,
    "mode": "session",
    "exec_id": "sess-exec-01",
    "run_id": "run-01",
    "session_ref": "sess-lane-01",
    "image": "localhost/herdr:1.0",
    "argv": ["/usr/local/bin/herdr", "server"],
    "workspace": {"mode": "scratch", "host_path": ""},
    "runtime_dirs": [
        {"purpose": "control_socket", "dst": "/run/herdr"},
        {"purpose": "transcript", "dst": "/var/log/herdr"},
    ],
    "limits": {
        "memory_max": "1024M",
        "timeout_s": 3600,
        "idle_timeout_s": 300,
    },
    "credential_files": [{"src": "/mock/cred/token.json", "dest": ".config/token.json", "required": True}],
    "env_secrets": [{"src": "/mock/cred/api_key", "env_by_prefix": [["sk-ant-oat-", "CLAUDE_CODE_OAUTH_TOKEN"]], "required": True}],
    "env": {
        "HERDR_CONFIG_PATH": "/etc/herdr/config.toml",
        "HERDR_SOCKET_PATH": "/run/herdr/herdr.sock",
        "HERDR_HOME": "/home/agent/.herdr",
    },
    "egress": "default",
}

MOCK_FILES = {
    "/mock/cred/token.json": b'{"access_token": "ya29.v2-session-oauth-token-val", "refresh_token": "1//04refreshtokenvalue1234"}',
    "/mock/cred/api_key": b"sk-ant-oat-SECRET-KEY-ABC12345\n",
    "/usr/local/bin/herdr": b"BINARY_BYTES_OF_HERDR_V1",
}


# ====================================================================================
# T-C-2: Dual-Version ExecSpec Validation
# ====================================================================================

def test_t_c_2_dual_version_acceptance(tmp_path):
    """T-C-2: container-ctl accepts both v1 and v2, preserving v1 bit-for-bit."""
    state_root = str(tmp_path / "sessions")

    # 1. v1 oneshot spec is accepted
    v1_spec = {
        "v": 1,
        "exec_id": "v1-exec",
        "run_id": "v1-run",
        "image": "localhost/agent:1",
        "argv": ["echo", "v1"],
        "workspace": {"mode": "scratch", "host_path": ""},
        "limits": {"memory_max": "512M", "timeout_s": 30},
    }
    x.validate(v1_spec)
    runner = FakeRunner()
    res1 = x.run_exec(v1_spec, runner=runner)
    assert res1["state"] == "succeeded"
    assert res1["v"] == 1

    # 2. v1 rejects mode=session
    v1_with_session = {**v1_spec, "mode": "session"}
    with pytest.raises(x.ExecSpecInvalid):
        x.validate(v1_with_session)

    # 3. v2 with mode=oneshot is accepted
    v2_oneshot = {**v1_spec, "v": 2, "mode": "oneshot"}
    x.validate(v2_oneshot)
    runner2 = FakeRunner()
    res2 = x.run_exec(v2_oneshot, runner=runner2)
    assert res2["state"] == "succeeded"
    assert res2["v"] == 2

    # 4. v2 with default mode (omitted) defaults to oneshot
    v2_default_mode = {**v1_spec, "v": 2}
    x.validate(v2_default_mode)
    res_def = x.run_exec(v2_default_mode, runner=FakeRunner())
    assert res_def["state"] == "succeeded"
    assert res_def["v"] == 2

    # 5. v2 with mode=session is accepted
    x.validate(SESSION_SPEC, read_file=lambda p: MOCK_FILES[p])
    runner_sess = FakeRunner()
    res_sess = x.session_open(
        SESSION_SPEC,
        runner=runner_sess,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res_sess["ok"] is True
    assert res_sess["exec_id"] == "sess-exec-01"

    # 6. v3 is rejected
    with pytest.raises(x.ExecSpecInvalid):
        x.validate({**v1_spec, "v": 3})

    # 7. Invalid mode is rejected
    with pytest.raises(x.ExecSpecInvalid):
        x.validate({**v1_spec, "v": 2, "mode": "unknown_mode"})


def test_t_c_2_session_ref_validation():
    """T-C-2: session_ref must match ^[a-z0-9][a-z0-9_-]{0,63}$."""
    valid_refs = ["sess-1", "0", "a_b-c", "sess" * 16]
    for r in valid_refs:
        spec = {**SESSION_SPEC, "session_ref": r}
        x.validate(spec, read_file=lambda p: MOCK_FILES[p])

    invalid_refs = ["", "-start-with-hyphen", "_start-underscore", "SESS-UPPER", "has space", "a" * 65]
    for r in invalid_refs:
        spec = {**SESSION_SPEC, "session_ref": r}
        with pytest.raises(x.ExecSpecInvalid):
            x.validate(spec, read_file=lambda p: MOCK_FILES[p])


def test_runtime_dirs_validation():
    """Validate runtime_dirs: purpose, absolute dst, clean path, no host_path."""
    # Invalid purpose
    with pytest.raises(x.ExecSpecInvalid):
        x.validate({**SESSION_SPEC, "runtime_dirs": [{"purpose": "other", "dst": "/tmp/sock"}]})

    # Duplicate purpose
    with pytest.raises(x.ExecSpecInvalid):
        x.validate({
            **SESSION_SPEC,
            "runtime_dirs": [
                {"purpose": "control_socket", "dst": "/tmp/s1"},
                {"purpose": "control_socket", "dst": "/tmp/s2"},
            ]
        })

    # Caller specifying host_path
    with pytest.raises(x.ExecSpecInvalid):
        x.validate({
            **SESSION_SPEC,
            "runtime_dirs": [{"purpose": "control_socket", "dst": "/tmp/s1", "host_path": "/var/tmp"}]
        })

    # Forbidden destination root paths
    for forbidden_dst in ("/", "/proc", "/sys", "/dev", "/etc", "/etc/herdr", "/proc/self"):
        with pytest.raises(x.ExecSpecInvalid):
            x.validate({
                **SESSION_SPEC,
                "runtime_dirs": [{"purpose": "control_socket", "dst": forbidden_dst}]
            })


# ====================================================================================
# T-CC-1: session_open Idempotency
# ====================================================================================

def test_t_cc_1_session_open_idempotent(tmp_path):
    """T-CC-1: Resending session_open with same exec_id returns existing handle without second container."""
    state_root = str(tmp_path / "sessions")
    runner = FakeRunner()

    res1 = x.session_open(
        SESSION_SPEC,
        runner=runner,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res1["ok"] is True
    assert res1["exec_id"] == "sess-exec-01"
    assert res1["container_id"] == "cid-session-12345"
    assert res1["session_ref"] == "sess-lane-01"
    assert "control_socket" in res1["runtime_dirs"]
    assert "transcript" in res1["runtime_dirs"]

    run_calls_1 = [c for c, _ in runner.calls if c[0] == "run"]
    assert len(run_calls_1) == 1

    # Second call with same exec_id (container is alive)
    res2 = x.session_open(
        SESSION_SPEC,
        runner=runner,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res2["ok"] is True
    assert res2["container_id"] == res1["container_id"]
    assert res2["session_ref"] == res1["session_ref"]
    assert res2["runtime_dirs"] == res1["runtime_dirs"]

    # Still only 1 run call made
    run_calls_2 = [c for c, _ in runner.calls if c[0] == "run"]
    assert len(run_calls_2) == 1


# ====================================================================================
# T-CC-2: session_close Cleanup
# ====================================================================================

def test_t_cc_2_session_close_cleanup(tmp_path):
    """T-CC-2: session_close force-removes container, deletes runtime_dirs, cleans container-ctl.session label."""
    state_root = str(tmp_path / "sessions")
    runner = FakeRunner()

    open_res = x.session_open(
        SESSION_SPEC,
        runner=runner,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert open_res["ok"] is True

    # Host runtime dirs exist
    sock_host = open_res["runtime_dirs"]["control_socket"]
    trans_host = open_res["runtime_dirs"]["transcript"]
    assert os.path.isdir(sock_host)
    assert os.path.isdir(trans_host)

    # Close session
    close_res = x.session_close(
        "sess-exec-01",
        runner=runner,
        state_root=state_root,
    )
    assert close_res["ok"] is True
    assert close_res["exec_id"] == "sess-exec-01"
    assert close_res["evidence"]["runtime_dirs_cleaned"] is True
    assert not os.path.exists(sock_host)
    assert not os.path.exists(trans_host)
    assert not os.path.exists(os.path.join(state_root, "sess-exec-01"))

    # Verify podman rm -f -v called for container and label
    rm_calls = [c for c, _ in runner.calls if c[:3] == ["rm", "-f", "-v"]]
    assert any("cctl-exec-sess-exec-01" in c for c in rm_calls)
    assert any("--filter" in c and "label=container-ctl.session=sess-lane-01" in c for c in rm_calls)


def test_t_cc_2_session_close_collect_patch(tmp_path):
    """T-CC-2: session_close extracts git patch when collect_patch=True on rw workspace."""
    state_root = str(tmp_path / "sessions")
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "file.txt").write_text("initial\n")

    spec = {
        **SESSION_SPEC,
        "workspace": {"mode": "rw", "host_path": str(proj_dir)},
    }

    # Open session with mock runner that modifies rw workspace
    class EditRunner(FakeRunner):
        def __call__(self, argv, stdin, timeout):
            if argv[0] == "run":
                for a in argv:
                    if a.endswith(":/workspace:rw"):
                        rw_copy = pathlib.Path(a.split(":")[0])
                        (rw_copy / "file.txt").write_text("updated content\n")
            return super().__call__(argv, stdin, timeout)

    runner = EditRunner()
    x.session_open(spec, runner=runner, state_root=state_root, read_file=lambda p: MOCK_FILES[p])

    close_res = x.session_close("sess-exec-01", collect_patch=True, runner=runner, state_root=state_root)
    assert close_res["ok"] is True
    patch = close_res["patch"]
    assert patch is not None
    assert "--- a/file.txt" in patch and "+updated content" in patch


# ====================================================================================
# T-CC-3: Secret String & Regex Rejection in argv
# ====================================================================================

@pytest.mark.parametrize("secret_arg", [
    "sk-ant-api03-0123456789abcdef",
    "sk-ant-oat01-0123456789abcdef",
    "AIzaSyD-abc123def456ghi789jkl012mno345",
    "sk-ant-oat-SECRET-KEY-ABC12345",  # projected secret value
])
def test_t_cc_3_argv_secret_rejection(secret_arg, tmp_path):
    """T-CC-3: argv containing secret patterns or projected secret values is rejected."""
    bad_spec = {
        **SESSION_SPEC,
        "argv": ["/usr/local/bin/herdr", "server", "--token", secret_arg],
    }
    with pytest.raises(x.ExecSpecInvalid):
        x.validate(bad_spec, read_file=lambda p: MOCK_FILES[p])

    runner = FakeRunner()
    res = x.session_open(
        bad_spec,
        runner=runner,
        state_root=str(tmp_path / "sessions"),
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res["ok"] is False
    assert res["error_code"] in ("SECRET_IN_ARGV", "SPEC_INVALID")
    # Podman run was never called
    assert not any(c[0] == "run" for c, _ in runner.calls)


# ====================================================================================
# T-S-5: Session Container Timeout & Labels
# ====================================================================================

def test_t_s_5_timeout_and_labels(tmp_path):
    """T-S-5: Container started with --timeout <timeout_s> and container-ctl.session/container-ctl.exec_id labels."""
    state_root = str(tmp_path / "sessions")
    runner = FakeRunner()

    res = x.session_open(
        SESSION_SPEC,
        runner=runner,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res["ok"] is True

    run_call = next(c for c, _ in runner.calls if c[0] == "run")
    # Verify timeout_s exactly matches 3600 without FUSE_MARGIN_S
    timeout_idx = run_call.index("--timeout")
    assert run_call[timeout_idx + 1] == "3600"

    # Verify labels
    assert "--label" in run_call
    assert "container-ctl.session=sess-lane-01" in run_call
    assert "container-ctl.exec_id=sess-exec-01" in run_call


# ====================================================================================
# T-H-6: Herdr Binary SHA256 Verification
# ====================================================================================

def test_t_h_6_sha256_verification(tmp_path):
    """T-H-6: Herdr binary sha256 mismatch rejects session_open without starting container."""
    state_root = str(tmp_path / "sessions")
    binary_content = MOCK_FILES["/usr/local/bin/herdr"]
    correct_sha = hashlib.sha256(binary_content).hexdigest()
    wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"

    # 1. Matching SHA256 succeeds
    ok_spec = {
        **SESSION_SPEC,
        "herdr_binary_sha256": {"path": "/usr/local/bin/herdr", "sha256": correct_sha},
    }
    runner_ok = FakeRunner()
    res_ok = x.session_open(
        ok_spec,
        runner=runner_ok,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res_ok["ok"] is True

    # 2. Mismatch SHA256 rejects
    bad_spec = {
        **SESSION_SPEC,
        "exec_id": "sess-exec-bad-sha",
        "herdr_binary_sha256": {"path": "/usr/local/bin/herdr", "sha256": wrong_sha},
    }
    runner_bad = FakeRunner()
    res_bad = x.session_open(
        bad_spec,
        runner=runner_bad,
        state_root=state_root,
        read_file=lambda p: MOCK_FILES[p],
    )
    assert res_bad["ok"] is False
    assert res_bad["error_code"] == "HERDR_BINARY_SHA256_MISMATCH"
    assert "mismatch" in res_bad["error"].lower()
    # Podman run was never called
    assert not any(c[0] == "run" for c, _ in runner_bad.calls)


# ====================================================================================
# session_status & session_mask
# ====================================================================================

def test_session_status(tmp_path):
    """session_status reports container alive status, started_at and RSS."""
    state_root = str(tmp_path / "sessions")
    runner = FakeRunner(running=True, rss="45.2MB / 1024MB")

    x.session_open(SESSION_SPEC, runner=runner, state_root=state_root, read_file=lambda p: MOCK_FILES[p])

    status = x.session_status("sess-exec-01", runner=runner, state_root=state_root)
    assert status["ok"] is True
    assert status["alive"] is True
    assert status["rss"] == "45.2MB"

    # When container is dead
    runner_dead = FakeRunner(running=False)
    status_dead = x.session_status("sess-exec-01", runner=runner_dead, state_root=state_root)
    assert status_dead["ok"] is True
    assert status_dead["alive"] is False


def test_session_mask_projected_and_refreshed_tokens(tmp_path):
    """session_mask scrubs literal secrets, plus refreshed token patterns and Bearer tokens."""
    state_root = str(tmp_path / "sessions")
    runner = FakeRunner()

    x.session_open(SESSION_SPEC, runner=runner, state_root=state_root, read_file=lambda p: MOCK_FILES[p])

    # 1. Literal secret from projected creds
    text1 = "Error connecting with sk-ant-oat-SECRET-KEY-ABC12345 in session."
    masked1 = x.session_mask("sess-exec-01", text1, state_root=state_root)
    assert "SECRET-KEY" not in masked1
    assert masked1 == "Error connecting with [REDACTED] in session."

    # 2. Refreshed Anthropic api key (never present in initial credentials)
    text2 = "API key rotated to sk-ant-api03-999988887777666655554444333322221111."
    masked2 = x.session_mask("sess-exec-01", text2, state_root=state_root)
    assert "sk-ant-api03" not in masked2
    assert masked2 == "API key rotated to [REDACTED]."

    # 3. Refreshed Bearer token
    text3 = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    masked3 = x.session_mask("sess-exec-01", text3, state_root=state_root)
    assert "eyJhbGciOi" not in masked3
    assert masked3 == "Authorization: [REDACTED]"

    # 4. Refreshed Google AI token
    text4 = "Using AIzaSyD-xyz123456789012345678901234567890 for Gemini."
    masked4 = x.session_mask("sess-exec-01", text4, state_root=state_root)
    assert "AIzaSyD" not in masked4
    assert masked4 == "Using [REDACTED] for Gemini."

    # 5. Bytes input support
    b_input = b"Token sk-ant-api03-abcdef1234567890abcdef is secret."
    b_masked = x.session_mask("sess-exec-01", b_input, state_root=state_root)
    assert isinstance(b_masked, bytes)
    assert b"[REDACTED]" in b_masked
    assert b"sk-ant-api03" not in b_masked


# ====================================================================================
# CLI Subcommand Dispatch
# ====================================================================================

def test_cli_subcommands(tmp_path, monkeypatch):
    """Test CLI subcommands: session_open, session_status, session_close, session_mask."""
    state_root = str(tmp_path / "sessions")
    monkeypatch.setenv("CONTAINER_CTL_EXEC_STATE_ROOT", state_root)

    # 1. session_open via CLI argument
    spec_json = json.dumps({
        **SESSION_SPEC,
        "limits": {"memory_max": "1024M", "timeout_s": 3600},
        "credential_files": [],
        "env_secrets": [],
    })
    out = io.StringIO()
    # monkeypatch default runner
    fake_runner = FakeRunner()
    monkeypatch.setattr(x, "default_runner", fake_runner)

    rc = x.main(["session_open", spec_json], stdout=out)
    assert rc == 0
    resp = json.loads(out.getvalue())
    assert resp["ok"] is True
    assert resp["exec_id"] == "sess-exec-01"

    # 2. session_status via CLI
    out_status = io.StringIO()
    rc = x.main(["session_status", "sess-exec-01"], stdout=out_status)
    assert rc == 0
    status_resp = json.loads(out_status.getvalue())
    assert status_resp["ok"] is True
    assert status_resp["alive"] is True

    # 3. session_mask via CLI (stdin pipe)
    out_mask = io.StringIO()
    in_mask = io.StringIO("Leak Authorization: Bearer refreshed_jwt_token_over_20_chars_here\n")
    rc = x.main(["session_mask", "sess-exec-01"], stdin=in_mask, stdout=out_mask)
    assert rc == 0
    assert "refreshed_jwt" not in out_mask.getvalue()
    assert "[REDACTED]" in out_mask.getvalue()

    # 4. session_close via CLI
    out_close = io.StringIO()
    rc = x.main(["session_close", "sess-exec-01"], stdout=out_close)
    assert rc == 0
    close_resp = json.loads(out_close.getvalue())
    assert close_resp["ok"] is True


def test_cli_stdin_session_mode_dispatch(tmp_path, monkeypatch):
    """Passing a session spec on stdin without subcommands dispatches to session_open."""
    state_root = str(tmp_path / "sessions")
    monkeypatch.setenv("CONTAINER_CTL_EXEC_STATE_ROOT", state_root)
    monkeypatch.setattr(x, "default_runner", FakeRunner())

    spec_json = json.dumps({
        **SESSION_SPEC,
        "limits": {"memory_max": "1024M", "timeout_s": 3600},
        "credential_files": [],
        "env_secrets": [],
    })
    out = io.StringIO()
    rc = x.main(stdin=io.StringIO(spec_json), stdout=out)
    assert rc == 0
    resp = json.loads(out.getvalue())
    assert resp["ok"] is True
    assert resp["exec_id"] == "sess-exec-01"


def test_session_close_records_egress_observation_when_asked_and_never_blocks(tmp_path, monkeypatch):
    """session close writes one watch-only egress row; a failing sample never blocks the close."""
    import json
    from container_ctl import exec_run as ex
    from tests.test_exec_run import TCP, UDP6

    state_root = str(tmp_path / "sessions")
    log = tmp_path / "egress.jsonl"

    class Watching(FakeRunner):
        def __call__(self, argv, stdin, timeout):
            if argv[0] == "exec" and "cat" in argv:
                return 0, (TCP + UDP6).encode(), b""
            return super().__call__(argv, stdin, timeout)

    runner = Watching()
    assert x.session_open(SESSION_SPEC, runner=runner, state_root=state_root, read_file=lambda p: MOCK_FILES[p])["ok"] is True

    monkeypatch.delenv("CONTAINER_CTL_EXEC_EGRESS_LOG", raising=False)
    assert x.session_close("sess-exec-01", runner=runner, state_root=state_root)["ok"] is True
    assert not log.exists()

    x.session_open(SESSION_SPEC, runner=runner, state_root=state_root, read_file=lambda p: MOCK_FILES[p])
    monkeypatch.setenv("CONTAINER_CTL_EXEC_EGRESS_LOG", str(log))
    assert x.session_close("sess-exec-01", runner=runner, state_root=state_root)["ok"] is True
    row = json.loads(log.read_text().splitlines()[-1])
    assert row["exec_id"] == "sess-exec-01" and row["profile"] == "default"
    assert row["remotes"] == ["1.1.1.1:53", "34.216.184.93:443"]

    failing = FakeRunner()
    def boom(argv, stdin, timeout):
        if argv[0] == "exec":
            raise OSError("podman gone")
        return FakeRunner.__call__(failing, argv, stdin, timeout)
    x.session_open(SESSION_SPEC, runner=failing, state_root=state_root, read_file=lambda p: MOCK_FILES[p])
    assert x.session_close("sess-exec-01", runner=boom, state_root=state_root)["ok"] is True
