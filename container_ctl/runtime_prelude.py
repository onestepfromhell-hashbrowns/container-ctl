#!/usr/bin/env python3
"""Runtime defense prelude for container-ctl tooling and subshell execution.

This module provides a fail-safe, non-interactive environment baseline across
all CLI entrypoints, preventing subprocesses and Git operations from launching
interactive pagers or blocking on unreadable stdin streams.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

DEFENSIVE_ENV: Final[Mapping[str, str]] = {
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "CI": "true",
    "DEBIAN_FRONTEND": "noninteractive",
}

__all__ = ["DEFENSIVE_ENV", "bootstrap_runtime_defense"]


def bootstrap_runtime_defense(
    custom_overrides: Mapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Ensure headless, non-interactive defense invariants for subshell execution.

    Invariants guaranteed (via setdefault, preserving caller-provided non-empty env):
    - PAGER: "cat" (prevents less/more terminal pager hangs)
    - GIT_PAGER: "cat" (prevents git log/diff pager allocation in subshells)
    - CI: "true" (tells interactive tools to run headless)
    - DEBIAN_FRONTEND: "noninteractive" (prevents apt/dpkg interactive prompts)

    Args:
        custom_overrides: Optional dictionary of environment overrides.

    Returns:
        The active defensive environment mapping currently in os.environ.
    """
    for key, val in DEFENSIVE_ENV.items():
        os.environ.setdefault(key, val)

    if custom_overrides:
        for key, val in custom_overrides.items():
            os.environ[key] = val

    return {k: os.environ.get(k, "") for k in DEFENSIVE_ENV}


if __name__ == "__main__":
    import json

    active = bootstrap_runtime_defense()
    print(json.dumps(active, indent=2))
