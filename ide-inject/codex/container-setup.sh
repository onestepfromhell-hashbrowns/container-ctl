#!/bin/sh
# container-setup.sh — 容器內 Codex Runtime 環境初始化與自癒腳本（純 POSIX / 無 Python 依賴）
# 所屬模組：ide-inject/codex/container-setup.sh
set -e

# A container has one canonical Codex runtime view. Do not inherit a host-side
# CODEX_HOME, which could split credentials/configuration across two homes.
CODEX_HOME="/root/.codex"
export CODEX_HOME

# 1. 確保 /root/.codex 基礎目錄結構存在（注意：sessions 軟連結由 ExtScope 受管單一投影，嚴禁在此預建）
mkdir -p /root/.codex /root/.codex/skills /usr/local/bin /etc/codex

# 2. 設置環境變數至 /etc/profile.d/codex.sh
cat > /etc/profile.d/codex.sh << 'EOF'
export CODEX_HOME="/root/.codex"
export SHELL="/bin/bash"
EOF
chmod 0644 /etc/profile.d/codex.sh

# 3. 憑證重注入（Ticket 04b / Ticket 14: Establish codex auth symlink from tmpfs）
AUTH_TMPFS="/run/secrets/codex/auth.json"
AUTH_LINK="${CODEX_HOME}/auth.json"

if [ -f "$AUTH_TMPFS" ]; then
    mkdir -p "$(dirname "$AUTH_LINK")"
    ln -sf "$AUTH_TMPFS" "$AUTH_LINK"
    echo "[ext] codex auth.json symlink established: $AUTH_LINK -> $AUTH_TMPFS"
elif [ -f "$AUTH_LINK" ]; then
    chmod 0600 "$AUTH_LINK" 2>/dev/null || true
fi

# 4. 安裝過濾完成的設定檔（Fail-Closed：僅允許宿主端審計過濾後的安全產物）
CONFIG_INJECT="/run/ext/tokens/config.toml"

if [ -f "$CONFIG_INJECT" ]; then
    mkdir -p /etc/codex "$CODEX_HOME"
    cp "$CONFIG_INJECT" /etc/codex/config.toml
    chmod 0644 /etc/codex/config.toml
    cp "$CONFIG_INJECT" "${CODEX_HOME}/config.toml"
    chmod 0600 "${CODEX_HOME}/config.toml"
    echo "[ext] codex config.toml installed successfully"
fi

if [ -f "${CODEX_HOME}/config.toml" ]; then
    chmod 0600 "${CODEX_HOME}/config.toml" 2>/dev/null || true
fi

# 5. 確保 Standalone Codex 唯一受管發行樹存在並建立 /usr/local/bin/codex 符號連結（Fail-Closed，拒絕舊 fallback）
if [ -x /run/ext/codex-dist/bin/codex ]; then
    ln -sfn /run/ext/codex-dist/bin/codex /usr/local/bin/codex
    # 確保伴生二進制工具具備執行權限 (P2 Remediation)
    [ -f /run/ext/codex-dist/bin/codex-code-mode-host ] && chmod 0755 /run/ext/codex-dist/bin/codex-code-mode-host 2>/dev/null || true
    [ -f /run/ext/codex-dist/codex-path/rg ] && chmod 0755 /run/ext/codex-dist/codex-path/rg 2>/dev/null || true
    [ -f /run/ext/codex-dist/codex-resources/bwrap ] && chmod 0755 /run/ext/codex-dist/codex-resources/bwrap 2>/dev/null || true
    # 確保受管 B-2 啟動入口 (codex-app-server) 具備執行權限並建立符號連結
    if [ -f /run/ext/codex-dist/bin/codex-app-server ]; then
        chmod 0755 /run/ext/codex-dist/bin/codex-app-server 2>/dev/null || true
        ln -sfn /run/ext/codex-dist/bin/codex-app-server /usr/local/bin/codex-app-server
    fi
else
    echo "[ext] ERROR: Injected runtime /run/ext/codex-dist/bin/codex not found or not executable" >&2
    exit 1
fi

# 6. 若系統技能存在於 FHS 中繼點，建立 /root/.codex/skills/.system 符號連結
if [ -d /opt/ext/fhs/codex/system-skills ]; then
    ln -sfn /opt/ext/fhs/codex/system-skills /root/.codex/skills/.system
fi

exit 0
