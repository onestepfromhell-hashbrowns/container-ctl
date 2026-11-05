# Containerized Herdr service contract

This document defines the site boundary around an external Herdr build.
The Herdr repository remains the source of truth for its command-line and IPC
wire protocol; this contract only defines how site starts and isolates it.

## Service declaration

The service blueprint must use a pinned image digest and set:

```json
{
  "name": "herdr",
  "scope": "shared",
  "tier": "infra",
  "image": "<registry>/<herdr>@sha256:<digest>",
  "command": ["herdr", "serve"],
  "herdr_control_plane": true,
  "podman_socket": {
    "host_path": "/run/user/<podman-uid>/podman/podman.sock",
    "container_path": "/run/podman/podman.sock",
    "uid": "<podman-uid>"
  },
  "herdr_socket_dir": "$CONTAINER_CTL_HERDR_SOCKET_ROOT/<project>",
  "read_only_rootfs": true,
  "tmpfs": ["/run/herdr:size=32m,mode=0700"],
  "health_cmd": ["herdr", "health", "--socket", "/run/herdr/herdr.sock"],
  "memory_max": "512M",
  "memory_swap_max": "512M",
  "lifecycle_policy": {"mode": "always_on"}
}
```

`podman_socket` is deliberately a service-level creation-time option. It is
never emitted into an agent container or accepted through a generic Extension
mount. Herdr runs as the rootless Podman user and receives no Linux
capabilities; access to the user Podman API is the only control-plane
authority.

The project Extension Scope `solution-herdr-agent-runtime` gives agents a
separate `/run/herdr` socket directory and injects:

```text
HERDR_SOCKET=/run/herdr/herdr.sock
```

Agents must not set `CONTAINER_HOST` to the Herdr or Podman socket. The
external Herdr pinned ref must provide the actual `serve`, `health`, and agent
registration semantics before an end-to-end deployment is enabled.
