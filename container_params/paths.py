"""
container_params.paths — FHS 6 大特規路徑動態解析引擎

嚴格遵循 FHS 兩大正交維度（靜態/動態、可共用/不可共用）與 Dual-Track 分軌哲學。
解析順序：
1. 顯式環境變數 (<PREFIX>_* 或 APP_*)
2. 開發 Repo 自動探測 (pyproject.toml / .git / /workspace)
3. 嚴格生產 FHS 標準 Fallback (/opt/<slug>, /etc/opt/<slug>, /run/<slug>, /var/log/<slug>, /var/lib/<slug>)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class FHSPaths:
    """容器標準 FHS 6 大特規路徑定義。"""

    project_name: str
    app_root: Path
    config_dir: Path
    runtime_dir: Path
    log_dir: Path
    data_dir: Path
    frontend_dir: Path
    static_vendors_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "PROJECT_NAME": self.project_name,
            "APP_ROOT": str(self.app_root),
            "APP_CONFIG_DIR": str(self.config_dir),
            "APP_RUNTIME_DIR": str(self.runtime_dir),
            "APP_LOG_DIR": str(self.log_dir),
            "APP_DATA_DIR": str(self.data_dir),
            "APP_FRONTEND_DIR": str(self.frontend_dir),
            "APP_STATIC_VENDORS_DIR": str(self.static_vendors_dir),
        }

    def get_writable_directories(self) -> list[Path]:
        """回傳生產環境 read-only rootfs 模式下必須可寫的目錄 (Volume/tmpfs 掛載點)。"""
        return [self.runtime_dir, self.log_dir, self.data_dir]

    def get_read_only_directories(self) -> list[Path]:
        """回傳生產環境 read-only rootfs 模式下唯讀的目錄。"""
        return [self.app_root, self.config_dir, self.frontend_dir]


class FHSPathResolver:
    """FHS 路徑解析器，支援多前綴、Dev/Prod 雙軌自動探測與環境變數覆寫。"""

    def __init__(
        self,
        project_name: str = "myapp",
        prefix: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self.project_name = project_name
        self.prefix = (prefix or "APP").upper().rstrip("_")
        self.environ = environ if environ is not None else os.environ
        self.base_dir = base_dir or Path.cwd()

    def _get_env(self, key_suffix: str) -> Optional[str]:
        """優先讀取具名前綴環境變數，次之讀取通用 APP_* 前綴。"""
        specific_key = f"{self.prefix}_{key_suffix}"
        if specific_key in self.environ and self.environ[specific_key].strip():
            return self.environ[specific_key].strip()
        generic_key = f"APP_{key_suffix}"
        if generic_key in self.environ and self.environ[generic_key].strip():
            return self.environ[generic_key].strip()
        return None

    def _detect_dev_repo_root(self) -> Optional[Path]:
        """探測當前執行環境是否為本地開發 Repo 或 Dev 容器。"""
        # 1. 顯式設定 APP_ROOT=/workspace 或 /app
        env_root = self._get_env("ROOT")
        if env_root:
            p = Path(env_root)
            if p.name == "workspace" or p.as_posix().startswith("/workspace"):
                return p

        # 2. 探測檔案系統特徵
        candidates = [self.base_dir, self.base_dir.parent, Path.cwd()]
        for candidate in candidates:
            if candidate and candidate.is_dir():
                if (
                    (candidate / "pyproject.toml").is_file()
                    or (candidate / "package.json").is_file()
                    or (candidate / "go.mod").is_file()
                    or (candidate / ".git").is_dir()
                ):
                    return candidate.resolve()
        return None

    def resolve(self) -> FHSPaths:
        """依優先權解析 FHS 6 大特規路徑。"""
        slug = self.project_name
        dev_repo_root = self._detect_dev_repo_root()

        # 1. APP_ROOT
        env_root = self._get_env("ROOT")
        if env_root:
            app_root = Path(env_root)
        elif dev_repo_root:
            app_root = dev_repo_root
        else:
            app_root = Path(f"/opt/{slug}")

        is_dev_mode = (dev_repo_root is not None) or (app_root.as_posix() == "/workspace")

        # 2. APP_CONFIG_DIR
        env_config = self._get_env("CONFIG_DIR")
        if env_config:
            config_dir = Path(env_config)
        elif is_dev_mode:
            # 優先檢查 app/configs 或 config 或 configs
            if (app_root / "app" / "configs").is_dir():
                config_dir = app_root / "app" / "configs"
            elif (app_root / "configs").is_dir():
                config_dir = app_root / "configs"
            elif (app_root / "config").is_dir():
                config_dir = app_root / "config"
            else:
                config_dir = app_root / "app" / "configs"
        else:
            config_dir = Path(f"/etc/opt/{slug}")

        # 3. APP_RUNTIME_DIR
        env_runtime = self._get_env("RUNTIME_DIR")
        if env_runtime:
            runtime_dir = Path(env_runtime)
        elif is_dev_mode:
            runtime_dir = app_root / "run"
        else:
            runtime_dir = Path(f"/run/{slug}")

        # 4. APP_LOG_DIR
        env_log = self._get_env("LOG_DIR")
        if env_log:
            log_dir = Path(env_log)
        elif is_dev_mode:
            log_dir = app_root / "logs"
        else:
            log_dir = Path(f"/var/log/{slug}")

        # 5. APP_DATA_DIR (SQLite / Snapshots / State)
        env_data = self._get_env("DATA_DIR")
        if env_data:
            data_dir = Path(env_data)
        elif is_dev_mode:
            data_dir = app_root / "data"
        else:
            data_dir = Path(f"/var/lib/{slug}")

        # 6. APP_FRONTEND_DIR & STATIC_VENDORS
        env_frontend = self._get_env("FRONTEND_DIR")
        if env_frontend:
            frontend_dir = Path(env_frontend)
        elif is_dev_mode:
            frontend_dir = app_root / "frontend"
        else:
            frontend_dir = Path(f"/opt/{slug}/frontend")

        env_vendors = self._get_env("STATIC_VENDORS_DIR")
        if env_vendors:
            static_vendors_dir = Path(env_vendors)
        elif is_dev_mode:
            static_vendors_dir = app_root / "static_vendors"
        else:
            static_vendors_dir = Path(f"/opt/{slug}/static_vendors")

        return FHSPaths(
            project_name=slug,
            app_root=app_root,
            config_dir=config_dir,
            runtime_dir=runtime_dir,
            log_dir=log_dir,
            data_dir=data_dir,
            frontend_dir=frontend_dir,
            static_vendors_dir=static_vendors_dir,
        )


def resolve_app_paths(
    project_name: str = "myapp",
    prefix: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> FHSPaths:
    """便利用戶函式，快速解析指定專案的 FHS 6 大特規路徑。"""
    resolver = FHSPathResolver(project_name=project_name, prefix=prefix, environ=environ)
    return resolver.resolve()
