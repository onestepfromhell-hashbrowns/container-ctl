# CONTEXT.md — container-ctl 專案通用語言與架構決策

> **Issue Tracker**：`.scratch/` (Local Markdown)
> **ADR 目錄**：`docs/adr/`
> **上次更新**：2026-08-11（grill-with-docs 完整對齊）

---

## 專案範疇 (Project Scope)

設計並實作 `site` 全域微服務容器依賴編排系統。**嚴格遵循宣告式與 FHS 標準設計**。

最小可行需求（MVP）：
- Host CLI（支援 user 選單 + agent CLI）手動實例化與服務掛載
- Host Agent 偵錯支援

長期目標：CI/CD 觸發 + 自動化 Repo 容器化流程。

---

## 業務流程：專案 Repo → 容器化 (Core Business Flow)

```
【開發者 / Host Agent】
         │
         ▼
 host cli deploy <repo> <ref>
         │
         ▼
【Step 1: 讀取宣告】
 git show <ref>:.deploy/manifest.json   (設定 repo 優先，未遷移才讀 Bare Repo；無需 checkout)
 git show <ref>:.deploy/containers/<name>.json  (各容器專屬配置)
         │
         ▼
【Step 2: 依賴解析 & Blueprint 生成】
 - 解析 manifest 中的依賴清單
 - 查詢 SQLite shared_refs 判斷共享服務是否已運行
 - 鎖定所有 Image Pinned Digest（禁止 :latest）
 - 生成版本化 Blueprint JSON → $CONTAINER_CTL_DEPLOYS_DIR/<project>/<ver>.json
         │
         ▼
【Step 3: Plan / Preview】
 - CLI 輸出即將建立/複用的實例清單
 - 標明共享服務（複用 or 新建）與專屬服務（隔離新建）
 - 等待確認 (user menu) 或直接 apply (agent cli --yes)
         │
         ▼
【Step 4: Apply — Quadlet 單元生成】
 - 生成 Quadlet .container / .network 單元至
   $CONTAINER_CTL_SERVICES_DIR/<project>/
 - 寫入 Drop-In Override（Port / Volume / Env 隔離）
 - systemctl --user daemon-reload
         │
         ▼
【Step 5: 非阻塞啟動 & 狀態記錄】
 - systemctl --user start --no-block <service>  (DAG 拓撲順序)
 - 寫入 SQLite: instances 表 (實例狀態)、shared_refs 表 (Ref Count +1)
 - lifecycle_events 表記錄 provision 事件
         │
         ▼
【SSH / IDE 接入】
 ssh-entry.sh → workspace mount → 專案目錄 (掛載關係不變)
 新版 SSH 套件通知生命週期管理器：專案 active
```

---

## .deploy/ 目錄結構 (設定 repo；未遷移專案為 Project Repo 內)

```
.deploy/
├── manifest.json          ← 專案名稱、目標版本、依賴清單
└── containers/
    ├── postgres.json      ← 各容器專屬配置
    └── relay.json
```

**manifest.json 範例：**
```json
{
  "schema_version": 1,
  "project_name": "WebApp",
  "version": "v1.2.0",
  "dependencies": ["postgres", "relay"]
}
```

**containers/postgres.json 範例：**
```json
{
  "name": "postgres",
  "image": "docker.io/library/postgres:15-alpine@sha256:<pinned>",
  "scope": "dedicated",
  "tier": "dev",
  "ports": ["127.0.0.1:5432:5432"],
  "volumes": [{ "storage_target": "production", "container_path": "/var/lib/postgresql/data" }],
  "env": { "POSTGRES_DB": "webservice_db" },
  "lifecycle_policy": { "mode": "auto_idle", "idle_timeout_sec": 300 }
}
```

---

## SQLite 狀態資料庫

**路徑**：`$CONTAINER_CTL_SERVICES_DIR/state.db`

| 表名 | 用途 |
| :--- | :--- |
| `instances` | 所有已 provision 的容器實例 + Blueprint 快照、當前狀態 |
| `shared_refs` | 共享服務的活性引用計數（Ref Count）與 lifecycle_policy |
| `lifecycle_events` | 所有生命週期事件的時間序列日誌（provision / start / stop / idle / destroy）|

---

## SSH ↔ 專案生命週期關係

- **Workspace 掛載不變**：`ssh-entry.sh` → workspace mount → 專案目錄。
- **新版 SSH 套件**：負責觸發「專案 active / idle」狀態轉換事件至生命週期管理器。
- **外部依賴自治**：外部依賴服務依照各自的 `lifecycle_policy` 與 SQLite Ref Count 決定是否停止，**不直接跟隨 SSH 斷線**。

---

## 通用語言 (Ubiquitous Language)

