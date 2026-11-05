# ADR-004: Dynamic Network Broker 動態容器網路連接設計

- **日期**：2026-08-11
- **狀態**：Accepted

## 背景

透過宿主機 HTTP API 與手動橋接 CLI 進行容器間動態網路 Attach，與 Quadlet 原生網路機制不整合。

## 決策

**① 禁止宿主機 HTTP 橋接管道**

- ⛔ 禁止：`http://host.containers.internal:8900` 宿主橋接 API
- ⛔ 禁止：手動橋接 CLI 管道設計
- 保留動態臨時容器通訊的**業務需求**，以事件驅動套件實作

**② 引入 Dynamic Network Broker**

一個事件驅動套件，職責：
1. 訂閱 Quadlet / systemd 產生的容器網路事件
2. 當專案 Bridge 網路（`net-prod-<project>`）建立時，廣播通知相關被動容器
3. 被動容器（如 Relay）接收事件後，自行透過 Podman 原生 API 串流加入 Bridge

**③ 容器間通訊底層**

容器間通訊底層改為 Podman 原生 Netavark/CNI DNS，不依賴宿主機 HTTP Proxy 轉發。

## 後果

- ✅ 網路設計符合 Podman 標準架構
- ✅ 動態 Attach 業務能力保留
- ⚠️ 被動容器需透過標準事件訂閱接入 Bridge
