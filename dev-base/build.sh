#!/usr/bin/env bash
# Build localhost/dev-base:py<ver> from local python slim images (no pull).
set -euo pipefail
cd "$(dirname "$0")"
for py in "${@:-3.10 3.11}"; do
  for v in $py; do
    podman build --pull=never --build-arg PY="$v" \
      --label "org.${CONTAINER_CTL_LABEL_NS:-container-ctl}.image-scope=dev-base" -t "localhost/dev-base:py$v" .
  done
done
