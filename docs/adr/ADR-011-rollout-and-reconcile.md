# ADR-011: Rollout 紀錄、調和與回滾

- **日期**：2026-10-08
- **狀態**：Accepted（P4 已實作）
- **關聯**：ADR-009、OpenGitOps v1（宣告式、版本化不可變、持續調和）、K8s `rollout undo`

## 背景
回滾曾以收據／state.db 版本欄推算「前一版」，與實際運行不符（0.3.4 vs 實際 0.3.2）時回到錯誤版本並污染設定庫。改配置沒有正式路徑，只能借道 `prod up --ver`。

## 決策
- **rollout＝把 (release, 設定 repo 修訂, 環境) 套用到宿主的一次事件**，寫入 state.db `rollouts`（release 版本、image_id、manifest_digest、config_commit、result、actor、時間）。`prod up`、`rollout apply`、`rollout undo` 都會記錄成功與失敗。
- `container-ctl rollout apply <project> <svc> [--env E] [--yes]`：不建置、不發布，只套用現行宣告（`config set` 之後的正規步驟）。流程：宣告與 release tag 一致性 → doctor 嚴格 → reconcile 預覽 → redeploy → health → smoke → verify／audit → doctor-final。與 `prod up` 共用 `prod_up.converge`。
- `container-ctl rollout undo [--to ID] [--mode both|image|config] [--yes]`：目標＝最近一個 **result=healthy** 且與目前不同的 rollout；`--mode image` 只還原 image／version，`config` 只還原其餘宣告（含 overlay），`both` 整份。預覽列出將還原／刪除的檔案；本機缺映像時自 OCI store 還原（無 manifest 記錄則拒絕）。寫設定 repo 後立即 apply。
- `container-ctl reconcile <project> <svc>`：唯讀，desired（設定 repo＋release）vs actual（`podman inspect`，沿用 `live_drift`），並報告多實例並存、最近 rollout 失敗、設定 repo 已前進。
- 舊 `prod rollback --to <ver>`（以 release tag 還原 image pin）保留；`--rollback-on-fail` 的目標改取自實際運行容器（已於 396ac6e）。
- 單元測試以 `CONTAINER_CTL_ROLLOUT_RECORD=off` 關閉預設 state.db 寫入；傳 `db_path` 時一律記錄。

## 後果
- ✅ 改 env／埠不發版：`config set` → `rollout apply`。回滾依據是「實際健康過」的事實。
- ⚠️ 此前的部署沒有 rollout 紀錄；`reconcile` 會提示，首次 `rollout apply` 後才有回滾基準。
