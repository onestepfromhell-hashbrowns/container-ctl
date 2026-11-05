# 領域語境與第一性原理字典：FHS 容器標準參數檢查套件 (container_params)

## 1. 核心本質與哲學 (First Principles)

### 1.1 FHS (Filesystem Hierarchy Standard) 的本質
檔案系統階層標準的核心本質是**透過「兩大正交維度」將系統狀態與生命週期徹底解耦**：

```
                    ┌─────────────────────────┬─────────────────────────┐
                    │  靜態 (Static / 唯讀)    │  動態 (Variable / 可寫)  │
┌───────────────────┼─────────────────────────┼─────────────────────────┐
│ 可共用 (Shareable) │  /usr, /opt/pkg (程式碼) │  /var/mail, /var/spool  │
├───────────────────┼─────────────────────────┼─────────────────────────┤
│ 不可共用 (Unshared)│  /etc/opt/pkg (設定檔)   │  /var/log, /run (狀態/日誌)│
└───────────────────┴─────────────────────────┴─────────────────────────┘
```

### 1.2 容器化雙軌 (Dual-Track) 為什麼仍須遵守 FHS？
1. **最小權限原則 (Principle of Least Privilege)**：在 Prod 容器中，`/opt/<package>` 為非 root 使用者（如 `appuser:10001`）的唯讀/靜態部署目錄，而 `/run` 與 `/var/log` 則是唯一開放寫入權限的易失/持久掛載點。
2. **唯讀根檔案系統 (Read-Only Rootfs)**：符合 FHS 標準方能支援安全生產環境的 `read_only: true` 容器宣告，僅將 `/run`（tmpfs）與 `/var/log`（Volume）開放為寫入區。
3. **環境無關性 (Environment Agnosticism)**：共用核心業務代碼不應知道自己是運行在開發者本機、Dev 容器的 `/workspace`，或是 Prod 容器的 `/opt/<package>`。

---

## 2. FHS 6 大特規路徑職責矩陣

| 領域範疇 | FHS 標準特規路徑 (Prod) | 開發環境路徑 (Dev Container / Local) | 變更屬性 | 寫入權限與掛載規範 |
| :--- | :--- | :--- | :--- | :--- |
| **應用程式碼與靜態資源** | `/opt/<app-slug>` | `${APP_ROOT}` (例如 `/workspace` 或 `/app`) | 靜態 (Static) | Prod 唯讀；Dev 由 Host 掛載可即時熱重載 |
| **主機專屬設定檔** | `/etc/opt/<app-slug>` | `${APP_ROOT}/app/configs` 或 `.env` | 靜態 (Config) | 唯讀；Prod 可透過 ConfigMap / Secret 掛載 |
| **易失性執行狀態 (PID/Socket)** | `/run/<app-slug>` | `${APP_ROOT}/run` | 易失 (Volatile) | tmpfs 或動態目錄，容器重啟自動清空 |
| **日誌產物 (Logs)** | `/var/log/<app-slug>` | `${APP_ROOT}/logs` (或 stdout) | 動態 (Append-Only) | 掛載至 Host Log Volume 或 stdout 管道收集 |
| **狀態資料庫 (SQLite / Snapshots)** | `/var/lib/<app-slug>` | `${APP_ROOT}/data` | 持久 (Persistent) | 具備防篡改要求之冷備份或本地狀態快照庫 |
| **前端動態 SFC / 靜態庫** | `/opt/<app-slug>/{frontend,static_vendors}` | `${APP_ROOT}/{frontend,static_vendors}` | 靜態 (Asset) | HTTP 靜態檔案中介層直接對齊存取 |

---

## 3. 5 大反模式 (Anti-Patterns) 與防禦矩陣

在多版本共用的核心代碼（`app/`、`main.py`、`control.sh`）中，以下行為被判定為嚴重架構違規：

1. **反模式 1：業務代碼直接拼裝絕對路徑**
   - ❌ **違規**：`with open("/opt/shipping-status/app/configs/template.json")` 或 `Path("/workspace/frontend")`
   - ✅ **防禦標準**：由 Path Resolver（如 `app_paths.py`）提供動態解析介面。
2. **反模式 2：環境分支污染業務邏輯 (`if tier == "dev"`)**
   - ❌ **違規**：業務邏輯出現 `if os.getenv("APP_ENV") == "dev": use_mock() else: use_prod()`
   - ✅ **防禦標準**：共用程式碼為**環境盲 (Environment-Blind)**，透過依賴注入 (DI) 或策略適配器根據配置初始化。
3. **反模式 3：寫死特定版本端點與埠號**
   - ❌ **違規**：寫死 `http://svc-dev-webservice-v1.0.0-shared:8273` 或 `port: 8001`
   - ✅ **防禦標準**：所有服務交互端點（`DATABASE_URL`, `JWT_JWKS_URL`, `EVENT_GATEWAY_URL`, `PORT`）透過環境變數注入。
4. **反模式 4：控制腳本路徑寫死**
   - ❌ **違規**：`control.sh` 直接寫死 `/workspace/run/service.pid`
   - ✅ **防禦標準**：由 `PID_DIR="${APP_RUNTIME_DIR:-${APP_ROOT}/run}"` 依序推進。
5. **反模式 5：資料庫憑證與主機硬編碼**
   - ❌ **違規**：在代碼中寫死帳密 `postgresql://user:pass@host:5432/...`
   - ✅ **防禦標準**：預設值為 `None`，由外部環境（`.deploy/containers/*.json` 或 CI/CD Secret）宣告式注入。

---

## 4. 5 層配置覆蓋階層 (Configuration Hierarchy)

```
[最高優先權]
  ▲  Layer 4: 執行時注入與動態鑑權 (Runtime CLI / Orchestrator Env / JWKS Rotation)
  │  Layer 3: 宣告式容器清冊 (.deploy/containers/*.json, docker-compose.yml)
  │  Layer 2: 外部環境變數與 FHS 設定檔 (/etc/opt/<app-slug>/*.json, .env)
  │  Layer 1: Path Resolver 自動探測 (Candidate Repo vs. /opt/<app-slug>)
  │  Layer 0: 代碼底層安全預設 (Pydantic BaseSettings 預設值)
[最低優先權]
```

---

## 5. 專案術語彙編 (Glossary)

- **FHS**：Filesystem Hierarchy Standard 檔案系統階層標準。
- **Dual-Track**：開發軌 (Dev Track / Volume 掛載 / 熱重載) 與生產軌 (Prod Track / 不可變映像檔 / 非 root 唯讀根) 分軌設計。
- **Path Resolver**：fail-closed 的多層路徑解析器，依序由環境變數、Repo 標識探測回退至 FHS 標準路徑。
- **Environment-Blind**：環境盲設計原則，核心業務邏輯不得包含環境判斷分支。
- **Clean-Room CI Gate**：靜態與動態檢查門禁，阻止任何硬編碼路徑、環境污染或架構破壞。
