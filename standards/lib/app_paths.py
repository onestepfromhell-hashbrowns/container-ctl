"""
app_paths.py — Application Path Resolver Template (Python)

用途：
    解析應用程式業務路徑（APP_ROOT、APP_CONFIG_DIR 等）。
    優先讀取 APP_* 環境變數，預設值為生產 FHS 路徑。

命名規則替換：
    各專案複製此模板後，必須將 APP_* 前綴替換為具業務語義的前綴。
    範例：WEBSERVICE_ROOT、DBPROXY_CONFIG_DIR。
    前綴必須與 Quadlet 單元名稱或容器名稱一致。

    替換步驟：
        1. 複製此檔案至專案的 src/lib/ 目錄。
        2. 全域取代 APP_ → <YOUR_SERVICE>_（例如 WEBSERVICE_）。
        3. 更新 os.getenv("APP_ROOT", ...) 的 key 名稱同步替換。
        4. 在 Quadlet .container 檔案宣告對應的 Environment= 行。

注意：
    本模組 ≠ /opt/container-ctl/<site-utils>/path_resolver.py
    <site-utils>/path_resolver.py 解析 site 基礎設施路徑（工具套件層）。
    本模組解析應用程式自身業務路徑（應用層）。
    兩者語義層次不同，請勿混淆。

開發環境覆寫（Dockerfile.dev 注入）：
    APP_ROOT=/workspace
    APP_CONFIG_DIR=/workspace/config
    APP_DATA_DIR=/workspace/.data
    APP_RUNTIME_DIR=/workspace/.run
    APP_LOG_DIR=/workspace/.logs
"""

import os
from pathlib import Path

_PROJECT_NAME: str = os.getenv("PROJECT_NAME", "myapp")

APP_ROOT: Path               = Path(os.getenv("APP_ROOT",               f"/opt/{_PROJECT_NAME}"))
APP_CONFIG_DIR: Path         = Path(os.getenv("APP_CONFIG_DIR",         f"/etc/opt/{_PROJECT_NAME}"))
APP_DATA_DIR: Path           = Path(os.getenv("APP_DATA_DIR",           f"/var/opt/{_PROJECT_NAME}"))
APP_RUNTIME_DIR: Path        = Path(os.getenv("APP_RUNTIME_DIR",        f"/run/{_PROJECT_NAME}"))
APP_LOG_DIR: Path            = Path(os.getenv("APP_LOG_DIR",            f"/var/log/{_PROJECT_NAME}"))
APP_FRONTEND_DIR: Path       = Path(os.getenv("APP_FRONTEND_DIR",       f"/opt/{_PROJECT_NAME}/frontend"))
APP_STATIC_VENDORS_DIR: Path = Path(os.getenv("APP_STATIC_VENDORS_DIR", f"/opt/{_PROJECT_NAME}/static_vendors"))

__all__ = [
    "APP_ROOT",
    "APP_CONFIG_DIR",
    "APP_DATA_DIR",
    "APP_RUNTIME_DIR",
    "APP_LOG_DIR",
    "APP_FRONTEND_DIR",
    "APP_STATIC_VENDORS_DIR",
]

