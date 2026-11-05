"""
TICKET-002 + TICKET-020 + TICKET-VOL-01: Preflight 預檢工具
在 deploy apply 執行前驗證環境就緒（SPEC §16 全部 5 項 + 網路分軌邊界預警 + Volume 預檢）。
"""
import os
import socket
import subprocess
import sys
from pathlib import Path

from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl.declaration_guard import check_declarations
from container_ctl.image_volume_guard import check_image_volumes
from container_ctl.volume_resolver import (
    ALLOWED_AUTO_CREATE_ROOTS,
    ALLOWED_MOUNT_READ_ROOTS,
    DEFAULT_VOLUMES_DIR,
    FORBIDDEN_WRITE_PATHS,
    ResolvedVolumeMount,
    ensure_volume_storage,
    resolve_volume_mount,
    validate_volumes,
)
from container_ctl import paths

# 保持相容性常數
ALLOWED_MOUNT_SRC_PREFIXES = tuple(str(r) + "/" for r in ALLOWED_MOUNT_READ_ROOTS)
VOLUMES_DIR = str(DEFAULT_VOLUMES_DIR)


def _check_mount(path: str) -> tuple[bool, str]:
    p = Path(path)
    if not (p.exists() and (p.is_mount() or any(parent.is_mount() for parent in p.parents))):
        return False, f"[WARN] {path} 未掛載 — 執行: wsl --mount <vhdx>"
    return True, f"[OK]   {path} 已掛載"


def _check_podman_socket() -> tuple[bool, str]:
    sock_path = "/run/user/1000/podman/podman.sock"
    if not Path(sock_path).exists():
        return False, (
            f"[ERROR] Podman socket 不存在: {sock_path}\n"
            "       執行: systemctl --user start podman.socket"
        )
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(sock_path)
        s.close()
        return True, f"[OK]   Podman socket 可達: {sock_path}"
    except (OSError, socket.timeout):
        return False, f"[ERROR] Podman socket 存在但無法連線: {sock_path}"


def _check_db(db_path: str) -> tuple[bool, str]:
    try:
        init_db(db_path)
        return True, f"[OK]   SQLite DB 可寫: {db_path}"
    except Exception as e:
        return False, f"[ERROR] SQLite DB 初始化失敗: {e}"


def _check_image_digests(blueprints: list[dict]) -> tuple[bool, str]:
    """
    TICKET-020 + A-05: 檢查 blueprint 中所有 image 不得使用 :latest tag 且必須包含 @sha256:（硬性中斷）。
    blueprints: [{"service": ..., "image": ...}, ...]
    """
    violations = []
    if not isinstance(blueprints, list):
        return False, "[ERROR] blueprints 必須為 list"

    for b in blueprints:
        if not isinstance(b, dict) and not hasattr(b, "config"):
            continue
        cfg = b.get("config", b) if isinstance(b, dict) else getattr(b, "config", {})
        if not isinstance(cfg, dict):
            continue
        img = cfg.get("image") or b.get("image", "")
        if not img or not isinstance(img, str):
            violations.append(f"{cfg.get('name', 'unknown')}: 缺少 image 定義")
            continue
        if img.endswith(":latest") or ":" not in img or "@sha256:" not in img:
            violations.append(img)

    if violations:
        return False, (
            "[ERROR] Image Pinned Digest 違規（禁止 :latest 且必須包含 @sha256:）:\n"
            + "\n".join(f"       - {v}" for v in violations)
        )
    return True, "[OK]   所有 image 已 Pinned Digest"


