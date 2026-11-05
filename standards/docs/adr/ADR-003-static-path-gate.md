# ADR-003: scan-path-refs 靜態路徑掃描門禁

**Status**: Proposed
**Date**: 2026-08-19
**Deciders**: 系統架構討論（對話 668d3491）
**修訂**: 2026-08-19（Step 2 規格合成，消除 C2 衝突：獨立建立工具）

---

## 問題背景 (Context)

Dual-Track + Path Resolver 架構的有效性依賴「沒有路徑硬編碼」的前提。
人為規範（Code Review）無法完全保證，需要自動化靜態分析工具作為安全防線。

> **設計邊界說明**：`<site-utils>/scan_path_refs.py` 是掃描 site 工具套件
> 自身路徑洩漏問題的工具，語義層次與本模組需求不同。
> 本 ADR 決定在 `container-ctl` **獨立建立** `tools/scan-path-refs`，
> 而非擴展 `site utilities` 模組（避免邊界污染與耦合）。

---

## 決策 (Decision)

**在 `container-ctl/tools/scan-path-refs` 建立獨立掃描工具**，
強制執行以下場景的路徑硬編碼靜態掃描：

### 掃描觸發時機

1. **Git Pre-commit Hook**：每次 commit 前掃描本次更改的文件
2. **CI/CD Pipeline**：`container-ctl deploy` 之前作為 Pre-flight Check
3. **Pull Request 門禁**：PR 合入主分支前必須通過掃描

### 違規規則（Forbidden Patterns）

| 規則 | 違規範例 | 掃描目標文件類型 |
| :--- | :--- | :--- |
| 生產配置不得含 `/workspace` | `Volume=/workspace/data` | `*.container`, `*.json`, `*.yaml` |
| 應用代碼不得硬編碼路徑 | `open("/workspace/config")` | `*.py`, `*.sh`, `*.js`, `*.go` |
| 應用代碼不得硬編碼 `/opt/<proj>` | `open("/opt/myapp/config")` | `*.py`, `*.sh`, `*.js`, `*.go` |
| 生產 Quadlet 不得含 `/home/<user>` | （繼承全域規則）| `*.container`, `*.network` |

### 允許的例外

- `Dockerfile.dev` 中出現 `/workspace`（此為設計行為）
- `.deploy/containers/dev.json` 中出現 `/workspace`（Dev 宣告本身）
- `CONTEXT.md`、`docs/adr/` 等文件（說明文件）
- `tests/` 目錄下的測試路徑 fixtures

### 實作方式：兩層制規則架構

`tools/scan-path-refs` 採用**兩層制規則**設計，支援配置外載：

```
Layer 1（全域核心規則）：tools/scan-path-refs/rules/core.yaml
  ── 永遠生效，覆蓋所有掃描對象
  ── 禁止 /workspace（生產配置）、/home/<user>（全域繼承）

Layer 2（專案例外規則）：<project>/.scan-path-refs.yaml（各專案可選）
  ── 擴充或覆寫例外清單，例如允許特定測試目錄
  ── 不得放寬 Layer 1 的核心禁止規則
```

**規則配置格式（YAML）範例**：

```yaml
# <project>/.scan-path-refs.yaml
version: "1"
extends: "container-ctl/tools/scan-path-refs/rules/core.yaml"
exceptions:
  - pattern: "/workspace"
    paths: ["tests/fixtures/**"]
    reason: "測試 fixture 允許包含 /workspace 路徑字串"
```

---

## 正向後果 (Positive Consequences)

- 自動化防禦：路徑硬編碼在合入代碼庫前即被攔截
- 可以為不同類型的文件配置不同的掃描規則（精細控制）
- 兩層制架構讓各專案可擴充例外清單，同時核心規則不可繞過
- 語義邊界清晰：與 `<site-utils>/scan_path_refs.py`（site 基礎設施掃描）完全獨立

## 負向後果 (Negative Consequences)

- 需要在 CI/CD 管線中配置額外的掃描步驟
- 初期可能存在誤報（False Positive），需要調整規則與例外清單
- 需要在 `container-ctl/tools/` 目錄下維護獨立工具

---

## 相關文件

- [ADR-001: Dual-Track Dockerfile](ADR-001-dual-track-dockerfile.md)
- [ADR-002: Env-Driven Path Resolver](ADR-002-env-driven-path-resolver.md)
- [ADR-004: 多語言 Path Resolver 插拔架構](ADR-004-multilang-path-resolver.md)
