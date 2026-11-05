# tools/scan-path-refs/README.md
# scan-path-refs — Static Path Reference Scanner

靜態掃描應用程式代碼中的硬編碼路徑引用，防止 `/workspace`、`/opt/<proj>` 與 `/home/<user>` 滲入生產配置。

---

## 設計邊界

> **本工具 ≠ `<site-utils>/scan_path_refs.py`**
>
> `<site-utils>/scan_path_refs.py` 掃描 site 基礎設施層路徑洩漏（工具套件層）。
> 本工具掃描應用程式代碼中的業務路徑硬編碼（應用層）。
> 兩者語義層次不同，禁止合併或擴展。

## 安裝

```bash
# 確保執行權限
chmod +x <packages>/container-ctl/tools/scan-path-refs/scan-path-refs

# 安裝 PyYAML（必需）
pip3 install pyyaml
```

## 基本使用

> 注意：宿主 PATH 上的裸 `scan-path-refs` 指向 `<site-opt-root>/bin/scan-path-refs`（site utilities 的基礎設施層工具），不是本工具。本工具一律用絕對路徑呼叫：`python3 <packages>/container-ctl/tools/scan-path-refs/scan-path-refs`。

```bash
# 掃描當前目錄
scan-path-refs

# 掃描指定目錄
scan-path-refs --target <projects-root>/myapp

# 僅掃描特定副檔名
scan-path-refs --ext py,sh,yaml

# Dry-run 模式（輸出但不 exit 1）
scan-path-refs --dry-run --target .

# 使用專案層例外配置
scan-path-refs --config .scan-path-refs.yaml --target .
```

## Exit Codes

| Code | 含義 |
|------|------|
| `0` | 通過（無違規） |
| `1` | 發現違規 |
| `2` | 工具錯誤（配置錯誤、規則檔無法讀取等） |

## 核心規則（`rules/core.yaml`）

| 規則 ID | 描述 | 目標檔案 |
|---------|------|----------|
| R-001 | 生產配置不得含 `/workspace` | `*.container`, `*.json`, `*.yaml`, `*.toml` |
| R-002 | 應用代碼不得硬編碼 `/workspace` | `*.py`, `*.sh`, `*.js`, `*.ts`, `*.go` |
| R-003 | 應用代碼不得硬編碼 `/opt/<proj>` | `*.py`, `*.sh`, `*.js`, `*.ts`, `*.go` |
| R-004 | 生產 Quadlet 不得含 `/home/<user>` | `*.container`, `*.network`, `*.volume` |

所有規則的 severity 均為 `error`，不可被專案層配置降低。

## 專案層例外配置（`.scan-path-refs.yaml`）

在專案根目錄建立 `.scan-path-refs.yaml`：

```yaml
version: "1"
extends: "container-ctl/tools/scan-path-refs/rules/core.yaml"
exceptions:
  - rule_id: R-002
    paths:
      - "tests/fixtures/**"
      - "tests/path-resolver-compat/**"
    reason: "測試 fixture 允許包含 /workspace 路徑字串"
```

> **限制**：例外規則只能增加 `exceptions`，不得降低 severity 或關閉規則。

## Git Pre-commit Hook 整合

```bash
# .git/hooks/pre-commit
#!/usr/bin/env sh
set -e
python3 <packages>/container-ctl/tools/scan-path-refs/scan-path-refs \
  --target . \
  --config .scan-path-refs.yaml
```

## CI 整合

```yaml
# GitHub Actions 範例
- name: Path Reference Scan
  run: |
    python3 tools/scan-path-refs/scan-path-refs \
      --target . \
      --config .scan-path-refs.yaml
```

## 執行自身測試

```bash
cd <packages>/container-ctl/tools/scan-path-refs
python3 -m pytest tests/test_scan.py -v --tb=short
```

## 相關文件

- [ADR-003: Static Path Gate](../../standards/docs/adr/ADR-003-static-path-gate.md)
- [ADR-002: Env-Driven Path Resolver](../../standards/docs/adr/ADR-002-env-driven-path-resolver.md)
