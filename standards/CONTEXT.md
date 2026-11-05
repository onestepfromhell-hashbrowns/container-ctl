# CONTEXT.md — container-ctl/standards 專案通用語言與架構決策

> **Issue Tracker**：`.scratch/` (Local Markdown)
> **ADR 目錄**：`docs/adr/`
> **上次更新**：2026-08-19（Step 2 規格合成：消除命名衝突、補充多語言 Path Resolver 與 scope-extensions 分發機制）

---

## 專案範疇 (Project Scope)

**目標**：為 `site` 容器化開發環境制定「開發態路徑（`/workspace`）」與「運行態路徑（FHS 標準）」的設計規範、實作工具與架構邊界。

**核心問題**：
> 開發容器的工作目錄為 `/workspace`，但實際 runtime 的目錄必須符合 FHS 標準（`/opt`、`/etc/opt`、`/var/opt`、`/run`）。如何讓容器內開發環境與生產 FHS 標準環境保持一致？

**採用方案**：**方案 4（Dual-Track Dockerfile 分軌構建）＋ 方案 2（Env-Driven Path Resolver 環境變數抽象層）** 的黃金組合。

---

## 業務流程：Dev/Prod 雙軌構建閉環 (Core Business Flow)

```
【專案 Git Repo】
       │
       ├──── .deploy/containers/dev.json    ← 開發容器宣告
       │         └── Dockerfile.dev          ← WORKDIR /workspace + Agent 工具
       │                   │                    注入 APP_* 環境變數（dev 覆寫值）
       │
       └──── .deploy/containers/prod.json   ← 生產容器宣告
                 └── Dockerfile.prod         ← WORKDIR /opt/<project> + FHS 嚴格隔離
                           │                    預設 APP_* 環境變數（FHS 標準值）

         scan-path-refs (CI 門禁)
         ── 攔截任何 /workspace 殘留在生產配置中
```

---

## 通用語言 (Ubiquitous Language)

| 術語 | 定義 |
| :--- | :--- |
| **Dev Track** | 開發軌：以 `Dockerfile.dev` 構建，`WORKDIR /workspace`，支援 Live Mount、Agent、IDE Server。 |
| **Prod Track** | 生產軌：以 `Dockerfile.prod` 構建，`WORKDIR /opt/<project>`，遵循嚴格 FHS，Quadlet 管理。 |
| **Host Publish IP** | 業務容器發布至宿主機時，Dev 固定 `127.0.0.1`、Prod 固定 `127.0.0.2`；`ports`、`PublishPort` 及 Compose `ports` 都要明列 Host IP。容器內部 listener 可依網橋需求使用 `0.0.0.0`。cloudflared、nginx、relay 入口另循專用規範。 |
| **Path Resolver** | 應用程式內的路徑解析抽象層，優先讀取 `APP_*` 環境變數，預設值為生產 FHS 路徑。本模組的實作模板命名為 `app_paths.*`（非 `path_resolver.*`），以避免與 `<site-utils>/path_resolver.py`（解析 site 工具套件路徑）混淆。兩者語義層次不同：前者解析應用程式業務路徑，後者解析 site 基礎設施路徑。 |
| **APP_ROOT** | 應用程式根目錄環境變數：Dev=`/workspace`，Prod=`/opt/<project>`。 |
| **APP_CONFIG_DIR** | 配置目錄環境變數：Dev=`/workspace/config`，Prod=`/etc/opt/<project>`。 |
| **APP_DATA_DIR** | 可變資料目錄環境變數：Dev=`/workspace/.data`，Prod=`/var/opt/<project>`。 |
| **APP_RUNTIME_DIR** | 行程運行目錄環境變數：Dev=`/workspace/.run`，Prod=`/run/<project>`。 |
| **FHS Parity** | 開發態能完整模擬 FHS 標準路徑語義的特性，防止 Dev/Prod 路徑斷層。 |
| **Path Hardcoding** | 在代碼或配置中寫死絕對路徑字串（如 `/workspace/config`），被 `scan-path-refs` 視為違規。 |
| **Dual-Track Drift** | 兩個 Dockerfile track 的基礎依賴（如 Python 版本）產生版本漂移，需靠 Shared Base Image 和 CI 防護。 |
| **Dev Overlay Env** | 開發容器啟動時由 `container-ctl` 自動注入的環境變數覆寫組，用於讓 Path Resolver 指向 `/workspace`。 |
| **Static Gate** | `scan-path-refs` 靜態掃描工具在 CI/CD 流水線中的掃描關卡，阻斷路徑硬編碼進入生產配置。 |

---

## 架構約束 (Hard Constraints)

