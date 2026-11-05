#!/usr/bin/env bash
# verify_bash.sh — Bash path resolver verification runner against matrix.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_PATH="$REPO_ROOT/lib/app_paths.sh"

echo "=== [Bash] app_paths.sh Verification ==="
FAILED=0

# Helper to run a test case in an isolated subshell
run_tc() {
    local tc_id="$1"
    local desc="$2"
    local env_setup="$3"
    local exp_root="$4"
    local exp_cfg="$5"
    local exp_data="$6"
    local exp_run="$7"
    local exp_log="$8"

    # Evaluate in a clean subshell where APP_* and PROJECT_NAME are unset first
    local out
    out=$(
        env -i HOME="$HOME" PATH="$PATH" bash -c "
            $env_setup
            . '$SRC_PATH'
            echo \"APP_ROOT=\$APP_ROOT\"
            echo \"APP_CONFIG_DIR=\$APP_CONFIG_DIR\"
            echo \"APP_DATA_DIR=\$APP_DATA_DIR\"
            echo \"APP_RUNTIME_DIR=\$APP_RUNTIME_DIR\"
            echo \"APP_LOG_DIR=\$APP_LOG_DIR\"
        "
    )

    local act_root act_cfg act_data act_run act_log
    act_root=$(echo "$out" | grep '^APP_ROOT=' | cut -d= -f2-)
    act_cfg=$(echo "$out" | grep '^APP_CONFIG_DIR=' | cut -d= -f2-)
    act_data=$(echo "$out" | grep '^APP_DATA_DIR=' | cut -d= -f2-)
    act_run=$(echo "$out" | grep '^APP_RUNTIME_DIR=' | cut -d= -f2-)
    act_log=$(echo "$out" | grep '^APP_LOG_DIR=' | cut -d= -f2-)

    local tc_failed=0
    if [ "$act_root" != "$exp_root" ]; then
        echo "  [$tc_id] FAIL ($desc): APP_ROOT expected '$exp_root', got '$act_root'"
        tc_failed=1
    fi
    if [ "$act_cfg" != "$exp_cfg" ]; then
        echo "  [$tc_id] FAIL ($desc): APP_CONFIG_DIR expected '$exp_cfg', got '$act_cfg'"
        tc_failed=1
    fi
    if [ "$act_data" != "$exp_data" ]; then
        echo "  [$tc_id] FAIL ($desc): APP_DATA_DIR expected '$exp_data', got '$act_data'"
        tc_failed=1
    fi
    if [ "$act_run" != "$exp_run" ]; then
        echo "  [$tc_id] FAIL ($desc): APP_RUNTIME_DIR expected '$exp_run', got '$act_run'"
        tc_failed=1
    fi
    if [ "$act_log" != "$exp_log" ]; then
        echo "  [$tc_id] FAIL ($desc): APP_LOG_DIR expected '$exp_log', got '$act_log'"
        tc_failed=1
    fi

    if [ "$tc_failed" -eq 0 ]; then
        echo "  [$tc_id] PASS: $desc"
    else
        FAILED=$((FAILED + 1))
    fi
}

# TC-01
run_tc "TC-01" "全部設定：APP_ROOT 覆寫優先" \
    "export APP_ROOT='/workspace' PROJECT_NAME='proj'" \
    "/workspace" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"

# TC-02
run_tc "TC-02" "僅 PROJECT_NAME：全部使用 FHS 預設值" \
    "export PROJECT_NAME='proj'" \
    "/opt/proj" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"

# TC-03
run_tc "TC-03" "全部未設定：使用 myapp 預設值" \
    "" \
    "/opt/myapp" "/etc/opt/myapp" "/var/opt/myapp" "/run/myapp" "/var/log/myapp"

# TC-04
run_tc "TC-04" "顯式設定 FHS 路徑" \
    "export APP_ROOT='/opt/proj' APP_CONFIG_DIR='/etc/opt/proj' APP_DATA_DIR='/var/opt/proj' APP_RUNTIME_DIR='/run/proj' APP_LOG_DIR='/var/log/proj' PROJECT_NAME='proj'" \
    "/opt/proj" "/etc/opt/proj" "/var/opt/proj" "/run/proj" "/var/log/proj"

if [ "$FAILED" -gt 0 ]; then
    echo -e "\n[Bash] Verification failed: $FAILED test case(s) failed." >&2
    exit 1
else
    echo -e "\n[Bash] All test cases passed successfully."
    exit 0
fi
