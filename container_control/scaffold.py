"""
Project scaffolding generator for container service control.
Generates standard Two-Tier architecture:
- Tier 1: entrypoint.sh (OCI Container Lifecycle & PID 1 Entrypoint)
- Tier 2: control.sh (In-Container Service Process Manager)
- Config: configs/service_control.json (Universal Declarative Configuration)
"""

import json
import os
from pathlib import Path
from typing import Optional


ENTRYPOINT_SH_TEMPLATE = """#!/usr/bin/env bash
# ==============================================================================
# Tier 1: OCI Container Lifecycle & PID 1 Entrypoint
# Responsibilities:
#   1. System-level workspace directory and permission preparation
#   2. Default foreground service launch via control.sh
#   3. Transparent passthrough for ad-hoc debugging / test commands (pytest, bash)
# ==============================================================================
set -e

# 1. Ensure runtime directories exist
mkdir -p /workspace/logs /workspace/configs /workspace/scripts

# 2. Command dispatching
if [ $# -eq 0 ] || [ "$1" = "default" ]; then
    echo "[entrypoint] Starting default service in foreground mode..."
    exec /workspace/control.sh start --foreground
else
    # Transparent passthrough for arbitrary container execution (e.g. pytest, bash)
    exec "$@"
fi
"""

CONTROL_SH_TEMPLATE = """#!/usr/bin/env bash
# ==============================================================================
# Tier 2: In-Container Service Process Controller (FHS Compliant)
# Operations: start | stop | restart | status | probe | logs | --help
#
# Three-block architecture:
#   1. Lookup block  — find container-control binary (PATH only, no host paths)
#   2. Delegate block — exec container-control when found (full feature set)
#   3. NATIVE_FALLBACK — self-contained Bash/Python when container-control absent
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${SCRIPT_DIR}/configs/service_control.json"
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"

show_help() {
    echo "Usage: ./control.sh <command> [options]"
    echo ""
    echo "FHS Compliant Container Service Control Interface (Tier 2)"
    echo ""
    echo "Core Operations:"
    echo "  start       Start service process (-f / --foreground for PID 1 mode)"
    echo "  stop        Stop running service (SIGTERM -> SIGKILL)"
    echo "  restart     Restart service gracefully"
    echo "  status      Display service health status"
    echo "  probe       Execute configured readiness probe"
    echo "  logs        Display recent stdout/stderr logs (-f to follow)"
    echo ""
    echo "Options:"
    echo "  -h, --help  Show this help message and exit"
    echo "  -f          Run in foreground mode (start command only)"
    echo ""
    echo "Examples:"
    echo "  ./control.sh start"
    echo "  ./control.sh start --foreground"
    echo "  ./control.sh status"
    echo "  ./control.sh probe"
    echo "  ./control.sh logs -f"
    echo "  ./control.sh restart"
    echo "  ./control.sh stop"
}

if [ "$1" = "-h" ] || [ "$1" = "--help" ] || [ "$1" = "help" ]; then
    show_help
    exit 0
fi

# -- Block 1: Lookup ----------------------------------------------------------
# Locate container-control in PATH only (no host-specific hardcoded paths).
if command -v container-control >/dev/null 2>&1; then
    EXEC_CMD="container-control"
else
    EXEC_CMD=""
fi

# -- Block 2: Delegate (when container-control is available) ------------------
if [ -n "$EXEC_CMD" ]; then
    exec "$EXEC_CMD" "$@"
fi

# -- Block 3: NATIVE_FALLBACK -------------------------------------------------
# Self-contained Bash/Python implementation. No host paths, no external imports.

# Inline JSON reader (requires only python3)
_json_get() {
    python3 - "$CONF" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f: d=json.load(f)
    s=d.get('service',d); p=s.get('readiness_probe') or {}; k=sys.argv[2]
    values={'name':s.get('name','app'),'pid_file':s.get('pid_file','app.pid'),
      'log_file':s.get('log_file','logs/app.log'),'port':s.get('port',''),
      'working_dir':s.get('working_dir','.'),
      'shutdown_timeout':s.get('shutdown_timeout',5),'probe_type':p.get('type','http'),
      'probe_port':p.get('port',s.get('port',8080)),'probe_path':p.get('path','/health'),
      'probe_timeout':p.get('timeout_seconds',3),'expected_status':p.get('expected_status',200)}
    print(values[k])
except Exception as e:
    print(f'Native fallback config error: {e}',file=sys.stderr); sys.exit(2)
PY
}

_validate_config() {
    python3 - "$CONF" <<'PY'
import json, math, re, sys
try:
    with open(sys.argv[1], encoding='utf-8') as stream: document=json.load(stream)
    service=document.get('service',document)
    if not isinstance(service,dict): raise ValueError('service must be an object')
    command=service.get('command',['python3','scripts/app.py'])
    if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command): raise ValueError('service.command must be a non-empty string array')
    for field in ('pre_start_hooks','pre_stop_hooks'):
        hooks=service.get(field,[])
        if not isinstance(hooks,list) or not all(isinstance(x,str) for x in hooks): raise ValueError(f'{field} must be a string array')
    env=service.get('env',{})
    if not isinstance(env,dict) or any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k) for k in env): raise ValueError('service.env must contain valid environment variable names')
    timeout=float(service.get('shutdown_timeout',5))
    if not math.isfinite(timeout) or timeout<0: raise ValueError('shutdown_timeout must be a finite non-negative number')
    for field,default in (('pid_file','app.pid'),('log_file','logs/app.log'),('working_dir','.')):
        if not isinstance(service.get(field,default),str): raise ValueError(f'{field} must be a path string')
except Exception as error:
    print(f'Native fallback config error: {error}',file=sys.stderr)
    sys.exit(1)
PY
}

_json_get_list() {
    python3 - "$CONF" "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1],encoding='utf-8') as f: d=json.load(f)
    items=d.get('service',d).get(sys.argv[2],[])
    if not isinstance(items,list) or not all(isinstance(x,str) for x in items): raise ValueError('expected string array')
    for item in items: sys.stdout.buffer.write(item.encode()+b'\\0')
except Exception as e:
    print(f'Native fallback config error: {e}',file=sys.stderr); sys.exit(2)
PY
}

_get_service_name()     { _json_get name; }
_resolve_path() { local p; p="$(_json_get "$1")"; case "$p" in /*) echo "$p" ;; *) echo "${SCRIPT_DIR}/${p}" ;; esac; }
_get_pid_file()         { _resolve_path pid_file; }
_get_log_file()         { _resolve_path log_file; }
_get_port()             { _json_get port; }
_get_shutdown_timeout() { _json_get shutdown_timeout; }

_get_command() {
    python3 - "$CONF" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f: d=json.load(f)
    cmd=d.get('service',d).get('command',['python3','scripts/app.py'])
    if not isinstance(cmd,list) or not cmd or not all(isinstance(x,str) for x in cmd): raise ValueError('service.command must be a non-empty string array')
    for arg in cmd: sys.stdout.buffer.write(arg.encode()+b'\\0')
except Exception as e:
    print(f'Native fallback config error: {e}',file=sys.stderr); sys.exit(2)
PY
}

_get_env_exports() {
    python3 - "$CONF" <<'PY'
import json, re, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f: d=json.load(f)
    values=d.get('service',d).get('env',{})
    if not isinstance(values,dict): raise ValueError('service.env must be an object')
    for k,v in values.items():
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k): sys.stdout.buffer.write(f'{k}={v}'.encode()+b'\\0')
except Exception as e:
    print(f'Native fallback config error: {e}',file=sys.stderr); sys.exit(2)
PY
}

_process_manager() {
    exec python3 - "$CONF" "$1" <<'PY'
import json, os, signal, subprocess, sys, tempfile, time
from pathlib import Path
try:
    with open(sys.argv[1], encoding='utf-8') as stream:
        document=json.load(stream)
    service=document.get('service',document)
    command=service.get('command',['python3','scripts/app.py'])
    if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command):
        raise ValueError('service.command must be a non-empty string array')
    root=Path(os.environ['SCRIPT_DIR'])
    def resolve(key, default):
        value=Path(service.get(key,default))
        return value if value.is_absolute() else root/value
    pid_file=resolve('pid_file','app.pid')
    cwd=resolve('working_dir','.')
    if not cwd.is_dir(): raise ValueError(f'working_dir does not exist: {cwd}')
    mode=sys.argv[2]
    if mode.startswith('start'):
        try:
            existing=int(pid_file.read_text().strip()); os.kill(existing,0)
        except (OSError,ValueError): existing=0
        if existing:
            print(f'Service is already running (PID: {existing}).'); sys.exit(0)
        pid_file.parent.mkdir(parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(dir=pid_file.parent,prefix=pid_file.name+'.')
        log=None
        try:
            foreground=mode=='start_foreground'
            if not foreground:
                log_path=resolve('log_file','logs/app.log')
                log_path.parent.mkdir(parents=True,exist_ok=True)
                log=open(log_path,'ab',buffering=0)
            child=None
            pending=[]
            def forward(signum,_frame):
                if child is None:
                    pending.append(signum)
                    return
                try: os.killpg(child.pid,signum)
                except ProcessLookupError: pass
            if mode=='start_foreground':
                signal.signal(signal.SIGTERM,forward); signal.signal(signal.SIGINT,forward)
            child=subprocess.Popen(command,cwd=cwd,env=os.environ.copy(),
                stdin=None if foreground else subprocess.DEVNULL,
                stdout=None if foreground else log,
                stderr=None if foreground else subprocess.STDOUT,
                start_new_session=True)
            with os.fdopen(fd,'w',encoding='ascii') as stream: stream.write(f'{child.pid}\\n')
            os.replace(temporary,pid_file)
            for signum in pending: forward(signum,None)
            if not foreground:
                log.close(); time.sleep(.1)
                if child.poll() is not None:
                    pid_file.unlink(missing_ok=True)
                    raise RuntimeError(f'service exited during startup (status {child.returncode})')
                print(f"Service '{service.get('name','app')}' started (PID: {child.pid}).")
                sys.exit(0)
            try: code=child.wait()
            finally: pid_file.unlink(missing_ok=True)
            sys.exit(code if code>=0 else 128-code)
        finally:
            if log and not log.closed: log.close()
            if os.path.exists(temporary): os.unlink(temporary)
    elif mode=='stop':
        try: pid=int(pid_file.read_text().strip()); os.kill(pid,0)
        except (OSError,ValueError):
            pid_file.unlink(missing_ok=True); print('Service is already stopped.'); sys.exit(0)
        try: os.killpg(pid,signal.SIGTERM)
        except ProcessLookupError: pass
        timeout=max(0.0,float(service.get('shutdown_timeout',5)))
        deadline=time.monotonic()+timeout
        def alive():
            try: os.killpg(pid,0); return True
            except ProcessLookupError: return False
        while alive() and time.monotonic()<deadline: time.sleep(min(.1,max(0,deadline-time.monotonic())))
        if alive(): os.killpg(pid,signal.SIGKILL)
        pid_file.unlink(missing_ok=True); print('Service stopped.')
    else: raise ValueError(f'unknown operation: {mode}')
except Exception as error:
    print(f'Native fallback error: {error}',file=sys.stderr); sys.exit(1)
PY
}

_read_pid() {
    local pf="$1"
    [ -f "$pf" ] && cat "$pf" || echo ""
}

_pid_alive() {
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

_run_hooks() {
    local hooks_field="$1" phase="$2" i=0 svc_name hook
    local -a hooks=()
    svc_name="$(_get_service_name)"
    mapfile -d '' -t hooks < <(_json_get_list "$hooks_field") || return 1
    for hook in "${hooks[@]}"; do
        i=$((i+1))
        echo "[${svc_name}] Running ${phase} [${i}]: ${hook}"
        eval "$hook" || { echo "[${svc_name}] Error: ${phase} failed: ${hook}" >&2; return 1; }
    done
}

native_start() {
    local foreground=false
    if [ "${1:-}" = "--foreground" ] || [ "${1:-}" = "-f" ]; then foreground=true; fi
    local assignment manager_pid="" stop_requested=false rc=0
    local -a assignments=()
    mapfile -d '' -t assignments < <(_get_env_exports) || return 1
    for assignment in "${assignments[@]}"; do export "$assignment"; done

    _run_hooks pre_start_hooks pre_start_hook || {
        echo "Aborting start due to pre_start_hook failure." >&2
        return 1
    }

    if $foreground; then
        _graceful_stop() {
            if [ -z "$manager_pid" ]; then stop_requested=true; return; fi
            kill -TERM "$manager_pid" 2>/dev/null || true
            wait "$manager_pid" 2>/dev/null || true
            exit 0
        }
        trap _graceful_stop TERM INT
        if $stop_requested; then _graceful_stop; fi
        export SCRIPT_DIR
        _process_manager start_foreground &
        manager_pid=$!
        if $stop_requested; then _graceful_stop; fi
        wait "$manager_pid" || rc=$?
        trap - TERM INT
        return "$rc"
    else
        export SCRIPT_DIR
        _process_manager start_detached
    fi
}

native_stop() {
    _run_hooks pre_stop_hooks pre_stop_hook || return 1
    export SCRIPT_DIR
    _process_manager stop
}

native_status() {
    local pid_file port svc_name pid port_ok
    pid_file="$(_get_pid_file)"
    port="$(_get_port)"
    svc_name="$(_get_service_name)"
    pid="$(_read_pid "$pid_file")"

    if [ -z "$pid" ] || ! _pid_alive "$pid"; then
        echo "[ ${svc_name} ] Status: STOPPED"; return 1
    fi

    port_ok="N/A"
    if [ -n "$port" ]; then
        if python3 - "$port" <<'PY'
import socket,sys,time
error=None
for attempt in range(5):
    try:
        s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),timeout=1)
        s.close()
        sys.exit(0)
    except OSError as failure:
        error=failure
        if attempt < 4: time.sleep(0.1)
print(f"Port check failed: {error}",file=sys.stderr)
sys.exit(1)
PY
        then
            port_ok="OPEN"
        else
            port_ok="CLOSED"
        fi
    fi
    if [ "$port_ok" = "CLOSED" ]; then
        echo "[ ${svc_name} ] Status: INITIALIZING (PID: ${pid}) | Port ${port}: CLOSED"
        return 1
    fi
    echo "[ ${svc_name} ] Status: RUNNING (PID: ${pid}) | Port ${port}: ${port_ok}"
    return 0
}

native_probe() {
    local svc_name probe_type probe_port probe_path probe_timeout expected_status
    svc_name="$(_get_service_name)"
    probe_type="$(_json_get probe_type)"
    probe_port="$(_json_get probe_port)"
    probe_path="$(_json_get probe_path)"
    probe_timeout="$(_json_get probe_timeout)"
    expected_status="$(_json_get expected_status)"
    [ -z "$expected_status" ] && expected_status=200

    if [ -z "$probe_type" ]; then
        echo "No readiness probe configured."; return 1
    fi

    python3 - "$probe_type" "$probe_port" "$probe_path" "$probe_timeout" "$expected_status" "$svc_name" <<'PY'
import socket, sys, urllib.error, urllib.request
kind, port, path, timeout, expected, name = sys.argv[1:]
try:
    if kind == "tcp":
        with socket.create_connection(("127.0.0.1", int(port)), timeout=float(timeout)): pass
        print(f"[ {name} ] Probe: PASS (TCP {port})")
    elif kind in ("http", "https"):
        request = urllib.request.Request(f"{kind}://127.0.0.1:{port}{path}")
        try: actual = urllib.request.urlopen(request, timeout=float(timeout)).getcode()
        except urllib.error.HTTPError as error: actual = error.code
        if actual != int(expected): raise RuntimeError(f"HTTP {actual}, expected {expected}")
        print(f"[ {name} ] Probe: PASS (HTTP {actual})")
    else:
        raise ValueError(f"unsupported readiness probe type: {kind}")
except Exception as error:
    print(f"[ {name} ] Probe: FAIL ({error})", file=sys.stderr)
    sys.exit(1)
PY
}

native_logs() {
    local log_file lines follow arg
    log_file="$(_get_log_file)"
    lines=20; follow=false
    for arg in "$@"; do
        case "$arg" in
            -f|--follow) follow=true ;;
            [0-9]*)      lines="$arg" ;;
        esac
    done
    if [ ! -f "$log_file" ]; then echo "No log file found at ${log_file}"; return 0; fi
    if $follow; then tail -n "$lines" -f "$log_file"; else tail -n "$lines" "$log_file"; fi
}

native_restart() { native_stop; sleep 1; native_start; }

# -- Dispatch -----------------------------------------------------------------
_validate_config || exit 1
CMD="${1:-}"
shift || true
case "$CMD" in
    start)   native_start "$@" ;;
    stop)    native_stop ;;
    restart) native_restart ;;
    status)  native_status ;;
    probe)   native_probe ;;
    logs)    native_logs "$@" ;;
    "")      show_help; exit 0 ;;
    *)       echo "[Error] Unknown command: ${CMD}" >&2; show_help >&2; exit 1 ;;
esac
"""