1. **`APP_*` 優先原則**：應用程式內所有路徑解析必須優先讀取 `APP_*` 環境變數，禁止直接硬編碼 `/workspace` 或 `/opt/<project>`。
2. **預設值必須為生產 FHS 標準**：Path Resolver 的預設值（fallback）永遠是 FHS 標準路徑，而非 `/workspace`。
3. **`Dockerfile.dev` 只能承載開發輔助工具**：除錯器、Language Server、Agent CLI 等開發專屬工具禁止出現在 `Dockerfile.prod`。
4. **`scan-path-refs` 為必設門禁**：禁止繞過或跳過靜態路徑掃描閘門。
5. **生產容器根檔案系統必須唯讀**：`Dockerfile.prod` 產生的容器映像需支援 `--read-only` rootfs 模式，所有可寫入路徑必須透過 Volume 外掛。
6. **禁止 `/home/<user>` 路徑**（繼承自 site 全域約束）。
7. **跨語言行為一致性**：`app_paths.*` 各語言實作（Python/Bash/Node.js/Go）必須對相同 `APP_*` 環境變數輸入產生相同的路徑解析結果，由共用行為測試矩陣（BDD）保證一致性。
8. **雙軌宿主端口隔離**：Dev 業務容器若發布端口，使用 `127.0.0.1:<host-port>:<container-port>`；Prod 使用 `127.0.0.2:<host-port>:<container-port>`。禁止省略 Host IP 或用 `0.0.0.0` 發布。同步更新 Repo 宣告、實例化 workspace、host Quadlet 與外部綁定配置，並驗證活躍容器實際發布位址。

---

## 路徑語義 (Path Semantics)

| 路徑 | 角色 | 說明 |
| :--- | :--- | :--- |
| `<site-opt-root>/standards/` | **工具目錄** | 本模組的設計規範、實作工具、模板代碼 |
| `.deploy/containers/dev.json` | **Dev 容器宣告** | 開發容器的 site 格式宣告，含 Dev Overlay Env |
| `.deploy/containers/prod.json` | **Prod 容器宣告** | 生產容器的 site 格式宣告 |
| `Dockerfile.dev` | **開發鏡像** | Dev Track 容器構建指令 |
| `Dockerfile.prod` | **生產鏡像** | Prod Track 容器構建指令 |
| `src/lib/app_paths.py` | **路徑解析模組（Python）** | 標準 Path Resolver 實作模板（Python）；注意：與 `<site-utils>/path_resolver.py` 語義層次不同 |
| `src/lib/app_paths.sh` | **路徑解析模組（Bash）** | 標準 Path Resolver 實作模板（Bash） |
| `src/lib/app_paths.js` | **路徑解析模組（Node.js）** | 標準 Path Resolver 實作模板（Node.js） |
| `src/lib/app_paths.go` | **路徑解析模組（Go）** | 標準 Path Resolver 實作模板（Go） |
| `tools/scan-path-refs` | **靜態掃描工具** | CI 路徑硬編碼掃描門禁（獨立實作，非 site utilities 擴展） |

---

## 分發機制 (Distribution Mechanism)

本模組的設計規範與標準件（`app_paths.*` 模板、`scan-path-refs` 工具、Dockerfile 模板）透過 **`<site-opt-root>/scope-extensions` 動態附加機制**注入到各專案的開發容器中：

```
container-ctl/standards（規範定義者）
        │
        └── scope-extensions（分發管道）
                    │
                    ├── project-A 開發容器  ← app_paths.* 模板、scan-path-refs
                    ├── project-B 開發容器  ← app_paths.* 模板、scan-path-refs
                    └── project-N 開發容器  ← （按需附加）
```

各專案基礎功能（`APP_*` 環境變數解析、路徑掃描門禁）開箱即用，進階擴充（自訂例外規則、語言特定 Wrapper）由各專案自行處理。

---

## APP_* 命名規則 (APP_* Naming Convention)

`APP_*` 為本模組定義的**模板前綴**（Template Prefix），用於說明架構模式。實際專案部署時，必須替換為具業務語義的前綴：

| 模板名稱 | 替換範例（Web Service） | 替換範例（DB Proxy） |
| :--- | :--- | :--- |
| `APP_ROOT` | `WEBSERVICE_ROOT` | `DBPROXY_ROOT` |
| `APP_CONFIG_DIR` | `WEBSERVICE_CONFIG_DIR` | `DBPROXY_CONFIG_DIR` |
| `APP_DATA_DIR` | `WEBSERVICE_DATA_DIR` | `DBPROXY_DATA_DIR` |
| `APP_RUNTIME_DIR` | `WEBSERVICE_RUNTIME_DIR` | `DBPROXY_RUNTIME_DIR` |
| `APP_LOG_DIR` | `WEBSERVICE_LOG_DIR` | `DBPROXY_LOG_DIR` |

> **規則**：同一服務的所有環境變數必須使用相同前綴。前綴應與服務的 Quadlet 單元名稱或容器名稱一致，便於運維識別。

---

## 架構決策記錄 (ADRs)

| ADR | 標題 | 狀態 |
| :--- | :--- | :--- |
| [ADR-001](docs/adr/ADR-001-dual-track-dockerfile.md) | Dual-Track Dockerfile 分軌構建策略 | Proposed |
| [ADR-002](docs/adr/ADR-002-env-driven-path-resolver.md) | Env-Driven Path Resolver 路徑抽象契約 | Proposed |
| [ADR-003](docs/adr/ADR-003-static-path-gate.md) | scan-path-refs 靜態路徑掃描門禁 | Proposed |
| [ADR-004](docs/adr/ADR-004-multilang-path-resolver.md) | 多語言 Path Resolver 插拔架構 | Proposed |
