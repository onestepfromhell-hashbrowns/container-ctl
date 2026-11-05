"""The label namespace comes from CONTAINER_CTL_LABEL_NS (env or conf file); nothing site-specific is built in."""
import os
import subprocess
import sys

from container_ctl import paths

CODE = ("from container_ctl import paths, portfwd;"
        "print(paths.LABEL_NS, paths.OCI_LABEL_NS, paths.GOV_LABEL, paths.PORTFWD_LABEL);"
        "print(bool(portfwd._LISTEN_LABEL_RE.search('Label=' + paths.PORTFWD_LABEL + '=9443')))")


def _run(env_extra, conf=None, tmp_path=None):
    env = {**os.environ, **env_extra}
    env.pop("CONTAINER_CTL_LABEL_NS", None) if "CONTAINER_CTL_LABEL_NS" not in env_extra else None
    if conf is not None:
        f = tmp_path / "c.conf"
        f.write_text(conf)
        env["CONTAINER_CTL_CONFIG"] = str(f)
    return subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, env=env)


def test_default_namespace_is_neutral():
    assert paths.LABEL_NS == "container-ctl"
    assert paths.GOV_LABEL == "container-ctl.governance.id" and paths.OCI_LABEL_NS == "org.container-ctl"


def test_env_and_conf_override(tmp_path):
    r = _run({"CONTAINER_CTL_LABEL_NS": "example.site"})
    assert r.stdout.split("\n")[:2] == ["example.site org.example.site example.site.governance.id example.site.portfwd.listen", "True"]
    r = _run({}, conf="CONTAINER_CTL_LABEL_NS=acme\n", tmp_path=tmp_path)
    assert r.stdout.startswith("acme org.acme acme.governance.id")


def test_invalid_namespace_is_rejected():
    r = _run({"CONTAINER_CTL_LABEL_NS": "Bad NS="})
    assert r.returncode != 0 and "CONTAINER_CTL_LABEL_NS" in r.stderr
