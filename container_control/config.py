"""
Configuration module for container service control.
Loads and resolves universal service configurations from JSON files or defaults.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ServiceConfig:
    name: str = "app"
    command: List[str] = field(default_factory=list)
    working_dir: str = "."
    port: Optional[int] = 8080
    pid_file: str = "app.pid"
    log_file: str = "logs/app.log"
    shutdown_timeout: float = 5.0
    pre_start_hooks: List[str] = field(default_factory=list)
    post_start_hooks: List[str] = field(default_factory=list)
    pre_stop_hooks: List[str] = field(default_factory=list)
    readiness_probe: Optional[Dict[str, Any]] = None
    env: Dict[str, str] = field(default_factory=dict)

    # Legacy fields for backward compatibility
    entry_point: Optional[str] = None
    python_executable: Optional[str] = None

    # Resolved absolute paths
    root_dir: Path = field(default_factory=Path.cwd)
    abs_entry_point: Path = field(init=False)
    abs_pid_file: Path = field(init=False)
    abs_log_file: Path = field(init=False)
    abs_working_dir: Path = field(init=False)

    def __post_init__(self):
        self.root_dir = Path(self.root_dir).resolve()
        self.abs_working_dir = (self.root_dir / self.working_dir).resolve()
        self.abs_pid_file = (self.root_dir / self.pid_file).resolve()
        self.abs_log_file = (self.root_dir / self.log_file).resolve()

        # Handle command vs legacy entry_point
        if not self.command:
            py_exe = self.python_executable or "python3"
            ep = self.entry_point or "scripts/app.py"
            self.command = [py_exe, ep]

        if not self.entry_point:
            if len(self.command) >= 2 and self.command[0] in ("python", "python3", "python3.11", "python3.12"):
                self.entry_point = self.command[1]
            elif self.command:
                self.entry_point = self.command[0]
            else:
                self.entry_point = "scripts/app.py"

        self.abs_entry_point = (self.root_dir / self.entry_point).resolve()

        if self.port is not None:
            try:
                self.port = int(self.port)
            except (ValueError, TypeError):
                self.port = None

        if self.shutdown_timeout is not None:
            try:
                self.shutdown_timeout = float(self.shutdown_timeout)
            except (ValueError, TypeError):
                self.shutdown_timeout = 5.0


def find_config_file(search_dir: Optional[Path] = None) -> Optional[Path]:
    """Searches for service configuration file in standard locations."""
    base = Path(search_dir or Path.cwd()).resolve()
    candidates = [
        base / "configs" / "service_control.json",
        base / "configs" / "config.json",
        base / "service_control.json",
        base / "config.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_service_config(config_path: Optional[str | Path] = None, base_dir: Optional[str | Path] = None) -> ServiceConfig:
    """
    Loads service configuration from file or returns defaults anchored at base_dir.
    """
    root = Path(base_dir or Path.cwd()).resolve()

    target_path: Optional[Path] = None
    if config_path:
        p = Path(config_path)
        target_path = p if p.is_absolute() else (root / p).resolve()
    else:
        target_path = find_config_file(root)

    data: Dict[str, Any] = {}
    if target_path and target_path.is_file():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                # Support nested "service" section or top-level keys
                data = raw.get("service", raw)
                for top_key in ("python_executable", "command", "readiness_probe", "pre_start_hooks", "post_start_hooks", "pre_stop_hooks"):
                    if top_key in raw and top_key not in data:
                        data[top_key] = raw[top_key]
        except Exception:
            pass

    cmd_val = data.get("command")
    if isinstance(cmd_val, str):
        cmd_list = [cmd_val]
    elif isinstance(cmd_val, list):
        cmd_list = [str(x) for x in cmd_val]
    else:
        cmd_list = []

    return ServiceConfig(
        name=str(data.get("name", "app")),
        command=cmd_list,
        working_dir=str(data.get("working_dir", ".")),
        port=data.get("port", 8080),
        entry_point=data.get("entry_point"),
        python_executable=data.get("python_executable"),
        pid_file=str(data.get("pid_file", "app.pid")),
        log_file=str(data.get("log_file", "logs/app.log")),
        shutdown_timeout=data.get("shutdown_timeout", 5.0),
        pre_start_hooks=list(data.get("pre_start_hooks", [])),
        post_start_hooks=list(data.get("post_start_hooks", [])),
        pre_stop_hooks=list(data.get("pre_stop_hooks", [])),
        readiness_probe=data.get("readiness_probe"),
        env=dict(data.get("env", {})),
        root_dir=root,
    )
