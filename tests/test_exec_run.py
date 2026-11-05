import pytest

from container_ctl import exec_run as x

SPEC = {"v": 1, "exec_id": "e1", "run_id": "r1", "image": "localhost/agent:1", "argv": ["/opt/ext/c/claude", "-p", "hi"],
        "workspace": {"mode": "scratch", "host_path": ""}, "mounts_ro": [{"src": "/h/c", "dst": "/opt/ext/c"}],
        "credential_files": [{"src": "/h/tok", "dest": ".cfg/tok", "required": True}],
        "env_secrets": [{"src": "/h/key", "env_by_prefix": [["sk-ant-oat", "CLAUDE_CODE_OAUTH_TOKEN"]], "required": True}],
        "env": {"A": "1"}, "egress": "default", "limits": {"memory_max": "900M", "timeout_s": 5}, "harness": False}
FILES = {"/h/tok": b"T", "/h/key": b"sk-ant-oat-SECRET\n"}


class Fake:
    def __init__(self, exists=False, run_rc=0, exec_rc=0, out=b"hello"):
        self.calls, self.exists, self.run_rc, self.exec_rc, self.out = [], exists, run_rc, exec_rc, out

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        if argv[:2] == ["container", "exists"]:
            return (0 if self.exists else 1), b"", b""
        if argv[0] == "run":
            return self.run_rc, b"id", b"boom" if self.run_rc else b""
        if argv[0] == "exec" and "-w" in argv:
            return self.exec_rc, self.out, b"err"
        return 0, b"", b""


def go(spec=SPEC, **kw):
    f = Fake(**kw)
    return x.run_exec(spec, runner=f, read_file=lambda p: FILES[p], clock=iter(range(0, 100, 2)).__next__), f


def test_success_runs_hardened_container_and_always_removes_it():
    r, f = go()
    assert r["state"] == "succeeded" and r["output"] == "hello" and r["exec_id"] == "e1"
    run = next(a for a, _ in f.calls if a[0] == "run")
    for flag in ("--read-only", "--init", "--cap-drop", "all", "no-new-privileges", "--pull=never"):
        assert flag in run
    assert "HOME=/home/agent" in run and "container-ctl.run=r1" in run and "container-ctl.exec=e1" in run and "/h/c:/opt/ext/c:ro" in run
    assert f.calls[-1][0][:2] == ["rm", "-f"]


def test_secrets_only_through_stdin_never_argv():
    r, f = go()
    argvs = " ".join(" ".join(a) for a, _ in f.calls)
    assert "SECRET" not in argvs and "/h/key" not in argvs
    stdins = [s for _, s in f.calls if s]
    assert b"T" in stdins and any(b"export CLAUDE_CODE_OAUTH_TOKEN='sk-ant-oat-SECRET'" in s for s in stdins)
    assert r["output"] == "hello" and "SECRET" not in str(r)


def test_nonzero_exit_and_timeout_map_to_states():
    assert go(exec_rc=2)[0]["state"] == "failed" and go(exec_rc=2)[0]["error_code"] == "EXIT_NONZERO"
    assert go(exec_rc=124)[0]["state"] == "timeout"


def test_resend_with_existing_exec_id_starts_nothing():
    r, f = go(exists=True)
    assert r["error_code"] == "EXEC_ID_IN_USE" and not any(a[0] == "run" for a, _ in f.calls)


def test_start_failure_still_cleans_up_nothing_leaks():
    r, f = go(run_rc=125)
    assert r["error_code"] == "CONTAINER_START_FAILED" and r["state"] == "failed"


@pytest.mark.parametrize("patch", [{"image": "localhost/agent:latest"}, {"image": "agent"}, {"argv": []}, {"v": 3},
                                   {"workspace": {"mode": "ro", "host_path": ""}}, {"limits": {"memory_max": "lots", "timeout_s": 5}},
                                   {"mounts_ro": [{"src": "/h", "dst": "/etc/x"}]}, {"env": {"bad name": "1"}}])
