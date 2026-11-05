# Container Agent Constitution

## Runtime Boundary
- **Scope**: Unprivileged rootless container sandbox (`WORKDIR /workspace`).
- **Terminal Defaults**: `PAGER=cat CI=true DEBIAN_FRONTEND=noninteractive`, `PYTHONUNBUFFERED=1`.
- **Host Isolation**: You execute **inside** the container. Never invoke host orchestrators (`podman`, `docker`, `systemctl`, `container-ctl`, `backup-ctl`, `ssh-entry`).

## In-Container Tooling
| Target | Command / Path | Purpose |
|---|---|---|
| **Process Control** | `./control.sh {start|stop|restart|status|probe|logs}` | Tier-2 daemon lifecycle; enforces triple-verification (PID + Port + Probe). |
| **Path Gate** | `scan-path-refs` | Static analyzer catching hardcoded `/workspace`, `/opt`, or `/home` paths. |
| **Diagnostics** | `container-doctor [--minimal]` | Asserts sandbox health, mount bindings, leakages, and memory ceilings. |
| **Path Resolver** | `/workspace/lib/standards/app_paths.*` | Multi-language FHS path resolution contract templates (Py/Sh/JS/Go). |

## Non-Negotiable Invariants
1. **Zero Hardcoded Paths**: Never embed `/workspace`, `/opt/<proj>`, or `/home/*` in source or configs. Resolve paths via `<SERVICE>_*` env vars. Fallback defaults **must** target Linux FHS (`/etc/opt/<proj>`, `/var/opt/<proj>`), never dev mounts.
2. **Environment Blindness**: Application code must remain tier-agnostic. Never branch on runtime profiles (e.g., `if env == 'dev'`). Drive all behavior via injected configuration.
3. **Deterministic Daemons**: Raw background spawns (`nohup`, `&`, `screen`, `tmux`) are prohibited. Services must be declared in `configs/service_control.json` and orchestrated strictly via `./control.sh`.
4. **Read-Only Rootfs Discipline**: Treat container rootfs as immutable. Confine transient and stateful writes exclusively to designated volumes (`/tmp`, `/var/opt/<proj>`).

## Gate Verification Loop
Run before declaring task completion:
```bash
scan-path-refs && pytest -q --tb=short && ./control.sh probe && container-doctor --minimal
```