| 術語 | 定義 |
| :--- | :--- |
| **Dual-Track** | 「營運容器」與「開發容器」嚴格分軌，禁止混用 |
| **Quadlet** | Podman Quadlet + Systemd — 營運容器編排唯一 SSOT |
| **Dynamic Network Broker** | 新版套件：訂閱 Quadlet 網路事件，廣播讓被動容器串流加入 Bridge |
| **Blueprint JSON** | CLI 解析 `.deploy/` 後生成的版本化完整狀態快照 |
| **`scope: shared`** | 全系統單例共用服務，Ref Count 管理，版本相容即重用 |
| **`scope: dedicated`** | 按專案完全隔離的專屬服務 |
| **`auto_idle`** | Ref Count 歸零後計時自動停止的生命週期策略 |
| **`always_on`** | 24/7 常駐，不受單一專案 stop 影響 |
| **Pinned Digest** | `@sha256:...` 格式 — 禁止 `:latest` |
| **Bare Repo** | `.git_repos/*.git` — 禁止直接寫入散落檔案 |

---

## 核心約束 (Hard Constraints)

1. **禁止靜態手寫與繞過編排**：不得使用未受管的靜態啟動腳本或宿主 HTTP Proxy 網路橋接。
2. **禁止 `/home/<user>` 硬編碼**。
3. **禁止 Bare Repo 直接寫入**。
4. **禁止 `:latest` Image**。
5. **禁止阻塞 SSH 大廳**：依賴啟動一律 `--no-block`。
6. **業務容器宿主發布位址分軌**：Dev 的 `ports`／Quadlet `PublishPort`／Compose `ports` 必須明列 `127.0.0.1:<host>:<container>`；Prod 必須明列 `127.0.0.2:<host>:<container>`。含協定與範圍的映射同樣適用。禁止省略 Host IP 或以 `0.0.0.0` 發布業務端口。容器命名空間內的服務監聽位址不受此條限制；cloudflared、nginx、relay 的入口與轉發端口依各自專用規範治理。
7. **宣告與實例同步**：變更端口時，同步設定 repo（未遷移專案為 Bare Repo）`.deploy`、host `.services` Quadlet 與實際掛載的外部配置；重啟受影響的執行中服務後，以 Podman 實際端口與服務探針驗證。遠端 host 的業務發布位址與 ingress upstream 必須成對遷移。

---

## 路徑語義 (Path Semantics)

| 路徑 | 角色 | 說明 |
| :--- | :--- | :--- |
| `<packages>/container-ctl/` | **工具目錄** | 管理/執行容器化的 CLI 程式碼、工具、設計文件 |
| `$CONTAINER_CTL_CONFIG_REPOS_DIR/<project>.git` | **設定 repo（宣告真源）** | 宿主管控、不掛進容器；`container-ctl config init/status`；見 ADR-008 |
| `$CONTAINER_CTL_GIT_REPOS_DIR/<project>.git` | **Bare Repo（程式碼）** | 唯讀；其 `.deploy/` 在已遷移專案為非權威舊副本 |
| `$CONTAINER_CTL_DEPLOYS_DIR/<project>/<ver>.json` | **版本化藍圖倉庫** | 儲存多個版本的 Blueprint（純配置，無實例化、未投入運行） |
| `$CONTAINER_CTL_SERVICES_DIR/<project>/` | **實例化配置** | 系統要拉起的 Quadlet 單元檔與 Drop-In Override（已有或即將有實例化專案）|
| `$CONTAINER_CTL_SERVICES_DIR/state.db` | **中央狀態 DB** | SQLite — instances / shared_refs / lifecycle_events |
| `$CONTAINER_CTL_VOLUMES_DIR/` | **持久化儲存** | 容器資料 Volume 掛載點 |
| `<site-var-root>/...` | **生產資料** | Production VHDX — 高度保護，禁止開發用途 |

> **`.deploys` vs `.services` 關鍵區別**：`.deploys` 存放多版本藍圖（非運行態，純配置快照）；`.services` 存放實際被 systemd 管理的 Quadlet 單元（運行態或曾經實例化的配置）。

---

## 架構決策記錄 (ADRs)

| ADR | 標題 | 狀態 |
| :--- | :--- | :--- |
| [ADR-001](docs/adr/ADR-001-quadlet-replaces-orchestrator.md) | Podman Quadlet + Systemd 作為容器編排唯一 SSOT | Accepted |
| [ADR-002](docs/adr/ADR-002-no-block-ssh-guard.md) | 非阻塞啟動 + SSH 生命週期套件分離 | Accepted |
| [ADR-004](docs/adr/ADR-004-dynamic-network-broker.md) | Dynamic Network Broker 動態容器網路連接 | Accepted |
| [ADR-005](docs/adr/ADR-005-sqlite-state-store.md) | SQLite 作為中央實例狀態儲存 | Accepted |
| [ADR-006](docs/adr/ADR-006-deploy-dir-in-repo.md) | `.deploy/` 目錄存於 Repo，透過 git show 讀取 | Superseded by ADR-008 |
| [ADR-008](docs/adr/ADR-008-deploy-config-repo.md) | 部署宣告獨立成設定 repo（雙讀、不掛容器） | Accepted |

> **離線依賴（SPEC-container-lifecycle §21）**：映像需要套件時預設用離線版本。Python 第三方套件 → `$CONTAINER_CTL_WHEELHOUSE_DIR/`（`pip install --no-index --find-links`，附 SHA256SUMS）；內部 wheel 放專案 `wheels/`；缺套件補進共用庫並一次列完傳遞依賴；禁止重複下載。`prepare-dev`／`prepare-prod` 對未帶 `--no-index` 的 `pip install` 輸出 WARN。