def test_invalid_specs_are_rejected_without_calling_podman(patch):
    r, f = go({**SPEC, **patch})
    assert r["error_code"] == "SPEC_INVALID" and not f.calls


def test_rw_and_unknown_egress_fail_closed():
    assert go({**SPEC, "workspace": {"mode": "rw", "host_path": "/etc"}})[0]["error_code"] == "RW_SOURCE_REJECTED"
    r, f = go({**SPEC, "egress": "allowlist"})
    assert r["error_code"] == "EGRESS_UNKNOWN" and not f.calls
    ok = x.run_exec({**SPEC, "egress": "allowlist"}, networks={"allowlist": ("container-egress", {"HTTPS_PROXY": "http://p"})},
                    runner=Fake(), read_file=lambda p: FILES[p])
    assert ok["state"] == "succeeded"


def test_credential_missing_aborts_before_running_the_agent():
    f = Fake()
    r = x.run_exec(SPEC, runner=f, read_file=lambda p: (_ for _ in ()).throw(FileNotFoundError()))
    assert r["error_code"] == "CREDENTIAL_MISSING" and not any("-w" in a for a, _ in f.calls) and f.calls[-1][0][0] == "rm"


def _rw(tmp_path, edit, **kw):
    tmp_path = tmp_path / str(len(list(tmp_path.iterdir())))
    tmp_path.mkdir()
    src = tmp_path / "proj"
    src.mkdir()
    (src / "a.txt").write_text("one\n")
    (src / "keep.txt").write_text("keep\n")
    spec = {**SPEC, "workspace": {"mode": "rw", "host_path": str(src)}, "argv": ["agent"]}
    mounted = {}

    class RwFake(Fake):
        def __call__(self, argv, stdin, timeout):
            if argv[0] == "run":
                mounted["b"] = next(a.split(":")[0] for a in argv if a.endswith(":/workspace:rw"))
            if argv[0] == "exec" and "-w" in argv:
                edit(__import__("pathlib").Path(mounted["b"]))
            return super().__call__(argv, stdin, timeout)
    f = RwFake(**kw)
    r = x.run_exec(spec, runner=f, read_file=lambda p: FILES[p], scratch_root=str(tmp_path))
    return r, src, f


def test_rw_produces_a_patch_and_never_touches_the_host_directory(tmp_path):
    def edit(b):
        (b / "a.txt").write_text("two\n")
        (b / "new.txt").write_text("hello\n")
        (b / "keep.txt").unlink()
    r, src, f = _rw(tmp_path, edit)
    assert r["state"] == "succeeded"
    assert (src / "a.txt").read_text() == "one\n" and (src / "keep.txt").exists() and not (src / "new.txt").exists()
    p = r["patch"]
    assert "--- a/a.txt" in p and "+two" in p and "+++ b/new.txt" in p and "-keep" in p
    assert not [d for d in tmp_path.iterdir() if d.name.startswith("cctl-rw-")]       # scratch copies are removed
    assert f.calls[-1][0][:2] == ["rm", "-f"]


def test_rw_without_changes_gives_an_empty_patch_and_failed_runs_still_return_theirs(tmp_path):
    r, *_ = _rw(tmp_path, lambda b: None)
    assert r["patch"] == ""
    r, *_ = _rw(tmp_path, lambda b: (b / "a.txt").write_text("x\n"), exec_rc=1)
    assert r["state"] == "failed" and "+x" in r["patch"]


def test_rw_patch_too_large_is_an_error_not_a_truncated_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(x, "MAX_PATCH", 10)
    r, *_ = _rw(tmp_path, lambda b: (b / "big.txt").write_text("y" * 100))
    assert r["error_code"] == "PATCH_TOO_LARGE" and r["patch"] is None


