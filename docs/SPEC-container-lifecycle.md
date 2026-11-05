# 技術規格書：容器依賴編排與生命週期管理系統

- **文件版本**：v1.0
- **狀態**：Active
- **日期**：2026-08-11
- **專案目錄**：`<packages>/container-ctl/`
- **Issue Tracker**：`.scratch/`

---

## 1. 系統概述

本系統為 `site` 環境提供微服務容器依賴編排與生命週期管理能力。

**設計原則**：
- 嚴格遵循宣告式與 FHS 標準架構
- 以 Podman Quadlet + Systemd 作為唯一容器編排 SSOT
- 業務配置與原始碼版本綁定（存於 Repo `.deploy/`）
- CLI 兩階段操作（plan → apply）確保可審查性
- SQLite 中央狀態庫統一管理實例、引用計數與事件

---

## 2. 路徑語義

| 路徑 | 角色 |
| :--- | :--- |
| `<packages>/container-ctl/` | CLI 工具程式碼與設計文件（本倉庫） |
| `$CONTAINER_CTL_GIT_REPOS_DIR/<project>.git` | Bare Repo — 唯讀，`git show` 讀取 `.deploy/` |
| `$CONTAINER_CTL_DEPLOYS_DIR/<project>/<ver>.json` | 版本化 Blueprint 倉庫（非運行態純配置） |
| `$CONTAINER_CTL_SERVICES_DIR/<project>/` | 實例化 Quadlet 單元 + Drop-In Override |
| `$CONTAINER_CTL_SERVICES_DIR/state.db` | SQLite 中央狀態資料庫 |
| `$CONTAINER_CTL_VOLUMES_DIR/` | Volume 持久化掛載點（路徑沿用，新版配置正確寫入） |
| `<site-var-root>/...` | Production VHDX — 生產資料，高度保護 |

---

## 3. 專案 Repo 容器化宣告格式（`.deploy/`）

### 3.1 目錄結構

```
<project-repo>/
└── .deploy/
    ├── manifest.json
    └── containers/
        ├── postgres.json
        └── relay.json
```

Host CLI 讀取方式（**禁止 checkout，一律透過 git show**）：

```bash
git -C $CONTAINER_CTL_GIT_REPOS_DIR/<project>.git \
    show <ref>:.deploy/manifest.json
```

### 3.2 `manifest.json` Schema

```json
{
  "schema_version": 1,
  "project_name": "WebApp",
  "version": "v1.2.0",
  "dependencies": ["postgres", "relay"]
}
```

| 欄位 | 型態 | 說明 |
| :--- | :--- | :--- |
| `schema_version` | int | 固定為 `1`（當前版本） |
| `project_name` | string | 唯一專案識別名稱 |
| `version` | string | 此宣告版本（對應 git ref/tag） |
| `dependencies` | string[] | `containers/` 內的容器名稱清單，決定實例化順序依賴 |

### 3.3 `containers/<name>.json` Schema

```json
{
  "name": "postgres",
  "image": "docker.io/library/postgres:15-alpine@sha256:<pinned-digest>",
  "scope": "dedicated",
  "tier": "dev",
  "ports": ["127.0.0.1:5432:5432"],
  "volumes": [
    {
      "host_path": "$CONTAINER_CTL_VOLUMES_DIR/<project>/postgres",
      "container_path": "/var/lib/postgresql/data",
      "options": "rw"
    }
  ],
  "env": {
    "POSTGRES_DB": "webservice_db",
    "POSTGRES_USER": "app"
  },
  "lifecycle_policy": {
    "mode": "auto_idle",
    "idle_timeout_sec": 300
  }
}
```

| 欄位 | 型態 | 約束 |
| :--- | :--- | :--- |
| `name` | string | 必填，對應 `manifest.dependencies` 清單 |
| `image` | string | 必填，**必須包含 `@sha256:` Pinned Digest，禁止 `:latest`** |
| `scope` | enum | `"shared"` 或 `"dedicated"`（見 §4） |
| `tier` | enum | `"dev"` 或 `"prod"`；決定業務容器宿主發布 IP |
| `ports` | string[] | 業務容器須用 `"<host-ip>:<host-port>:<container-port>[/protocol]"`；Dev=`127.0.0.1`、Prod=`127.0.0.2`，可用相同 Host port 分軌並存。禁止省略 Host IP 或使用 `0.0.0.0`。入口容器 cloudflared／nginx／relay 依專用規範處理。 |
| `volumes` | object[] | `host_path` / `container_path` / `options` |
| `env` | object | 鍵值對，敏感值應透過 systemd credential 注入（非明文） |
| `lifecycle_policy.mode` | enum | `"manual"`、`"auto_idle"` 或 `"always_on"`；dev 建議明寫（缺省隱含 manual，validate 給 WARN），其餘缺省 `always_on` |
| `lifecycle_policy.idle_timeout_sec` | int | 僅 `auto_idle` 有效，預設 300 |

---

### 3.4 manual 邊界與 `app_hooks`（兩層生命週期）

生命週期分兩層，宣告各管各的：

| 層 | 由誰管 | 內容 |
| --- | --- | --- |
| 容器層 | `lifecycle_policy.mode` | `manual`：Quadlet 無 `[Install]`、`redeploy`/`apply` 只生成單元與 state.db、不啟動；以 `container-ctl service start\|restart <project> <service> --container-name <exact>` 啟動 |
| app 層 | `app_hooks`（選填） | 容器內應用的啟動鏈，指向專案 Tier-2 `control.sh`；`manual` 容器常為 `sleep infinity` 空殼，重啟後 app 不會自動回來 |

```json
"app_hooks": {
  "start": "bash control.sh start",
  "readiness_probe": ["curl", "-fsS", "http://127.0.0.1:8080/health"],
  "start_timeout": 120,
  "probe_timeout": 60
}
```

- `start`、`readiness_probe`：字串（`sh -c`）或字串陣列（直接 exec，皆經 `podman exec`）。`start` 必填。
- **`start` 必須自我脫離**：若 `control.sh start` 會常駐（daemon 繼承 exec 的 stdout/stderr），`podman exec` 永遠等不到 EOF，工具逾時失敗。寫法：`cd /workspace && setsid bash control.sh start >>logs/start.log 2>&1 </dev/null &`；探針以 `readiness_probe` 判定成敗，不靠 start 的結束碼。
- `service start|restart` 的保證：治理准入 → pinned image 與 Quadlet 的 Image 一致 → Quadlet 的 Exec/Volume/Environment/PublishPort/Network 與 blueprint 渲染一致（否則拒絕，需 `redeploy --service`）→ 送出 systemd job → **等 unit active 且容器 Running 才寫 state.db**（逾時記 `*_failed` 事件、不改狀態）。
- `--with-app`：容器就緒後執行 `app_hooks.start`，再輪詢 `readiness_probe`；任一步失敗即非零結束。未加此旗標時 CLI 會提示 app 尚未啟動。
- `redeploy` 不重啟 manual 實例：宣告、Quadlet、DB 更新後，運行中容器維持舊內容直到 `service restart`；此期間 `audit-dev` 報漂移屬預期。
- 不在 manual 範圍：prod 的 always_on 實例、`auto_idle` 的閒置回收。

