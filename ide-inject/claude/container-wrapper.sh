#!/bin/sh
# Runtime-only Claude Code launcher.  The credential is intentionally read
# only for this child process; it is neither added to a profile nor persisted
# under the container root filesystem.
set -eu

runtime_bin="${CLAUDE_RUNTIME_BIN:-/run/ext/bin/claude-native}"
secret_file="${CLAUDE_SECRET_FILE:-/run/secrets/claude/anthropic_api_key}"

if [ ! -x "$runtime_bin" ]; then
    echo "Claude runtime is not projected; enable ext-system-ide-claude." >&2
    exit 127
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -r "$secret_file" ]; then
    case "$(tr -d '\r\n[:space:]' < "$secret_file")" in
        sk-ant-oat*) exec env CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '\r\n[:space:]' < "$secret_file")" "$runtime_bin" "$@" ;;
        *) exec env ANTHROPIC_API_KEY="$(tr -d '\r\n[:space:]' < "$secret_file")" "$runtime_bin" "$@" ;;
    esac
fi

exec "$runtime_bin" "$@"
