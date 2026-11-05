# Manual fallback steps (kept as a backup source)

These steps do by hand what a tool does. Each step has an anchor in the tool code.
The anchors and the rules of use are in `docs/ADR-0001-manual-fallback-anchors.md`.

Use a step here only when all of these conditions are true:

1. The tool is missing or the tool fails and you cannot find the cause.
2. The user approves the manual step in the current session.
3. You write a deviation record in the project `.scratch` directory. The record has the step ID, the reason, the command, and the output.
4. After the step, you run the tool again to make the state match (`prod doctor`, `image audit-prod`, `deploy verify`).

A manual step that does not obey these conditions is a major error.

## M1 Host directories (anchor: `host_prepare.apply_actions`)

```bash
mkdir <path>                                              # create only. Never delete or change existing data.
podman --remote=false unshare chown <uid>:<gid> <path>    # map the container user to the host subuid
```

## M2 Build the prod image (anchor: `image_prod.prepare_prod_image`)

```bash
git --git-dir <repo> archive <commit> | tar -x -C <context>
podman build --pull=never \
  --label org.container-ctl.source-commit=<commit> --label org.container-ctl.version=<version> \
  --label org.container-ctl.project=<project> --label org.container-ctl.service=<service> \
  --label org.container-ctl.image-scope=prod \
  -f <context>/Dockerfile.prod -t <image>:prepare-<commit12>-<random> <context>
```

Use `pip install --no-index --find-links $CONTAINER_CTL_WHEELHOUSE_DIR` in the Dockerfile. As of 2026-11-02 this directory does not exist yet; create it before use, and do not fall back to online pip.

## M3 Release tag and image pin (anchor: `image_prod.publish_prod_image`, `image_prod._commit_pin_to_config_repo`)

```bash
# 1. In a clone of the config repo, set "image" to <name>@sha256:<id> in .deploy/containers/<service>.json
# 2. Commit and push only that change.
git --git-dir <config-repo> -c user.name=container-ctl -c user.email=container-ctl@localhost \
  tag -a release/<service>/<version> <pin-commit> -m '<json record>'
```

The tag cannot be overwritten. Use a new version for a change.

## M4 Restart one service (anchor: `service.operate_manual_instance`)

```bash
systemctl --user restart <unit>.service
```

This step bypasses `state.db` and the governance check. Run `container-ctl deploy redeploy <project> HEAD` afterward to find drift.

## M5 Export an image (tool removed)

```bash
podman save sha256:<image-id> -o <file>.tar     # use the image ID. A local image has no RepoDigest.
```

## M6 Quadlet by hand (tool removed)

The `release export-quadlet` command has been removed. Do not write a Quadlet file from memory; use `container-ctl deploy plan/redeploy`, which renders units with `quadlet_gen`.

## M7 Secrets, certificates, tokens (no tool: the user owns the step)

The user creates these files in `$CONTAINER_CTL_VOLUMES_DIR/<project>/secrets/<env>`.
The agent does not read, print, create, or sign them. The command `prod doctor` checks only that the file exists and that the owner and mode are correct.
