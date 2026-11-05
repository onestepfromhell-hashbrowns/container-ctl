# ADR-006: `.deploy/` 目錄存於 Repo，透過 git show 讀取

- **日期**：2026-08-11
- **狀態**：Superseded by [ADR-008](ADR-008-deploy-config-repo.md)（存放位置；`git show` 讀取方式沿用）

## 背景

容器化宣告應與原始碼版本綁定，確保可重現性。Host 只有 Bare Repo，無工作區 checkout。

## 決策

- 容器化宣告存入 Repo 的 `.deploy/` 目錄
- Host CLI 一律透過 `git show <ref>:.deploy/<file>` 從 Bare Repo 讀取，**不執行 checkout**
- 目錄結構：
  - `.deploy/manifest.json` — 專案名稱、版本、依賴清單
  - `.deploy/containers/<name>.json` — 各容器專屬配置（image、scope、ports、volumes、env、lifecycle_policy）

## 後果

- ✅ 容器化宣告與程式碼版本完全綁定，可重現
- ✅ Host 不需要工作區目錄，Bare Repo 保持純淨
- ✅ 可在不影響任何執行環境的情況下查閱任意版本的容器宣告
