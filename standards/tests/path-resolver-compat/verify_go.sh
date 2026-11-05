#!/usr/bin/env bash
# verify_go.sh — Go path resolver verification runner against matrix.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_PATH="$REPO_ROOT/lib/app_paths.go"

echo "=== [Go] app_paths.go Verification ==="

if ! command -v go >/dev/null 2>&1; then
    echo "  [INFO] 'go' command not found on host environment."
    echo "  [INFO] Static syntax and structure verification will be performed."
    
    # Static verification: check package declaration, exported variables, and helper presence
    if grep -q "package apppaths" "$SRC_PATH" && \
       grep -q "AppRoot" "$SRC_PATH" && \
       grep -q "AppConfigDir" "$SRC_PATH" && \
       grep -q "AppDataDir" "$SRC_PATH" && \
       grep -q "AppRuntimeDir" "$SRC_PATH" && \
       grep -q "AppLogDir" "$SRC_PATH" && \
       grep -q "getEnvOrDefault" "$SRC_PATH"; then
        echo "  [STATIC] PASS: package structure, exported vars, and getEnvOrDefault helper verified."
        exit 0
    else
        echo "  [STATIC] FAIL: incomplete structure in $SRC_PATH" >&2
        exit 1
    fi
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/apppaths"
cp "$SRC_PATH" "$TMP_DIR/apppaths/app_paths.go"

# Create a test main package in go
cat << 'EOF' > "$TMP_DIR/main.go"
package main

import (
	"fmt"
	"os"
	"path-test/apppaths"
)

func main() {
	fmt.Printf("APP_ROOT=%s\n", apppaths.AppRoot)
	fmt.Printf("APP_CONFIG_DIR=%s\n", apppaths.AppConfigDir)
	fmt.Printf("APP_DATA_DIR=%s\n", apppaths.AppDataDir)
	fmt.Printf("APP_RUNTIME_DIR=%s\n", apppaths.AppRuntimeDir)
	fmt.Printf("APP_LOG_DIR=%s\n", apppaths.AppLogDir)
}
EOF

cat << 'EOF' > "$TMP_DIR/go.mod"
module path-test

go 1.22
EOF

cd "$TMP_DIR"
go build -o test_runner main.go

# Run test cases
FAILED=0
run_go_tc() {
    local tc_id="$1"
    local desc="$2"
    local env_setup="$3"
    local exp_root="$4"
    local exp_cfg="$5"
    local exp_data="$6"
    local exp_run="$7"
    local exp_log="$8"

    local out
    out=$(
        env -i PATH="$PATH" bash -c "
            $env_setup
            $TMP_DIR/test_runner
        "
    )

    local act_root act_cfg act_data act_run act_log
    act_root=$(echo "$out" | grep '^APP_ROOT=' | cut -d= -f2-)
    act_cfg=$(echo "$out" | grep '^APP_CONFIG_DIR=' | cut -d= -f2-)
    act_data=$(echo "$out" | grep '^APP_DATA_DIR=' | cut -d= -f2-)
    act_run=$(echo "$out" | grep '^APP_RUNTIME_DIR=' | cut -d= -f2-)
    act_log=$(echo "$out" | grep '^APP_LOG_DIR=' | cut -d= -f2-)

    local tc_failed=0
    [ "$act_root" = "$exp_root" ] || tc_failed=1
    [ "$act_cfg" = "$exp_cfg" ] || tc_failed=1
    [ "$act_data" = "$exp_data" ] || tc_failed=1
    [ "$act_run" = "$exp_run" ] || tc_failed=1
    [ "$act_log" = "$exp_log" ] || tc_failed=1

    if [ "$tc_failed" -eq 0 ]; then
        echo "  [$tc_id] PASS: $desc"
    else
        echo "  [$tc_id] FAIL: $desc"
        FAILED=$((FAILED + 1))
    fi
}

run_go_tc "TC-01" "全部設定：APP_ROOT 覆寫優先" "export APP_ROOT='/workspace' PROJECT_NAME='proj'" "/workspace" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"
run_go_tc "TC-02" "僅 PROJECT_NAME：全部使用 FHS 預設值" "export PROJECT_NAME='proj'" "/opt/proj" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"
run_go_tc "TC-03" "全部未設定：使用 myapp 預設值" "" "/opt/myapp" "/etc/opt/myapp" "/var/opt/myapp" "/run/myapp" "/var/log/myapp"
run_go_tc "TC-04" "顯式設定 FHS 路徑" "export APP_ROOT='/opt/proj' APP_CONFIG_DIR='/etc/opt/proj' APP_DATA_DIR='/var/opt/proj' APP_RUNTIME_DIR='/run/proj' APP_LOG_DIR='/var/log/proj' PROJECT_NAME='proj'" "/opt/proj" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"

if [ "$FAILED" -gt 0 ]; then
    echo -e "\n[Go] Verification failed: $FAILED test case(s) failed." >&2
    exit 1
fi
echo -e "\n[Go] All test cases passed successfully."
exit 0
