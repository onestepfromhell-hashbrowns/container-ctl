#!/bin/sh
# proxy-compat-setup.sh — 容器內 SSH/IDE 代理網路工具鏈 (socat & ss) 動態補全腳本
set -e

mkdir -p /usr/local/bin

# 1. 動態補全 ss 工具（若容器缺乏原生 ss）
if ! command -v ss >/dev/null 2>&1; then
    cat << 'SS_EOF' > /usr/local/bin/ss
#!/bin/sh
# Lightweight POSIX ss emulator reading from /proc/net/tcp and /proc/net/tcp6
for candidate in /usr/bin/ss /bin/ss /sbin/ss /usr/sbin/ss; do
    if [ -x "$candidate" ] && [ "$candidate" != "$0" ]; then
        exec "$candidate" "$@"
    fi
done

echo "State      Recv-Q Send-Q Local Address:Port               Peer Address:Port"
if [ -f /proc/net/tcp ]; then
    while read -r sl local rem st rest; do
        if [ "$st" = "0A" ]; then
            port_hex=$(echo "$local" | cut -d: -f2)
            port=$((0x$port_hex))
            echo "LISTEN     0      128    127.0.0.1:${port}        0.0.0.0:*"
        fi
    done < /proc/net/tcp
fi
if [ -f /proc/net/tcp6 ]; then
    while read -r sl local rem st rest; do
        if [ "$st" = "0A" ]; then
            port_hex=$(echo "$local" | cut -d: -f2)
            port=$((0x$port_hex))
            echo "LISTEN     0      128    [::]:${port}             [::]:*"
        fi
    done < /proc/net/tcp6
fi
SS_EOF
    chmod 0755 /usr/local/bin/ss
fi

# 2. 動態補全 socat 工具（若容器缺乏原生 socat）
if ! command -v socat >/dev/null 2>&1; then
    cat << 'SOCAT_EOF' > /usr/local/bin/socat
#!/bin/sh
# Lightweight zero-dependency socat emulator for stdio <-> TCP bridging
for candidate in /usr/bin/socat /bin/socat /usr/local/socat/socat; do
    if [ -x "$candidate" ] && [ "$candidate" != "$0" ]; then
        exec "$candidate" "$@"
    fi
done

target=""
for arg in "$@"; do
    case "$arg" in
        TCP:*|tcp:*|TCP4:*|tcp4:*)
            target=$(echo "$arg" | sed -E 's/^[tT][cC][pP]4?://')
            ;;
    esac
done

if [ -z "$target" ]; then
    echo "socat shim: missing TCP target" >&2
    exit 1
fi

host=$(echo "$target" | cut -d: -f1)
port=$(echo "$target" | cut -d: -f2)
[ -z "$host" ] && host="127.0.0.1"

# 1. 優先嘗試 Node.js（IDE Server 自帶 runtime）
NODE_BIN=""
for n in /usr/bin/node /usr/local/bin/node /root/.antigravity-ide-server/bin/*/node /root/.vscode-server/bin/*/node; do
    if [ -x "$n" ]; then
        NODE_BIN="$n"
        break
    fi
done

if [ -n "$NODE_BIN" ]; then
    exec "$NODE_BIN" -e '
const net = require("net");
const sock = net.connect(process.argv[2], process.argv[1], () => {
    process.stdin.pipe(sock);
    sock.pipe(process.stdout);
});
sock.on("error", () => process.exit(1));
sock.on("close", () => process.exit(0));
' "$host" "$port"
fi

# 2. 次要嘗試 Python 3
if command -v python3 >/dev/null 2>&1; then
    exec python3 -u -c '
import sys, socket, threading
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((sys.argv[1], int(sys.argv[2])))
def to_sock():
    try:
        while True:
            chunk = sys.stdin.buffer.read1(4096) if hasattr(sys.stdin.buffer, "read1") else sys.stdin.buffer.read(4096)
            if not chunk: break
            s.sendall(chunk)
    except Exception: pass
    finally:
        try: s.shutdown(socket.SHUT_WR)
        except Exception: pass
def from_sock():
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
    except Exception: pass
threading.Thread(target=to_sock, daemon=True).start()
from_sock()
s.close()
' "$host" "$port"
fi

# 3. 嘗試 nc (netcat)
if command -v nc >/dev/null 2>&1; then
    exec nc "$host" "$port"
fi

echo "socat shim: no node, python3, or nc found for TCP tunnel" >&2
exit 1
SOCAT_EOF
    chmod 0755 /usr/local/bin/socat
fi

exit 0
