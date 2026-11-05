#!/usr/bin/env python3
"""
CLI Entrypoint for container-control utility.
"""

import argparse
import json
import os
from pathlib import Path
import sys

# Ensure package root is in sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
UTILS_DIR = SCRIPT_DIR.parent
if str(UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(UTILS_DIR))

try:
    from runtime_prelude import bootstrap_runtime_defense
    bootstrap_runtime_defense()
except ImportError:
    for key, val in {
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "CI": "true",
        "DEBIAN_FRONTEND": "noninteractive",
    }.items():
        os.environ.setdefault(key, val)

from container_control.controller import ServiceController
from container_control.scaffold import init_project


def main():
    parser = argparse.ArgumentParser(
        prog="container-control",
        description="Universal In-Container Service Control & Process Manager",
    )
    parser.add_argument("-c", "--config", help="Path to service configuration JSON")
    parser.add_argument("-d", "--dir", help="Project root directory (default: current working directory)")

    subparsers = parser.add_subparsers(dest="command")

    # status
    p_status = subparsers.add_parser("status", help="Show service status dashboard (Triple-Verification)")
    p_status.add_argument("--json", action="store_true", help="Output status in JSON format")

    # start
    p_start = subparsers.add_parser("start", help="Start service process")
    p_start.add_argument("-f", "--foreground", action="store_true", help="Run in foreground (PID 1 compliant with signal traps)")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop running service")
    p_stop.add_argument("-t", "--timeout", type=float, default=None, help="Graceful shutdown timeout in seconds")

    # restart
    subparsers.add_parser("restart", help="Restart service")

    # probe
    p_probe = subparsers.add_parser("probe", help="Execute readiness probe and report health")
    p_probe.add_argument("--json", action="store_true", help="Output probe result in JSON format")

    # logs
    p_logs = subparsers.add_parser("logs", help="Display service logs")
    p_logs.add_argument("-n", "--lines", type=int, default=20, help="Number of trailing lines to show")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output continuously")

    # init
    p_init = subparsers.add_parser("init", help="Scaffold service control into a project directory")
    p_init.add_argument("target_dir", nargs="?", default=".", help="Target project directory")
    p_init.add_argument("--name", default="app", help="Service name")
    p_init.add_argument("--port", type=int, default=8080, help="Listening port")
    p_init.add_argument("--entry", default="scripts/app.py", help="Service entry point file")

    args = parser.parse_args()

    if not args.command or args.command == "status":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        if getattr(args, "json", False):
            print(json.dumps(controller.get_status(), indent=2))
        else:
            controller.print_dashboard()
    elif args.command == "start":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        success = controller.start(foreground=getattr(args, "foreground", False))
        sys.exit(0 if success else 1)
    elif args.command == "stop":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        success = controller.stop(timeout=args.timeout)
        sys.exit(0 if success else 1)
    elif args.command == "restart":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        success = controller.restart()
        sys.exit(0 if success else 1)
    elif args.command == "probe":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        is_ok, detail = controller.probe()
        if getattr(args, "json", False):
            print(json.dumps({"healthy": is_ok, "detail": detail}, indent=2))
        else:
            print(f"Readiness Probe: [{'HEALTHY' if is_ok else 'FAILED'}] {detail}")
        sys.exit(0 if is_ok else 1)
    elif args.command == "logs":
        controller = ServiceController(config_path=args.config, base_dir=args.dir)
        controller.logs(lines=args.lines, follow=args.follow)
    elif args.command == "init":
        init_project(
            target_dir=args.target_dir,
            service_name=args.name,
            port=args.port,
            entry_point=args.entry,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