def test_rw_rejects_symlink_and_oversized_sources(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    r = x.run_exec({**SPEC, "workspace": {"mode": "rw", "host_path": str(link)}}, runner=Fake(), read_file=lambda p: FILES[p])
    assert r["error_code"] == "RW_SOURCE_REJECTED"
    monkeypatch.setattr(x, "MAX_RW_BYTES", 1)
    (real / "f").write_text("zz")
    r = x.run_exec({**SPEC, "workspace": {"mode": "rw", "host_path": str(real)}}, runner=Fake(), read_file=lambda p: FILES[p])
    assert r["error_code"] == "RW_SOURCE_REJECTED"


def test_make_patch_is_git_format_with_same_path_both_sides(tmp_path):
    import os, subprocess
    from container_ctl.exec_run import make_patch
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "a.txt").write_text("hello\n"); (b / "a.txt").write_text("hello\nchanged\n"); (b / "b.txt").write_text("added\n")
    (b / ".gitignore").write_text("*\n")                       # an ignore rule in the workspace must not hide changes
    patch = make_patch(str(tmp_path))
    assert "diff --git a/a.txt b/a.txt" in patch and "diff --git a/b.txt b/b.txt" in patch and "+changed" in patch
    base = tmp_path / "t"; base.mkdir(); (base / "a.txt").write_text("hello\n")
    subprocess.run(["git", "init", "-q", str(base)], check=True)
    subprocess.run(["git", "apply", "-p1", "--check", "-"], input=patch.encode(), cwd=base, check=True)
    assert make_patch(str(tmp_path)) == patch                   # deterministic


def test_rw_timeout_still_returns_what_the_agent_wrote(tmp_path):
    r, src, f = _rw(tmp_path, lambda b: (b / "a.txt").write_text("late\n"), exec_rc=124)
    assert r["state"] == "timeout" and r["error_code"] == "TIMEOUT"
    assert "+late" in r["patch"]                                               # the work is not thrown away
    assert (src / "a.txt").read_text() == "one\n"                             # and the host is still untouched
    assert not [d for d in tmp_path.iterdir() if d.name.startswith("cctl-rw-")]
    kinds = [c[0][:2] for c in f.calls]
    assert kinds.count(["rm", "-f"]) == 2 and kinds.index(["rm", "-f"]) < len(kinds) - 1   # stopped before the snapshot, and again in cleanup


def test_stop_exec_removes_only_the_named_container_and_refuses_odd_ids():
    seen = []
    def runner(argv, stdin, timeout):
        seen.append(argv)
        return 0, b"", b""
    assert x.stop_exec("e1", runner) is True
    assert seen == [["container", "exists", "cctl-exec-e1"], ["rm", "-f", "-t", "2", "cctl-exec-e1"]]
    for bad in ("", "a b", "../x", "e1;rm", "x" * 65, None):
        assert x.stop_exec(bad, runner) is False
    assert len(seen) == 2                                       # nothing ran for the bad ones


def test_cli_stop_reports_failure_in_the_exit_code():
    import io
    out = io.StringIO()
    assert x.main(["--stop", "bad id"], stdin=io.StringIO(""), stdout=out) == 1 and '"stopped": false' in out.getvalue()


def test_podman_run_carries_a_host_side_time_fuse():
    """A crashed worker must not leave a container running for ever: podman kills it at limit + margin."""
    from container_ctl.exec_run import FUSE_MARGIN_S, render_run_argv
    argv = render_run_argv(SPEC, {"default": ("default", {})})
    assert argv[argv.index("--timeout") + 1] == str(int(SPEC["limits"]["timeout_s"]) + FUSE_MARGIN_S)


def test_sweep_removes_orphan_containers_and_scratch_copies_only(tmp_path):
    from container_ctl.exec_run import sweep
    (tmp_path / "cctl-rw-abc").mkdir(); (tmp_path / "cctl-rw-abc" / "f").write_text("x"); (tmp_path / "keep").mkdir()
    calls = []

    def runner(args, stdin, timeout):
        calls.append(args)
        return (0, b"cctl-exec-e1\nsomething-else\ncctl-exec-e2\n", b"") if args[0] == "ps" else (0, "", "")
    out = sweep(str(tmp_path), runner)
    assert out == {"ok": True, "containers_seen": 2, "containers_removed": 2, "scratch_removed": 1}
    assert [c[-1] for c in calls if c[0] == "rm"] == ["cctl-exec-e1", "cctl-exec-e2"]          # a foreign container is never touched
    assert not (tmp_path / "cctl-rw-abc").exists() and (tmp_path / "keep").exists()


