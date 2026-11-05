# ADR-005: SQLite 作為中央實例狀態儲存

- **日期**：2026-08-11
- **狀態**：Accepted

## 背景

共享服務的 Ref Count、實例 provision 狀態與生命週期事件需要持久化記錄，供 CLI、SSH 套件與 Network Broker 查詢。

## 決策

採用 SQLite 資料庫作為單一中央狀態儲存：

- **路徑**：`$CONTAINER_CTL_SERVICES_DIR/state.db`
- **三張核心表**：
  - `instances`：所有已 provision 的容器實例、Blueprint 快照、當前狀態
  - `shared_refs`：共享服務的活性引用計數與 lifecycle_policy
  - `lifecycle_events`：所有生命週期事件的時間序列日誌

## 後果

- ✅ CLI、SSH 套件、Network Broker 可查詢同一真實來源
- ✅ 支援 Ref Count 精確管理，避免共享服務提前停止或洩漏
- ✅ 生命週期事件可稽核
- ⚠️ 需處理 SQLite 檔案鎖（多程序寫入需序列化或使用 WAL 模式）
