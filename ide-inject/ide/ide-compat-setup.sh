#!/bin/sh
# ide-compat-setup.sh — 容器內 IDE Server 相容性與工具鏈自癒腳本
set -e

# 1. 確保基礎目錄結構（處理 dangling symlink 或現有 symlink，轉為實體目錄）
ensure_real_dir() {
    for _target_dir in "$@"; do
        if [ -L "$_target_dir" ]; then
            rm -f "$_target_dir"
        fi
        mkdir -p "$_target_dir" 2>/dev/null || true
    done
}

ensure_real_dir /root/.antigravity-ide-server \
                /root/.antigravity-ide-server/bin \
                /root/.antigravity-ide-server/extensions \
                /root/.vscode-server \
                /root/.vscode-server/bin \
                /root/.vscode-server/extensions \
                /root/.gemini/antigravity-ide \
                /usr/local/bin

# 2. 清理過期或無效的 installation_lock
if [ -f /root/.antigravity-ide-server/.installation_lock ]; then
    if ! grep -qs 'server-main.js' /proc/[0-9]*/cmdline 2>/dev/null && ! pgrep -f '[o]ut/server-main.js' >/dev/null 2>&1; then
        rm -f /root/.antigravity-ide-server/.installation_lock 2>/dev/null || true
    fi
fi
if [ -f /root/.vscode-server/.installation_lock ]; then
    if ! grep -qs 'server-main.js' /proc/[0-9]*/cmdline 2>/dev/null && ! pgrep -f '[o]ut/server-main.js' >/dev/null 2>&1; then
        rm -f /root/.vscode-server/.installation_lock 2>/dev/null || true
    fi
fi

# 3. 提供 wget 相容轉發器（若容器無 wget 但有 curl）
if ! command -v wget >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1; then
        cat << 'EOF' > /usr/local/bin/wget
#!/bin/sh
# wget compatibility wrapper using curl
output=""
quiet=0
url=""

while [ $# -gt 0 ]; do
    case "$1" in
        -O)
            output="$2"
            shift 2
            ;;
        -O*)
            output="${1#-O}"
            shift 1
            ;;
        -q|--quiet|-nv)
            quiet=1
            shift 1
            ;;
        --no-check-certificate)
            shift 1
            ;;
        -t|-T|--timeout|--tries)
            shift 2
            ;;
        http://*|https://*|ftp://*)
            if [ -z "$url" ]; then
                url="$1"
            else
                url="$url $1"
            fi
            shift 1
            ;;
        *)
            if [ -n "$url" ]; then
                url="$url $1"
            fi
            shift 1
            ;;
    esac
done

if [ -z "$url" ]; then
    echo "wget: missing URL" >&2
    exit 1
fi

curl_opts="-fL"
if [ "$quiet" -eq 1 ]; then
    curl_opts="${curl_opts}s"
fi

encoded_url=$(printf '%s' "$url" | sed 's/ /%20/g')

if [ -n "$output" ]; then
    if [ "$output" = "-" ]; then
        exec curl $curl_opts "$encoded_url"
    else
        exec curl $curl_opts -o "$output" "$encoded_url"
    fi
else
    filename=$(basename "$url")
    exec curl $curl_opts -O "$encoded_url"
fi
EOF
        chmod 0755 /usr/local/bin/wget
    fi
fi

# 4. 若中繼點有擴充套件，安全建立目錄並複製檔案（非符號連結）
ext_src=""
if [ -d /run/ext/ide-extensions ] && [ "$(ls -A /run/ext/ide-extensions 2>/dev/null)" ]; then
    ext_src="/run/ext/ide-extensions"
elif [ -d /opt/ext/fhs/ide-extensions ] && [ "$(ls -A /opt/ext/fhs/ide-extensions 2>/dev/null)" ]; then
    ext_src="/opt/ext/fhs/ide-extensions"