def test_sweep_is_not_ok_when_podman_cannot_list_or_remove(tmp_path):
    from container_ctl.exec_run import sweep
    assert sweep(str(tmp_path), lambda a, i, t: (125, b"", b"boom"))["ok"] is False
    def rm_fails(a, i, t): return (0, b"cctl-exec-e1\n", b"") if a[0] == "ps" else (1, b"", b"")
    assert sweep(str(tmp_path), rm_fails)["ok"] is False


def test_stop_of_a_container_that_does_not_exist_yet_is_not_a_stop():
    from container_ctl.exec_run import stop_exec
    calls = []
    def runner(a, i, t):
        calls.append(a[0]); return (1, b"", b"") if a[0] == "container" else (0, b"", b"")
    assert stop_exec("e1", runner) is False and calls == ["container"]          # never reached `rm`
    assert stop_exec("e1", lambda a, i, t: (0, b"", b"")) is True


TCP = """  sl  local_address rem_address   st tx_queue rx_queue
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 0 0 1 1 0
   1: 0F02000A:C350 5DB8D822:01BB 01 00000000:00000000 00:00000000 0 0 2 1 0
   2: 0100007F:C351 0100007F:1F90 01 00000000:00000000 00:00000000 0 0 3 1 0
"""
UDP6 = """  sl  local_address                         remote_address                        st
   0: 00000000000000000000000000000000:0000 00000000000000000000000000000000:0000 07
   1: 0000000000000000FFFF00000F02000A:9C40 0000000000000000FFFF000001010101:0035 01
"""


def test_parse_remotes_keeps_real_peers_only():
    from container_ctl.exec_run import parse_remotes
    assert parse_remotes(TCP + UDP6) == {"34.216.184.93:443", "1.1.1.1:53"}      # no listener, no loopback, v4-mapped v6 shown as v4
    assert parse_remotes("garbage\n\x00\n") == set()


def test_egress_observation_is_written_only_when_asked_and_never_blocks(tmp_path, monkeypatch):
    import json
    log = tmp_path / "egress.jsonl"

    class Watching(Fake):
        def __call__(self, argv, stdin, timeout):
            if argv[0] == "exec" and argv[2:3] == ["cat"]:
                return 0, (TCP + UDP6).encode(), b""
            return super().__call__(argv, stdin, timeout)

    def run():
        return x.run_exec(SPEC, runner=Watching(), read_file=lambda p: FILES[p], clock=iter(range(0, 100, 2)).__next__)
    monkeypatch.delenv("CONTAINER_CTL_EXEC_EGRESS_LOG", raising=False)
    assert run()["state"] == "succeeded" and not log.exists()
    monkeypatch.setenv("CONTAINER_CTL_EXEC_EGRESS_LOG", str(log))
    assert run()["state"] == "succeeded"
    row = json.loads(log.read_text().splitlines()[-1])
    assert row["remotes"] == ["1.1.1.1:53", "34.216.184.93:443"] and row["run_id"] == "r1" and set(row) == {"ts", "run_id", "exec_id", "profile", "remotes"}


# --- Secret handling: source roots, token characters, output scrubbing ---------------------------------

def _go_with(files, spec=SPEC, **kw):
    f = Fake(**kw)
    return x.run_exec(spec, runner=f, read_file=lambda p: files[p], clock=iter(range(0, 100, 2)).__next__), f


def test_credential_src_must_be_absolute_without_dotdot():
    for bad in ("rel/key", "/h/../etc/key", ""):
        r, f = go({**SPEC, "env_secrets": [{"src": bad, "env_by_prefix": [["sk", "K"]]}]})
        assert r["error_code"] == "SPEC_INVALID" and not f.calls
    r, f = go({**SPEC, "credential_files": [{"src": "x", "dest": "d"}]})
    assert r["error_code"] == "SPEC_INVALID" and not f.calls


