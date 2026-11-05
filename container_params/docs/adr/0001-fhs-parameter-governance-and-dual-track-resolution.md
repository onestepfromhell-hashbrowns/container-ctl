# ADR 0001: 通用 FHS 容器標準參數檢查套件架構決策 (FHS Parameter Governance & Dual-Track Resolution)

## 狀態
已通過 (Accepted) - 2026-09-02

## 背景 (Context)
在微服務與容器化開發流程中（如 `ServiceA` 及 site 相關容器），不同環境（Dev Container、Local Checkout、Prod Container）常遭遇以下問題：
1. **路徑硬編碼**：共用代碼寫死 `/workspace` 或 `/opt/<app>`，導致跨環境執行失敗。
2. **環境判斷污染**：在業務代碼中出現 `if env == 'dev'` 等邏輯分支，破壞可攜性。
3. **配置混亂**：缺乏統一的 FHS 標準路徑與配置覆蓋階層，易導致 PID/Log/DB 存取權限衝突。

因此需要抽取出通用的 FHS 標準參數檢查與路徑解析套件（`container_params`）。

## 決策 (Decision)
1. **建立通用參數治理與檢查器**：
   - 提供靜態檢查器（CLI / 函式庫），掃描程式碼庫與設定檔，自動抓取 5 大反模式（絕對路徑硬編碼、環境分支、硬編碼埠號/主機、控制腳本寫死路徑、密鑰外洩）。
   - 提供通用 Python Path Resolver 與 Config Resolver 抽象基底類別或模板，供不同專案直接引用或繼承。
2. **嚴格遵守 6 大 FHS 職責路徑與 5 層配置覆蓋階層**：
   - Prod: `/opt/<slug>`, `/etc/opt/<slug>`, `/run/<slug>`, `/var/log/<slug>`, `/var/lib/<slug>`, `/opt/<slug>/{frontend,static_vendors}`
   - Dev: `${APP_ROOT}`, `${APP_ROOT}/app/configs`, `${APP_ROOT}/run`, `${APP_ROOT}/logs`, `${APP_ROOT}/data`, `${APP_ROOT}/{frontend,static_vendors}`
3. **提供 CI 門禁與對抗式驗證矩陣**：
   - 包含 AST 乾淨室掃描與跨環境路徑矩陣單元測試。

## 後果 (Consequences)
- **正面效益**：各容器專案可直接複用標準檢查器與解析器，大幅減少重複代碼，確保生產環境具備唯讀根檔案系統相容性與最高安全性。
- **維護代價**：各專案需在 CI/CD 中引入路徑檢查門禁，並統一遵循 FHS 命名與解析規則。
