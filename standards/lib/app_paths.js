// src/lib/app_paths.js
// Application Path Resolver Template (Node.js / CommonJS)
//
// 用途：
//   解析應用程式業務路徑（APP_ROOT、APP_CONFIG_DIR 等）。
//   優先讀取 APP_* 環境變數，預設值為生產 FHS 路徑。
//
// 使用方式：
//   const paths = require('./app_paths');
//   console.log(paths.APP_ROOT);
//
// 命名規則替換：
//   各專案複製此模板後，將 APP_* 前綴替換為具業務語義的前綴。
//   前綴必須與 Quadlet 單元名稱或容器名稱一致。
//
//   替換步驟：
//     1. 複製此檔案至專案的 src/lib/ 目錄。
//     2. 全域取代 APP_ → <YOUR_SERVICE>_（例如 WEBSERVICE_）。
//     3. 更新 process.env.APP_* 的 key 名稱同步替換。
//     4. 在 Quadlet .container 的 Environment= 行宣告對應前綴變數。
//
// ESM 使用者（package.json "type":"module" 或 .mjs）：
//   將 module.exports = { ... } 替換為：
//     export const APP_ROOT = process.env.APP_ROOT || `/opt/${projectName}`;
//     ... （其餘同理）
//
// 注意：
//   本模組 ≠ site site utilities 的路徑解析（基礎設施層）。
//   本模組解析應用程式自身業務路徑（應用層）。
//   require / import 時不建立目錄、不觸發任何 I/O。
//   回傳型別為 string；消費者可自行包裝為 path.resolve() 或 URL。
//
// 開發環境覆寫（Dockerfile.dev 注入）：
//   APP_ROOT=/workspace
//   APP_CONFIG_DIR=/workspace/config
//   APP_DATA_DIR=/workspace/.data
//   APP_RUNTIME_DIR=/workspace/.run
//   APP_LOG_DIR=/workspace/.logs

'use strict';

const projectName = process.env.PROJECT_NAME || 'myapp';

module.exports = {
  APP_ROOT:               process.env.APP_ROOT               || `/opt/${projectName}`,
  APP_CONFIG_DIR:         process.env.APP_CONFIG_DIR         || `/etc/opt/${projectName}`,
  APP_DATA_DIR:           process.env.APP_DATA_DIR           || `/var/opt/${projectName}`,
  APP_RUNTIME_DIR:        process.env.APP_RUNTIME_DIR        || `/run/${projectName}`,
  APP_LOG_DIR:            process.env.APP_LOG_DIR            || `/var/log/${projectName}`,
  APP_FRONTEND_DIR:       process.env.APP_FRONTEND_DIR       || `/opt/${projectName}/frontend`,
  APP_STATIC_VENDORS_DIR: process.env.APP_STATIC_VENDORS_DIR || `/opt/${projectName}/static_vendors`,
};