def test_credential_src_roots_are_enforced_when_configured(tmp_path, monkeypatch):
    root, other = tmp_path / "creds", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    (root / "tok").write_text("T")
    (other / "tok").write_text("T")
    (root / "escape").symlink_to(other / "tok")
    spec = {**SPEC, "env_secrets": [], "credential_files": [{"src": str(root / "tok"), "dest": "t"}]}
    monkeypatch.setenv("CONTAINER_CTL_EXEC_CRED_ROOTS", f"{root}:/nonexistent-root")
    files = {str(root / "tok"): b"T", str(other / "tok"): b"T", str(root / "escape"): b"T"}
    assert _go_with(files, spec)[0]["state"] == "succeeded"
    for src in (str(other / "tok"), str(root / "escape")):       # outside the root, and a symlink that leaves it
        r, f = _go_with(files, {**spec, "credential_files": [{"src": src, "dest": "t"}]})
        assert r["error_code"] == "SPEC_INVALID" and not f.calls
    monkeypatch.delenv("CONTAINER_CTL_EXEC_CRED_ROOTS")
    assert _go_with(files, {**spec, "credential_files": [{"src": str(other / "tok"), "dest": "t"}]})[0]["state"] == "succeeded"


@pytest.mark.parametrize("token", [b"sk-ant-oat-x'; touch /tmp/pwn; '", b"sk-ant-oat-line1\nline2", b"sk-ant-oat- spaced"])
def test_token_with_quote_newline_or_space_is_refused_before_the_agent_runs(token):
    r, f = _go_with({**FILES, "/h/key": token})
    assert r["error_code"] == "CREDENTIAL_UNRECOGNIZED" and not any("-w" in a for a, _ in f.calls)
    assert not any(s and b"touch" in s for _, s in f.calls)


def test_projected_values_are_scrubbed_from_output_and_error_text():
    out = b"key=sk-ant-oat-SECRET and file=TOKENFILEVALUE!"
    r, _ = _go_with({**FILES, "/h/tok": b"TOKENFILEVALUE!\n"}, out=out, exec_rc=2)
    assert r["state"] == "failed"
    assert "SECRET" not in r["output"] and "TOKENFILEVALUE" not in r["output"] and r["output"].count(x.REDACTED) == 2
    assert "sk-ant-oat-SECRET" not in str(r)


def test_json_credential_string_values_are_scrubbed_but_short_ones_are_not():
    doc = b'{"access_token": "ya29.A0-long-access-token-value", "kind": "oauth", "n": 7}'
    spec = {**SPEC, "env_secrets": [], "credential_files": [{"src": "/h/j", "dest": "j"}]}
    r, _ = _go_with({"/h/j": doc}, spec, out=b"tok=ya29.A0-long-access-token-value kind=oauth")
    assert "ya29" not in r["output"] and "kind=oauth" in r["output"]


def test_output_without_secrets_is_untouched_and_short_values_are_ignored():
    r, _ = _go_with({**FILES, "/h/tok": b"T"}, out=b"T is fine, hello")
    assert r["output"] == "T is fine, hello"


def test_rw_patch_is_scrubbed_too(tmp_path):
    r, *_ = _rw(tmp_path, lambda b: (b / "leak.txt").write_text("sk-ant-oat-SECRET\n"))
    assert r["state"] == "succeeded" and "SECRET" not in r["patch"] and x.REDACTED in r["patch"]


# -- Post-run executor checks --

CHECKS = [{"name": "unit", "argv": ["python3", "-m", "pytest"], "timeout_s": 30}]


