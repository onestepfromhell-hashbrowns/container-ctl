# ADR-002: SSH 大廳非阻塞啟動與專案生命週期管控

- **日期**：2026-08-11
- **狀態**：Accepted（修正於 grill-with-docs 對齊後）

## 背景

原設計問題：`ssh-entry.sh` 大廳在依賴服務啟動時會因同步等待健康檢查卡頓，造成連線超時。

**澄清**：SSH 與專案並非完全脫鉤。`ssh-entry.sh` → workspace mount → 專案目錄的掛載關係**完整保留**，IDE workspace 仍透過 mount 連接到專案。

## 決策

**① 非阻塞啟動（解決登入卡頓）**

所有外部依賴服務的拉起一律使用：
```bash
systemctl --user start --no-block <service>
```
健康檢查輪詢嚴禁在 SSH 大廳登入路徑上執行。

**② 新版 SSH 套件負責專案生命週期管控**

由升級後的 SSH 連線套件處理：
- SSH 登入 → 觸發專案相關容器啟動（非阻塞）
- SSH 登出 → 通知專案進入 idle 狀態

**③ 外部依賴依 lifecycle_policy 自治**

專案外部依賴服務（如 PostgreSQL、Relay）依照各自的 `lifecycle_policy` 與專案實際狀態決定是否停止：
- `always_on`：SSH 登出後仍繼續運行
- `auto_idle`：Ref Count 歸零後計時自動停止

## 後果

- ✅ SSH 大廳瞬間登入，不卡頓
- ✅ Workspace mount 掛載關係保持不變，IDE 不受影響
- ✅ 共享服務不因單一 SSH 登出而中斷其他依賴方
- ⚠️ 需開發新版 SSH 套件處理專案生命週期事件
