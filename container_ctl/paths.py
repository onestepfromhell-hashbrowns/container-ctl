"""Site paths of container-ctl. The package names no site: every location comes from here.

Resolution order for each setting: environment variable > conf file > native FHS default.
Conf file (first one that exists): $CONTAINER_CTL_CONFIG, /etc/container-ctl/container-ctl.conf,
~/.config/container-ctl/container-ctl.conf. Format: KEY=VALUE per line, '#' comments, optional quotes;
nothing is evaluated. A site installs its values there; see deploy/container-ctl.conf.template.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_CONF_CANDIDATES = (
    "/etc/container-ctl/container-ctl.conf",
    os.path.join(os.path.expanduser("~"), ".config/container-ctl/container-ctl.conf"),
)


def _read_conf() -> dict[str, str]:
    explicit = os.environ.get("CONTAINER_CTL_CONFIG")
    for cand in ((explicit,) if explicit else _CONF_CANDIDATES):
        try:
            text = Path(cand).read_text(encoding="utf-8")
        except OSError:
            continue
        out: dict[str, str] = {}
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


def setting(key: str, default: str) -> str:
    """Environment variable, then conf file, then the given default (empty values count as unset)."""
    return os.environ.get(key) or _CONF.get(key) or default


def setting_list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Same as setting(); the value is a ':'-separated list."""
    raw = os.environ.get(key) or _CONF.get(key)
    return tuple(p for p in raw.split(":") if p) if raw else tuple(default)


DATA_ROOT = setting("CONTAINER_CTL_DATA_ROOT", "/var/lib/container-ctl")

# Label namespace for the labels container-ctl writes and reads (Quadlet units, images, declarations):
#   <LABEL_NS>.governance.id, <LABEL_NS>.portfwd.listen, org.<LABEL_NS>.{project,service,version,...}
# and the Dockerfile directive `# <LABEL_NS>: squash-all`. A site that already labelled its containers sets its own value.
LABEL_NS = setting("CONTAINER_CTL_LABEL_NS", "container-ctl")
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", LABEL_NS):
    raise ValueError(f"CONTAINER_CTL_LABEL_NS: lowercase letters, digits, '.' and '-' only (got {LABEL_NS!r})")
OCI_LABEL_NS = f"org.{LABEL_NS}"
GOV_LABEL = f"{LABEL_NS}.governance.id"
PORTFWD_LABEL = f"{LABEL_NS}.portfwd.listen"


def _dir(key: str, sub: str) -> str:
    return setting(key, os.path.join(DATA_ROOT, sub))


SERVICES_DIR = _dir("CONTAINER_CTL_SERVICES_DIR", "services")            # Quadlet units + <project>/ext-default.json
DEPLOYS_DIR = _dir("CONTAINER_CTL_DEPLOYS_DIR", "deploys")               # blueprints, stage and report roots
GIT_REPOS_DIR = _dir("CONTAINER_CTL_GIT_REPOS_DIR", "git-repos")         # bare repos (read only)
CONFIG_REPOS_DIR = _dir("CONTAINER_CTL_CONFIG_REPOS_DIR", "config-repos")
WORKTREES_DIR = _dir("CONTAINER_CTL_WORKTREES_DIR", "worktrees")
ENVIRONMENTS_DIR = _dir("CONTAINER_CTL_ENVIRONMENTS_DIR", "environments")
SCOPES_DIR = setting("CONTAINER_CTL_SCOPES_DIR", os.path.join(DATA_ROOT, "scopes"))  # extension scope catalog (layers system/biz/solution/preset)
VOLUMES_DIR = _dir("CONTAINER_CTL_VOLUMES_DIR", "volumes")               # persistent volumes
VOLUME_BACKUPS_DIR = _dir("CONTAINER_CTL_VOLUME_BACKUPS_DIR", "volume-backups")
DB_BACKUPS_DIR = _dir("CONTAINER_CTL_DB_BACKUPS_DIR", "db-backups")      # pre-upgrade dumps
PG_BACKUPS_DIR = _dir("CONTAINER_CTL_PG_BACKUPS_DIR", "pg-backups")
WHEELHOUSE_DIR = _dir("CONTAINER_CTL_WHEELHOUSE_DIR", "wheelhouse")      # offline python wheels
OCI_STORE = _dir("CONTAINER_CTL_OCI_STORE", "oci-store")
PROJECTS_CODE_ROOT = _dir("CONTAINER_CTL_PROJECTS_CODE_ROOT", "projects")  # per-project data root (<root>/<project>/...)
HERDR_SOCKET_ROOT = setting("CONTAINER_CTL_HERDR_SOCKET_ROOT", "/run/container-ctl/herdr")
STATE_DB = setting("CONTAINER_CTL_STATE_DB", os.path.join(SERVICES_DIR, "state.db"))
BROKER_CONFIG = setting("CONTAINER_CTL_BROKER_CONFIG", os.path.join(SERVICES_DIR, "infra", "broker-config.json"))

# Guards. A site lists its own code/data roots here; the native defaults are the narrowest safe set.
MOUNT_SRC_PREFIXES = setting_list("CONTAINER_CTL_MOUNT_SRC_PREFIXES", (DATA_ROOT,))          # allowed Extension Mount sources
MOUNT_READ_ROOTS = setting_list("CONTAINER_CTL_MOUNT_READ_ROOTS", (DATA_ROOT, "/var/lib/extscope"))
AUTO_CREATE_PREFIXES = setting_list("CONTAINER_CTL_AUTO_CREATE_PREFIXES", (DATA_ROOT,))      # rw mount sources that may be created
FORBIDDEN_WRITE_PATHS = setting_list("CONTAINER_CTL_FORBIDDEN_WRITE_PATHS", (GIT_REPOS_DIR,))
FORBIDDEN_MOUNT_PREFIXES = setting_list("CONTAINER_CTL_FORBIDDEN_MOUNT_PREFIXES", ("/home",))
PREFLIGHT_MOUNTS = setting_list("CONTAINER_CTL_PREFLIGHT_MOUNTS", (DATA_ROOT,))
