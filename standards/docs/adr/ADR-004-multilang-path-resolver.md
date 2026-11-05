# ADR-004: 多語言 Path Resolver 插拔架構

**Status**: Proposed
**Date**: 2026-08-19
**Deciders**: Step 1 設計考問（對話 74b1facf）、Step 2 規格合成

---

## 問題背景 (Context)

`container-ctl/standards` 的 Path Resolver 機制需要服務使用不同語言的專案：

- 現有 ADR-002 已定義 `APP_*` 環境變數標準契約與 Python/Bash 實作範本
- 實際生產中存在 Node.js（前端 SSR、CLI 工具）和 Go（系統工具、Proxy）等語言需求
- 若各語言自行實作路徑解析邏輯，可能因解析順序、fallback 行為不一致導致 Dev/Prod 差異

**核心挑戰**：如何保證跨語言的路徑解析行為 100% 一致？

---

## 決策 (Decision)

**採用「語言無關中間層 + 各語言獨立實作 + 共用行為測試矩陣」的插拔架構**：

### 架構設計

```
┌─────────────────────────────────────────────────────┐
│          APP_* 環境變數契約（語言無關中間層）            │
│  APP_ROOT / APP_CONFIG_DIR / APP_DATA_DIR / ...     │
│  （由 ADR-002 定義，.deploy/containers/dev.json 注入） │
└──────────────────────┬──────────────────────────────┘
                       │ 各語言獨立讀取環境變數
         ┌─────────────┼──────────────┬──────────────┐
         ▼             ▼              ▼              ▼
  app_paths.py   app_paths.sh   app_paths.js   app_paths.go
  （Python）      （Bash）        （Node.js）     （Go）
```

### 標準實作規格

所有語言實作必須遵守以下行為規格（BDD 行為測試矩陣基準）：

| 行為 | 規格 |
| :--- | :--- |
| **環境變數存在** | 直接回傳環境變數的值，不做任何後處理 |
| **環境變數不存在** | 回傳對應的 FHS 預設值（使用 `PROJECT_NAME` 動態組合） |
| **`PROJECT_NAME` 未設定** | Fallback 到 `myapp`（或各專案替換後的預設值） |
| **路徑回傳型別** | 字串（各語言可包裝為 Path 物件，但核心邏輯基於字串比較） |
| **不得有副作用** | 模組匯入時不得建立目錄、不得觸發 I/O |

### Node.js 標準實作範本

```javascript
// src/lib/app_paths.js
'use strict';

const projectName = process.env.PROJECT_NAME || 'myapp';

module.exports = {
  APP_ROOT:        process.env.APP_ROOT        || `/opt/${projectName}`,
  APP_CONFIG_DIR:  process.env.APP_CONFIG_DIR  || `/etc/opt/${projectName}`,
  APP_DATA_DIR:    process.env.APP_DATA_DIR    || `/var/opt/${projectName}`,
  APP_RUNTIME_DIR: process.env.APP_RUNTIME_DIR || `/run/${projectName}`,
  APP_LOG_DIR:     process.env.APP_LOG_DIR     || `/var/log/${projectName}`,
};
```

### Go 標準實作範本

```go
// src/lib/app_paths.go
package apppaths

import (
    "fmt"
    "os"
)

func getEnvOrDefault(key, defaultFmt string, args ...any) string {
    if v := os.Getenv(key); v != "" {
        return v
    }
    return fmt.Sprintf(defaultFmt, args...)
}

func projectName() string {
    if n := os.Getenv("PROJECT_NAME"); n != "" {
        return n
    }
    return "myapp"
}

var (
    AppRoot       = getEnvOrDefault("APP_ROOT",        "/opt/%s",         projectName())
    AppConfigDir  = getEnvOrDefault("APP_CONFIG_DIR",  "/etc/opt/%s",     projectName())
    AppDataDir    = getEnvOrDefault("APP_DATA_DIR",    "/var/opt/%s",     projectName())
    AppRuntimeDir = getEnvOrDefault("APP_RUNTIME_DIR", "/run/%s",         projectName())
    AppLogDir     = getEnvOrDefault("APP_LOG_DIR",     "/var/log/%s",     projectName())
)
```

### APP_* 命名規則與各專案替換

`APP_*` 為模板前綴，各專案須替換為具語義的前綴（詳見 CONTEXT.md「APP_* 命名規則」段落）。
各語言實作模板中的 `APP_*` 字串應同步替換，確保所有語言使用相同的業務前綴。

### 行為測試矩陣（BDD）

各語言實作必須通過以下測試案例（位於 `tests/path-resolver-compat/`）：

| 測試案例 | 環境變數輸入 | 期望輸出 |
| :--- | :--- | :--- |
| 全部設定 | `APP_ROOT=/workspace, PROJECT_NAME=proj` | `APP_ROOT=/workspace` |
| 全部未設定 | `PROJECT_NAME=proj`（無 APP_ROOT）| `APP_ROOT=/opt/proj` |
| PROJECT_NAME 未設定 | （無任何變數）| `APP_ROOT=/opt/myapp` |
| FHS 生產預設值 | `APP_ROOT=/opt/proj`（顯式設定） | `APP_ROOT=/opt/proj` |

---

## 正向後果 (Positive Consequences)

- 各語言可獨立引入標準件，不需跨語言依賴
- 共用行為測試矩陣確保跨語言一致性，防止 Dev/Prod 行為差異
- 新語言的加入只需實作標準規格並通過 BDD 測試矩陣，無需修改中間層

## 負向後果 (Negative Consequences)

- 需維護多份語言實作，有輕微維護成本
- **緩解方案**：BDD 測試矩陣作為契約，實作者只需通過測試，無需閱讀其他語言代碼

---

## 相關文件

- [ADR-002: Env-Driven Path Resolver 路徑抽象契約](ADR-002-env-driven-path-resolver.md)
- [ADR-003: scan-path-refs 靜態路徑掃描門禁](ADR-003-static-path-gate.md)
- [CONTEXT.md — APP_* 命名規則](../../../CONTEXT.md)
