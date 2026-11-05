---
name: container-doctor
category: Container Architecture & Diagnostics
description: >-
  容器環境對齊與輕量診斷核查工具 (container-doctor)。當進入容器工作階段、排查掛載異常、動態 SSH 憑證或沙盒邊界狀態、或執行低 Token 消耗的環境快速檢驗 (minimal/diff 模式) 時使用。
version: 1.2.0
last_updated: 2026-09-09
---

# Container Doctor 環境對齊與輕量診斷指南

`container-doctor` 是一套極低 Token 開銷 (~15-50 tokens) 的容器環境動態核查工具，專為 AI Agent 在容器生命週期中驗證沙盒邊界、掛載路徑與服務狀態而設計。

> **硬化規格**：宿主入口點 `the container image entry point` 已載入 `runtime_prelude.sh`，預設注入非互動環境變數。

## 核心操作命令（標準防禦外殼規範）

所有 Agent 調用必須包裹標準防禦外殼：

1. **極簡健康檢查 (Minimal 模式，預設推薦，~15 tokens)**：
   ```bash
   timeout -k 2s 5s bash -c 'container-doctor check' < /dev/null
   ```
2. **差異修復檢驗 (Diff 模式，遇錯時精確輸出修復指令，~50 tokens)**：
   ```bash
   timeout -k 2s 5s bash -c 'container-doctor check --diff' < /dev/null
   ```
3. **機器可讀 JSON 模式 (~40 tokens)**：
   ```bash
   timeout -k 2s 5s bash -c 'container-doctor check --json' < /dev/null
   ```
4. **比對具體設定檔對齊**：
   ```bash
   timeout -k 2s 5s bash -c 'container-doctor reconcile --config <config_file>' < /dev/null
   ```

## 輸出策略與 Token 消耗評估
* **minimal** (~15 tokens)：單行狀態摘要，正常時維持 context 乾淨，為 Agent 開機檢驗首選。
* **diff** (~50 tokens)：僅輸出不一致點與具體可執行之修復指令。
* **json** (~40 tokens)：單行 JSON 格式，便於程式解析。
* **full** (~300 tokens)：完整層級診斷樹，僅在深度排錯時使用。
