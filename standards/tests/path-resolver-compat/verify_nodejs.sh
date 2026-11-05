#!/usr/bin/env bash
# verify_nodejs.sh — Node.js path resolver verification runner against matrix.yaml.
set -euo pipefail

if ! command -v node >/dev/null 2>&1; then
    echo "=== [Node.js] Node.js not found in PATH, skipping ==="
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC_PATH="$REPO_ROOT/lib/app_paths.js"

echo "=== [Node.js] app_paths.js Verification ==="

node - <<EOF
const path = require('path');
const fs = require('fs');

const srcPath = '$SRC_PATH';

function testWithEnv(envVars) {
  // Clear require cache and clean env
  delete require.cache[require.resolve(srcPath)];
  const origEnv = { ...process.env };
  
  // Clean all APP_* and PROJECT_NAME
  for (const k of Object.keys(process.env)) {
    if (k.startsWith('APP_') || k === 'PROJECT_NAME') {
      delete process.env[k];
    }
  }
  Object.assign(process.env, envVars);
  
  try {
    const mod = require(srcPath);
    return {
      APP_ROOT: mod.APP_ROOT,
      APP_CONFIG_DIR: mod.APP_CONFIG_DIR,
      APP_DATA_DIR: mod.APP_DATA_DIR,
      APP_RUNTIME_DIR: mod.APP_RUNTIME_DIR,
      APP_LOG_DIR: mod.APP_LOG_DIR,
    };
  } finally {
    process.env = origEnv;
  }
}

const testCases = [
  {
    id: 'TC-01',
    desc: '全部設定：APP_ROOT 覆寫優先',
    env: { APP_ROOT: '/workspace', PROJECT_NAME: 'proj' },
    expect: {
      APP_ROOT: '/workspace',
      APP_CONFIG_DIR: '/etc/opt/proj',
      APP_DATA_DIR: '/var/opt/proj',
      APP_RUNTIME_DIR: '/run/proj',
      APP_LOG_DIR: '/var/log/proj'
    }
  },
  {
    id: 'TC-02',
    desc: '僅 PROJECT_NAME：全部使用 FHS 預設值',
    env: { PROJECT_NAME: 'proj' },
    expect: {
      APP_ROOT: '/opt/proj',
      APP_CONFIG_DIR: '/etc/opt/proj',
      APP_DATA_DIR: '/var/opt/proj',
      APP_RUNTIME_DIR: '/run/proj',
      APP_LOG_DIR: '/var/log/proj'
    }
  },
  {
    id: 'TC-03',
    desc: '全部未設定：使用 myapp 預設值',
    env: {},
    expect: {
      APP_ROOT: '/opt/myapp',
      APP_CONFIG_DIR: '/etc/opt/myapp',
      APP_DATA_DIR: '/var/opt/myapp',
      APP_RUNTIME_DIR: '/run/myapp',
      APP_LOG_DIR: '/var/log/myapp'
    }
  },
  {
    id: 'TC-04',
    desc: '顯式設定 FHS 路徑',
    env: {
      APP_ROOT: '/opt/proj',
      APP_CONFIG_DIR: '/etc/opt/proj',
      APP_DATA_DIR: '/var/opt/proj',
      APP_RUNTIME_DIR: '/run/proj',
      APP_LOG_DIR: '/var/log/proj',
      PROJECT_NAME: 'proj'
    },
    expect: {
      APP_ROOT: '/opt/proj',
      APP_CONFIG_DIR: '/etc/opt/proj',
      APP_DATA_DIR: '/var/opt/proj',
      APP_RUNTIME_DIR: '/run/proj',
      APP_LOG_DIR: '/var/log/proj'
    }
  }
];

let failed = 0;
for (const tc of testCases) {
  const actual = testWithEnv(tc.env);
  let tcFailed = false;
  for (const [k, v] of Object.entries(tc.expect)) {
    if (actual[k] !== v) {
      console.error(\`  [\${tc.id}] FAIL (\${tc.desc}): \${k} expected '\${v}', got '\${actual[k]}'\`);
      tcFailed = true;
    }
  }
  if (!tcFailed) {
    console.log(\`  [\${tc.id}] PASS: \${tc.desc}\`);
  } else {
    failed++;
  }
}

if (failed > 0) {
  console.error(\`\n[Node.js] Verification failed: \${failed} test case(s) failed.\`);
  process.exit(1);
} else {
  console.log(\`\n[Node.js] All test cases passed successfully.\`);
  process.exit(0);
}
EOF