## 4. 服務範疇模型（Scope）

### 4.1 `scope: "shared"` — 共享服務

- **原則**：全系統單例，版本相容即重用，絕不重複建立
- **Ref Count**：由 `shared_refs` 表維護，每個專案 attach 時 +1，detach 時 -1
- **停止條件**：Ref Count 歸零後，依 `lifecycle_policy.mode` 決定
  - `auto_idle`：計時 `idle_timeout_sec` 秒後停止
  - `always_on`：永不自動停止
- **容器命名**：dev 為 `svc-<action.name>-shared`；prod 為 `svc-<action.name>-<version>-shared`。action.name 已含 tier 前綴。

### 4.2 `scope: "dedicated"` — 專屬服務

- **原則**：按專案與實例 ID 完全隔離
- **Port / Volume**：各實例獨立，由 Drop-In Override 指定
- **生命週期**：與所屬專案生命週期綁定；專案 destroy 時一併清理
- **容器命名**：dev 為 `svc-<action.name>`；prod 為 `svc-<action.name>-<version>`。action.name 已含 tier 前綴。

---

## 5. CLI 業務流程

### 5.1 整體流程圖

```
host cli deploy <project> <ref>
        │
        ▼
  [讀取宣告]
  git show <ref>:.deploy/manifest.json
  git show <ref>:.deploy/containers/*.json
        │
        ▼
  [依賴解析 & Blueprint 生成]
  ├─ 解析 manifest.dependencies DAG
  ├─ 查詢 SQLite shared_refs（共享服務是否已運行）
  ├─ 驗證所有 image 包含 @sha256 Pinned Digest
  └─ 生成 Blueprint JSON
     → $CONTAINER_CTL_DEPLOYS_DIR/<project>/<ver>.json
        │
        ▼
  [Plan / Preview]
  輸出即將建立 / 複用的實例清單
  標明：NEW | REUSE | CONFLICT
  等待確認（user menu）或 --yes（agent cli）
        │
        ▼
  [Apply — Quadlet 生成]
  ├─ 生成 .container 單元（從共通模板）
  ├─ 生成 .network 單元
  ├─ 寫入 Drop-In Override（Port / Volume / Env）
  │   路徑：$CONTAINER_CTL_SERVICES_DIR/<project>/
  └─ systemctl --user daemon-reload
        │
        ▼
  [非阻塞啟動（DAG 拓撲順序）]
  systemctl --user start --no-block <service>
        │
        ▼
  [狀態記錄]
  ├─ instances：寫入實例記錄 + Blueprint 快照
  ├─ shared_refs：Ref Count +1
  └─ lifecycle_events：寫入 provision 事件
```

### 5.2 CLI 指令規格

| 指令 | 說明 |
| :--- | :--- |
| `deploy plan <project> <ref>` | 讀取 `.deploy/`，生成 Blueprint，輸出 preview |
| `deploy apply <project> <ref> [--yes]` | plan + 確認後 apply（生成 Quadlet + 啟動）|
| `deploy stop <project>` | 停止專案實例，共享服務依 Ref Count 決定 |
| `deploy destroy <project>` | 停止並清除所有專案實例與 Quadlet 單元 |
| `status list` | 列出所有實例狀態（來自 SQLite） |
| `status show <project>` | 顯示指定專案的詳細實例狀態 |

---

## 6. Quadlet 單元生成規格

### 6.1 具體容器單元

路徑：`$CONTAINER_CTL_SERVICES_DIR/<project>/svc-<inst-id>.container`
其中 dev 的 `inst-id` 不含版本；prod 的 `inst-id` 含版本。共享服務另加 `-shared`。

```ini
[Unit]
Description=Container svc-<inst-id> for project <project>
After=network.target

[Container]
ContainerName=svc-<inst-id>
Image=<pinned-image>

[Service]
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
```

### 6.2 實例 Drop-In Override

路徑：`$CONTAINER_CTL_SERVICES_DIR/<project>/svc-<inst-id>.container.d/10-override.conf`

```ini
[Container]
PublishPort=127.0.0.1:5432:5432
Volume=$CONTAINER_CTL_VOLUMES_DIR/<project>/postgres:/var/lib/postgresql/data:rw
Environment=POSTGRES_DB=webservice_db
```

### 6.3 `.network` 單元

路徑：`$CONTAINER_CTL_SERVICES_DIR/<project>/net-<project>.network`

```ini
[Network]
NetworkName=net-prod-<project>
Driver=bridge
```

---

## 7. SQLite 狀態資料庫

**路徑**：`$CONTAINER_CTL_SERVICES_DIR/state.db`

### 7.1 `instances` 表

```sql
CREATE TABLE instances (
    id            TEXT PRIMARY KEY,        -- <project>-<name>-<version>
    project_name  TEXT NOT NULL,
    service_name  TEXT NOT NULL,
    version       TEXT NOT NULL,
    scope         TEXT NOT NULL,           -- 'shared' | 'dedicated'
    status        TEXT NOT NULL,           -- 'provisioned' | 'running' | 'stopped' | 'destroyed'
    blueprint     TEXT NOT NULL,           -- JSON snapshot
    quadlet_path  TEXT NOT NULL,           -- .services/<project>/ 路徑
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

### 7.2 `shared_refs` 表

```sql
CREATE TABLE shared_refs (
    service_id       TEXT PRIMARY KEY,     -- <name>-<version>-shared
    service_name     TEXT NOT NULL,
    version          TEXT NOT NULL,
    ref_count        INTEGER NOT NULL DEFAULT 0,
    lifecycle_mode   TEXT NOT NULL,        -- 'auto_idle' | 'always_on'
    idle_timeout_sec INTEGER,
    idle_since       TEXT,                 -- 歸零計時起點（ISO8601）
    status           TEXT NOT NULL
);
```

### 7.3 `lifecycle_events` 表

```sql
CREATE TABLE lifecycle_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  TEXT NOT NULL,
    event_type   TEXT NOT NULL,  -- 'provision'|'start'|'stop'|'idle'|'destroy'
    triggered_by TEXT,           -- 'cli'|'ssh_disconnect'|'idle_timeout'|'ref_count_zero'
    detail       TEXT,           -- JSON 附加資訊
    occurred_at  TEXT NOT NULL   -- ISO8601
);
```

---

## 8. Dynamic Network Broker

### 8.1 職責

提供動態臨時容器通訊與網路事件自動連接的業務能力。

### 8.2 運作機制

```
[Quadlet / systemd]
  網路事件（net-prod-<project>.network 建立）
        │
        ▼