def test_checks_run_in_fresh_credentialless_offline_containers_after_success():
    r, f = go({**SPEC, "checks": CHECKS})
    assert r["state"] == "succeeded"
    (c,) = r["checks"]
    assert c["name"] == "unit" and c["exit_code"] == 0 and c["timed_out"] is False and len(c["output_sha256"]) == 64
    runs = [a for a, _ in f.calls if a[0] == "run"]
    assert len(runs) == 2
    check_run = runs[1]
    assert "--network" in check_run and check_run[check_run.index("--network") + 1] == "none"
    assert "container-ctl.exec=e1.c0" in check_run and "A=1" not in check_run and "/h/c:/opt/ext/c:ro" in check_run
    assert "--read-only" in check_run
    execs = [a for a, _ in f.calls if a[0] == "exec" and "-w" in a]
    assert execs[-1][-3:] == ["python3", "-m", "pytest"]
    assert not any(a[0] == "exec" and "-i" in a and "cctl-exec-e1.c0" in a for a, _ in f.calls)   # no credential projection
    assert f.calls[-1][0][:2] == ["rm", "-f"]


def test_checks_mount_the_rw_workspace_read_only(tmp_path):
    (tmp_path / "proj").mkdir(); (tmp_path / "proj" / "a.txt").write_text("one\n"); (tmp_path / "scratch").mkdir()
    spec = {**SPEC, "checks": CHECKS, "workspace": {"mode": "rw", "host_path": str(tmp_path / "proj")}}
    f = Fake()
    r = x.run_exec(spec, runner=f, read_file=lambda p: FILES[p], scratch_root=str(tmp_path / "scratch"))
    agent, check = [a for a, _ in f.calls if a[0] == "run"]
    assert any(a.endswith(":/workspace:rw") for a in agent)
    ws = [a for a in check if ":/workspace:" in a]
    assert len(ws) == 1 and ws[0].endswith(":/workspace:ro") and ws[0].split(":")[0] != str(tmp_path / "proj")    # the isolated copy, read-only
    assert r["checks"][0]["exit_code"] == 0


def test_run_fuse_covers_the_checks_budget_and_check_containers_do_not_add_it_twice():
    two = [{"name": "a", "argv": ["t"], "timeout_s": 30}, {"name": "b", "argv": ["t"], "timeout_s": 40}]
    assert x.checks_budget_s(two) == 70 and x.checks_budget_s(None) == 0
    _, f = go({**SPEC, "checks": two})
    fuses = [int(a[a.index("--timeout") + 1]) for a, _ in f.calls if a[0] == "run"]
    assert fuses == [5 + x.FUSE_MARGIN_S + 70, 30 + x.FUSE_MARGIN_S, 40 + x.FUSE_MARGIN_S]


def test_checks_not_run_when_agent_fails_and_absent_without_checks():
    r, f = go({**SPEC, "checks": CHECKS}, exec_rc=1)
    assert r["state"] == "failed" and "checks" not in r
    r, _ = go()
    assert "checks" not in r


def test_check_tail_is_scrubbed_and_bounded():
    r, _ = go({**SPEC, "checks": CHECKS}, out=b"x" * 5000 + b"sk-ant-oat-SECRET")
    tail = r["checks"][0]["tail"]
    assert "sk-ant-oat-SECRET" not in tail and len(tail.encode()) <= 1024


@pytest.mark.parametrize("bad", [[], [{"name": "A", "argv": ["x"], "timeout_s": 1}], [{"name": "a", "argv": [], "timeout_s": 1}],
                                 [{"name": "a", "argv": ["x"], "timeout_s": 0}], CHECKS * 2,
                                 [{"name": "a", "argv": ["cat", "/h/key"], "timeout_s": 1}]])
def test_bad_checks_are_spec_invalid(bad):
    r, _ = go({**SPEC, "checks": bad})
    assert r["state"] == "failed" and r["error_code"] == "SPEC_INVALID"


# -- Exec liveness for workspace lease reclaim --

def _exists(answers):
    calls = []

    def runner(argv, stdin, timeout):
        calls.append(argv)
        return answers.get(argv[-1], 1), b"", b""
    return runner, calls


def test_exec_alive_true_when_agent_or_check_container_exists():
    r, _ = _exists({"cctl-exec-e1": 0})
    assert x.exec_alive("e1", runner=r) is True
    r, _ = _exists({"cctl-exec-e1.c3": 0})
    assert x.exec_alive("e1", runner=r) is True


