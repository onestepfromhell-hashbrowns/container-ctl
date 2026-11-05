"""
TICKET-006: Quadlet 單元生成器
依 Blueprint 在 .services/<project>/ 生成 Quadlet 單元與 Drop-In Override，
並建立 symlink 至 systemd user Quadlet 掃描目錄。
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.lifecycle_adapter import get_default_manager
from container_ctl.governance_compiler import (
    GovernanceManifest,
    WorkloadGovernanceMapping,
    compile_governance,
)
from container_ctl.volume_resolver import resolve_volume_mount
from container_ctl.image_volume_guard import runtime_image_ref
from container_ctl import paths

DEFAULT_SERVICES_DIR = paths.SERVICES_DIR
# systemd --user Quadlet 掃描目錄
SYSTEMD_QUADLET_DIR = Path.home() / ".config/containers/systemd"
VOLUMES_DIR = paths.VOLUMES_DIR


ALLOWED_MOUNT_SRC_PREFIXES = tuple(p.rstrip("/") + "/" for p in paths.MOUNT_SRC_PREFIXES)

_PODMAN_SOCKET_RE = re.compile(r"^/run/user/(?P<uid>[1-9][0-9]*)/podman/podman\.sock$")
_CAPABILITY_RE = re.compile(r"^[A-Z0-9_]+$")


def _append_herdr_runtime_config(lines: list[str], config: dict[str, Any]) -> None:
    """Render the deliberately narrow Herdr control-plane contract.

    Herdr may access the rootless Podman API, but agents must never receive
    that socket.  The socket is therefore an explicit service-level option,
    not a generic Extension mount.  Validation stays here so malformed or
    privileged declarations cannot silently reach Quadlet.
    """
    if not config.get("herdr_control_plane", False):
        return

    socket_cfg = config.get("podman_socket")
    if not isinstance(socket_cfg, dict):
        raise ValueError("herdr_control_plane requires podman_socket object")

    host_path = socket_cfg.get("host_path")
    container_path = socket_cfg.get("container_path", "/run/podman/podman.sock")
    if not isinstance(host_path, str) or not _PODMAN_SOCKET_RE.fullmatch(host_path):
        raise ValueError(
            "Herdr podman_socket.host_path must be /run/user/<uid>/podman/podman.sock"
        )
    if container_path != "/run/podman/podman.sock":
        raise ValueError("Herdr Podman socket destination is fixed at /run/podman/podman.sock")

    uid = int(_PODMAN_SOCKET_RE.fullmatch(host_path).group("uid"))
    declared_uid = socket_cfg.get("uid", uid)
    if declared_uid != uid:
        raise ValueError("podman_socket.uid must match host_path owner UID")

    lines.append(f"Volume={host_path}:{container_path}:ro")
    socket_dir = config.get("herdr_socket_dir")
    if not isinstance(socket_dir, str) or not re.fullmatch(
        re.escape(paths.HERDR_SOCKET_ROOT.rstrip("/")) + r"/[A-Za-z0-9][A-Za-z0-9_.-]*", socket_dir
    ):
        raise ValueError(
            f"herdr_socket_dir must be {paths.HERDR_SOCKET_ROOT.rstrip('/')}/<project>"
        )
    lines.append(f"Volume={socket_dir}:/run/herdr:rw")
    lines.append(f"User={declared_uid}")
    lines.append("DropCapability=ALL")
    for capability in config.get("add_capabilities", []):
        if not isinstance(capability, str) or not _CAPABILITY_RE.fullmatch(capability):
            raise ValueError(f"Invalid Herdr capability: {capability!r}")
        # A Herdr Podman API client does not need kernel capabilities.
        raise ValueError(f"Herdr must not add Linux capabilities: {capability}")

    if config.get("read_only_rootfs", True):
        lines.append("ReadOnly=true")

    health_cmd = config.get("health_cmd")
    if health_cmd:
        if not isinstance(health_cmd, list) or not all(isinstance(x, str) for x in health_cmd):
            raise ValueError("Herdr health_cmd must be a string array")
        lines.append("HealthCmd=" + " ".join(health_cmd))
        for key in ("health_interval", "health_timeout", "health_retries", "health_start_period"):
            if config.get(key) is not None:
                lines.append(f"{''.join(part.title() for part in key.split('_'))}={config[key]}")


class HostAdaptationBindingError(PermissionError):
    """當試圖將 Host Adaptation 宣告 (adaptation_target: 'host') 綁定至容器時拋出。"""
    pass


def resolve_extension_volumes(
    project_name: str,
    services_dir: str = DEFAULT_SERVICES_DIR,
    *,
    read_only: bool = False,
) -> list[dict]:
    """
    TICKET-SSOT: 讀取 .services/<project_name>/ext-default.json（若存在且啟用），
    展平並回傳通過白名單驗證的 Volume 字典清單：
    [
        {"host_path": "...", "container_path": "...", "options": "ro"}
    ]
    """
    if not project_name:
        return []

    ext_path = Path(services_dir) / project_name / "ext-default.json"
    if not ext_path.exists():
        alt_path = Path(paths.SERVICES_DIR) / project_name / "ext-default.json"
        if alt_path.exists():
            ext_path = alt_path
        else:
            return []

    try:
        import subprocess
        scopes_dir = Path(os.environ.get("CONTAINER_CTL_SCOPES_DIR") or paths.SCOPES_DIR)
        files = sorted(str(f) for layer in ("system", "biz", "solution", "preset") for f in (scopes_dir / layer).glob("*.json")
                       if f.is_file() and not f.is_symlink())          # one explicit catalog; symlinked scopes are refused by extscope
        entry_uuid = json.loads(ext_path.read_text(encoding="utf-8"))["uuid"]
        cmd = ["extscope", "plan", "--var", f"PROJECT_NAME={project_name}", "--var", f"USER_HOME={os.environ.get('USER_HOME') or '/root'}"]
        for f in files + [os.path.realpath(ext_path)]:     # ext-default.json is a link into .deploys; extscope refuses link scope files
            cmd += ["--scope", f]
        res = subprocess.run(cmd + ["--entry", entry_uuid], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "EXTSCOPE_DEBUG": "1"})
        if res.returncode != 0:
            raise RuntimeError(f"extscope plan exit {res.returncode}: {(res.stderr or '').strip()[:300]}")
        items = json.loads(res.stdout)["items"]
        for origin in {i.get("origin") for i in items if i.get("origin")}:
            try:
                host = json.loads(Path(origin).read_text(encoding="utf-8")).get("adaptation_target") == "host"
            except (OSError, ValueError):
                host = False
            if host:
                raise HostAdaptationBindingError(
                    f"[REJECT] 拒絕綁定 Host Adaptation 宣告至容器: 專案 '{project_name}' 包含 adaptation_target: 'host' 之 Scope ({Path(origin).name})，"
                    f"嚴禁流入容器 Volume 掛載或 Quadlet 生成。"
                )
        volumes = []
        for entry in items:
            it = entry["item"]
            if it.get("type") == "mount" and it.get("dst_fhs"):
                src = it["src"]
                if not any(src.startswith(prefix) for prefix in ALLOWED_MOUNT_SRC_PREFIXES):
                    raise PermissionError(
                        f"Extension Mount 來源路徑 '{src}' 未在允許白名單中 ({ALLOWED_MOUNT_SRC_PREFIXES})"
                    )
                mode = it.get("mode") or "ro"
                if not read_only and mode == "rw" and not Path(src).exists() and src.startswith(tuple(p.rstrip("/") + "/" for p in paths.AUTO_CREATE_PREFIXES)):
                    try:
                        Path(src).mkdir(parents=True, exist_ok=True, mode=0o770)
                    except OSError:
                        pass
                volumes.append({
                    "host_path": src,
                    "container_path": it["dst_fhs"],
                    "options": mode,
                    "adaptation_target": "container",
                })
        return volumes
    except PermissionError:
        raise
    except Exception as e:      # noqa: BLE001 - never a silent `return []`: extension volumes vanishing must be visible
        import sys
        print(f"container-ctl: WARNING extension volumes for '{project_name}' NOT resolved ({type(e).__name__}: {str(e)[:300]}); "
              f"the generated unit will lack them", file=sys.stderr)
        return []


def stable_naming(cfg: dict) -> bool:
    """`unit_naming: "stable"`（ADR-009 P5）：單元／DB 身分不含版本，版本放 label；預設 versioned（舊行為）。"""
    return cfg.get("unit_naming") == "stable"


def _inst_id(action: ServiceAction, project: str, version: str) -> str:
    """生成實例 ID。shared: 跨專案單例；dedicated: 專案內以 name 區分，避免碰撞。"""
    if action.config.get("tier") == "dev" or (stable_naming(action.config) and action.config.get("scope") != "shared"):
        return f"{action.name}-shared" if action.config.get("scope") == "shared" else action.name
    cfg_ver = action.config.get("version", version)
    if action.config.get("scope") == "shared":
        return f"{action.name}-{cfg_ver}-shared"
    return f"{action.name}-{cfg_ver}"


def _container_name(action: ServiceAction, project: str, version: str) -> str:
    inst = _inst_id(action, project, version)
    return f"svc-{inst}"


def _db_instance_id(action: ServiceAction, project: str, version: str) -> str:
    """dev 工作區保留固定 DB ID；prod 仍按部署版本識別。"""
    if action.config.get("tier") == "dev" or stable_naming(action.config):
        return f"{project}-{action.name}"
    return f"{project}-{action.name}-{action.config.get('version') or version}"


def _render_container_unit(
    action: ServiceAction,
    image: str,
    project: str = "",
    version: str = "",
    extra_volumes: list[dict] | None = None,
    governance_mapping: Optional[WorkloadGovernanceMapping] = None,
) -> str:
    """TICKET-025: infra tier 先於 prod，prod/dev 等待 network-online.target。生成包含所有容器設定與 [Install] 的完整 Quadlet 單元。"""
    tier = action.config.get("tier", "prod")
    mode = action.config.get("lifecycle_policy", {}).get("mode", "manual" if tier == "dev" else "always_on")
    inst = _inst_id(action, project, version) if project else "%i"
    cname = f"svc-{inst}"

    comments = (
        action.config.get("comment")
        or action.config.get("comments")
        or action.config.get("notes")
        or action.config.get("remarks")
    )
    lines = []
    if comments:
        lines.append("# ─────────────────────────────────────────────────────────────────────────────")
        lines.append("# 【容器藍圖備註 (Blueprint Comment)】")
        if isinstance(comments, str):
            for c_line in comments.strip().splitlines():
                lines.append(f"# {c_line}")
        elif isinstance(comments, list):
            for item in comments:
                if isinstance(item, str):
                    for c_line in item.splitlines():
                        lines.append(f"# {c_line}")
                elif isinstance(item, dict):
                    for k, v in item.items():
                        lines.append(f"# {k}: {v}")
        lines.append("# ─────────────────────────────────────────────────────────────────────────────")
        lines.append("")

    lines.extend([
        "[Unit]",
        f"Description=Container {action.name} ({tier}) instance {inst}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Container]",
        f"ContainerName={cname}",
        f"Image={image}",
        "Pull=never",
    ])

    _append_herdr_runtime_config(lines, action.config)

    if not action.config.get("herdr_control_plane", False):
        if action.config.get("user"):
            lines.append(f"User={action.config['user']}")
        userns = action.config.get("userns")
        if userns is not None:
            if userns != "keep-id":   # only the rootless uid-preserving mode is supported; anything else is a config error
                raise ValueError(f"unsupported userns {userns!r} (only 'keep-id')")
            lines.append("UserNS=keep-id")
        if action.config.get("read_only", action.config.get("read_only_rootfs", False)):
            lines.append("ReadOnly=true")
        health = action.config.get("healthcheck") or {}
        test = health.get("test") or action.config.get("health_cmd")
        if test:
            if not isinstance(test, list) or not all(isinstance(x, str) for x in test):
                raise ValueError("healthcheck test must be a string array")
            if test[0] == "CMD":
                command = shlex.join(test[1:])
            elif test[0] == "CMD-SHELL":
                if len(test) != 2:
                    raise ValueError("CMD-SHELL healthcheck requires one command")
                command = test[1]
            else:
                command = shlex.join(test)
            lines.append("HealthCmd=" + command)
            for key in ("interval", "timeout", "retries", "start_period"):
                value = health.get(key, action.config.get("health_" + key))
                if value is not None:
                    lines.append(f"Health{''.join(p.title() for p in key.split('_'))}={value}")
    entrypoint = action.config.get("entrypoint")
    if entrypoint is not None:
        if not isinstance(entrypoint, list) or not all(isinstance(x, str) for x in entrypoint):
            raise ValueError("entrypoint must be a string array")
        # Quadlet forwards this as one JSON argument to podman --entrypoint.
        import json
        lines.append("Entrypoint=" + json.dumps(entrypoint))

    for port in action.config.get("ports", []):
        lines.append(f"PublishPort={port}")

    for key, value in (action.config.get("sysctls") or {}).items():
        lines.append(f'Sysctl="{key}={value}"')

    seen_destinations: set[str] = set()
    all_volumes = action.config.get("volumes", [])
    has_multiple_vols = len(all_volumes) > 1
    for vol in all_volumes:
        resolved, err = resolve_volume_mount(
            vol=vol,
            project=project,
            service_name=action.name,
            has_multiple_vols=has_multiple_vols,
        )
        if resolved:
            seen_destinations.add(resolved.container_path)
            lines.append(f"Volume={resolved.host_path}:{resolved.container_path}:{resolved.options}")
        else:
            # 門禁安全原則：Fail-Closed。Volume 驗證失敗嚴禁靜默放行原樣寫入 Quadlet
            raise ValueError(f"[FAIL-CLOSED] Volume 驗證失敗，拒絕產生 Quadlet: {err or vol}")

    # 容器生命週期隔離門禁 (SPEC-015 / Ticket 03): 嚴禁 Host Adaptation 注入容器
    for ext_vol in (extra_volumes or []):
        if isinstance(ext_vol, dict) and ext_vol.get("adaptation_target") == "host":
            raise HostAdaptationBindingError(
                f"[REJECT] 拒絕渲染容器單元: Volume '{ext_vol.get('host_path')}' 標記有 adaptation_target: 'host'，嚴禁綁定至容器。"
            )
    for vol in action.config.get("volumes", []):
        if isinstance(vol, dict) and vol.get("adaptation_target") == "host":
            raise HostAdaptationBindingError(
                f"[REJECT] 拒絕渲染容器單元: 容器配置 volumes 標記有 adaptation_target: 'host'，嚴禁綁定至容器。"
            )

    # TICKET-SSOT: 開發或工作區容器自動注入 Extension Volume
    is_dev_container = (
        tier == "dev"
        or any(v.get("container_path") == "/workspace" for v in action.config.get("volumes", []))
    )
    if is_dev_container and extra_volumes:
        for ext_vol in extra_volumes:
            c_path = ext_vol["container_path"]
            if c_path not in seen_destinations:
                seen_destinations.add(c_path)
                h_path = ext_vol["host_path"]
                opts = ext_vol.get("options", "ro")
                lines.append(f"Volume={h_path}:{c_path}:{opts}")

    # Tmpfs 配額注入 (INV-02 / INV-03)
    for raw_tmpfs in action.config.get("tmpfs", []):
        if "size=" not in raw_tmpfs:
            quota = "128m" if tier == "dev" else "64m"
            raw_tmpfs = f"{raw_tmpfs}:size={quota},mode=1777"
        lines.append(f"Tmpfs={raw_tmpfs}")

    for key, val in action.config.get("env", {}).items():
        if key.startswith("_"):
            continue
        lines.append(f"Environment={key}={val}")

    labels = action.config.get("labels", {})
    has_gov_id = False
    if isinstance(labels, dict):
        for key, val in labels.items():
            if key in (f"{paths.LABEL_NS}.governance.id", "governance.id"):
                has_gov_id = True
            lines.append(f"Label={key}={val}")
    elif isinstance(labels, list):
        for item in labels:
            if item.startswith(f"{paths.LABEL_NS}.governance.id=") or item.startswith("governance.id="):
                has_gov_id = True
            lines.append(f"Label={item}")

    if stable_naming(action.config):  # 單元名稱不含版本時，以 label 保留可查詢的 release 身分
        lines.append(f"Label={paths.OCI_LABEL_NS}.release={action.config.get('version') or version}")
        lines.append(f"Label={paths.OCI_LABEL_NS}.service={action.name}")
        if project:
            lines.append(f"Label={paths.OCI_LABEL_NS}.project={project}")

    # 持久化 governance stable ID 以供 cgroup-sync 與動態審計精確對齊
    if not has_gov_id:
        gov_id = (
            (governance_mapping.stable_id if governance_mapping else None)
            or action.config.get("stable_id")
            or (action.config.get("governance", {}).get("id") if isinstance(action.config.get("governance"), dict) else None)
            or action.name
        )
        if gov_id:
            lines.append(f"Label={paths.LABEL_NS}.governance.id={gov_id}")

    cmd = action.config.get("command") or action.config.get("exec")
    needs_run_init = (
        is_dev_container
        or cmd == "sleep infinity"
        or bool(action.config.get("run_init", False))
    )
    if needs_run_init:
        lines.append("RunInit=true")

    if cmd:
        if isinstance(cmd, list):
            if not cmd or not all(isinstance(arg, str) and arg for arg in cmd):
                raise ValueError("container command list must contain non-empty strings")
            lines.append(f"Exec={shlex.join(cmd)}")
        else:
            lines.append(f"Exec={cmd}")

    if project:
        custom_networks = action.config.get("networks", [])
        if custom_networks and isinstance(custom_networks, list):
            for cnet in custom_networks:
                cnet_str = str(cnet).strip()
                if cnet_str:
                    unit_fname = cnet_str if cnet_str.endswith(".network") else f"{cnet_str}.network"
                    lines.append(f"Network={unit_fname}")
        else:
            network_tier = "dev" if tier == "dev" else "prod"
            lines.append(f"Network=net-{network_tier}-{project}.network")

        # 注入 NetworkAlias 以解耦容器間通訊與 host 實例命名
        aliases: list[str] = []
        custom_alias = action.config.get("network_alias")
        if custom_alias:
            if isinstance(custom_alias, list):
                aliases.extend(custom_alias)
            elif isinstance(custom_alias, str):
                aliases.append(custom_alias)
        custom_aliases = action.config.get("network_aliases")
        if custom_aliases:
            if isinstance(custom_aliases, list):
                aliases.extend(custom_aliases)
            elif isinstance(custom_aliases, str):
                aliases.append(custom_aliases)

        if not aliases:
            aliases.append(action.name)
            for prefix in ("dev-", "prod-", "test-"):
                if action.name.startswith(prefix):
                    base_name = action.name[len(prefix):]
                    if base_name and base_name not in aliases:
                        aliases.append(base_name)
                    if base_name.endswith("-postgres") and "postgres" not in aliases:
                        aliases.append("postgres")
                    elif base_name.endswith("-redis") and "redis" not in aliases:
                        aliases.append("redis")

        seen_aliases: set[str] = set()
        for alias in aliases:
            alias_str = str(alias).strip()
            if alias_str and alias_str not in seen_aliases:
                seen_aliases.add(alias_str)
                lines.append(f"NetworkAlias={alias_str}")

    lines.extend([
        "",
        "[Service]",
        "Restart=on-failure",
        "RestartSec=10s",
        "RestartPreventExitStatus=125",
        "StartLimitIntervalSec=60s",
        "StartLimitBurst=3",
    ])

    if governance_mapping and governance_mapping.slice:
        lines.append(f"Slice={governance_mapping.slice}")

    if governance_mapping and governance_mapping.guard:
        lines.append(governance_mapping.guard)

    gov_limits = governance_mapping.limits if governance_mapping else {}

    mem_high = gov_limits.get("memory_high")
    if mem_high is not None:
        lines.append(f"MemoryHigh={mem_high}")

    # 資源限制防護：dev-tier 容器 RAM 上限預設為 2G，prod-tier 預設為 1G，支援 config 自訂調整
    memory_max = (
        gov_limits.get("memory_max")
        or action.config.get("memory_max")
        or action.config.get("memory")
        or (action.config.get("resources", {}).get("memory_max") if isinstance(action.config.get("resources"), dict) else None)
        or ("2G" if tier == "dev" else "1G")
    )
    if memory_max:
        lines.append(f"MemoryMax={memory_max}")

    # MemorySwapMax 硬性預設 (INV-01): 治理 mapping 優先，其次 config，rescue 豁免，否則預設 0
    gov_swap = gov_limits.get("swap_max")
    if gov_swap is not None:
        mem_swap = gov_swap
    else:
        cfg_swap = action.config.get("memory_swap_max")
        if cfg_swap is not None:
            mem_swap = cfg_swap
        elif tier == "rescue":
            mem_swap = None
        else:
            mem_swap = "0"

    if mem_swap is not None:
        lines.append(f"MemorySwapMax={mem_swap}")

    tasks_max = (
        gov_limits.get("tasks_max")
        or action.config.get("tasks_max")
        or action.config.get("tasks")
        or (action.config.get("resources", {}).get("tasks_max") if isinstance(action.config.get("resources"), dict) else None)
        or ("2048" if tier == "dev" else None)
    )
    if tasks_max:
        lines.append(f"TasksMax={tasks_max}")

    if mode != "manual":
        lines.extend([
            "",
            "[Install]",
            "WantedBy=default.target",
        ])

    return "\n".join(lines) + "\n"


def _render_network_unit(network_name: str) -> str:
    return dedent(f"""\
        [Network]
        NetworkName={network_name}
        Driver=bridge
    """)


def find_container_unit_drift(
    plan: DeployPlan,
    services_dir: str = DEFAULT_SERVICES_DIR,
    quadlet_dir: Path | None = None,
) -> set[str]:
    """Return actions whose generated container unit or systemd link differs from disk."""
    scan_dir = quadlet_dir or SYSTEMD_QUADLET_DIR
    project_dir = Path(services_dir) / plan.project_name
    ext_volumes = resolve_extension_volumes(plan.project_name, services_dir, read_only=True)
    drifted: set[str] = set()
    for action in plan.actions:
        if action.action == "REUSE":
            continue
        name = _container_name(action, plan.project_name, plan.version)
        unit_path = project_dir / f"{name}.container"
        link_path = scan_dir / unit_path.name
        expected = _render_container_unit(
            action,
            runtime_image_ref(action.config["image"]),
            plan.project_name,
            plan.version,
            extra_volumes=ext_volumes,
        )
        try:
            actual = unit_path.read_text(encoding="utf-8")
        except OSError:
            actual = None
        if (
            actual != expected
            or not link_path.is_symlink()
            or link_path.resolve() != unit_path.resolve()
        ):
            drifted.add(action.name)
    return drifted


def generate_units(
    plan: DeployPlan,
    services_dir: str = DEFAULT_SERVICES_DIR,
    policy: Optional[Any] = None,
    dry_run: bool = False,
    start_check_bin: str = "/usr/local/bin/memory-ctl",
) -> dict[str, str]:
    """
    依 Blueprint 生成 Quadlet 單元。
    回傳 {container_name: quadlet_path} 映射表。
    並執行 systemctl --user daemon-reload（dry-run 模式下跳過）。
    """
    manifest = None
    if policy is not None:
        # Atomic validation before touching filesystem (AC4: policy invalid 不能留下半套輸出)
        manifest = compile_governance(policy, plan=plan, start_check_bin=start_check_bin)

    project_dir = Path(services_dir) / plan.project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    SYSTEMD_QUADLET_DIR.mkdir(parents=True, exist_ok=True)
    for item in SYSTEMD_QUADLET_DIR.iterdir():
        if item.is_symlink() and not item.exists():
            item.unlink(missing_ok=True)

    # 寫入 Governance Manifest 與 Slices
    if manifest is not None:
        manifest_file = project_dir / "governance_manifest.json"
        manifest_file.write_text(manifest.to_json(), encoding="utf-8")

        for slice_name, slice_spec in manifest.slices.items():
            slice_file = project_dir / slice_name
            slice_file.write_text(slice_spec.unit_content, encoding="utf-8")
            if not dry_run:
                _symlink(slice_file)

    container_map: dict[str, str] = {}

    # 解析所有需要的 Network 單元
    networks_to_generate: dict[str, str] = {}
    for action in plan.actions:
        custom_networks = action.config.get("networks", [])
        if custom_networks and isinstance(custom_networks, list):
            for cnet in custom_networks:
                cnet_str = str(cnet).strip()
                if cnet_str:
                    unit_fname = cnet_str if cnet_str.endswith(".network") else f"{cnet_str}.network"
                    net_name = cnet_str[:-8] if cnet_str.endswith(".network") else cnet_str
                    networks_to_generate[unit_fname] = net_name
        else:
            tier = action.config.get("tier", "prod")
            net_tier = "dev" if tier == "dev" else "prod"
            unit_fname = f"net-{net_tier}-{plan.project_name}.network"
            net_name = f"net-{net_tier}-{plan.project_name}"
            networks_to_generate[unit_fname] = net_name

    if not networks_to_generate:
        unit_fname = f"net-prod-{plan.project_name}.network"
        networks_to_generate[unit_fname] = f"net-prod-{plan.project_name}"

    # 生成 .network 單元
    for unit_fname, net_name in networks_to_generate.items():
        net_file = project_dir / unit_fname
        net_file.write_text(_render_network_unit(net_name))
        if not dry_run:
            _symlink(net_file)

    ext_volumes = resolve_extension_volumes(plan.project_name, services_dir)

    for action in plan.actions:
        if action.action == "REUSE":
            # 複用共享服務，不重新生成單元
            inst = action.existing_instance_id or _inst_id(action, plan.project_name, plan.version)
            cname = f"svc-{inst}"
            container_map[action.name] = cname
            continue

        image = action.config["image"]
        quadlet_image = runtime_image_ref(image)
        inst = _inst_id(action, plan.project_name, plan.version)
        cname = _container_name(action, plan.project_name, plan.version)

        gov_map = None
        if manifest is not None:
            labels_cfg = action.config.get("labels", {})
            gov_label = (
                (labels_cfg.get(f"{paths.LABEL_NS}.governance.id") or labels_cfg.get("governance.id"))
                if isinstance(labels_cfg, dict)
                else None
            )
            stable_id = (
                action.config.get("stable_id")
                or (action.config.get("governance", {}).get("id") if isinstance(action.config.get("governance"), dict) else None)
                or gov_label
                or action.name
            )
            gov_map = manifest.workloads.get(stable_id)

        # 生成實例化 Quadlet 檔案
        container_file = project_dir / f"{cname}.container"
        container_file.write_text(
            _render_container_unit(
                action,
                quadlet_image,
                plan.project_name,
                plan.version,
                extra_volumes=ext_volumes,
                governance_mapping=gov_map,
            )
        )
        if not dry_run:
            _symlink(container_file)

        container_map[action.name] = cname

    if not dry_run:
        # daemon-reload 讓 systemd 感知新 Quadlet 單元
        get_default_manager().daemon_reload()

    return container_map


def _symlink(source: Path) -> None:
    """在 systemd Quadlet 掃描目錄建立 symlink（若已存在則覆寫）。"""
    link = SYSTEMD_QUADLET_DIR / source.name
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(source.resolve())
