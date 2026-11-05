# ADR-007: Release Bundle 機制 — Strip Export 與離線部署

- **日期**：2026-08-11
- **狀態**：Accepted

## 背景

開發環境擁有完整 Bare Repo。Release 後的生產環境可能無 Bare Repo、需要精簡鏡像（移除 dev 工具）、或需要在無網路環境離線部署。

## 決策

引入 **Release Bundle** 格式作為開發到生產的橋接：

1. **Bundle 與 `.deploy/` 格式完全相容**：`blueprint.json` 格式與 `.deploys/` 中的 Blueprint JSON 相同，Quadlet 生成流水線不需修改。

2. **Strip 建置可選**：若容器宣告含 `strip.containerfile` + `strip.target`，`release export --strip` 觸發多階段建置並重新鎖定 Pinned Digest；否則直接沿用開發 Image Digest。

3. **離線模式可選**：`--offline` 旗標執行 `podman save`，將 Image 封裝為 tar 存入 Bundle，供 air-gapped 環境載入。

4. **部署指令統一**：`release deploy <bundle-dir>` 複製 Blueprint 至 `.deploys/`，接入標準 `§5 Apply` 流水線，與一般部署行為完全一致。

## 後果

- ✅ 同一套 `.deploy/` 格式同時支援開發部署與生產 Release
- ✅ 精簡鏡像（Strip）與 Image Digest 鎖定機制完全相容
- ✅ 支援 air-gapped 離線環境
- ✅ Bundle 版本獨立，可隨時回滾至任意舊版 Bundle
- ⚠️ Strip 建置需要 Containerfile 包含多階段 `production` target
- ⚠️ `release export` 需要在有 Bare Repo 的環境執行（通常是 CI 或開發機）
