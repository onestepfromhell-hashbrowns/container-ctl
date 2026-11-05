from container_ctl.smoke import _published_tcp, run_smoke


def _insp(*states):
    it = iter(states)
    return lambda c: next(it)


def _run(cfg, before, after, tcp=True, http=200):
    return run_smoke("c", cfg, sleep=lambda s: None, inspect=_insp(before, after),
                     tcp_check=lambda h, p: tcp, http_check=lambda u, i: http)


OKS = {"RestartCount": 0, "State": {"Status": "running"}}


def test_published_tcp_skips_ranges_udp_and_bad():
    assert _published_tcp(["127.0.0.2:9443:8000", "8080:80", "1000-1010:1000-1010", "53:53/udp"]) == [
        ("127.0.0.2", 9443), ("127.0.0.1", 8080)]


def test_all_pass():
    ok, msgs = _run({"ports": ["127.0.0.2:9443:8000"], "smoke": {"http": {"url": "http://x", "status": 200}}}, OKS, OKS)
    assert ok and not any(m.startswith("[ERROR]") for m in msgs)


def test_crash_loop_detected():
    ok, msgs = _run({}, OKS, {"RestartCount": 2, "State": {"Status": "running"}})
    assert not ok and "crash loop" in msgs[0]


def test_not_running_fails():
    ok, _ = _run({}, OKS, {"RestartCount": 0, "State": {"Status": "exited"}})
    assert not ok


def test_tcp_fail_and_http_mismatch():
    ok, msgs = _run({"ports": ["127.0.0.2:9443:8000"]}, OKS, OKS, tcp=False)
    assert not ok and any("TCP" in m and m.startswith("[ERROR]") for m in msgs)
    ok, msgs = _run({"smoke": {"http": {"url": "http://x"}}}, OKS, OKS, http=503)
    assert not ok and "503" in msgs[-1]


def test_tcp_can_be_disabled():
    ok, msgs = _run({"ports": ["127.0.0.2:9443:8000"], "smoke": {"tcp": False}}, OKS, OKS, tcp=False)
    assert ok


def test_inspect_failure_is_error():
    def boom(c):
        raise RuntimeError("no such container")
    ok, msgs = run_smoke("c", {}, sleep=lambda s: None, inspect=boom)
    assert not ok and "穩定性" in msgs[0]
