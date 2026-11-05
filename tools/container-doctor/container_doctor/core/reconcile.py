"""
Declared configuration loader and reconciliation schema for Container Doctor.
Parses .deploy/manifest.json, .deploy/containers/*.json, and .scopes/*.json.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from container_doctor.core import site_paths


class DeclaredMount:
    __slots__ = ("src", "dst", "mode", "mount_type", "origin_file")

    def __init__(
        self,
        src: str,
        dst: str,
        mode: str = "ro",
        mount_type: str = "bind",
        origin_file: Optional[str] = None,
    ):
        self.src = src
        self.dst = dst
        self.mode = mode.lower()
        self.mount_type = mount_type
        self.origin_file = origin_file

    @property
    def is_ro(self) -> bool:
        return "ro" in self.mode

    @property
    def is_rw(self) -> bool:
        return "rw" in self.mode


class DeclaredResourceLimits:
    __slots__ = ("memory_limit_bytes", "cpu_quota_pct", "cpu_cores", "origin_file")

    def __init__(
        self,
        memory_limit_bytes: Optional[int] = None,
        cpu_quota_pct: Optional[float] = None,
        cpu_cores: Optional[float] = None,
        origin_file: Optional[str] = None,
    ):
        self.memory_limit_bytes = memory_limit_bytes
        self.cpu_quota_pct = cpu_quota_pct
        self.cpu_cores = cpu_cores
        self.origin_file = origin_file


class DeclaredConfig:
    __slots__ = (
        "project_name",
        "container_name",
        "workdir",
        "read_only_rootfs",
        "declared_mounts",
        "declared_env",
        "declared_resources",
        "declared_ports",
        "sources_loaded",
    )

    def __init__(
        self,
        project_name: Optional[str] = None,
        container_name: Optional[str] = None,
        workdir: Optional[str] = None,
        read_only_rootfs: Optional[bool] = None,
        declared_mounts: Optional[List[DeclaredMount]] = None,
        declared_env: Optional[Dict[str, str]] = None,
        declared_resources: Optional[DeclaredResourceLimits] = None,
        declared_ports: Optional[List[str]] = None,
        sources_loaded: Optional[List[str]] = None,
    ):
        self.project_name = project_name
        self.container_name = container_name
        self.workdir = workdir
        self.read_only_rootfs = read_only_rootfs
        self.declared_mounts = declared_mounts if declared_mounts is not None else []
        self.declared_env = declared_env if declared_env is not None else {}
        self.declared_resources = declared_resources
        self.declared_ports = declared_ports if declared_ports is not None else []
        self.sources_loaded = sources_loaded if sources_loaded is not None else []


def _parse_size_bytes(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val = val.strip().lower()
        if val in ("", "max", "unlimited"):
            return None
        units = {
            "k": 1024,
            "kb": 1024,
            "m": 1024 * 1024,
            "mb": 1024 * 1024,
            "g": 1024 * 1024 * 1024,
            "gb": 1024 * 1024 * 1024,
        }
        for unit, mult in units.items():
            if val.endswith(unit):
                num_part = val[: -len(unit)].strip()
                try:
                    return int(float(num_part) * mult)
                except ValueError:
                    return None
        try:
            return int(val)
        except ValueError:
            return None
    return None


def load_declared_config(
    workspace_path: Path = Path("/workspace"),
    config_path: Optional[str] = None,
    deep_scan: bool = False,
) -> DeclaredConfig:
    """
    Discover and parse declared configuration from:
    1. Explicit config path (if specified)
    2. .deploy/manifest.json and .deploy/containers/*.json or deploy/*.json
    3. .scopes/*.json (workspace and system/biz scopes if deep_scan or configured)
    """
    config = DeclaredConfig()
    sources: List[str] = []

    def parse_container_json(fpath: Path):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            sources.append(str(fpath))

            if not config.project_name and "PROJECT_NAME" in data.get("env", {}):
                config.project_name = data["env"]["PROJECT_NAME"]
            if not config.project_name and "project_name" in data:
                config.project_name = data["project_name"]
            if not config.container_name and "name" in data:
                config.container_name = data["name"]
            if not config.workdir and "workdir" in data:
                config.workdir = data["workdir"]
            if config.read_only_rootfs is None and "read_only_rootfs" in data:
                config.read_only_rootfs = bool(data["read_only_rootfs"])

            if "env" in data and isinstance(data["env"], dict):
                for k, v in data["env"].items():
                    if isinstance(v, str):
                        config.declared_env[k] = v

            if "mounts" in data and isinstance(data["mounts"], list):
                for m in data["mounts"]:
                    if isinstance(m, dict):
                        src = m.get("source", "")
                        dst = m.get("target", "")
                        opts = m.get("options", "ro")
                        mtype = m.get("type", "bind")
                        if dst:
                            config.declared_mounts.append(
                                DeclaredMount(src=src, dst=dst, mode=opts, mount_type=mtype, origin_file=str(fpath))
                            )

            if "volumes" in data and isinstance(data["volumes"], list):
                for v in data["volumes"]:
                    if isinstance(v, str):
                        parts = v.split(":")
                        src = parts[0]
                        dst = parts[1] if len(parts) > 1 else parts[0]
                        mode = parts[2] if len(parts) > 2 else "rw"
                        config.declared_mounts.append(
                            DeclaredMount(src=src, dst=dst, mode=mode, mount_type="volume", origin_file=str(fpath))
                        )

            if "resources" in data or "memory" in data or "cpu" in data or "memory_limit" in data:
                res_data = data.get("resources", {})
                mem_val = data.get("memory_limit") or data.get("memory") or res_data.get("memory")
                cpu_val = data.get("cpus") or data.get("cpu") or res_data.get("cpus")
                mem_bytes = _parse_size_bytes(mem_val)
                cpu_cores = float(cpu_val) if cpu_val and str(cpu_val).replace(".", "").isdigit() else None
                config.declared_resources = DeclaredResourceLimits(
                    memory_limit_bytes=mem_bytes,
                    cpu_cores=cpu_cores,
                    origin_file=str(fpath),
                )

            if "ports" in data and isinstance(data["ports"], list):
                for p in data["ports"]:
                    config.declared_ports.append(str(p))

        except Exception:
            pass

    def parse_scope_json(fpath: Path):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            sources.append(str(fpath))

            items = data.get("include_items", [])
            for item in items:
                if isinstance(item, dict):
                    src = item.get("src", "")
                    dst_fhs = item.get("dst_fhs", "")
                    mode = item.get("mode", "ro")
                    itype = item.get("type", "mount")
                    if dst_fhs:
                        config.declared_mounts.append(
                            DeclaredMount(src=src, dst=dst_fhs, mode=mode, mount_type=itype, origin_file=str(fpath))
                        )
                    for step in item.get("pipeline", []):
                        cfg = step.get("config", {})
                        if "target" in cfg and "src" in cfg:
                            config.declared_mounts.append(
                                DeclaredMount(src=cfg["src"], dst=cfg["target"], mode="ro", mount_type="symlink", origin_file=str(fpath))
                            )
                        elif "dst" in cfg and step.get("module") == "copy_inject":
                            config.declared_mounts.append(
                                DeclaredMount(src=src, dst=cfg["dst"], mode=mode, mount_type="copy_inject", origin_file=str(fpath))
                            )
        except Exception:
            pass

    # 1. If explicit config path passed
    if config_path:
        cp = Path(config_path)
        if cp.is_file():
            if "scope" in cp.name.lower():
                parse_scope_json(cp)
            else:
                parse_container_json(cp)
        elif cp.is_dir():
            for p in cp.glob("**/*.json"):
                if "scope" in p.name.lower():
                    parse_scope_json(p)
                else:
                    parse_container_json(p)
        config.sources_loaded = sources
        return config

    # 2. Local workspace deployment declarations
    deploy_dirs = [
        workspace_path / ".deploy",
        Path.cwd() / ".deploy",
        workspace_path / "deploy",
        Path.cwd() / "deploy",
    ]
    for ddir in deploy_dirs:
        if ddir.exists() and ddir.is_dir():
            try:
                for jf in ddir.iterdir():
                    if jf.is_file() and jf.name.endswith(".json"):
                        parse_container_json(jf)
            except Exception:
                pass

    # 3. Local workspace scopes or runtime injection scopes
    scope_dirs = [
        workspace_path / ".scopes",
        Path.cwd() / ".scopes",
        Path("/run/ext"),
    ]
    if deep_scan:
        scope_dirs.extend([
            *(Path(d) for d in site_paths.SCOPES_DEEP_DIRS),
        ])

    for sdir in scope_dirs:
        if sdir.exists() and sdir.is_dir():
            try:
                for sf in sdir.glob("**/*.json") if deep_scan else sdir.iterdir():
                    if sf.is_file() and sf.name.endswith(".json"):
                        parse_scope_json(sf)
            except Exception:
                pass

    config.sources_loaded = sources
    return config
