---
name: container-service-control
description: >
  容器標準雙層生命週期治理與啟動鏈控制技能 (Two-Tier Container Service Control & Lifecycle Governance)。
  包含 container-control 缺席時的 Native Fallback 契約。
  當專案需要在容器內部管理服務進程 (API / Web / Worker)、配置標準雙層啟動鏈 (entrypoint.sh / control.sh / configs/service_control.json)、
  執行核心操作 (start / stop / restart / status / probe / logs / --help)、進行三重驗證健康檢查 (Triple-Verification: PID + Port + Readiness Probe)、
  設定宣告式生命週期鉤子 (pre_start_hooks / pre_stop_hooks) 或診斷進程衝突與探針異常時強制觸發此 Skill。
---

# 容器標準雙層生命週期治理與進程控制 (Container Service Control v2.0)

本技能依據**第一性原理**定義了通用、去業務化、符合 Linux FHS 與 OCI 容器標準的**標準雙層進程治理架構 (Two-Tier Control Architecture)**。透過統一的 `container-control` CLI、`entrypoint.sh` (PID 1) 與**三重驗證引擎 (Triple-Verification)**，確保容器內部無論是開機自動啟動還是進入容器互動式維護，皆具備 100% 行為一致性與完備健康可觀測性。

---

## 🏛️ 標準雙層架構 (Two-Tier Architecture)

容器生命週期治理嚴格劃分為兩個職責層級：

```mermaid
flowchart TD
    subgraph Tier 1: 容器生命週期入口 (Container Lifecycle Entrypoint / PID 1)
        A["OCI / Podman / K8s 啟動容器"] --> B["entrypoint.sh (PID 1)<br>• 系統級初始化 (目錄結構/權限修復)<br>• 轉交控制權 (exec)"]
        B -->|"預設啟動 (無參數 / default)"| C["exec /workspace/control.sh start --foreground"]
        B -->|"臨時指令 (pytest / bash)"| D["exec \"$@\" (透明透傳)"]
    end

    subgraph Tier 2: 服務進程治理 (Service Process Governance)
        C --> E["control.sh / container-control<br>• 讀取 configs/service_control.json<br>• 執行 pre_start_hooks (宣告式前置檢查)<br>• 啟動通用主進程 (command)<br>• 三重驗證 (PID + Port + Probe)<br>• PID 1 信號捕捉 (SIGTERM/SIGINT Trap)"]
        F["開發者 / Agent 進入容器維修"] --> G["./control.sh restart / status / probe / logs"]
    end
```

| 層級 (Tier) | 檔案 / 組件 | 角色定位 | 核心職責 |
| :--- | :--- | :--- | :--- |
| **Tier 1** | **`entrypoint.sh`** | **容器層 Entrypoint (PID 1)** | 容器開機第一道門，負責系統級工作區初始化，預設 `exec /workspace/control.sh start --foreground`，支援透明透傳臨時命令。 |
| **Tier 2** | **`control.sh`** | **應用層 Controller** | 統一的服務治理中樞（`start`, `stop`, `restart`, `status`, `probe`, `logs`），具備 PID 1 信號轉發與背景守護雙模態。 |
| **Config** | **`configs/service_control.json`** | **通用宣告式配置** | 定義通用 `command`、Port、`pre_start_hooks`、`readiness_probe` 與超時時間，實現 100% 去業務化。 |

---

## 🎯 核心操作 (Core Operations)

所有容器專案均支援統一的標準操作介面：

| 操作指令 | 說明 | 關鍵行為與機制 |
| :--- | :--- | :--- |
| **`start`** | 啟動服務進程 | 預設以 Detached 背景模式執行 (`start_new_session=True`)；支援 `-f` / `--foreground` 接管 PID 1 前景執行並處理信號 Trap。 |
| **`stop`** | 停止運行中服務 | 優先執行 `pre_stop_hooks`，發送 `SIGTERM` 進行優雅關閉（預設逾時 5 秒），逾時發送 `SIGKILL`，並清理 PID 鎖。 |
| **`restart`** | 重啟服務 | 執行 `stop` 流程後等待 1 秒再執行 `start`，確保連接埠與資源完全釋放。 |
| **`status`** | 三重驗證健康儀表板 | 執行 **Triple-Verification**（PID 存活 + Port 綁定 + Readiness Probe），顯示 ANSI 著色儀表板（支援 `--json`）。 |
| **`probe`** | 主動探針診斷 | 直接對配置的 `readiness_probe` 進行即時探測並輸出詳細健康診斷結果（支援 `--json`）。 |
| **`logs`** | 日誌檢視與串流 | 查看最近 N 行日誌（預設 20 行），支援 `-f` 即時追蹤串流。 |
| **`--help` / `-h`** | 指令幫助手冊 | 查看全域或子指令的詳細使用說明與參數清單。 |

