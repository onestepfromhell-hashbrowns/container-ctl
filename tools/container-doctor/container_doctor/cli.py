"""
CLI Interface and Dual-Mode Execution Dispatcher for Container Doctor.
Optimized for low-latency execution (<50ms wall-time) and low-token output.
"""

import os
from pathlib import Path
import sys
import time
from typing import List, Optional, Set

from container_doctor import __version__
from container_doctor.core.models import (
    CheckDomain,
    CheckResult,
    CheckStatus,
    DiagnosticReport,
)


def validate_include_domains(include_str: str) -> Optional[Set[str]]:
    """
    Parse and validate include string against CheckDomain.
    Returns set of domain strings or None if validation fails.
    """
    raw_domains = [d.strip() for d in include_str.split(",") if d.strip()]
    if not raw_domains:
        return None

    valid_values = CheckDomain.all_values()
    for d in raw_domains:
        if d != "all" and d not in valid_values:
            return None

    return set(raw_domains)


def run_host_delegated_check(
    container_name: str,
    strategy: str,
    include_domains: str,
    command: str = "check",
    config_path: Optional[str] = None,
) -> int:
    """
    Host-side delegator:
    1. Executes preliminary `podman inspect` to verify container existence and running state.
    2. Runs in-container doctor via `podman exec`.
    """
    import subprocess

    try:
        inspect_res = subprocess.run(
            ["podman", "inspect", "--format", "{{.State.Running}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if inspect_res.returncode != 0:
            sys.stderr.write(f"[FAIL] Container '{container_name}' not found: {inspect_res.stderr.strip()}\n")
            return 1

        is_running = inspect_res.stdout.strip().lower() == "true"
        if not is_running:
            sys.stderr.write(f"[FAIL] Container '{container_name}' is stopped or not in running state.\n")
            return 1
    except FileNotFoundError:
        sys.stderr.write("[FAIL] Podman CLI not found on host system.\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"[FAIL] Preliminary podman inspect failed for '{container_name}': {e}\n")
        return 1

    # Execute in-container
    cmd = [
        "podman", "exec", "-i", container_name,
        "python3", "-m", "container_doctor", command,
        "--strategy", strategy,
        "--include", include_domains,
    ]
    if config_path:
        cmd.extend(["--config", config_path])

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.stdout:
            sys.stdout.write(res.stdout)
        if res.stderr:
            sys.stderr.write(res.stderr)
        return res.returncode
    except Exception as e:
        sys.stderr.write(f"[FAIL] Error delegating check to container '{container_name}': {e}\n")
        return 1


def run_in_container_check(
    context,
    strategy: str,
    include_domains: Set[str],
    fix_hints: bool = True,
) -> int:
    """
    Execute check locally in current container/host environment.
    """
    from container_doctor.checkers import (
        BoundaryChecker,
        MountChecker,
        NetworkChecker,
        ResourceChecker,
        SshChecker,
    )
    from container_doctor.core.engine import DiagnosticEngine
    from container_doctor.formatters import (
        CompactFormatter,
        DiffFormatter,
        FullFormatter,
        JsonFormatter,
    )

    engine = DiagnosticEngine(context)
    engine.register_checker(CheckDomain.MOUNTS.value, MountChecker())
    engine.register_checker(CheckDomain.PATHS.value, BoundaryChecker())
    engine.register_checker(CheckDomain.NETWORK.value, NetworkChecker())
    engine.register_checker(CheckDomain.RESOURCES.value, ResourceChecker())
    engine.register_checker(CheckDomain.SSH.value, SshChecker())

    report = engine.run(include_domains=include_domains)

    # Format output
    if strategy == "minimal":
        output = CompactFormatter().format(report)
    elif strategy == "diff":
        output = DiffFormatter().format(report, show_fix_hints=fix_hints)
    elif strategy == "json":
        output = JsonFormatter().format(report)
    elif strategy == "full":
        output = FullFormatter().format(report)
    else:
        output = CompactFormatter().format(report)

    print(output)
    return 0 if report.overall_status in (CheckStatus.PASS, CheckStatus.WARN) else 1


def _fast_parse_args(args: List[str]) -> Optional[dict]:
    """
    Fast-path parser for common CLI invocations to bypass argparse import overhead (<0.1ms).
    Returns dict of parsed arguments or None to fall back to argparse.
    """
    if not args:
        return {
            "command": "check",
            "strategy": "minimal",
            "include": "all",
            "target_container": None,
            "mode": "auto",
            "fix_hints": True,
            "config_path": None,
        }

    command = "check"
    idx = 0
    if args[0] in ("check", "reconcile"):
        command = args[0]
        idx = 1
    elif args[0] in ("-h", "--help", "-v", "--version"):
        return None

    strategy = "minimal"
    include = "all"
    target_container = None
    mode = "auto"
    fix_hints = True
    config_path = None

    while idx < len(args):
        arg = args[idx]
        if arg in ("--minimal", "-s=minimal"):
            strategy = "minimal"
            idx += 1
        elif arg in ("--diff", "-s=diff"):
            strategy = "diff"
            idx += 1
        elif arg in ("--json", "-s=json"):
            strategy = "json"
            idx += 1
        elif arg in ("--full", "--verbose", "-s=full"):
            strategy = "full"
            idx += 1
        elif arg in ("-s", "--strategy"):
            if idx + 1 >= len(args):
                return None
            strategy = args[idx + 1]
            idx += 2
        elif arg.startswith("--strategy="):
            strategy = arg.split("=", 1)[1]
            idx += 1
        elif arg == "--include":
            if idx + 1 >= len(args):
                return None
            include = args[idx + 1]
            idx += 2
        elif arg.startswith("--include="):
            include = arg.split("=", 1)[1]
            idx += 1
        elif arg in ("-c", "--container"):
            if idx + 1 >= len(args):
                return None
            target_container = args[idx + 1]
            idx += 2
        elif arg.startswith("--container="):
            target_container = arg.split("=", 1)[1]
            idx += 1
        elif arg in ("-m", "--mode"):
            if idx + 1 >= len(args):
                return None
            mode = args[idx + 1]
            idx += 2
        elif arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
            idx += 1
        elif arg == "--no-fix-hints":
            fix_hints = False
            idx += 1
        elif arg == "--config":
            if idx + 1 >= len(args):
                return None
            config_path = args[idx + 1]
            idx += 2
        elif arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            idx += 1
        else:
            return None  # Unknown argument, fallback to argparse

    return {
        "command": command,
        "strategy": strategy,
        "include": include,
        "target_container": target_container,
        "mode": mode,
        "fix_hints": fix_hints,
        "config_path": config_path,
    }


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="container-doctor",
        description="Container Doctor - Lightweight Environment Reconciliation Plugin\n"
                    "Verifies mounts, sandbox boundaries, dynamic SSH tokens, network, and resource bounds.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Output Strategies & Token Footprint:\n"
               "  minimal : ~15 tokens (Single line status summary, recommended default)\n"
               "  diff    : ~50 tokens (Discrepancies and actionable remedies only)\n"
               "  json    : ~40 tokens (Single-line machine parseable JSON)\n"
               "  full    : ~300 tokens (Detailed hierarchical diagnostic tree)\n"
    )

    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"container-doctor {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 1. check subcommand
    check_parser = subparsers.add_parser(
        "check",
        help="Run reconciliation checks (default command)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_shared_arguments(check_parser)

    # 2. reconcile subcommand
    reconcile_parser = subparsers.add_parser(
        "reconcile",
        help="Reconcile active environment against specific config file or .deploy declarations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_shared_arguments(reconcile_parser)
    reconcile_parser.add_argument(
        "--config",
        dest="config_path",
        help="Path to specific deploy JSON, scope JSON, or manifest file to reconcile against",
    )

    return parser


def _add_shared_arguments(parser) -> None:
    import argparse
    parser.add_argument(
        "-s", "--strategy",
        choices=["minimal", "diff", "json", "full"],
        default="minimal",
        help="Output strategy (default: minimal)",
    )
    parser.add_argument(
        "--minimal", action="store_const", dest="strategy", const="minimal",
        help="Alias for --strategy minimal",
    )
    parser.add_argument(
        "--diff", action="store_const", dest="strategy", const="diff",
        help="Alias for --strategy diff",
    )
    parser.add_argument(
        "--json", action="store_const", dest="strategy", const="json",
        help="Alias for --strategy json",
    )
    parser.add_argument(
        "--full", "--verbose", action="store_const", dest="strategy", const="full",
        help="Alias for --strategy full",
    )
    parser.add_argument(
        "--include",
        default="all",
        help="Comma-separated domains to inspect (choices: all,mounts,paths,network,resources,ssh)",
    )
    parser.add_argument(
        "-c", "--container",
        dest="target_container",
        help="Target container name or ID when inspecting from host",
    )
    parser.add_argument(
        "-m", "--mode",
        choices=["auto", "inside", "host"],
        default="auto",
        help="Execution mode (default: auto)",
    )
    parser.add_argument(
        "--no-fix-hints",
        dest="fix_hints",
        action="store_false",
        default=True,
        help="Suppress actionable remediation commands in diff output",
    )


def main(args: Optional[List[str]] = None) -> int:
    if args is None:
        args = sys.argv[1:]

    parsed_dict = _fast_parse_args(args)
    if parsed_dict is None:
        # Fallback to standard argparse
        if not args or (args and args[0] not in ("check", "reconcile", "-h", "--help", "-v", "--version")):
            args = ["check"] + args
        parser = build_parser()
        parsed = parser.parse_args(args)
        parsed_dict = {
            "command": parsed.command,
            "strategy": parsed.strategy,
            "include": parsed.include,
            "target_container": parsed.target_container,
            "mode": parsed.mode,
            "fix_hints": parsed.fix_hints,
            "config_path": getattr(parsed, "config_path", None),
        }

    cmd = parsed_dict["command"]
    if cmd in ("check", "reconcile"):
        include_str = parsed_dict["include"]
        domains = validate_include_domains(include_str)
        if domains is None:
            sys.stderr.write(
                f"[FAIL] Invalid --include domain: '{include_str}'. "
                f"Allowed domains: all, {', '.join(sorted(CheckDomain.all_values()))}\n"
            )
            return 1

        from container_doctor.core.context import (
            ExecutionContext,
            ExecutionMode,
            detect_runtime_environment,
        )

        mode_str = parsed_dict["mode"]
        mode_val = ExecutionMode(mode_str) if mode_str != "auto" else ExecutionMode.AUTO
        target_container = parsed_dict["target_container"]
        config_path = parsed_dict["config_path"]
        strategy = parsed_dict["strategy"]
        fix_hints = parsed_dict["fix_hints"]

        context = detect_runtime_environment(
            forced_mode=mode_val,
            target_container=target_container,
            config_path=config_path,
            load_declarations=(cmd == "reconcile" or config_path is not None),
        )

        if context.mode == ExecutionMode.HOST and target_container:
            return run_host_delegated_check(
                container_name=target_container,
                strategy=strategy,
                include_domains=include_str,
                command=cmd,
                config_path=config_path,
            )
        else:
            return run_in_container_check(
                context=context,
                strategy=strategy,
                include_domains=domains,
                fix_hints=fix_hints,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
