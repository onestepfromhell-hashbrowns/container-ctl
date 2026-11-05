"""
volume_resolver.py — 容器持久化 Volume 單一真理來源 (SSOT) 解析器與邊界防禦
提供:
1. 嚴格的 Canonical Containment (Path.resolve + is_relative_to) 阻斷路徑穿越與 symlink 逃逸
2. 區分可唯讀掛載來源 (ALLOWED_MOUNT_READ_ROOTS) 與可自動補建目錄 (ALLOWED_AUTO_CREATE_ROOTS)
3. 嚴禁對程式碼與 Bare Repo (paths.FORBIDDEN_WRITE_PATHS) 進行寫入補建
4. 純唯讀預檢 (validate_volumes) 與確認後實體補建 (ensure_volume_storage) 的 CQS 解耦
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from container_ctl import paths

DEFAULT_VOLUMES_DIR = Path(paths.VOLUMES_DIR).resolve()

# 允許掛載的合法來源根目錄 (Canonical Path)
ALLOWED_MOUNT_READ_ROOTS = tuple(Path(p).resolve() for p in paths.MOUNT_READ_ROOTS)

# 僅允許自動補建的專屬 Volume 根目錄
ALLOWED_AUTO_CREATE_ROOTS = (
    DEFAULT_VOLUMES_DIR,
)

# 嚴格禁止寫入/自動建立的保護路徑
FORBIDDEN_WRITE_PATHS = tuple(Path(p).resolve() for p in paths.FORBIDDEN_WRITE_PATHS)

_PRIVATE_KEY_SUFFIXES = (".key", ".pem")


def _is_ro_file_mount_allowed(path: Path, options: str) -> bool:
    """單檔掛載條件：一般檔案（resolve 後）、options 含 ro、檔名不是私鑰（*.key／*.pem）。"""
    return (
        path.is_file()
        and "ro" in str(options).split(",")
        and not path.name.lower().endswith(_PRIVATE_KEY_SUFFIXES)
    )


@dataclass(frozen=True)
class ResolvedVolumeMount:
    host_path: Path
    container_path: str
    options: str = "rw"
    is_auto_creatable: bool = False
    exists: bool = False


def _check_containment(path: Path, roots: tuple[Path, ...]) -> bool:
    """檢查路徑是否隸屬於任一給定之根目錄"""
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except AttributeError:
            # Python < 3.9 fallback if ever needed
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def resolve_volume_mount(
    vol: Any,
    project: str | None,
    service_name: str,
    has_multiple_vols: bool = False,
    base_volumes_dir: Path | None = None,
    allowed_read_roots: tuple[Path, ...] | None = None,
    allowed_create_roots: tuple[Path, ...] | None = None,
    forbidden_write_paths: tuple[Path, ...] | None = None,
) -> tuple[ResolvedVolumeMount | None, str | None]:
    """
    解析單一 Volume 定義，執行安全校驗並推導 Canonical Host Path。
    回傳: (ResolvedVolumeMount, None) 或 (None, error_message)
    """
    if not isinstance(vol, dict):
        return None, f"Volume 配置格式錯誤: 預期 dict，收到 {type(vol).__name__}"

    container_path = vol.get("container_path")
    if not container_path or not isinstance(container_path, str):
        return None, f"Volume 缺少有效的 container_path: {vol}"

    # 檢查是否含有換行、NULL、冒號等注入字元
    if any(c in container_path for c in ("\n", "\r", "\0")):
        return None, f"container_path 含有非法控制字元: {container_path!r}"

    options = vol.get("options", "rw")
    if not isinstance(options, str) or any(c in options for c in ("\n", "\r", "\0", ":")):
        return None, f"Volume options 含有非法字元: {options!r}"

    volumes_dir = (base_volumes_dir or DEFAULT_VOLUMES_DIR).resolve()
    read_roots = allowed_read_roots if allowed_read_roots is not None else ALLOWED_MOUNT_READ_ROOTS
    create_roots = allowed_create_roots if allowed_create_roots is not None else ALLOWED_AUTO_CREATE_ROOTS
    forbidden_paths = forbidden_write_paths if forbidden_write_paths is not None else FORBIDDEN_WRITE_PATHS

    raw_host_path = vol.get("host_path")
    if raw_host_path:
        if not isinstance(raw_host_path, (str, Path)):
            return None, f"host_path 格式錯誤: {raw_host_path!r}"
        host_str = str(raw_host_path)
        if any(c in host_str for c in ("\n", "\r", "\0", ":")):
            return None, f"host_path 含有非法控制字元: {host_str!r}"
        target_path = Path(host_str)
    else:
        # 自動推導預設路徑
        sub_name = vol.get("name") or (Path(container_path).name if (has_multiple_vols and container_path != "/workspace") else "")
        proj = project or "unknown"
        if sub_name:
            target_path = volumes_dir / proj / service_name / sub_name
        else:
            target_path = volumes_dir / proj / service_name

    # 取得正規化 canonical path (若路徑不存在，resolve 會正規化語法上的 ..)
    try:
        resolved_path = target_path.resolve()
    except Exception as e:
        return None, f"無法解析 Volume 路徑 {target_path}: {e}"

    # 1. 白名單 Containment 檢查 (防禦 .. 與 symlink 逃逸)
    if not _check_containment(resolved_path, read_roots):
        return None, f"Volume 路徑未在允許白名單中: {resolved_path} (允許根目錄: {[str(r) for r in read_roots]})"

    # 2. 存在性與型別檢查
    if resolved_path.exists():
        if resolved_path.is_file() and _is_ro_file_mount_allowed(resolved_path, options):
            # 最小權限：機密／憑證以「單一檔案、唯讀」掛載，避免為一個檔案掛整個含其他機密的目錄（SPEC §23.3）
            return ResolvedVolumeMount(
                host_path=resolved_path,
                container_path=container_path,
                options=options,
                is_auto_creatable=False,
                exists=True,
            ), None
        if not resolved_path.is_dir():
            return None, f"Volume 掛載來源必須為目錄 (非一般檔案或特殊節點；單一檔案僅限 ro 且非私鑰檔): {resolved_path}"
        return ResolvedVolumeMount(
            host_path=resolved_path,
            container_path=container_path,
            options=options,
            is_auto_creatable=False,
            exists=True,
        ), None

    # 3. 缺失目錄的 Auto-create 權限邊界判定
    is_in_create_root = _check_containment(resolved_path, create_roots)
    is_in_forbidden = _check_containment(resolved_path, forbidden_paths)

    if is_in_create_root and not is_in_forbidden:
        return ResolvedVolumeMount(
            host_path=resolved_path,
            container_path=container_path,
            options=options,
            is_auto_creatable=True,
            exists=False,
        ), None
    else:
        return None, f"Volume 目錄不存在且非允許自動補建之專屬儲存區: {resolved_path}"


def validate_volumes(
    blueprints: list[Any],
    project: str | None = None,
    base_volumes_dir: Path | None = None,
    allowed_read_roots: tuple[Path, ...] | None = None,
    allowed_create_roots: tuple[Path, ...] | None = None,
    forbidden_write_paths: tuple[Path, ...] | None = None,
) -> tuple[bool, list[str], list[ResolvedVolumeMount]]:
    """
    純唯讀預檢所有 blueprint 中的 Volume 定義 (Zero Side-Effects)。
    不執行任何 mkdir 操作。
    回傳: (all_ok, messages, resolved_volumes)
    """
    messages = []
    all_ok = True
    resolved_list: list[ResolvedVolumeMount] = []

    if not isinstance(blueprints, list):
        return False, [f"[ERROR] blueprints 必須為 list，收到 {type(blueprints).__name__}"], []

    for bp in blueprints:
        if not isinstance(bp, dict) and not hasattr(bp, "config"):
            messages.append(f"[ERROR] Blueprint 格式無效: {bp}")
            all_ok = False
            continue

        cfg = bp.get("config", bp) if isinstance(bp, dict) else getattr(bp, "config", {})
        if not isinstance(cfg, dict):
            messages.append(f"[ERROR] Blueprint config 格式無效: {cfg}")
            all_ok = False
            continue

        svc_name = cfg.get("name") or bp.get("name") or bp.get("service") or "unknown"
        raw_volumes = cfg.get("volumes", [])
        if not raw_volumes:
            continue
        if not isinstance(raw_volumes, list):
            messages.append(f"[ERROR] 服務 {svc_name} 的 volumes 必須為 list")
            all_ok = False
            continue

        has_multiple_vols = len(raw_volumes) > 1
        for vol in raw_volumes:
            resolved, err = resolve_volume_mount(
                vol=vol,
                project=project,
                service_name=str(svc_name),
                has_multiple_vols=has_multiple_vols,
                base_volumes_dir=base_volumes_dir,
                allowed_read_roots=allowed_read_roots,
                allowed_create_roots=allowed_create_roots,
                forbidden_write_paths=forbidden_write_paths,
            )
            if err:
                messages.append(f"[ERROR] {err}")
                all_ok = False
            elif resolved:
                resolved_list.append(resolved)
                if not resolved.exists and resolved.is_auto_creatable:
                    messages.append(f"[WARN]  Volume 目錄不存在，將於部署時自動補建: {resolved.host_path}")

    if all_ok and not messages:
        messages.append("[OK]   所有 Volume 掛載目錄預檢通過")

    return all_ok, messages, resolved_list


def ensure_volume_storage(resolved_volumes: list[ResolvedVolumeMount]) -> tuple[bool, list[str]]:
    """
    在使用者確認 Apply 後，實體建立所有需要自動補建的 Volume 目錄。
    """
    messages = []
    all_ok = True

    for r_vol in resolved_volumes:
        if not r_vol.exists and r_vol.is_auto_creatable:
            try:
                r_vol.host_path.mkdir(parents=True, exist_ok=True, mode=0o755)
                messages.append(f"[INFO] 已建立 Volume 目錄: {r_vol.host_path}")
            except OSError as e:
                messages.append(f"[ERROR] Volume 目錄建立失敗: {r_vol.host_path} ({e})")
                all_ok = False

    return all_ok, messages
