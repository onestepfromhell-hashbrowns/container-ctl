# Changelog

All notable changes to `container-ctl` are documented in this file.

## [0.1.0] - 2026-11-05

### Added
- **Post-Run Execution Checks**: `exec_run` runs declared `checks` sequentially in an isolated `--network=none`, `--read-only`, `--cap-drop=ALL` container against the post-run workspace before computing the final git patch and before destroying the scratch copy.
- **Liveness Probe (`--alive EXEC_ID`)**: Added `exec_alive()` and CLI `--alive` flag for workspace lease liveness verification (`alive`, `dead`, `unknown`).
- **Session Lifecycle (`ExecSpec v2`)**: Added `session_open`, `session_exec`, `session_close`, and `session_sample` actions with strict environment allowlists, credential tmpfs isolation, binary SHA-256 verification, and watch-only `/proc/net/tcp*` egress sampling.
- **Configurable Label Namespace (`CONTAINER_CTL_LABEL_NS`)**: Governance, port-forward, OCI release, and Dockerfile directive labels now read their namespace prefix from `CONTAINER_CTL_LABEL_NS` (env or `/etc/container-ctl/container-ctl.conf`), defaulting to `container-ctl`. Validated against `^[a-z0-9][a-z0-9._-]*$`.
- **`container-doctor` Configuration**: Added `/etc/container-doctor/container-doctor.conf` support and `tools/container-doctor/container-doctor.conf.template`.

### Changed
- Standardized all FHS default paths under `/var/lib/container-ctl`, `/etc/container-ctl`, `/run/container-ctl`, and `/opt/container-ctl`.
- Standardized all environment variable names under the `CONTAINER_CTL_*` prefix (`CONTAINER_CTL_HOST_AGENT`, `CONTAINER_CTL_DEPLOYMENTS_DB`, `CONTAINER_CTL_EXEC_CRED_ROOTS`, `CONTAINER_CTL_EXEC_EGRESS_LOG`, `CONTAINER_CTL_EXEC_STATE_ROOT`, `CONTAINER_CTL_EXEC_SCRATCH`, `CONTAINER_CTL_SCOPES_DIR`, `CONTAINER_CTL_SCAN_PATH_REFS`, `CONTAINER_CTL_GOVERNANCE_LEDGER`).
- Standardized execution container naming (`cctl-exec-*`), scratch workspace prefixes (`cctl-rw-*`), and execution labels (`container-ctl.run`, `container-ctl.exec`, `container-ctl.session`, `container-ctl.exec_id`).
