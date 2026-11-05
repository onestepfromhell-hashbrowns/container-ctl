from container_ctl.image_checks import image_findings


def test_image_findings():
    ok = {"Config": {"WorkingDir": "/opt/proj-x", "Env": ["A=1"], "Cmd": ["python"]}}
    assert image_findings(ok, "s") == []
    bad = {"Config": {"WorkingDir": "/workspace", "Env": ["APP_ROOT=/workspace"], "Cmd": ["python", "-m", "debugpy"]}}
    msgs = "\n".join(image_findings(bad, "s"))
    assert msgs.count("[ERROR]") == 3
    assert "[WARN]" in image_findings({"Config": {"WorkingDir": "/"}}, "s")[0]


def test_upstream_image_skips_workdir_warning():
    assert image_findings({"Config": {"WorkingDir": "/"}}, "pg", check_workdir=False) == []
    assert image_findings({"Config": {"WorkingDir": "/workspace"}}, "pg", check_workdir=False)[0].startswith("[ERROR]")