SAMPLE_APP_PY_TEMPLATE = """#!/usr/bin/env python3
import http.server
import os
import socketserver
import sys

PORT = int(os.environ.get("PORT", 8080))

class HealthHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"sample-app"}\\n')
        else:
            super().do_GET()

def main():
    print(f"Starting server on port {PORT}...")
    with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
        print(f"Serving HTTP on 0.0.0.0 port {PORT} (http://0.0.0.0:{PORT}/) ...")
        httpd.serve_forever()

if __name__ == "__main__":
    main()
"""


def init_project(
    target_dir: Optional[str | Path] = None,
    service_name: str = "my-service",
    port: int = 8080,
    entry_point: str = "scripts/app.py",
) -> None:
    root = Path(target_dir or Path.cwd()).resolve()
    root.mkdir(parents=True, exist_ok=True)

    configs_dir = root / "configs"
    scripts_dir = root / "scripts"
    logs_dir = root / "logs"

    configs_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write universal config
    cfg_file = configs_dir / "service_control.json"
    if not cfg_file.exists():
        cfg_data = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "service": {
                "name": service_name,
                "command": ["python3", entry_point],
                "working_dir": ".",
                "port": port,
                "pid_file": "app.pid",
                "log_file": "logs/app.log",
                "shutdown_timeout": 5.0,
                "pre_start_hooks": [],
                "post_start_hooks": [],
                "pre_stop_hooks": [],
                "readiness_probe": {
                    "type": "http",
                    "path": "/health",
                    "port": port,
                    "timeout_seconds": 3.0,
                    "expected_status": 200,
                },
                "env": {
                    "PORT": str(port),
                },
            },
        }
        cfg_file.write_text(json.dumps(cfg_data, indent=2), encoding="utf-8")
        print(f"Created: {cfg_file}")

    # 2. Write entrypoint.sh (Tier 1)
    entrypoint_sh = root / "entrypoint.sh"
    if not entrypoint_sh.exists():
        entrypoint_sh.write_text(ENTRYPOINT_SH_TEMPLATE, encoding="utf-8")
        entrypoint_sh.chmod(entrypoint_sh.stat().st_mode | 0o755)
        print(f"Created: {entrypoint_sh}")

    # 3. Write control.sh (Tier 2)
    control_sh = root / "control.sh"
    if not control_sh.exists():
        control_sh.write_text(CONTROL_SH_TEMPLATE, encoding="utf-8")
        control_sh.chmod(control_sh.stat().st_mode | 0o755)
        print(f"Created: {control_sh}")

    # 4. Write sample app if entry_point does not exist
    entry_file = root / entry_point
    if not entry_file.exists():
        entry_file.parent.mkdir(parents=True, exist_ok=True)
        entry_file.write_text(SAMPLE_APP_PY_TEMPLATE, encoding="utf-8")
        entry_file.chmod(entry_file.stat().st_mode | 0o755)
        print(f"Created: {entry_file}")

    print(f"Project initialized successfully at {root} (Two-Tier Architecture)")
