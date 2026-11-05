# ADR-010: Artifact 身分採 OCI manifest digest；OCI Image Layout 作為 artifact store

- **日期**：2026-10-08
- **狀態**：Accepted（P3 已實作）
- **關聯**：ADR-009、DESIGN-prod-version-anchor、ADR-007

## 背景
release 記錄只有本機 `image_id`（config digest）。它不是 OCI manifest digest：離開本機儲存庫無法由第三方驗證，也無法對應 registry 內容。本機建置映像沒有 RepoDigests，映像一旦被清理就無法還原。

## 決策
- 身分＝OCI **manifest digest**（對 OCI Image Spec）。`release/<svc>/<ver>` 記錄升為 schema 2：`manifest_digest`、`image_id`（保留供 `audit-prod`）、`source_commit`、`config_commit`、`contract_hash`。舊記錄（無 `manifest_digest`）仍可讀，`image verify` 回報為舊格式。
- artifact store＝OCI Image Layout（`$CONTAINER_CTL_OCI_STORE/<project>/<service>/`，ref name＝version），由 `podman push --digestfile oci:` 寫入；寫入後重算 manifest blob 的 sha256，與 digest 比對。同 ref 不可覆寫。`CONTAINER_CTL_OCI_STORE=off` 停用。
- `publish-prod` 先寫 store、再寫 release tag；store 失敗＝不發布（不留下沒有可還原內容的 release）。
- 建置加 `org.opencontainers.image.{revision,version,title,created}`；`created` 取 source commit 時間，保持可重現。
- 不引入 registry 服務；store 介面（`put/verify/load`）與 OCI Distribution 語意一致，日後替換不影響上層。

## 後果
- ✅ 映像可由 store 驗證與還原（`artifact_store.load`），可封入 bundle（P6）。
- ✅ 供應鏈欄位（SBOM、簽章）有掛載點（P7）。
- ⚠️ 儲存成本：每版一份映像副本；清理規則同映像（只封存、不刪）。
- ⚠️ 既有 release 不補 manifest digest（adopted/舊版以 image_id 錨定）。