def _check_bare_repo(repo_path: str, ref: str) -> tuple[bool, str]:
    """
    TICKET-020: 驗證 Bare Repo 可讀性 —— `git show <ref>:.deploy/manifest.json` 必須成功。
    失敗時列出可用 ref。
    """
    result = subprocess.run(
        ["git", "--git-dir", repo_path, "show", f"{ref}:.deploy/manifest.json"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"[OK]   Bare repo 可讀: {repo_path} @ {ref}"

    # 列出可用 ref
    tags = subprocess.run(
        ["git", "--git-dir", repo_path, "tag", "-l"],
        capture_output=True, text=True,
    ).stdout.strip()
    branches = subprocess.run(
        ["git", "--git-dir", repo_path, "branch", "-a"],
        capture_output=True, text=True,
    ).stdout.strip()
    return False, (
        f"[ERROR] Bare repo 無法讀取 {ref}:.deploy/manifest.json\n"
        f"       可用 tags: {tags or '無'}\n"
        f"       可用 branches: {branches or '無'}"
    )


def _check_network_tier_conformance() -> tuple[bool, str]:
    """
    TICKET-NETGATE: 靜態檢查 Quadlet 網路分軌邊界。
    僅輸出警告提示 (Non-blocking)，一律回傳 (True, msg)。
    """
    try:
        from container_ctl.scan_network_quadlets import scan_network_tier_conformance
        quadlet_dir = Path.home() / ".config/containers/systemd"
        services_dir = Path(paths.SERVICES_DIR)
        warnings = scan_network_tier_conformance([quadlet_dir, services_dir])
        if warnings:
            msg = "\n".join(warnings)
            return True, msg
        return True, "[OK]   網路分軌邊界檢查通過"
    except Exception as e:
        return True, f"[WARN] 網路分軌邊界檢查略過 ({e})"


def _check_portfwd_conflicts() -> tuple[bool, str]:
    from container_ctl.portfwd import find_listen_conflicts
    conflicts = find_listen_conflicts()
    if conflicts:
        detail = "; ".join(f"{p}: {', '.join(n)}" for p, n in sorted(conflicts.items()))
        return False, f"[ERROR] {paths.LABEL_NS}.portfwd.listen 重複宣告：{detail}"
    return True, "[OK] portfwd.listen 無重複宣告"


def _check_volumes(
    blueprints: list[dict],
    project: str | None = None,
    auto_create: bool = False,
) -> tuple[bool, list[str]]:
    """
    TICKET-VOL-01: 掃描 blueprint 中所有 Volume 定義，驗證主機端路徑合法性、白名單與存在性。
    委託 volume_resolver 統一處理。
    """
    ok, msgs, resolved = validate_volumes(blueprints, project=project)
    if not ok:
        return False, msgs

    if auto_create:
        create_ok, create_msgs = ensure_volume_storage(resolved)
        msgs.extend(create_msgs)
        if not create_ok:
            return False, msgs

    return True, msgs


def run_preflight(
    db_path: str = DEFAULT_DB_PATH,
    verbose: bool = True,
    blueprints: list[dict] | None = None,
    repo_path: str | None = None,
    ref: str | None = None,
    project: str | None = None,
) -> bool:
    """
    執行所有 preflight 檢查（SPEC §16 全部 5 項 + 網路分軌邊界預警 + Volume 預檢）。
    回傳 True 代表全部通過；False 代表至少一項失敗。
    """
    checks = [
        *(_check_mount(m) for m in paths.PREFLIGHT_MOUNTS),
        _check_podman_socket(),
        _check_db(db_path),
        _check_network_tier_conformance(),
        _check_portfwd_conflicts(),
    ]

    # TICKET-020 & TICKET-VOL-01: 可選項目（在 deploy apply / plan 路徑供應時執行）
    if blueprints is not None:
        img_ok, img_msg = _check_image_digests(blueprints)
        checks.append((img_ok, img_msg))
        decl_ok, decl_msgs = check_declarations(blueprints)
        checks.extend((decl_ok or "[ERROR]" not in m, m) for m in decl_msgs)
        vol_ok, vol_msgs = _check_volumes(blueprints, project=project, auto_create=False)
        for msg in vol_msgs:
            is_ok = False if "[ERROR]" in msg else True
            checks.append((is_ok, msg))
        if not vol_ok:
            checks.append((False, "[ERROR] Volume 掛載預檢未通過"))
        image_ok, image_msgs = check_image_volumes(blueprints)
        checks.extend((image_ok and "[ERROR]" not in msg, msg) for msg in image_msgs)

    if repo_path and ref:
        checks.append(_check_bare_repo(repo_path, ref))

    all_ok = True
    for ok, msg in checks:
        if verbose:
            print(msg, file=sys.stderr)
        if not ok:
            all_ok = False

    return all_ok
