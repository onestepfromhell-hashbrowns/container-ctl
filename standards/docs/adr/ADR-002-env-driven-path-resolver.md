# ADR-002: Env-Driven Path Resolver 路徑抽象契約

**Status**: Proposed
**Date**: 2026-08-19
**Deciders**: 系統架構討論（對話 668d3491）

---

## 問題背景 (Context)

在 Dual-Track 分軌架構下，應用程式代碼本身仍存在寫死絕對路徑的風險：
- 開發者習慣直接 `open("/workspace/config/app.yaml")` 或 `open("/opt/myapp/config.yaml")`
- 無法透過 Dockerfile 分軌自動修正代碼內的路徑假設
- 需要一個應用層的「路徑解析抽象契約」來徹底解耦

---

## 決策 (Decision)

**採用 Env-Driven Path Resolver 模式**：

所有應用程式代碼，透過標準化的 Path Resolver 模組讀取路徑，**不得硬編碼任何絕對路徑**。

### 標準環境變數契約

| 環境變數 | 開發態預設值 | 生產態預設值 |
| :--- | :--- | :--- |
| `APP_ROOT` | `/workspace` | `/opt/<project>` |
| `APP_CONFIG_DIR` | `/workspace/config` | `/etc/opt/<project>` |
| `APP_DATA_DIR` | `/workspace/.data` | `/var/opt/<project>` |
| `APP_RUNTIME_DIR` | `/workspace/.run` | `/run/<project>` |
| `APP_LOG_DIR` | `/workspace/.logs` | `/var/log/<project>` |

### 預設值設計原則

> **預設值永遠指向生產 FHS 標準路徑，而非 `/workspace`。**
> 開發容器由 `container-ctl` 的 Dev Overlay Env 機制注入覆寫值。

這確保：「若忘記注入環境變數，應用程式的行為與生產環境完全一致」。

### Python 標準實作範本

```python
import os
from pathlib import Path

_PROJECT_NAME = os.getenv("PROJECT_NAME", "myapp")

APP_ROOT = Path(os.getenv("APP_ROOT", f"/opt/{_PROJECT_NAME}"))
APP_CONFIG_DIR = Path(os.getenv("APP_CONFIG_DIR", f"/etc/opt/{_PROJECT_NAME}"))
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", f"/var/opt/{_PROJECT_NAME}"))
APP_RUNTIME_DIR = Path(os.getenv("APP_RUNTIME_DIR", f"/run/{_PROJECT_NAME}"))
APP_LOG_DIR = Path(os.getenv("APP_LOG_DIR", f"/var/log/{_PROJECT_NAME}"))
```

### Bash 標準實作範本

```bash
PROJECT_NAME="${PROJECT_NAME:-myapp}"
APP_ROOT="${APP_ROOT:-/opt/${PROJECT_NAME}}"
APP_CONFIG_DIR="${APP_CONFIG_DIR:-/etc/opt/${PROJECT_NAME}}"
APP_DATA_DIR="${APP_DATA_DIR:-/var/opt/${PROJECT_NAME}}"
APP_RUNTIME_DIR="${APP_RUNTIME_DIR:-/run/${PROJECT_NAME}}"
APP_LOG_DIR="${APP_LOG_DIR:-/var/log/${PROJECT_NAME}}"
```

---

## 正向後果 (Positive Consequences)

- 代碼庫與任何特定作業系統路徑完全解耦，可在任意環境部署
- 生產環境無需注入任何額外環境變數即可正常運行（預設即為 FHS 標準）
- Git 歷史 100% 乾淨：代碼中不存在任何環境特定的絕對路徑字串

## 負向後果 (Negative Consequences)

- 需要在全體專案中推行此規範，舊代碼有一定的侵入性改造成本
- 第三方套件可能不支援自訂路徑環境變數（需要 Wrapper 包裝）

---

## 相關文件

- [ADR-001: Dual-Track Dockerfile](ADR-001-dual-track-dockerfile.md)
- [ADR-003: scan-path-refs 靜態路徑掃描門禁](ADR-003-static-path-gate.md)
