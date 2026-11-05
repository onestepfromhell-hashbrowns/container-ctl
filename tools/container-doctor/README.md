# container-doctor

A small, dependency-free diagnostic that runs **inside** a container and reports whether the sandbox matches the container contract. It is independent of `container_ctl` (it never imports it) and is meant to be copied into, or mounted in, the container it inspects.

| Checker | What it verifies |
|---|---|
| `mounts` | the package root is mounted read-only; `/workspace` exists and is writable; declared mounts match |
| `boundary` | no host environment variables or host/WSL drive paths leak into the container; declared env matches |
| `ssh` | routing and proxy paths used for dynamic SSH credentials exist |
| `network` | network reachability expectations |
| `resource` | cgroup memory bound and CPU quota are set |

## Use

```bash
tools/container-doctor/bin/container-doctor check            # one-line summary
tools/container-doctor/bin/container-doctor check --diff     # only the mismatches, with fix commands
tools/container-doctor/bin/container-doctor check --json
tools/container-doctor/bin/container-doctor reconcile --config <file>
```

Exit status is non-zero when a check fails. `SKILL.md` describes the agent-oriented usage.

## Site paths

Every root it probes comes from an environment variable or a conf file (`KEY=VALUE` lines; first found of `$CONTAINER_DOCTOR_CONFIG`, `/etc/container-doctor/container-doctor.conf`, `~/.config/container-doctor/container-doctor.conf`; see `container-doctor.conf.template`). The built-in defaults are neutral; set your site's layout there.

| Key | Default |
|---|---|
| `CONTAINER_DOCTOR_OPT_ROOT` | `/opt/container-ctl` |
| `CONTAINER_DOCTOR_VAR_OPT_ROOT` | `/var/opt/container-ctl` |
| `CONTAINER_DOCTOR_ETC_OPT_ROOT` | `/etc/opt/container-ctl` |
| `CONTAINER_DOCTOR_PROJECTS_ROOT` | `/var/lib/container-ctl/projects` |
| `CONTAINER_DOCTOR_SHARED_ROOT` | `/var/lib/container-ctl/shared` |
| `CONTAINER_DOCTOR_RUNTIME_SUBDIR` | `container-ctl` (under `/run/user/<uid>/`) |
| `CONTAINER_DOCTOR_ROUTING_DIR` | `<VAR_OPT_ROOT>/runtime/routing` |
| `CONTAINER_DOCTOR_PROXY_SCRIPT` | `<OPT_ROOT>/bin/container-proxy.py` |
| `CONTAINER_DOCTOR_SCOPES_DIRS` | `<OPT_ROOT>/scopes` (`:`-separated) |

## Tests

```bash
cd tools/container-doctor && python3 -m pytest -q container_doctor
```
