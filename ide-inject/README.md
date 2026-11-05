# ide-inject

由 Extension Scope（`ext-system-ide-{claude,antigravity,codex}`）注入開發容器的 IDE 與 Agent 輔助檔：

- Agent CLI 二進位檔不包含於本倉庫，於執行期透過只讀掛載投影至 `/run/ext/`（例如 `container-wrapper.sh` 以 `CLAUDE_RUNTIME_BIN` 呼叫）。
- 憑證類來源（`anthropic_api_key`、`auth.json`、`session_token`）由執行期 tmpfs 機密掛載提供，不在倉庫內。
