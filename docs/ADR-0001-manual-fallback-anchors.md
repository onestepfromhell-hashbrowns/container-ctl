# ADR-0001：手動步驟保留為備源選項，並錨定到工具程式碼

- 狀態：採納（2026-10-05）
- 決策者：使用者（指示「禁止刪除手動步驟，改為備源選項以及工具程式碼的對應錨定」）

## 背景

dev→prod 流程已工具化（`prod doctor`／`host prepare`／`prod up`）。審查發現舊的手動步驟（手動 `podman build`、`chown`、`systemctl restart`、`podman save` 等）散在各專案文件，與現行流程矛盾。先前做法是刪除或標過時，會遺失在工具缺口時的退路與當初的知識。

## 決策

1. **不刪除手動步驟**。保留於 `containerize-deploy/references/manual-fallback.md`（現行 container-ctl 的對應手動步驟）與 `prod-deployment-workflow-deploy/SKILL.md`（舊 legacy deploy 流程，僅供查閱）。
2. 手動步驟是**備源選項，不是預設**。使用條件（全部成立）：工具缺失或失敗且查不出原因；使用者在本 session 批准；寫偏離記錄（步驟 ID、原因、指令、輸出）；事後以工具重跑對齊（`prod doctor`、`image audit-prod`、`deploy verify`）。不符條件的手動部署仍是重大錯誤（使用者 2026-10-05 規則）。
3. **每個手動步驟必須錨定到一個工具程式碼符號**（`檔案::符號`）。錨定表如下；測試 `tests/test_adr_anchors.py` 驗證每個錨點存在，符號被改名或刪除時測試失敗，迫使本 ADR 與手動步驟同步更新。
4. 機密、憑證、token 沒有工具錨點，歸使用者；agent 不得手動代做。

## 錨定表

| ID | 手動步驟 | 工具入口 | 錨點（檔案::符號） |
| --- | --- | --- | --- |
| M1 | 建宿主目錄與 uid 映射 | `host prepare`、`prod up` step 2 | `container_ctl/cli/host_prepare.py::apply_actions` |
| M1p | 規劃要建的目錄 | `host prepare`（預覽） | `container_ctl/cli/host_prepare.py::plan_host_prepare` |
| M2 | 由乾淨 commit 建 prod 映像 | `image prepare-prod` | `container_ctl/cli/image_prod.py::prepare_prod_image` |
| M3 | 寫 release tag | `image publish-prod` | `container_ctl/cli/image_prod.py::publish_prod_image` |
| M3p | 提交 image pin 到設定 repo | `image publish-prod` | `container_ctl/cli/image_prod.py::_commit_pin_to_config_repo` |
| M4 | 重啟單一服務 | `service restart` | `container_ctl/cli/service.py::operate_manual_instance` |
| M4a | 重啟後啟動 app | `service restart --with-app` | `container_ctl/cli/service.py::_run_app_hooks` |
| M5 | 匯出映像 | `release export` | 已停止支援（未註冊子命令） |
| M5d | 由匯出包部署 | `release deploy` | 已停止支援（未註冊子命令） |
| M6 | 渲染 Quadlet | `release export-quadlet` | 已停止支援（改用 `deploy plan/redeploy`） |
| M8 | 整條 prod 流程 | `prod up` | `container_ctl/cli/prod_up.py::prod_up` |
| M9 | 預檢（宣告、gate、路徑、映像） | `prod doctor` | `container_ctl/cli/prod_doctor.py::run_prod_doctor` |
| M10 | 宣告慣例檢查 | `validate` | `container_ctl/declaration_guard.py::check_runtime_conventions` |
| M11 | 找實際容器與宣告的差異 | `redeploy` | `container_ctl/live_drift.py::find_live_drift` |
| M7 | 機密、TLS、token 放置 | 無（使用者） | — |

## 後果

- 手動路徑可被查到、可被審計，但每次使用都留下偏離記錄。
- 工具改名或移除符號時，錨定測試失敗，ADR 必須同步。
- 專案端（例如 app）的手動腳本不刪除；改為標註對應的 ID 與備源狀態，由該專案 session 自行審查（見 skill 的通知步驟）。