fi
if [ -n "$ext_src" ]; then
    ensure_real_dir /root/.antigravity-ide-server/extensions /root/.vscode-server/extensions
    cp -rn "$ext_src"/* /root/.antigravity-ide-server/extensions/ 2>/dev/null || true
    cp -rn "$ext_src"/* /root/.vscode-server/extensions/ 2>/dev/null || true
fi

# 5. 若中繼點有宿主機已下載之 IDE Server 快取，安全建立目錄並複製檔案（純 hash + 2.5.5 前綴相容）
ag_src=""
if [ -d /run/ext/bin/ide-antigravity-bin ]; then
    ag_src="/run/ext/bin/ide-antigravity-bin"
elif [ -d /opt/ext/fhs/ide-antigravity-bin ]; then
    ag_src="/opt/ext/fhs/ide-antigravity-bin"
fi
if [ -n "$ag_src" ]; then
    for item_dir in "$ag_src"/*; do
        if [ -d "$item_dir" ]; then
            bname=$(basename "$item_dir")
            commit_hash=$(echo "$bname" | grep -oE '[a-f0-9]{40}' || true)
            [ -z "$commit_hash" ] && commit_hash="$bname"

            ensure_real_dir "/root/.antigravity-ide-server/bin/$commit_hash"
            cp -a "$item_dir"/* "/root/.antigravity-ide-server/bin/$commit_hash/" 2>/dev/null || true
            rm -rf "/root/.antigravity-ide-server/bin/2.5.5-$commit_hash" 2>/dev/null || true
            ensure_real_dir "/root/.antigravity-ide-server/bin/2.5.5-$commit_hash"
            cp -a "$item_dir"/* "/root/.antigravity-ide-server/bin/2.5.5-$commit_hash/" 2>/dev/null || true
        fi
    done
fi

vs_src=""
if [ -d /run/ext/bin/ide-vscode-bin ]; then
    vs_src="/run/ext/bin/ide-vscode-bin"
elif [ -d /opt/ext/fhs/ide-vscode-bin ]; then
    vs_src="/opt/ext/fhs/ide-vscode-bin"
fi
if [ -n "$vs_src" ]; then
    for vs_item_dir in "$vs_src"/*; do
        if [ -d "$vs_item_dir" ]; then
            bname=$(basename "$vs_item_dir")
            commit_hash=$(echo "$bname" | grep -oE '[a-f0-9]{40}' || true)
            [ -z "$commit_hash" ] && commit_hash="$bname"
            ensure_real_dir "/root/.vscode-server/bin/$commit_hash"
            cp -rn "$vs_item_dir"/* "/root/.vscode-server/bin/$commit_hash/" 2>/dev/null || true
        fi
    done
fi

# 6. 若中繼點有 Antigravity Brain / Conversations 持久化目錄，自癒軟連結
if [ -d /opt/ext/fhs/antigravity/brain ]; then
    if [ ! -L /root/.gemini/antigravity-ide/brain ] || [ ! -e /root/.gemini/antigravity-ide/brain ]; then
        rm -rf /root/.gemini/antigravity-ide/brain 2>/dev/null || true
        ln -s /opt/ext/fhs/antigravity/brain /root/.gemini/antigravity-ide/brain 2>/dev/null || true
    fi
fi
if [ -d /opt/ext/fhs/antigravity/conversations ]; then
    if [ ! -L /root/.gemini/antigravity-ide/conversations ] || [ ! -e /root/.gemini/antigravity-ide/conversations ]; then
        rm -rf /root/.gemini/antigravity-ide/conversations 2>/dev/null || true
        ln -s /opt/ext/fhs/antigravity/conversations /root/.gemini/antigravity-ide/conversations 2>/dev/null || true
    fi
fi

# 7. 寫入 ExtScope 完成 IDE 環境就緒標誌
mkdir -p /run/ext 2>/dev/null || true
touch /run/ext/.ide_env_ready 2>/dev/null || true

exit 0
