# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report them privately through **GitHub Private Vulnerability
Reporting** on this repository
(`https://github.com/onestepfromhell-hashbrowns/container-ctl/security/advisories/new`).
Include a minimal reproduction, the affected component and commit, and the
impact you believe it has. You can expect an initial acknowledgement within 7
days.

## Threat Model & Deployment Assumptions

This project manages container lifecycles and isolated container execution on a
Linux host using rootless Podman and systemd Quadlet. It enforces policy as
configured by the operator; several guarantees depend on how the host is set up:

- **The ExecSpec caller is trusted.** `exec_run` validates the shape of a spec (absolute paths, no `..`, secret patterns, mount overlap, resource profiles), but it trusts the caller to supply an intended `image_digest`, `workspace`, and `command` / `argv`. Do not expose `python3 -m container_ctl.exec_run` directly to untrusted input.
- **`CONTAINER_CTL_EXEC_CRED_ROOTS` must be set in production.** When it is set, every `credential_files`/`env_secrets` source must resolve inside one of its colon-separated root directories (after `realpath`, defeating symlinks). When it is **unset**, any absolute host path readable by the worker user is accepted as a credential source. Always set `CONTAINER_CTL_EXEC_CRED_ROOTS` in production service units.
- **Rootless Podman + systemd user session** is the assumed runtime. Containers run with `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and per-profile memory/PID/CPU limits unless the caller's profile says otherwise.
- **`egress/egress_proxy.py`** is a minimal HTTP `CONNECT` domain-allowlist proxy intended to sit on an internal Podman network in front of session containers. It is **not** a hardened internet-facing proxy: it checks the `CONNECT host:port` authority against `egress-allow.txt` (port 443 only), does not inspect TLS SNI or terminate TLS, and relies on DNS resolution performed by the proxy host. Bind it only to the isolated container network interface, never to `0.0.0.0`.
- **Site paths** default to FHS locations under `/var/lib/container-ctl`, `/etc/container-ctl`, and `/run/container-ctl` and are configurable via environment variables or `/etc/container-ctl/container-ctl.conf` (see `deploy/container-ctl.conf.template`).

## Scope

In scope:
- Escape from the container / mount / credential boundaries enforced by `container_ctl/exec_run.py`, `declaration_guard.py`, or `quadlet_gen.py`.
- Bypass of `scan-path-refs` or `container_params` security checks (e.g. secret leak or path traversal rules) when used as documented.
- Arbitrary command execution or privilege escalation through `agent_exec.py` or `db_provision.py` beyond their documented trust model.

Out of scope:
- Vulnerabilities in Podman, systemd, the Linux kernel, or container base images themselves.
