"""Site paths probed by container-doctor.

Resolution for each setting: environment variable > conf file > neutral default.
Conf file (first that exists): $CONTAINER_DOCTOR_CONFIG, /etc/container-doctor/container-doctor.conf,
~/.config/container-doctor/container-doctor.conf. Format: KEY=VALUE per line, '#' comments, optional quotes;
nothing is evaluated. The defaults name no site; a site sets its own layout there.

container-doctor runs inside containers and stays standalone, so it does not import container_ctl.paths.
"""
import os
from pathlib import Path

_CONF_CANDIDATES = (
    "/etc/container-doctor/container-doctor.conf",
    os.path.join(os.path.expanduser("~"), ".config/container-doctor/container-doctor.conf"),
)


def _read_conf() -> dict:
    explicit = os.environ.get("CONTAINER_DOCTOR_CONFIG")
    for cand in ((explicit,) if explicit else _CONF_CANDIDATES):
        try:
            text = Path(cand).read_text(encoding="utf-8")
        except OSError:
            continue
        out = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            out[key.strip()] = val
        return out
    return {}


_CONF = _read_conf()


def _env(key: str, default: str) -> str:
    return os.environ.get(key) or _CONF.get(key) or default


def _env_list(key: str, default: tuple) -> tuple:
    raw = os.environ.get(key) or _CONF.get(key)
    return tuple(p for p in raw.split(":") if p) if raw else tuple(default)


OPT_ROOT = _env("CONTAINER_DOCTOR_OPT_ROOT", "/opt/container-ctl")                 # read-only site package mount
VAR_OPT_ROOT = _env("CONTAINER_DOCTOR_VAR_OPT_ROOT", "/var/opt/container-ctl")
ETC_OPT_ROOT = _env("CONTAINER_DOCTOR_ETC_OPT_ROOT", "/etc/opt/container-ctl")
PROJECTS_ROOT = _env("CONTAINER_DOCTOR_PROJECTS_ROOT", "/var/lib/container-ctl/projects")
SHARED_ROOT = _env("CONTAINER_DOCTOR_SHARED_ROOT", "/var/lib/container-ctl/shared")
RUNTIME_SUBDIR = _env("CONTAINER_DOCTOR_RUNTIME_SUBDIR", "container-ctl")          # under /run/user/<uid>/

ROUTING_DIR = _env("CONTAINER_DOCTOR_ROUTING_DIR", VAR_OPT_ROOT + "/runtime/routing")
PROXY_SCRIPT = _env("CONTAINER_DOCTOR_PROXY_SCRIPT", OPT_ROOT + "/bin/container-proxy.py")
SCOPES_DEEP_DIRS = _env_list("CONTAINER_DOCTOR_SCOPES_DIRS", (OPT_ROOT + "/scopes",))
