# Shared dev toolchain base for containerised projects (tools only; no project files).
# Build: ./build.sh   Used by .deploy/dev-images/<svc>/Dockerfile.dev as FROM localhost/dev-base:py<ver>
ARG PY=3.12
FROM docker.io/library/python:${PY}-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget jq git build-essential pkg-config \
    libssl-dev libffi-dev procps iproute2 socat \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pytest==8.3.4

RUN groupadd -g 1000 devuser && \
    useradd -u 1000 -g 1000 -m -s /bin/bash devuser && \
    mkdir -p /workspace && chown 1000:1000 /workspace

USER 1000:1000
WORKDIR /workspace
