"""
Execution context detection and environment representation.
Optimized for ultra-fast startup without heavy inspect/dataclasses overhead.
"""

from enum import Enum
import os
from pathlib import Path
from typing import Dict, Optional

from container_doctor.core import site_paths
from container_doctor.core.reconcile import DeclaredConfig, load_declared_config


class ExecutionMode(str, Enum):
    INSIDE = "inside"
    HOST = "host"
    AUTO = "auto"


def is_running_in_container() -> bool:
    """
    Check if the current process is running inside a container.
    Podman creates /run/.containerenv
    Docker creates /.dockerenv
    """
    if Path("/run/.containerenv").exists():
        return True
    if Path("/.dockerenv").exists():
        return True
    if os.environ.get("container") in ("podman", "docker"):
        return True
    return False


class ExecutionContext:
    __slots__ = (
        "mode",
        "is_container",
        "target_container",
        "env",
        "workspace_path",
        "opt_lab_path",
        "declared_config",
    )

    def __init__(
        self,
        mode: ExecutionMode,
        is_container: bool,
        target_container: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        workspace_path: Path = Path("/workspace"),
        opt_lab_path: Path = Path(site_paths.OPT_ROOT),
        declared_config: Optional[DeclaredConfig] = None,
    ):
        self.mode = mode
        self.is_container = is_container
        self.target_container = target_container
        self.env = env if env is not None else dict(os.environ)
        self.workspace_path = workspace_path
        self.opt_lab_path = opt_lab_path
        self.declared_config = declared_config


def detect_runtime_environment(
    forced_mode: ExecutionMode = ExecutionMode.AUTO,
    target_container: Optional[str] = None,
    config_path: Optional[str] = None,
    workspace_path: Path = Path("/workspace"),
    load_declarations: bool = True,
) -> ExecutionContext:
    """
    Detect or resolve execution context and declared config.
    """
    in_container = is_running_in_container()

    if forced_mode == ExecutionMode.INSIDE:
        mode = ExecutionMode.INSIDE
    elif forced_mode == ExecutionMode.HOST:
        mode = ExecutionMode.HOST
    else:
        # Auto-detect
        if target_container:
            mode = ExecutionMode.HOST
        elif in_container:
            mode = ExecutionMode.INSIDE
        else:
            mode = ExecutionMode.HOST

    declared_cfg = (
        load_declared_config(workspace_path=workspace_path, config_path=config_path)
        if load_declarations
        else DeclaredConfig()
    )

    return ExecutionContext(
        mode=mode,
        is_container=in_container,
        target_container=target_container,
        env=dict(os.environ),
        workspace_path=workspace_path,
        opt_lab_path=Path(site_paths.OPT_ROOT),
        declared_config=declared_cfg,
    )
