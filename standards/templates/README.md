# templates/ — Dual-Track Dockerfile 模板使用說明

`standards/templates/` 提供 Dual-Track Dockerfile 模板，
作為各子專案容器化的起點（ADR-001）。

---

## 模板清單

| 檔案 | 用途 |
|------|------|
| `Dockerfile.dev` | 開發軌道：掛載宿主代碼，覆寫 APP_* 至 /workspace |
| `Dockerfile.prod` | 生產軌道：嵌入代碼，使用 FHS 標準路徑 |
| `.dockerignore` | 生產建構排除規則 |

---

## 快速開始

### 步驟 1：複製模板至專案

```bash
PROJECT=myservice
mkdir -p <projects-root>/$PROJECT
cp templates/Dockerfile.dev  <projects-root>/$PROJECT/
cp templates/Dockerfile.prod <projects-root>/$PROJECT/
cp templates/.dockerignore   <projects-root>/$PROJECT/
```

### 步驟 2：替換佔位符

```bash
cd <projects-root>/$PROJECT

# 替換所有 'myapp' 為實際服務名稱
sed -i "s/myapp/$PROJECT/g" Dockerfile.dev Dockerfile.prod
```

### 步驟 3：補充語言 Runtime

在 `Dockerfile.dev` 中取消注釋所需語言 runtime 安裝指令（Python/Node.js/Go）。

在 `Dockerfile.prod` 中確認 runtime 依賴最小化。

### 步驟 4：驗證路徑配置

確認 `Dockerfile.dev` 的 ENV APP_* 覆寫與 `src/lib/app_paths.*` 模板一致：

| 環境 | APP_ROOT | APP_CONFIG_DIR |
|------|----------|----------------|
| Dev  | `/workspace` | `/workspace/config` |
| Prod | `/opt/<proj>` | `/etc/opt/<proj>` |

---

## 設計原則（ADR-001）

### Dev 軌道規則
- `WORKDIR /workspace`
- 不含 `COPY . /workspace`（宿主代碼由 container-ctl Live Mount 提供）
- 必須宣告 `ENV APP_*` 覆寫至 `/workspace/*`
- 可包含開發工具（debugger、language server、git）

### Prod 軌道規則
- `WORKDIR /opt/<proj>`（FHS 標準）
- 必須包含 `COPY . /opt/<proj>`（使用 `.dockerignore` 排除開發產物）
- **不覆寫** `APP_*` 環境變數（依賴 `app_paths.*` 的 FHS 預設值）
- 可寫資料由部署宣告的 `volumes`／`tmpfs` 提供，避免映像 `VOLUME` 產生匿名卷
- 禁止包含開發工具

---

## 相關文件

- [ADR-001: Dual-Track Dockerfile](../docs/adr/ADR-001-dual-track-dockerfile.md)
- [ADR-002: APP_* 命名規範](../docs/adr/ADR-002-app-env-vars.md)
- [src/lib/app_paths.py](../src/lib/app_paths.py) — Python 路徑解析器模板
