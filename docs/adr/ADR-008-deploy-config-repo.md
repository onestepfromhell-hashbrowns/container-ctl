# ADR-008: 部署宣告獨立成設定 repo（取代 ADR-006 的存放位置）

- **日期**：2026-10-03（2026-10-04 補實施狀態）
- **狀態**：Accepted
- **取代**：ADR-006 的「宣告存於程式碼 Repo」；沿用其「以 `git show` 讀取、不 checkout」

## 背景

宣告與程式碼同住造成：能改程式碼者（含 dev 容器內 agent）就能改 prod 宣告；`publish-dev` 的 pin commit 混進程式碼分支；release tag 與宣告分屬不同歷史。

## 決策

- 宣告放 `$CONTAINER_CTL_CONFIG_REPOS_DIR/<project>.git`（bare，分支 `main`），內容沿用 `.deploy/` 路徑結構。
- 解析層 `resolve_deploy_repo`：設定 repo 優先，不存在才回退程式碼 Bare Repo（逐專案雙讀，回退＝刪設定 repo）。
- release tag 與 dev 映像 pin 都寫進設定 repo。
- 設定 repo **不得掛進任何容器**（`declaration_guard` 對所有 tier 檢查）。
- 多環境（如 remote VPS）放同一設定 repo 的 `.deploy/envs/<env>/containers/`；讀取層尚未實作。
- 基礎設施（如 nginx-ingress）以專案名 `infra` 建設定 repo，配置檔版本化於其 `nginx/`，密鑰留宿主。

## 後果

- ✅ 寫入權限與程式碼分離；宣告、pin、release tag 同史可稽核。
- ⚠️ 同 uid 下沒有真正的存取隔離（issue 22）。
- ⚠️ 雙讀期間兩處都有 `.deploy`；`config status`／`validate` 的 WARN 防誤改舊處。legacy 讀取待全部遷完且穩定後另案移除。

設計與步驟見 `docs/DESIGN-deploy-config-repo-20261003.md`。
