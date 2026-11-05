# ADR-001: Dual-Track Dockerfile 分軌構建策略

**Status**: Proposed
**Date**: 2026-08-19
**Deciders**: 系統架構討論（對話 668d3491）

---

## 問題背景 (Context)

容器化開發中存在兩套路徑認知：
- 開發態慣例：`/workspace`（IDE、Agent、DevContainer 生態標準）
- 運行態規範：FHS 標準（`/opt/<project>`、`/etc/opt/<project>`、`/var/opt/<project>`）

若用單一 Dockerfile 同時服務兩種場景，必然導致：
- 開發工具污染生產映像（增大攻擊面）
- 生產路徑假設無法在開發容器中驗證
- IDE、Agent、Language Server 的開箱即用性降低

---

## 決策 (Decision)

**採用 Dual-Track Dockerfile 分軌構建策略**：

- `Dockerfile.dev`：開發軌
  - `WORKDIR /workspace`
  - 包含除錯器、Language Server、Agent CLI、git 工具
  - 宿主機 Repo 以 Bind-Mount 掛載至 `/workspace`（Live Mount）
  - 由 `container-ctl` 注入 Dev Overlay Env（`APP_ROOT=/workspace` 等）

- `Dockerfile.prod`：生產軌
  - `WORKDIR /opt/<project>`（或由 `APP_ROOT` 決定）
  - `COPY . /opt/<project>`（透過 `.dockerignore` 排除 `.git`、`.data`、開發工具）
  - 僅保留 runtime 必要依賴
  - 支援 `--read-only` rootfs，可寫路徑全部透過 Quadlet `Volume=` 外掛

---

## 正向後果 (Positive Consequences)

- 開發者和 Agent 在 IDE 中享有標準 `/workspace` 單一工作區體驗
- 生產容器輕量化、零開發工具殘留、最小攻擊面
- 兩軌的 Git 交互完全乾淨：代碼操作都在 `/workspace` 的單一 Git Repo 根目錄完成

## 負向後果 (Negative Consequences)

- 需維護兩個 Dockerfile，引入 Dual-Track Drift 風險
- **緩解方案**：使用共享 Base Image（`FROM dev-base:...`）確保依賴版本一致

> **[TODO] Shared Base Image 責任歸屬待確認**
> Shared Base Image 的定義、版本管理與發布流程應由 `container-ctl` 模組負責，
> `standards` 僅作為消費者，僅宣告 `FROM` 來源。
> 此決策跨越模組邊界，待 `container-ctl` 發布對應 ADR 後，本 ADR 將新增交叉引用。
> 在此之前，各專案可暫以 `FROM ubuntu:24.04` 作為基礎並自行固定版本。

---

## 相關文件

- [ADR-002: Env-Driven Path Resolver](ADR-002-env-driven-path-resolver.md)
- [ADR-003: scan-path-refs 靜態路徑掃描門禁](ADR-003-static-path-gate.md)
