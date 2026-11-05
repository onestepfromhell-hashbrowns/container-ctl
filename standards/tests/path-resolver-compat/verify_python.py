#!/usr/bin/env python3
"""
verify_python.py — Python path resolver verification runner against matrix.yaml.
"""

import os
import sys
import importlib.util
from pathlib import Path

try:
    import yaml
except ImportError:
    print("verify_python: PyYAML is required", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_PATH = REPO_ROOT / "lib" / "app_paths.py"
MATRIX_FILE = Path(__file__).resolve().parent / "matrix.yaml"


def load_app_paths(env_vars: dict[str, str]):
    """Execute app_paths.py with an isolated environment clean of other APP_* vars."""
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("APP_") and k != "PROJECT_NAME"}
    clean_env.update(env_vars)
    
    # Save original os.environ
    orig_env = os.environ.copy()
    os.environ.clear()
    os.environ.update(clean_env)
    
    try:
        spec = importlib.util.spec_from_file_location("app_paths_test", SRC_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {
            "APP_ROOT": str(mod.APP_ROOT),
            "APP_CONFIG_DIR": str(mod.APP_CONFIG_DIR),
            "APP_DATA_DIR": str(mod.APP_DATA_DIR),
            "APP_RUNTIME_DIR": str(mod.APP_RUNTIME_DIR),
            "APP_LOG_DIR": str(mod.APP_LOG_DIR),
        }
    finally:
        os.environ.clear()
        os.environ.update(orig_env)


def main():
    raw = yaml.safe_load(MATRIX_FILE.read_text(encoding="utf-8"))
    test_cases = raw.get("test_cases", [])
    failed = 0

    print("=== [Python] app_paths.py Verification ===")
    for tc in test_cases:
        tc_id = tc["id"]
        desc = tc.get("description", "")
        env_vars = tc.get("env", {})
        expected = tc.get("expect", {})
        
        actual = load_app_paths(env_vars)
        tc_failed = False
        for k, v in expected.items():
            if actual.get(k) != v:
                print(f"  [{tc_id}] FAIL ({desc}): {k} expected '{v}', got '{actual.get(k)}'")
                tc_failed = True
        
        if not tc_failed:
            print(f"  [{tc_id}] PASS: {desc}")
        else:
            failed += 1

    if failed > 0:
        print(f"\n[Python] Verification failed: {failed} test case(s) failed.", file=sys.stderr)
        sys.exit(1)
    else:
        print("\n[Python] All test cases passed successfully.")
        sys.exit(0)


if __name__ == "__main__":
    main()