def test_exec_alive_false_only_when_every_container_is_confirmed_missing():
    r, calls = _exists({})
    assert x.exec_alive("e1", runner=r) is False and len(calls) == 1 + x.CHECKS_MAX
    r, _ = _exists({"cctl-exec-e1": 125})             # podman error: not a confirmed miss
    assert x.exec_alive("e1", runner=r) is True
    assert x.exec_alive("bad id!", runner=r) is False


def test_alive_cli(monkeypatch):
    import io
    monkeypatch.setattr(x, "exec_alive", lambda eid: eid == "e1")
    out = io.StringIO()
    assert x.main(["--alive", "e1"], stdout=out) == 0 and '"alive": true' in out.getvalue()
    assert x.main(["--alive"], stdout=io.StringIO()) == 2


class RmFails(Fake):
    """`podman rm` fails; the container is still there afterwards unless `gone_after_rm`."""
    def __init__(self, gone_after_rm=False, **kw):
        super().__init__(**kw)
        self.started, self.gone_after_rm = False, gone_after_rm

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        if argv[:2] == ["container", "exists"]:
            return (0 if self.started and not self.gone_after_rm else 1), b"", b""
        if argv[0] == "run":
            self.started = True
            return 0, b"id", b""
        if argv[0] == "rm":
            return 125, b"", b"rm failed"
        if argv[0] == "exec" and "-w" in argv:
            return 0, b"hello", b""
        return 0, b"", b""


def test_failed_remove_with_container_still_present_raises_instead_of_returning():
    # A normal return guarantees the container is gone so the caller may safely release the workspace lease.
    f = RmFails()
    with pytest.raises(x.ContainerLeft):
        x.run_exec(SPEC, runner=f, read_file=lambda p: FILES[p], clock=iter(range(0, 100, 2)).__next__)


def test_failed_remove_of_a_confirmed_missing_container_still_returns():
    f = RmFails(gone_after_rm=True)
    r = x.run_exec(SPEC, runner=f, read_file=lambda p: FILES[p], clock=iter(range(0, 100, 2)).__next__)
    assert r["state"] == "succeeded"


def test_check_argv_with_lone_surrogate_is_refused_like_the_gateway():
    # Gateway parse_checks rejects text that is not strict UTF-8; the executor must not accept what admission would refuse.
    bad = {**SPEC, "checks": [{"name": "unit", "argv": ["node", "x\ud800"], "timeout_s": 60}]}
    r, f = go(bad)
    assert r["error_code"] == "SPEC_INVALID" and not f.calls
    ok = {**SPEC, "checks": [{"name": "unit", "argv": ["node", "é"], "timeout_s": 60}]}
    r, _ = go(ok)
    assert r["error_code"] != "SPEC_INVALID"


def test_mount_strings_cannot_be_injected_and_ro_workspace_rejects_system_roots():
    import pytest
    for bad in ({**SPEC, "mounts_ro": [{"src": "/h/c:/opt/ext/x:rw", "dst": "/opt/ext/c"}]},
                {**SPEC, "mounts_ro": [{"src": "/h/c", "dst": "/opt/ext/c,rw"}]},
                {**SPEC, "workspace": {"mode": "ro", "host_path": "/"}},
                {**SPEC, "workspace": {"mode": "ro", "host_path": "/etc"}},
                {**SPEC, "workspace": {"mode": "ro", "host_path": "/srv/w:/workspace:rw"}}):
        with pytest.raises(x.ExecSpecInvalid):
            x.validate(bad)
    x.validate({**SPEC, "workspace": {"mode": "ro", "host_path": "/srv/project"}})


def test_credential_dest_cannot_escape_home_or_staging_dir():
    import pytest
    for dest in ("../../../etc/cron.d/x", "a/../../b", "", "/", "a:b", None):
        with pytest.raises(x.ExecSpecInvalid):
            x.validate({**SPEC, "credential_files": [{"src": "/h/tok", "dest": dest}]})
    x.validate({**SPEC, "credential_files": [{"src": "/h/tok", "dest": "/.cfg/tok"}]})
