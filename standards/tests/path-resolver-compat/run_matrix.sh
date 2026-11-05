#!/usr/bin/env bash
# run_matrix.sh — Master test runner for cross-language path resolver consistency.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "============================================================"
echo " Running BDD Cross-Language Path Resolver Test Matrix"
echo "============================================================"

FAILED_LANGS=()

# 1. Python
if python3 "$SCRIPT_DIR/verify_python.py"; then
    echo ">> Python: PASS"
else
    echo ">> Python: FAIL" >&2
    FAILED_LANGS+=("Python")
fi
echo ""

# 2. Bash
if bash "$SCRIPT_DIR/verify_bash.sh"; then
    echo ">> Bash: PASS"
else
    echo ">> Bash: FAIL" >&2
    FAILED_LANGS+=("Bash")
fi
echo ""

# 3. Node.js
if bash "$SCRIPT_DIR/verify_nodejs.sh"; then
    echo ">> Node.js: PASS"
else
    echo ">> Node.js: FAIL" >&2
    FAILED_LANGS+=("Node.js")
fi
echo ""

# 4. Go
if bash "$SCRIPT_DIR/verify_go.sh"; then
    echo ">> Go: PASS"
else
    echo ">> Go: FAIL" >&2
    FAILED_LANGS+=("Go")
fi
echo ""

echo "============================================================"
if [ ${#FAILED_LANGS[@]} -eq 0 ]; then
    echo " ALL LANGUAGES PASSED THE TEST MATRIX SUCCESSFULLY!"
    echo "============================================================"
    exit 0
else
    echo " MATRIX FAILED for: ${FAILED_LANGS[*]}" >&2
    echo "============================================================"
    exit 1
fi