---

## 🏗️ 專案目錄佈局標準

```text
/workspace/
├── entrypoint.sh                   # Tier 1: 容器 OCI 入口 (PID 1 轉交 control.sh 或透明透傳)
├── control.sh                      # Tier 2: 專案統一控制腳本 (FHS Wrapper, 支援 start/stop/restart/status/probe/logs)
├── configs/
│   └── service_control.json        # 通用服務設定檔 (Command, Port, Hooks, Probe)
├── scripts/
│   └── app.py                      # 服務進入點 (或任意語言之啟動腳本 / 編譯產物)
├── logs/
│   └── app.log                     # 服務 stdout/stderr 聚合日誌
└── app.pid                         # 執行時期 PID 鎖定檔 (由控制器自動管理)
```

---

## 📋 通用配置合約範例 (`configs/service_control.json`)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "service": {
    "name": "my-service",
    "command": ["python3", "scripts/app.py"],
    "working_dir": ".",
    "port": 8080,
    "pid_file": "app.pid",
    "log_file": "logs/app.log",
    "shutdown_timeout": 5.0,
    "pre_start_hooks": [],
    "post_start_hooks": [],
    "pre_stop_hooks": [],
    "readiness_probe": {
      "type": "http",
      "path": "/health",
      "port": 8080,
      "timeout_seconds": 3.0,
      "expected_status": 200
    },
    "env": {
      "PORT": "8080"
    }
  }
}
```

---

## 💡 業務客製化設計食譜 (Declarative Business Recipes)

雖然 `container_control` 核心保持 100% 去業務化，但各類型專案可透過宣告式配置輕鬆對接常見業務場景：

### 1. Python + Alembic 資料庫遷移
```json
{
  "service": {
    "name": "fastapi-backend",
    "command": ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
    "port": 8000,
    "pre_start_hooks": [
      "alembic upgrade head"
    ],
    "readiness_probe": {
      "type": "http",
      "path": "/healthz",
      "port": 8000,
      "timeout_seconds": 3.0,
      "expected_status": 200
    }
  }
}
```

### 2. Node.js + Prisma 前置產生與遷移
```json
{
  "service": {
    "name": "node-api",
    "command": ["node", "dist/server.js"],
    "port": 3000,
    "pre_start_hooks": [
      "npx prisma migrate deploy",
      "npx prisma generate"
    ],
    "readiness_probe": {
      "type": "http",
      "path": "/api/health",
      "port": 3000,
      "timeout_seconds": 2.0
    }
  }
}
```

### 3. Go / Rust 編譯型二進位檔 + TCP Probe
```json
{
  "service": {
    "name": "go-microservice",
    "command": ["./bin/server", "--config", "configs/prod.yaml"],
    "port": 9090,
    "readiness_probe": {
      "type": "tcp",
      "port": 9090,
      "timeout_seconds": 1.0
    }
  }
}
```

---

## 🔍 三重驗證 (Triple-Verification) 狀態診斷矩陣

| 狀態代碼 | 判定條件 | 排查與修復步驟 |
| :--- | :--- | :--- |
| **`RUNNING`** | `PID 存活` ∧ `Port 綁定自己` ∧ `Readiness Probe 通過` | 服務處於 100% 健康就緒狀態，可安全放行外部請求。 |
| **`INITIALIZING`** | `PID 存活`，但 (Port 尚未綁定 ∨ Probe 尚未通過) | 服務正在初始化、預熱或執行前置載入。可執行 `container-control probe` 或查看日誌確認瓶頸。 |
| **`CONFLICT`** | `Port 已綁定`，但占用者 PID 與執行命令不符 | 連接埠被容器內其他進程佔用。執行 `container-control status` 查看占用者 PID 與 Cmdline 並更換端口。 |
| **`ZOMBIE`** | `PID 檔案存在`，但進程已消亡 | 進程異常退出殘留鎖。執行 `container-control start` 或 `restart`，控制器會自動清理 stale 鎖並重新拉起。 |
| **`STOPPED`** | `無 PID 存活` 且 `Port 未綁定` | 服務處於完全停止狀態。 |
| **`HOOK_FAILED`** | `pre_start_hooks` 任一指令回傳非零代碼 | 前置檢查（如 DB 遷移失敗或配置無效）阻斷啟動。檢視標準輸出修復遷移腳本。 |

---

## 🛡️ 防禦不變量 (Invariants)

1. **行為完全一致性**：無論是容器開機（OCI Tier 1）還是互動式運維（Tier 2），執行的都是同一套 `control.sh` 與宣告式 Hooks，杜絕環境漂移。
2. **PID 1 優雅下線**：前景模式下強制捕捉 `SIGTERM`/`SIGINT`，將信號正確傳遞至子進程並執行 `pre_stop_hooks`，防止 10 秒後遭 `SIGKILL` 暴力中斷。
3. **零外部進程守護依賴**：不依賴 systemd 或 supervisord，純 Python POSIX 進程隔離 (`start_new_session=True`) 自成閉環。
4. **絕對禁止寫入主目錄**：所有 PID、Log 與臨時狀態均存放在專案內部或 FHS 指定之 `<site-var-root>` 與 `/run/user/<uid>/container-ctl`，嚴禁寫入 `/home/<user>`。
5. **Native Standalone Fallback**：`control.sh` MUST implement a pure-Bash/Python fallback when `container-control` is absent. The fallback MUST cover `start`, `stop`, `status`, `probe`, and `logs`. It MUST NOT import from `<site-opt-root>` or any host-only path. Parse JSON with fixed parser logic; preserve command arguments as an array and never evaluate configuration values as shell code. Only declared lifecycle hooks may be evaluated.

## Native Fallback Reference

`control.sh` has three blocks:

1. **Lookup** — use `command -v container-control`; do not search host paths.
2. **Delegation** — when found, use `exec "$EXEC_CMD" -c "$CONFIG_FILE" -d "$SCRIPT_DIR" "$@"` (or the equivalent config arguments for the project).
3. **Native fallback** — parse `configs/service_control.json` with a fixed Python parser. Emit command arguments and environment assignments with NUL delimiters; load the command into a Bash array and invoke it as `"${command[@]}"`. Resolve relative PID/log paths under the project root while preserving absolute paths. Run hooks in order, write a PID file, handle TERM/INT in foreground mode, and support stop/status/probe/logs.

Fallback parser and dispatcher contract:

```bash
_config() { python3 - "$CONFIG_FILE" "$@" <<'PY'
# Fixed JSON parsing only; selector arguments choose known fields.
PY
}
native_start() { :; }
native_stop() { :; }
native_status() { :; }
native_probe() { :; }
native_logs() { :; }
```

Do not interpolate JSON values into Python source. Treat `command` as an argument array and `env` as data. `pre_start_hooks` are declared shell commands and are the only configuration values evaluated by the fallback.

## Naming Reference

| Name | Location | Role |
| --- | --- | --- |
| `container-ctl` | Host / WSL2 | Host orchestration CLI; never invoke it from a project workspace script. |
| `container-control` | In-container, optional | Optional lifecycle enhancer; uses self-contained Native Fallback when absent. |
| `control.sh` | In-container, mandatory | Service process owner and required Native Fallback entry point. |

## 與容器宣告的銜接

Tier 2 `control.sh` 是容器內 app 的唯一啟停入口。manual 容器（PID 1 為 `sleep infinity`）的宣告以 `app_hooks.start`（通常 `cd /workspace && bash control.sh start`）與 `app_hooks.readiness_probe`（可對應 `control.sh probe` 或健康端點）描述啟動鏈；宿主以 `container-ctl service restart <project> <service> --container-name <name> --with-app` 一併重啟並探測。規格見 `container-ctl/docs/SPEC-container-lifecycle.md` §3.4。
