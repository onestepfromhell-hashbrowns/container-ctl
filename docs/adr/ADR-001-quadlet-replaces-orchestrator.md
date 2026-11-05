# ADR-001: Podman Quadlet + Systemd 作為容器編排唯一 SSOT

- **日期**：2026-08-11
- **狀態**：Accepted

## 背景

靜態 `podman run` Shell 啟動腳本無法支援開機自啟、崩潰重啟與依賴編排。

## 決策

採用 **Podman Quadlet + Systemd Generator** 作為營運容器編排之唯一 SSOT：
- 取代靜態 Shell 啟動邏輯
- 透過 `@` 模板單元 + Drop-In Overrides 實現多實例隔離
- 保留開發區掛載與熱掛載機制繼續共存

## 後果

- ✅ 開機自啟、崩潰自動重啟由 systemd 原生管理
- ✅ 多實例 Port/Volume 隔離乾淨
- ⚠️ 需將所有受管服務統一為 Quadlet 單元檔
