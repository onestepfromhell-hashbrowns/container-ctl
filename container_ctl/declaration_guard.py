"""Static checks on .deploy container declarations: digest format and host publish address by tier."""

import re

TIER_HOST_IP = {"dev": "127.0.0.1", "prod": "127.0.0.2"}
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")


def _host_ips(profile: dict | None) -> dict:
    """環境 profile 的 Host IP 規則（ADR-009 P4）；未給則用內建預設。"""
    return {**TIER_HOST_IP, **((profile or {}).get("host_ip") or {})}


def _tier(cfg: dict) -> str | None:
    tier = cfg.get("tier")
    if tier in TIER_HOST_IP:
        return tier
    name = str(cfg.get("name", ""))
    for t in TIER_HOST_IP:
        if name.startswith(f"{t}-"):
            return t
    return tier


def _publish_host_ip(port) -> str | None:
    """Host IP of a PublishPort entry, or None when omitted (`[ip:][host:]container[/proto]`)."""
    parts = str(port).split("/", 1)[0].split(":")
    if len(parts) < 3:
        return None
    return ":".join(parts[:-2])


def _port_spec(port) -> str:
    """PublishPort 去掉 Host IP 後的 `[host:]container[/proto]` 部分，供例外比對。"""
    body, _, proto = str(port).partition("/")
    parts = body.split(":")
    spec = ":".join(parts[-2:]) if len(parts) >= 2 else body
    return f"{spec}/{proto}" if proto else spec


def _publish_exceptions(cfg: dict) -> tuple[set[str], list[str]]:
    """讀 `publish_exceptions`：只允許帶 reason 的 UDP 埠範圍免除 Host IP 規則。回傳 (放行規格, 錯誤訊息)。"""
    allowed: set[str] = set()
    errors: list[str] = []
    for item in cfg.get("publish_exceptions", []) or []:
        spec = item.get("ports") if isinstance(item, dict) else None
        reason = str(item.get("reason", "")).strip() if isinstance(item, dict) else ""
        if not spec or not reason:
            errors.append(f"publish_exceptions 項目須含 ports 與 reason：{item!r}")
        elif not str(spec).endswith("/udp"):
            errors.append(f"publish_exceptions 只允許 UDP 埠：{spec}")
        else:
            allowed.add(str(spec))
    return allowed, errors


_PROJECT_SOURCE_MARKERS = ("/.worktrees/", "/.git_repos/", "/worktrees/", "/git-repos/")


def _prod_source_mounts(cfg: dict) -> list[str]:
    """prod 不得以 dev 方式掛載專案工作樹／Bare Repo／/workspace 提供程式碼（程式碼須在映像內）。"""
    bad = []
    for vol in cfg.get("volumes", []) or []:
        if not isinstance(vol, dict):
            continue
        host = str(vol.get("host_path") or "") + "/"
        if vol.get("container_path") == "/workspace" or any(m in host for m in _PROJECT_SOURCE_MARKERS):
            bad.append(f"{vol.get('host_path') or '(volume)'} -> {vol.get('container_path')}")
    return bad


def _config_repo_mounts(cfg: dict) -> list[str]:
    """任何軌道的容器都不得掛載設定 repo（宣告的寫入權限須與容器分離）。"""
    return [
        str(vol.get("host_path"))
        for vol in cfg.get("volumes", []) or []
        if isinstance(vol, dict) and any(m in (str(vol.get("host_path") or "") + "/") for m in ("/.config_repos/", "/config-repos/"))
    ]


