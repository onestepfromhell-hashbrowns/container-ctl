# 容器開發標準分發機制 (Container Standards Distribution)

本文檔說明 `container-ctl/standards` 定義的標準件（`app_paths.*` 多語言路徑解析器與 `scan-path-refs` 靜態掃描工具）以及 Agent 知識 Plugin 如何透過 `scope-extensions` 分發至各專案容器中。

---

## 分發架構

```
container-ctl/standards（規範、標準件與 Plugin）
        │
        └── scope-extensions（動態附加管道）
                    │
                    ├── [scope: biz-container-standards] (UUID: a08f3f72-d816-4198-b79c-7cf1227a94f4)
                    │   ├── /workspace/lib/standards/ (app_paths.* 模板)
                    │   └── /usr/local/bin/scan-path-refs (CLI 執行檔)
                    │
                    └── [scope: biz-container-dev-bundle] (UUID: c58a1234-abcd-4e5f-9876-0123456789ab)
                        ├── (包含上述標準件工具鏈)
                        ├── /workspace/.agents/plugins/container-dev/ (完整 Plugin、Prompts 與 Rules)
                        └── /workspace/.agents/workflows/new-project-dual-track.md (一鍵工作流)
```

---

## 專案啟用方式

在專案的 Extension Scope 配置中（宿主端慣例路徑 `$CONTAINER_CTL_SERVICES_DIR/<project>/ext-default.json` 或專案版控 `.deploy/ext-default.json`）加入 `biz-container-dev-bundle`（或 `biz-container-standards`）：

```json
{
  "name": "my-project-extension",
  "include_scopes": [
    "c58a1234-abcd-4e5f-9876-0123456789ab"
  ]
}
```

---

## 注入產物說明

| 容器內部路徑 | 來源 | 用途 |
|---|---|---|
| `/workspace/lib/standards/app_paths.*` | `container-ctl/standards/src/lib/` | 多語言路徑解析模板（Python/Bash/JS/Go） |
| `/usr/local/bin/scan-path-refs` | `container-ctl/tools/scan-path-refs` | 靜態路徑閘控 CLI |
| `/workspace/.agents/plugins/container-dev/` | `container-ctl/standards/.agents/plugins/` | 容器內 Agent 標準 Plugin（含 Prompts、Skills、Rules） |
| `/workspace/.agents/workflows/new-project-dual-track.md` | `container-ctl/standards/.agents/workflows/` | 新專案 Dual-Track 設置工作流 |

---

## 專案端自訂與覆寫

1. **複製模板**：專案將 `/workspace/lib/standards/app_paths.<ext>` 複製至自己的 `src/lib/` 並將 `APP_` 替換為服務名稱（例如 `WEBSERVICE_`）。
2. **例外規則配置**：在專案根目錄建立 `.scan-path-refs.yaml` 設定白名單例外路徑（參見 `templates/scan-path-refs.yaml`）。
3. **Agent 一鍵引導**：容器內 Agent 可直接執行 `/new-project-dual-track` 或載入 Plugin Prompts 進行快速合規配置。
