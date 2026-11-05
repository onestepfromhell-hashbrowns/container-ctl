# tests/path-resolver-compat — BDD 跨語言行為測試矩陣

確保 Python / Bash / Node.js / Go 四種語言實作在相同環境變數輸入下，產生 100% 一致的路徑解析輸出。

---

## 測試案例（`matrix.yaml`）

| 測試 ID | 說明 | 輸入環境變數 | 期望 APP_ROOT | 期望 APP_CONFIG_DIR |
|---------|------|--------------|---------------|---------------------|
| `TC-01` | 全部設定 | `APP_ROOT=/workspace`, `PROJECT_NAME=proj` | `/workspace` | `/etc/opt/proj` |
| `TC-02` | 僅專案名稱 | `PROJECT_NAME=proj` | `/opt/proj` | `/etc/opt/proj` |
| `TC-03` | 全部未設定 | （無） | `/opt/myapp` | `/etc/opt/myapp` |
| `TC-04` | 顯式 FHS | `APP_ROOT=/opt/proj`, `PROJECT_NAME=proj` | `/opt/proj` | `/etc/opt/proj` |

---

## 執行方式

### 執行全語言矩陣測試

```bash
cd <packages>/container-ctl/standards/tests/path-resolver-compat
bash run_matrix.sh
```

### 執行單一語言驗證

```bash
# Python
python3 verify_python.py

# Bash
bash verify_bash.sh

# Node.js
bash verify_nodejs.sh

# Go
bash verify_go.sh
```

---

## 相關文件

- [ADR-004: Multilang Path Resolver](../../docs/adr/ADR-004-multilang-path-resolver.md)
- [src/lib/app_paths.py](../../src/lib/app_paths.py)
- [src/lib/app_paths.sh](../../src/lib/app_paths.sh)
- [src/lib/app_paths.js](../../src/lib/app_paths.js)
- [src/lib/app_paths.go](../../src/lib/app_paths.go)