# 工具讀取並生效的鍵
_HONORED_KEYS = frozenset(
    "add_capabilities app_hooks command config container_path containerfile entrypoint env exec governance "
    "health_cmd health_interval health_timeout health_retries health_start_period healthcheck "
    "herdr_control_plane herdr_socket_dir host_path image labels lifecycle_policy memory memory_max "
    "memory_swap_max name network_alias network_aliases networks owner_uid podman_socket portfwd portfwd_port "
    "ports publish_exceptions read_only read_only_rootfs release_image_name resources run_init scope stable_id "
    "public_files secrets smoke strip sysctls target tasks tasks_max tier tmpfs uid user version volumes unit_naming update_strategy".split()
)
# 純文件／中繼資料鍵（不影響生成，不警告）
_DOC_KEYS = frozenset(
    "comment comments notes remarks known_consumers ingress_contract postgres_major volume_templates "
    "readiness_probe".split()
)
_IGNORED_HINTS = {
    "depends_on": "工具不處理 depends_on，無啟動順序保證；請自行確認依賴服務已 Up",
    "memory_high": "生成器不處理 memory_high；請改用 memory_max",
    "network": "請改用 networks（陣列）",
}


def check_unknown_keys(cfg: dict) -> list[str]:
    """宣告了但工具不認的鍵 → WARN（避免被靜默忽略，issue 1005）。"""
    name = cfg.get("name", "unknown")
    out = []
    for key in sorted(cfg):
        if key in _HONORED_KEYS or key in _DOC_KEYS:
            continue
        hint = _IGNORED_HINTS.get(key, "生成器不認得此鍵，將被靜默忽略；若屬純註記請改用 comment／notes")
        out.append(f"[WARN] {name}: 宣告鍵 '{key}' 未被工具處理（{hint}）")
    return out


_SECRET_KEY_WORDS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "PRIVATE_KEY", "API_KEY", "DB_PASS")
_SECRET_SAFE_SUFFIXES = ("_FILE", "_PATH", "_DIR", "_URL_FILE")
_URL_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_FHS_PREFIXES = ("/opt/", "/etc/opt/", "/var/opt/", "/run/", "/var/log/", "/var/cache/", "/var/lib/", "/tmp/")


def check_runtime_conventions(cfg: dict) -> tuple[bool, list[str]]:
    """SPEC §23 路徑與機密規範：明文密碼、prod 的 /workspace 與 APP_*、volume_templates、secrets 路徑、大小寫。"""
    name = cfg.get("name", "unknown")
    tier = _tier(cfg)
    env = cfg.get("env") or {}
    ok = True
    out: list[str] = []
    level = "ERROR" if tier == "prod" else "WARN"
    for key, value in env.items():
        if not isinstance(value, str) or not value:
            continue
        upper = key.upper()
        if any(w in upper for w in _SECRET_KEY_WORDS) and not upper.endswith(_SECRET_SAFE_SUFFIXES):
            out.append(f"[{level}] {name}: env {key} 疑似明文機密；改用 {key}_FILE 指向掛載的機密檔（SPEC §23.3）")
            ok = ok and level != "ERROR"
        elif _URL_USERINFO_RE.search(value):
            out.append(f"[{level}] {name}: env {key} 的 URL 內嵌帳密；改用 *_FILE 並由應用組合連線字串（SPEC §23.3）")
            ok = ok and level != "ERROR"
        if tier == "prod":
            if "/workspace" in value:
                out.append(f"[ERROR] {name}: prod env {key} 含 /workspace（SPEC §23.1）")
                ok = False
            elif key.startswith("APP_") and key.endswith(("_ROOT", "_DIR")) and not value.startswith(_FHS_PREFIXES):
                out.append(f"[ERROR] {name}: prod env {key}={value} 不是 FHS 路徑（須在 {'、'.join(_FHS_PREFIXES)} 之下）")
                ok = False
    if cfg.get("volume_templates"):
        out.append(f"[WARN] {name}: volume_templates 不會被生成器處理，其中的掛載不會進 Quadlet；請改寫入 volumes[]（SPEC §23.4）")
    for vol in cfg.get("volumes", []) or []:
        if isinstance(vol, dict) and "/secrets" in str(vol.get("container_path") or ""):
            cpath = str(vol["container_path"])
            if not cpath.startswith("/etc/opt/"):
                out.append(f"[WARN] {name}: secrets 掛載點 {cpath} 應位於 /etc/opt/<PROJECT_NAME>/secrets（SPEC §23.3）")
    project = env.get("PROJECT_NAME")
    if isinstance(project, str) and project:
        for vol in cfg.get("volumes", []) or []:
            cpath = str(vol.get("container_path") or "") if isinstance(vol, dict) else ""
            for root in ("/etc/opt/", "/var/opt/", "/var/log/"):
                if cpath.startswith(root):
                    seg = cpath[len(root):].split("/", 1)[0]
                    if seg.lower() == project.lower() and seg != project:
                        out.append(f"[WARN] {name}: 掛載點 {cpath} 與 PROJECT_NAME={project} 大小寫不同；app_paths 逐字使用 PROJECT_NAME（SPEC §23.2）")
    return ok, out