[Dynamic Network Broker]
  訂閱 systemd D-Bus 或 Podman events socket
  偵測到新 Bridge 網路建立
        │
        ▼
  廣播通知目標被動容器（如 shared Relay）
        │
        ▼
[被動容器]
  透過 Podman 原生 API 自行 attach 加入 Bridge 網路
```

### 8.3 介面設計（待細化）

- 事件訂閱：Podman events socket（`podman events --format json`）
- 通知方式：Unix socket 或輕量 HTTP（localhost）
- 配置：`$CONTAINER_CTL_SERVICES_DIR/infra/broker-config.json`

---

## 9. SSH ↔ 生命週期整合

- **Workspace 掛載**：`ssh-entry.sh` → workspace mount → 專案目錄（**不變**）
- **新版 SSH 套件**：SSH 登入時通知生命週期管理器「專案 active」；登出時通知「專案 idle」
- **外部依賴自治**：外部依賴服務依 `lifecycle_policy` 與 Ref Count 自行決定，**不直接跟隨 SSH 斷線停止**
- **啟動保障**：任何容器啟動均使用 `systemctl --user start --no-block`，**嚴禁阻塞 SSH 大廳登入**

---

## 10. 廢棄架構與禁止模式

| 模式 | 禁止理由 | 規範方案 |
| :--- | :--- | :--- |
| 靜態 Shell 編排腳本 | 無自動重啟、無依賴編排 | Podman Quadlet + Systemd |
| 宿主機 HTTP 網路橋接 API | 與 Quadlet 原生網路不整合 | Dynamic Network Broker |
| 手動橋接 CLI 管道 | 同上 | Dynamic Network Broker |

---

## 11. 硬性約束（Hard Constraints）

1. **禁止未受管編排與橋接管道**：一律使用 Podman Quadlet 與 Dynamic Network Broker
2. **禁止 `/home/<user>` 硬編碼**：所有路徑使用 FHS 規範路徑
3. **禁止 Bare Repo 直接寫入**：`.git_repos/*.git` 內部不寫入散落檔案
4. **禁止 `:latest` Image**：所有 image 必須包含 `@sha256:` Pinned Digest
5. **禁止阻塞 SSH 大廳**：所有容器啟動使用 `--no-block`

---

## 12. 系統控制業務功能

CLI 提供統一的系統控制入口 `container-ctl`：

| 操作類型 | CLI 指令 | 說明 |
| :--- | :--- | :--- |
| **啟動** | `container-ctl service start <project> <service> --container-name <名稱>` | systemd unit 優先，fallback 至 podman |
| **關閉（優雅）** | `container-ctl deploy stop <project>` | 先停 WebService（保障 SQLite WAL flush），後停 Relay |
| **快速重啟** | `container-ctl service restart <project> <service> --container-name <名稱>` | `systemctl --user restart`，不重建 Quadlet |
| **熱重載（Remount）** | `container-ctl deploy redeploy <project> <ref>` | 重讀 `.deploy/`，重生成 Quadlet，daemon-reload，重啟 |
| **WSL 安全關閉** | `container-ctl wsl-shutdown` | 依序停止，等待 SQLite flush |
| **WSL 安全重啟** | `container-ctl wsl-restart` | 同上 + 提示 `wsl --shutdown` |
| **健康診斷** | `container-ctl status list` / `status show <project>` | 含隧道端點 + podman ps + SQLite 狀態 |
| **防呆批次確認** | `--yes` flag / 互動式 y/N 提示 | Agent CLI 用 `--yes` 跳過，user 互動確認 |

### 12.1 優雅停止順序（Graceful Stop Order）

必須按以下 DAG 倒序停止，確保資料完整性：

```
1. 開發容器（agent-dev-* / 已 detach workspace）
2. 業務 WebService（等待 SQLite WAL checkpoint 完成）
3. Relay / Tunnel 服務
4. Watchdog 服務
5. 共享服務（依 Ref Count 與 lifecycle_policy 決定）
6. 基礎設施 Pod / SSH 隧道（最後停止，保持控制通道）
```

---

## 13. 熱重載機制（Hot Redeploy）

### 13.1 定義與觸發條件

**熱重載（redeploy）**：在**不中斷 SSH 大廳連線**、**不影響其他專案**的前提下，重新讀取 Repo 的新版 `.deploy/` 宣告、重生成 Quadlet 單元並重啟指定服務。

觸發情境：
- 專案更新了 `.deploy/` 配置後重新 push（版本 tag 變更）
- Port / Volume / Env 需要更新
- Image 需要升級至新的 Pinned Digest

### 13.2 熱重載流程

```
container-ctl deploy redeploy <project> <new-ref>
        │
        ▼
  [驗證新版 Blueprint]
  git show <new-ref>:.deploy/manifest.json
  git show <new-ref>:.deploy/containers/*.json
  驗證所有 image 包含 @sha256 Pinned Digest
        │
        ▼
  [生成新 Blueprint JSON]
  → $CONTAINER_CTL_DEPLOYS_DIR/<project>/<new-ver>.json
        │
        ▼
  [Plan 差異預覽]
  比對新舊 Blueprint，輸出變更清單（NEW/CHANGED/UNCHANGED/REMOVED）
  等待確認（互動）或 --yes（agent）
        │
        ▼
  [Apply — 重生成 Quadlet]
  覆寫 .services/<project>/ 內的 .container / .network / Override 檔
  systemctl --user daemon-reload
        │
        ▼
  [安全重啟]
  systemctl --user restart <affected-services>  ← 僅重啟有變更的服務
  更新 SQLite instances 表與 lifecycle_events
```

### 13.3 熱重載約束

- **不影響其他專案**：只重啟 `<project>` 名下的服務，共享服務透過 Ref Count 判斷是否需要更新
- **不阻塞 SSH**：reload 過程中 SSH 大廳保持可用，開發容器 workspace 掛載不斷開
- **回滾能力**：`.deploys/` 保留所有版本 Blueprint，可執行 `redeploy <project> <old-ref>` 回滾

---

## 14. 備份機制（Backup）

### 14.1 備份範疇

| 備份對象 | 路徑 | 重要性 | 備份策略 |
| :--- | :--- | :--- | :--- |
| **SQLite 狀態 DB** | `$CONTAINER_CTL_SERVICES_DIR/state.db` | 高 | 每日定時備份，SQLite `.backup` 熱備份（WAL 模式） |
| **Quadlet 單元與 Override** | `$CONTAINER_CTL_SERVICES_DIR/<project>/` | 中 | Blueprint JSON 即可重生成，Git 追蹤為主 |
| **Blueprint 版本倉庫** | `$CONTAINER_CTL_DEPLOYS_DIR/` | 中 | 只讀快照，版本歷史自動保留 |
| **Volume 持久化資料** | `$CONTAINER_CTL_VOLUMES_DIR/` | **最高** | 依容器類型與 storage_target 決定備份策略 |

### 14.2 SQLite WAL 熱備份指令

```bash
# 在 CLI 中提供 `container-ctl backup state` 指令
sqlite3 $CONTAINER_CTL_SERVICES_DIR/state.db \
    ".backup '$CONTAINER_CTL_SERVICES_DIR/state.db.bak.$(date +%Y%m%d_%H%M%S)'"
```

- 使用 SQLite `.backup` API（非 `cp`），確保備份時資料一致性
- 在 `wsl-shutdown` 流程末尾自動執行一次備份

### 14.3 Volume 備份觸發點

- **WSL 安全關閉前**：`container-ctl wsl-shutdown` 自動觸發 Volume 備份鉤子（用戶可配置）
- **手動備份**：`container-ctl backup volumes <project>` 執行指定專案 Volume tar 打包

---

## 15. 已知問題應對機制（Operational Hardening）

基於既有運維問題案例，以下防禦措施**必須**納入新版設計：

### 15.1 Pod / 容器殘留後狀態不一致（對應案例八）

**問題**：systemd 關閉時 D-Bus 提前關閉，Pod 元資料殘留但 Infra 容器已消失，下次啟動時錯誤判定為「已存在」。

**新版防禦**：
- 啟動前，CLI 一律使用 **Infra 容器 ID 存在性** 而非 `pod exists` 作為判斷依據
- 若檢測到元資料殘留但 Infra 容器不存在，自動執行 `podman pod rm -f` 清理後重建
- SQLite `instances` 表狀態與實際 Podman 狀態定期對齊（startup reconcile）

```bash
# 新版啟動守衛邏輯（偽代碼）
svc_id=$(podman container inspect svc-<inst-id> --format "{{.Id}}" 2>/dev/null)
if [ -z "$svc_id" ]; then
    # 清理殘留 Quadlet 狀態，重新 apply
    systemctl --user reset-failed svc-<inst-id>.service || true
    systemctl --user start svc-<inst-id>.service
fi
```

### 15.2 動態 SSH 使用者 XDG 權限衝突（對應案例十二）

**問題**：`s-*` 動態使用者執行 Podman CLI 時，XDG_RUNTIME_DIR 指向 `/run/user/1000` 造成權限拒絕。

**新版防禦**：
- CLI 工具執行環境必須設定：
  ```bash
  export XDG_RUNTIME_DIR="/tmp/run-${USER}"
  export CONTAINER_HOST="unix:///run/user/1000/podman/podman.sock"
  ```
- `/run/user/1000` 權限設為 `710`（`drwx--x---`），允許 `<host-user>` 群組穿透存取 Podman Socket
- Quadlet 單元不依賴 XDG，只透過 systemd `--user` 模式運行，天然規避此問題

### 15.3 開機競態條件（對應案例五）

**問題**：Windows 開機時 vNIC 尚未就緒，WSL 自動啟動的 Cloudflare Tunnel 或 sshd 因競態失敗。

**新版防禦**：
- Quadlet `.service` 單元加入 `After=network-online.target`
- 開機自啟的服務加入 `ExecStartPre=/bin/sleep 5` 或使用 `systemd-networkd-wait-online`
- `wsl-shutdown` / `wsl-restart` 流程輸出明確的等待提示，不依賴 Windows 排程器計時

### 15.4 外部 VHDX 未掛載致 fstab 失敗（對應案例十八）

**問題**：外部 VHDX 未掛載時，`mount -a` 失敗退回 Emergency Mode，阻擋所有服務。

**新版防禦**：
- 所有 `.services/` / `.deploys/` / `.containers/` 路徑依賴的 VHDX 掛載點，在 `/etc/fstab` 必須包含 `nofail,noatime,x-systemd.device-timeout=30s`
- CLI 啟動時執行 **掛載點可用性預檢**：
  ```bash
  # container-ctl 啟動前必須通過的 preflight check
  check_mounts() {
      for path in <projects-root> <site-var-root>; do
          mountpoint -q "$path" || echo "[WARN] $path 未掛載，相關服務將無法啟動"
      done
  }
  ```
- 啟動失敗時，輸出清晰錯誤訊息引導用戶掛載 VHDX，而非靜默失敗

### 15.5 DevContainer `--network=host` Port/Socket 搶佔（對應案例十一）

**問題**：DevContainer 使用 `--network=host` 時，Agent Server 與宿主機 Agent Server 在 `127.0.0.1` 發生 Port/Socket 搶佔。

**新版防禦**：
- 開發容器**禁止使用 `--network=host`**；改以獨立私有 Bridge 網段（`net-<project>`）
- IDE Port Forwarding 透過 `ide_port_forwarder.py` 守護程序以**非阻塞 OS 原生 Socket** 轉發（`os.read(fd, 4096)` + `c_sock.recv(4096)`），禁止 `BufferedReader.read()`
- Agent brain 掛載路徑獨立至專案腦區，避免 credential / PID lock 衝突

### 15.6 SQLite 多程序寫入競態

**新版防禦**：
- SQLite 啟用 **WAL（Write-Ahead Logging）** 模式：`PRAGMA journal_mode=WAL`
- 所有寫入操作加入 **5 秒 busy timeout**：`PRAGMA busy_timeout=5000`
- CLI 對 `state.db` 的寫入操作序列化（不並行）

---

## 16. 啟動前 Preflight 檢查（Startup Preflight）

`container-ctl deploy apply` 執行前，必須通過以下檢查：

| 檢查項目 | 失敗處置 |
| :--- | :--- |
| VHDX 掛載點可用性（`<projects-root>`、`<site-var-root>`） | 輸出警告，提示掛載指令，阻止繼續 |
| Podman Socket 可達性（`/run/user/1000/podman/podman.sock`） | 輸出錯誤，提示啟動 podman socket 服務 |
| SQLite DB 可寫性（`state.db`） | 若不存在則自動初始化；若鎖定則重試 5 次後報錯 |
| Image Pinned Digest 合規（無 `:latest`） | 硬性中斷，拒絕 apply |
| Bare Repo 可讀性（`git show <ref>:.deploy/manifest.json`） | 輸出錯誤，顯示可用 ref 清單 |

---

## 17. Release Bundle 機制（Strip Export & 離線部署）

### 17.1 問題場景

開發環境擁有完整 Bare Repo（`$CONTAINER_CTL_GIT_REPOS_DIR/<project>.git`）。
當發行為 Release 分支特定版本後，目標環境可能：

1. **不擁有 Bare Repo**（純生產環境，無原始碼）
2. **需要精簡鏡像**（Strip：移除 dev 工具、測試依賴、原始碼層）
3. **需要離線部署**（Air-gapped：無法從 registry 拉取）

**設計原則**：Release Bundle 與 `.deploy/` 格式**完全相容**，Quadlet 生成流水線不需修改。唯一差異是 Image 引用來源由「Registry Pull」替換為「Bundle 內嵌 tar」或「已載入本地 image store」。

---

### 17.2–17.5 匯出、部署、Strip 宣告、使用場景（已封存）

> **已封存**。`container-ctl release export`／`export-kit`／`export-quadlet`／`deploy` 子命令已移除註冊，改由 `prod up` 與 `rollout` 流程取代。

---

### 17.6 ADR 更新

| ADR | 標題 | 狀態 |
| :--- | :--- | :--- |
| [ADR-007](../adr/ADR-007-release-bundle-export.md) | Release Bundle 機制 — Strip Export 與離線部署 | Accepted |

---

## 18. 容器層級抽象模型（Container Tier Model）

### 18.1 三層業務層級定義

新版容器管理在 Dual-Track 基礎上正式引入三層業務層級（`tier`），作為容器配置、生命週期與 CLI 操作的核心分類維度：

```
┌──────────────────────────────────────────────────────────────┐
│                    宿主機 Control Plane                       │
│          Podman Quadlet + Systemd + container-ctl            │
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
  ┌─────────────┐    ┌──────────────┐    ┌──────────────────┐
  │  infra 層   │    │  prod 層     │    │  dev 層          │
  │ (基礎設施)  │    │ (生產發行)   │    │ (開發工作區)     │
  ├─────────────┤    ├──────────────┤    ├──────────────────┤
  │ • Relay     │    │ • WebService │    │ • agent-dev-*    │
  │ • Proxy     │    │ • PostgreSQL │    │   由 ssh-entry   │
  │ • Broker    │    │   (dedicated)│    │   動態建立       │
  │             │    │ • prod 依賴  │    │                  │
  ├─────────────┤    ├──────────────┤    ├──────────────────┤
  │ scope:      │    │ scope:       │    │ 非 Quadlet 管理  │
  │  shared     │    │  dedicated   │    │ 方案 2 負責      │
  │ always_on   │    │  或 shared   │    │ IDE/git/skill    │
  │ Quadlet     │    │ Quadlet 管理 │    │ 掛載由此層執行   │
  └─────────────┘    └──────────────┘    └──────────────────┘
```

### 18.2 各層特性對照

| 特性 | `infra` 層 | `prod` 層 | `dev` 層 |
| :--- | :--- | :--- | :--- |
| **管理方式** | Quadlet + systemd | Quadlet + systemd | `ssh-entry.sh`（方案 2） |
| **建立觸發** | 開機自啟或 CLI | `deploy apply` | SSH 登入 |
| **預設 lifecycle** | `always_on` | 依 `.deploy/` 配置 | SSH 斷線後 auto_idle |
| **volume 類型** | 共享或無狀態 | Production VHDX（高保護） | workspace / git repo / skill |
| **IDE 掛載** | ❌ | ❌ | ✅（`ssh-entry.sh` 配置） |
| **Git Repo 掛載** | ❌ | ❌ | ✅（方案 2） |
| **Skill 掛載** | ❌ | ❌ | ✅（方案 2） |
| **熱掛載（方案 3）** | ✅ 相容 | ✅ 相容 | ✅ 相容 |
| **Release Bundle** | ❌ | ✅（主要對象） | ❌ |
| **Strip Export** | ❌ | ✅ | ❌ |
| **Release 映像建置與錨定**（`image prepare-prod`／`publish-prod`／`adopt-prod`） | ✅ | ✅ | ❌（走 `prepare-dev`） |

> 「可重現地建出映像並以 digest 錨定」與角色無關，只要是 `localhost/` 自建映像就適用，所以 `prepare-prod` 接受 `infra` 與 `prod`（程式常數 `RELEASE_BUILD_TIERS`）。`tier` 本身仍只決定生命週期、啟停順序、cgroup slice 與網路規則（ADR-009）；Release Bundle、Strip Export、prod 回滾等發布流程維持只對 `prod`。

### 18.3 Tier 在 `.deploy/containers/<name>.json` 中的宣告

```json
{
  "name": "postgres",
  "tier": "prod",
  "image": "docker.io/library/postgres:15-alpine@sha256:<pinned>",
  "scope": "dedicated",
  "lifecycle_policy": { "mode": "auto_idle", "idle_timeout_sec": 300 }
}
```

`tier` 欄位可選值：`"infra"` | `"prod"` | `"dev"`（預設 `"prod"`）

### 18.4 CLI 層級過濾

部分 CLI 指令（如 `deploy plan`、`status`）支援 `--tier` 過濾；`deploy stop` 無 `--tier`：

```bash
container-ctl status list --tier prod     # 只顯示 prod 層實例
container-ctl deploy stop <project>  # 依專案宣告停止（無 --tier）
container-ctl status list                 # 預設顯示 infra + prod（不顯示 dev）
```

---

## 19. 方案 3（`container_mounts.py`）相容性說明

### 19.1 相容性結論

方案 3 的 `nsenter`-based 熱掛載機制**與 Quadlet 容器完全相容**，原因：

- `nsenter` 操作於 Linux namespace 層級，不依賴容器啟動方式
- Quadlet 容器的 namespace 結構與 `podman run` 啟動的容器**完全相同**
- `hot-mount`、`reconcile-links`（Symlink 修補）均無需修改

### 19.2 唯一整合點：容器名稱查詢

新版容器命名規則（`svc-<inst-id>`）與舊版不同，方案 3 需要能根據業務名稱查到實際容器名稱。

**解法**：在 SQLite `instances` 表新增 `container_name` 欄位：

```sql
ALTER TABLE instances ADD COLUMN container_name TEXT;
-- 值範例：svc-prod-postgres-v15 或 svc-dev-app-hub
```

方案 3 查詢介面：

```bash
# 方案 3 呼叫 container-ctl 提供的查詢接口
container-ctl query container-name <project> <service>
# 輸出：svc-postgres@WebApp-v15

# 方案 3 取得 PID 後執行 nsenter（不變）
PID=$(podman inspect <container-name> --format "{{.State.Pid}}")
nsenter -t "$PID" -m -- mount --bind <src> <dst>
```

### 19.3 各掛載類型的不變性聲明

| 掛載類型 | 執行路徑 | 新版是否需要修改 |
| :--- | :--- | :--- |
| IDE workspace 掛載 | `ssh-entry.sh` → dev 容器建立時 | **不需修改**（方案 2 保留） |
| Git Repo 掛載 | `ssh-entry.sh` → Volume 參數 | **不需修改** |
| Skill 掛載 | `ssh-entry.sh` → Volume 參數 | **不需修改** |
| Agent Brain 掛載 | `ssh-entry.sh` → Volume 參數 | **不需修改** |
| 熱掛載（hot-mount） | 方案 3 `nsenter` | **不需修改**（僅需更新容器名稱查詢來源） |
| Symlink reconcile | 方案 3 | **不需修改** |

## 20. dev 與 prod 容器差異矩陣

> 規範性對照。標「待裁決」之列尚未定案，不得視為規範（見 `.scratch/prod-app-final-plan.md` 第六節）。

| 面向 | dev | prod |
| --- | --- | --- |
| 容器角色 | 殼容器，agent 於 `/workspace` 開發，應用另行啟動 | 唯讀成品，應用即容器主程序 |
| 映像來源 | `localhost/` 本機建置（`dev-images/<svc>/`），基底 `dev-base` | 程式碼 repo ref 經 `Dockerfile.prod` 建置 |
| 映像錨定 | `publish-dev` 於設定 repo 寫 pin（`@sha256`） | `prepare-prod`／`publish-prod` 寫不可覆寫的 `release/<svc>/<ver>` tag |
| 錨定檢查 | `audit-dev`（漂移為 INFO／WARN） | `check_release_anchor`（redeploy 嚴格）、`audit-prod` |
| 版本命名 | 不帶版本 | 帶版本；tag 版本等於容器 `version` |
| 程式碼來源 | 專案 volume 內的 dev clone（`volumes/<proj>/<svc>`，由 `volume_resolver` 在 `/workspace` 且無 `host_path` 時推導）bind mount 至 `/workspace` | 打包進映像，禁止掛工作樹 |
| 生命週期 | `manual`（Quadlet 無 `[Install]`） | `always_on` |
| 應用啟動 | `service start\|restart --with-app` 執行 `app_hooks`（§3.4） | entrypoint 直接執行 |
| redeploy | 不啟動、不重啟 manual 容器 | 啟動並驗證 active |
| rootfs | 可寫 | `--read-only`，僅掛載的 etc/var/run/log 可寫 |
| 設定檔路徑 | 相對路徑可用 | 必須絕對路徑 |
| 埠綁定 | `127.0.0.1` | `127.0.0.2` |
| 資料目錄 | `$CONTAINER_CTL_VOLUMES_DIR/<proj>/…`（自動補建） | `$CONTAINER_CTL_PROJECTS_CODE_ROOT/<proj>/…`（須先建，見 §20.1） |
| 目錄權限 | 容器 uid 1000 | `user: 1000:1000` 映射宿主 subuid，需 `podman unshare chown` |
| 資料庫 | dev-postgres（納管） | prod-postgres（未納管；`depends_on` 工具不處理） |
| 設定 repo 可見性 | 容器不可見 | 容器不可見 |
| 不可逆動作 | 無 | `publish-prod`（tag）、`redeploy --yes`，各需使用者批准 |
| 備份 | 專案 VHDX 冷備份 | prod VHDX 冷備份；程序化備份範圍未驗證 |

> **映像與配置分離（ADR-009，2026-10-08）**：上表 `127.0.0.2`、`<site-var-root>/...`、容器命名帶版本屬 **site 環境規則／現行實作**，不是映像屬性；映像不得內嵌環境值；改 env／埠／掛載走 `config set` → `redeploy`，不發新版。環境規則已抽成 **環境 profile**（`envs/<env>/profile.json`，預設 `site`），`tier` 只表示角色（dev／prod：映像形態與生命週期），不再等同環境；穩定單元名為 opt-in（`unit_naming: stable`）。細節見 §24 與 ADR-009。

### 20.1 prod 持久化目錄規範（2026-10-05 使用者裁定）

- prod tier 容器的 etc／var／run／log 持久化目錄置於 `$CONTAINER_CTL_PROJECTS_CODE_ROOT/<project>/`；該區不在自動補建範圍，須由使用者或經使用者批准的指令清單先建立，並以 `podman unshare chown` 對映容器 uid。
- secrets 與跨專案共享憑證置於 `$CONTAINER_CTL_VOLUMES_DIR/<project>/secrets`。
- 程序化備份（`backup-ctl`）不涵蓋 `$CONTAINER_CTL_PROJECTS_CODE_ROOT/*`，僅靠 prod VHDX 冷備份；需要時另案加入。
- prod 容器生命週期預設 `always_on`；探針不得依賴映像未安裝的工具。

### 20.2 prod 切換與實際驗收入口（2026-10-05）

- `container-ctl deploy migrate-postgres <project> <ref> --service <name> --source-container <legacy-name> --yes`：僅處理未納管的既有 PostgreSQL；宣告、release anchor、來源主版本與空目的地檢查通過後，建立私有邏輯備份、停止來源、備份並離線複製 PGDATA/state、部署指定服務且等待 healthy。來源容器與資料保留，失敗時回復來源。不得以此命令覆寫既有目的地或初始化新 cluster。
- `container-ctl deploy verify <project> <ref> [--service <name>]`：驗證 prod release anchor、DB / Quadlet / 實際映像、env / 掛載、active / healthy、read-only 與 generator 開機依賴；成功才寫入 `.deploys/<project>/verified-prod.json` 及觀測到的 lifecycle 事件。
- 一般容器宣告的 `user`、`read_only`、`entrypoint`、`healthcheck` / `health_cmd` 由生成器落實；`CMD` 探針依 argv 安全引用，`CMD-SHELL` 探針保留 shell 命令；entrypoint 使用 Podman JSON 陣列格式。

## 21. 離線依賴規範（2026-10-05 使用者裁定）

映像（dev 與 prod）需要套件時，**預設使用離線版本，不連網下載**。

- **Python 第三方套件**：預設來源 `$CONTAINER_CTL_WHEELHOUSE_DIR/`（附 `SHA256SUMS`）。Dockerfile 以 `pip install --no-index --find-links <wheelhouse>` 安裝；wheel 複製進 build context（prod 的 `wheelhouse/`）。
- **內部 wheel**（`internal_*`、`app_client` 等）不在共用庫，放各專案 `wheels/`。
- **缺套件**：補進共用庫並重建 `SHA256SUMS`，一次列完整傳遞依賴（含 extras，例如 `uvicorn[standard]` 的 uvloop）；禁止重複下載共用庫已有的資源，禁止從 dev 容器複製 site-packages。
- **工具落實**：`prepare-dev`／`prepare-prod` 建置前靜態檢查 Dockerfile，`pip install` 未帶 `--no-index` 輸出 `[WARN]`（`container_ctl/offline_deps.py`；不阻擋建置）。`offline_deps.verify_wheelhouse()` 核對 `SHA256SUMS`。
- 待辦（未實作）：`prepare-*` 的 `--wheelhouse` 參數與建置前自動 `sha256sum -c`、`--network=none` 建置、`--dry-build`。

## 22. prod 部署唯一流程（工具串接）

Agent 操作入口：skill `containerize`（`container-ctl/.agents/skills/containerize/SKILL.md`，STE 英文）。

```
container-ctl prod doctor <project> [ref] [--service S]            # 唯讀體檢，一次列出阻塞與修法
container-ctl host prepare <project> --service S [--apply]         # 只新建宿主目錄／巢狀掛載點／uid 映射（預設預覽）
container-ctl prod up <project> <service> --ver V [--code-ref R] --yes
```

`prod up`：doctor → host prepare → prepare-prod → publish-prod（提交 pin＋release tag）→ `redeploy --service` → 等 healthy → `deploy verify` ＋ `audit-prod` → 發布後嚴格 doctor。未帶 `--yes` 僅預覽。每步依現況判斷是否已完成（release tag 已存在則略過 prepare／publish），失敗時停在該步並印出 `[NEXT]` 指令，可重跑續做；報告寫入 `$CONTAINER_CTL_DEPLOYS_DIR/<project>/prod-up-<service>-<ver>.json`。
不在流程內（由專案方提供）：secret 檔內容、TLS 憑證、資料庫與角色。缺少時 doctor 會列為阻塞。
**改配置（不發版，ADR-009／ADR-011）**：`container-ctl config set <project> <svc> KEY=VAL [--env E] --yes`（驗證契約、寫稽核）→ `container-ctl rollout apply <project> <svc> --yes`（預設預覽）。回滾：`rollout undo`（取最近一個實際健康的 rollout）。漂移檢查：`container-ctl reconcile <project> <svc>`。`prod up` 只用於發新版本（會建置並發布新 artifact）。

手動 `podman build`／`systemctl` 等步驟**保留為備源選項**，不是預設：須工具缺失或失敗、使用者本 session 批准、寫偏離記錄、事後以工具對齊；每個手動步驟錨定到工具程式碼符號（ADR-0001，`tests/test_adr_anchors.py` 驗證）。不符條件者仍是重大偏離（見 feedback：部署只走工具）。

## 23. 路徑與機密規範（2026-10-05 對抗式審查定案）

### 23.1 路徑（dev／prod 兩軌）

- `app_paths` 預設值為 prod FHS：`/opt/<P>`、`/etc/opt/<P>`、`/var/opt/<P>`、`/run/<P>`、`/var/log/<P>`。`<P>` 逐字取自 `PROJECT_NAME`，不轉小寫。
- dev：`APP_*` 指向 `/workspace/*`，`WORKDIR /workspace`。
- prod：宣告**可**顯式設定 `APP_*`，值必須是 FHS 路徑；任何 env 值含 `/workspace` 為 ERROR（`check_runtime_conventions`）。映像內不得有 `/workspace`。
- dev `/workspace` 的來源是專案 volume 內的獨立 clone，不是 worktree；備份範圍跟隨 volume。

### 23.2 命名

- 容器內掛載點的專案段必須與 `PROJECT_NAME` 完全相同（大小寫一致），不一致 WARN。

### 23.3 機密

- 只允許 `*_FILE` 契約。env 鍵含 PASSWORD／SECRET／TOKEN／PRIVATE_KEY／API_KEY 且不以 `_FILE`／`_PATH`／`_DIR` 結尾，或 URL 內嵌 `user:pass@`：prod ERROR、dev WARN。
- 容器內機密路徑：`/etc/opt/<PROJECT_NAME>/secrets/<name>`，唯讀掛載。不得由 `APP_*` 推導；應用只讀 `*_FILE` 指向的路徑。
- 機密供應（工具產生，agent 不碰內容）：宣告 `secrets: [{env, kind: password|token, bytes}]` → `container-ctl secret provision`（`secret_provision.py`）在 `podman unshare` 內以 O_EXCL 建立、0400、chown 給容器 user；只建不覆寫，內容不印、不進 argv、不進報告。`kind` 為其他值（cert、key）一律不產生。資料庫角色與密碼檔由 `container-ctl db provision`（`db_provision.py`，委派 PG-Hub `bin/create-service-db`，依消費者 `.deploy/pg-hub.json`）發放；密碼檔已存在則略過，不自動輪替。兩者皆由 `prod up` 自動呼叫。
- 公開 CA：宣告 `public_files: [{src, dest, kind: "ca-cert"}]`，由 `host prepare --apply`（`prod up` 與 dev 皆適用）呼叫 `public_files.py` 複製到專用目錄。來源必須是只含 CERTIFICATE 區塊的 PEM（含 PRIVATE KEY、非 PEM、超過 64KB 一律拒絕且不寫出）；dest 須在白名單根目錄內、不得為私鑰檔名；內容相同不改動。消費者改掛該專用目錄，不再從含 `server.key` 的目錄掛單檔。
- 掛載選項只用 `ro`／`rw`。生成器不產生 `:Z`；WSL2 rootless 無 SELinux，不列入規範。
- `prod doctor`（含 `prod up` 的 preflight）呼叫 `static_gate`（`gate_runner.py`，只掃宣告 json）：prod 違規 ERROR 阻擋、dev 違規 WARN。`redeploy`、`apply` 本身不呼叫；agent 必須走 `prod up`。
- `prod doctor` 同時呼叫 `scan-path-refs`（`path_scan_runner.py`，只掃 prod 宣告 json，R-001）與映像檢查（`image_checks.py`：WORKDIR、/workspace、debugpy）。整包應用碼掃描不阻擋：核心規則 R-006 已改 `(?<!/var)<site-opt-root>`，不再誤報 `<site-var-root>/...`（2026-10-05）。

### 23.4 宣告為唯一真源

- 所有掛載寫在 `volumes[]`。`volume_templates` 是純文件鍵，生成器不處理；有內容時 WARN。
- 受管 Quadlet 旁的 `<unit>.container.d/*.conf` 是繞過宣告的手工覆寫；`redeploy` 偵測到會 WARN。內容須遷入宣告後移除。

### 23.5 prod rootfs

- `read_only: true` 時，可寫位置只能是宿主 bind（etc／var／run／log）或 `tmpfs`。podman 在 `--read-only` 下會自動掛載 `/tmp`、`/run` 的 tmpfs（2026-10-05 於 svc-prod-app-0.2.0 實測），因此不需為 `/tmp` 宣告 `tmpfs`；寫入其他路徑須有宣告的 volume。
- Dockerfile.prod 建議不使用 `VOLUME`（避免匿名卷）。若映像宣告 `VOLUME`，每個路徑都必須有明確掛載；`redeploy`／`apply` 的預檢會阻擋未掛載者。

### 23.6 脫離 site

- 匯出器 `release export-quadlet` 已封存；dev 以 `deploy plan/redeploy` 產生單元。
- 宣告 json 的原生子集（`image`、`user`、`read_only`、`ports`、`env`、`healthcheck`、`entrypoint`、`command`、`tmpfs`、`sysctls`、`memory_max`、`volumes` 且含 `host_path`）可映射為 Quadlet。
- `tier`、`scope`、`lifecycle_policy`、`app_hooks`、`governance`、`portfwd`、`labels`、`volumes` 的自動推導由 `container-ctl` 翻譯；脫離時不可手工宣稱「直接轉換」。

## 24. 配置契約、環境 overlay 與發布物（ADR-009／010／011，2026-10-08）

**三層分離**：Artifact（OCI 映像，身分＝manifest digest）→ Release（`release/<svc>/<ver>` tag，內容 `release.json` schema 2，不含配置值）→ Rollout（把 release＋設定 repo 修訂套用到某環境，寫 state.db `rollouts`）。版本只標 artifact；配置修訂用設定 repo 的 git commit。

### 24.1 配置契約 `.deploy/contract.json`
- JSON Schema 2020-12（對標 Helm `values.schema.json`）：`env`（頂層）與 `services.<svc>.env`（覆蓋）描述環境必須提供的鍵、型別、格式；契約不含任何環境值。
- 屬性標 `x-secret-file: true`：鍵名須以 `_FILE` 結尾，值須落在該服務宣告的掛載點下（機密一律檔案掛載）。
- 驗證點：`config set`（寫入前）、`config check`、`validate`／`prod doctor`；缺契約的 prod 服務為 `[WARN]`。契約雜湊記入 release 記錄。

### 24.2 環境 overlay 與 profile
- 設定 repo：`.deploy/containers/<svc>.json`（base）＋ `.deploy/envs/<env>/containers/<svc>.json`（overlay）＋ `envs/<env>/profile.json`（Host IP 規則等）。無 `envs/` 的專案行為不變。
- 合併：dict 遞迴、list 與純量整體取代；只允許覆寫 `env`、`ports`、`volumes[].host_path`、`networks`、記憶體、`healthcheck` 時序、`secrets[].host_path` 等環境欄位；`image`、`version`、`name`、`tier`、`command`、`entrypoint`、`user`、`read_only` 禁止覆寫。
- 環境由 `CONTAINER_CTL_ENV`（預設 `site`）或 `--env` 指定。

### 24.3 Artifact 身分與 release 記錄
- 建置帶 OCI 標註（`org.opencontainers.image.{revision,version,title,created}`）。`publish-prod` 把映像推進 OCI Image Layout store（`$CONTAINER_CTL_OCI_STORE/<project>/<svc>`，`CONTAINER_CTL_OCI_STORE=off` 停用），記錄 manifest digest；store 寫入失敗則不建立 release tag。
- release 記錄：`{schema:2, service, version, image_id, manifest_digest, source_commit, config_commit, contract_hash, sbom, signature, provenance}`；舊記錄（無 manifest_digest）以 image_id 錨定並於 verify 時 `[WARN]`。
- `container-ctl image verify <project> <svc> <ver> [--require-signature]`：重算 store blob 雜湊對 release 記錄；SBOM／cosign 欄位由 `supply_chain` 填寫，syft／cosign 未安裝時為 null（安裝須使用者批准，§21）。`--require-signature` 在無簽章或無法驗證時失敗。

### 24.4 Rollout、調和、單元命名
- `rollout apply|status|history|undo`、`reconcile`：desired（設定 repo＋release）vs actual（podman inspect）；`undo` 取最近一個實際健康的 rollout（可 `image|config|both`），先預覽再確認。
- 換單元前的埠衝突預檢；固定宿主埠的服務採 stop-first（先停舊再啟新），`update_strategy` 目前僅接受 `stop-first`。
- `unit_naming: stable`（opt-in）：單元與容器名 `svc-<name>`，版本改入 `org.container-ctl.release` 等標籤；預設仍為帶版本命名。既有 prod 服務遷移須逐服務取得使用者同意。

### 24.5 可攜發布包
- `release export --format bundle2`（可攜發布包）已封存。

### 24.6 環境層 profile／policy 與 `host check`
- 環境層真值：`$CONTAINER_CTL_ENVIRONMENTS_DIR/<env>/{profile,policy}.json`（路徑可由 `CONTAINER_CTL_ENVIRONMENTS_DIR` 覆寫）。`read_profile` 優先序：內建預設 < 環境層 profile < 專案層 `.deploy/envs/<env>/profile.json`；環境層缺檔時行為與改動前相同。
- `container-ctl host check [--env E] [--units-dir D] [--output json] [--strict] [--verbose]`：唯讀；掃 `profile.host.quadlet_dir` 的 `*.container`，依 `policy.json` 規則的 `params` 檢查 `loopback-publish`、`cgroup-guard`、`fhs-paths`，並檢查 `profile.paths` 存在性。無檢查器的規則回報 `skipped`。
- 強度：`policy.default_strength=advisory`（預設）時違規為 WARN、退出碼 0；`--strict` 或 `mandatory` 時違規退出碼 1；環境檔載入失敗退出碼 2。
- 範圍限制：掃描的是已宣告的單元檔，含已被取代的舊版本單元，不代表運行狀態。
- 門檻由環境宣告決定：規則 `params` 值為 `null` 或缺省＝不檢查該項；`memory_max`、`memory_swap_max` 為上限（超過才違規）。`target` 記錄生產目標值，僅供遷移對照，`host check` 不用。
- `policy.deferred[]`（`id`、`note`、`expires`、`followup`）：登記暫緩項；`host check` 輸出 `[NOTE]`，過期改 `[WARN]`。`profile.paths_on_demand` 的路徑不檢查存在性。

