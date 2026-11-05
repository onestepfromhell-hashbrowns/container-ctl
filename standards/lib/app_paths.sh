#!/usr/bin/env sh
# app_paths.sh — Application Path Resolver Template (Bash/POSIX)
#
# 用途：
#   解析應用程式業務路徑（APP_ROOT、APP_CONFIG_DIR 等）。
#   優先讀取 APP_* 環境變數，預設值為生產 FHS 路徑。
#
# 使用方式（source 引入）：
#   . /path/to/app_paths.sh
#   echo "$APP_ROOT"
#
# 命名規則替換：
#   各專案複製此模板後，將 APP_* 前綴替換為具業務語義的前綴。
#   前綴必須與 Quadlet 單元名稱或容器名稱一致。
#
#   替換步驟：
#     1. 複製此檔案至專案的 src/lib/ 目錄。
#     2. 全域取代 APP_ → <YOUR_SERVICE>_（例如 WEBSERVICE_）。
#     3. 在 Quadlet .container 的 Environment= 行宣告對應前綴變數。
#
# 注意：
#   本模組 ≠ site site utilities 的路徑解析（基礎設施層）。
#   本模組解析應用程式自身業務路徑（應用層）。
#   Source 此檔案無任何副作用（不執行 mkdir、不寫入任何檔案）。
#
# 開發環境覆寫（Dockerfile.dev 注入）：
#   APP_ROOT=/workspace
#   APP_CONFIG_DIR=/workspace/config
#   APP_DATA_DIR=/workspace/.data
#   APP_RUNTIME_DIR=/workspace/.run
#   APP_LOG_DIR=/workspace/.logs

PROJECT_NAME="${PROJECT_NAME:-myapp}"

APP_ROOT="${APP_ROOT:-/opt/${PROJECT_NAME}}"
APP_CONFIG_DIR="${APP_CONFIG_DIR:-/etc/opt/${PROJECT_NAME}}"
APP_DATA_DIR="${APP_DATA_DIR:-/var/opt/${PROJECT_NAME}}"
APP_RUNTIME_DIR="${APP_RUNTIME_DIR:-/run/${PROJECT_NAME}}"
APP_LOG_DIR="${APP_LOG_DIR:-/var/log/${PROJECT_NAME}}"
APP_FRONTEND_DIR="${APP_FRONTEND_DIR:-/opt/${PROJECT_NAME}/frontend}"
APP_STATIC_VENDORS_DIR="${APP_STATIC_VENDORS_DIR:-/opt/${PROJECT_NAME}/static_vendors}"