def check_declarations(configs: list[dict], profile: dict | None = None) -> tuple[bool, list[str]]:
    host_ips = _host_ips(profile)
    messages: list[str] = []
    ok = True
    for cfg in configs:
        name = cfg.get("name", "unknown")
        image = cfg.get("image", "")
        if not _DIGEST_RE.search(image):
            messages.append(f"[ERROR] {name}: image 須以 @sha256:<64 位小寫 hex> 結尾，實際值: '{image}'")
            ok = False

        ports = cfg.get("ports", [])
        tier = _tier(cfg)
        for mount in _config_repo_mounts(cfg):
            messages.append(f"[ERROR] {name}: 容器不得掛載設定 repo（{mount}）；部署宣告只能由宿主受控入口修改")
            ok = False
        if tier == "prod":
            for mount in _prod_source_mounts(cfg):
                messages.append(f"[ERROR] {name}: prod 不得掛載專案工作樹或 /workspace（{mount}）；程式碼須在映像內")
                ok = False
        if cfg.get("unit_naming") not in (None, "versioned", "stable"):
            messages.append(f"[ERROR] {name}: unit_naming 只接受 versioned／stable，實際 {cfg.get('unit_naming')!r}")
            ok = False
        if cfg.get("update_strategy") not in (None, "stop-first"):
            messages.append(f"[ERROR] {name}: update_strategy 目前只支援 stop-first（start-first 未實作），實際 {cfg.get('update_strategy')!r}")
            ok = False
        mode = (cfg.get("lifecycle_policy") or {}).get("mode")
        if tier == "dev" and mode is None:
            messages.append(f"[WARN] {name}: dev 宣告未明寫 lifecycle_policy.mode（隱含 manual），請明確宣告")
        messages.extend(check_unknown_keys(cfg))
        rt_ok, rt_msgs = check_runtime_conventions(cfg)
        messages.extend(rt_msgs)
        ok = ok and rt_ok
        hooks = cfg.get("app_hooks")
        if hooks is not None:
            if not isinstance(hooks, dict) or not hooks.get("start"):
                messages.append(f"[ERROR] {name}: app_hooks 必須為物件且含 start（字串或指令陣列）")
                ok = False
            else:
                for key in ("start", "readiness_probe"):
                    val = hooks.get(key)
                    if val is not None and not (
                        isinstance(val, str) or (isinstance(val, list) and all(isinstance(x, str) for x in val))
                    ):
                        messages.append(f"[ERROR] {name}: app_hooks.{key} 必須是字串或字串陣列")
                        ok = False
        expected = host_ips.get(tier)
        if ports and expected is None:
            messages.append(f"[WARN] {name}: tier='{tier}' 無分軌 Host IP 規則，略過端口檢查")
            continue
        allowed, exc_errors = _publish_exceptions(cfg)
        for msg in exc_errors:
            messages.append(f"[ERROR] {name}: {msg}")
            ok = False
        for port in ports:
            actual = _publish_host_ip(port)
            if _port_spec(port) in allowed:
                messages.append(f"[WARN] {name}: 端口 '{port}' 依 publish_exceptions 免除 Host IP 規則")
                continue
            if actual != expected:
                shown = actual if actual is not None else "(省略)"
                messages.append(
                    f"[ERROR] {name}: tier={tier} 的端口 '{port}' Host IP 為 {shown}，須明列 {expected}"
                )
                ok = False
    return ok, messages
