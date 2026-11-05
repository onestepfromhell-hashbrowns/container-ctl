"""
container_params.cli — 通用 FHS 容器標準參數與配置檢查 CLI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checker import AntiPatternChecker, check_codebase
from .config import ConfigHierarchy
from .paths import FHSPathResolver, resolve_app_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="container-params",
        description="FHS 容器標準參數、配置階層與 5 大反模式檢查工具",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. check 子命令
    check_parser = subparsers.add_parser("check", help="掃描代碼庫中的 5 大架構反模式")
    check_parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="掃描根目錄 (預設為當前目錄)",
    )
    check_parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="專案名稱/標識",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出結果",
    )

    # 2. resolve 子命令
    resolve_parser = subparsers.add_parser("resolve", help="解析並顯示當前環境的 FHS 6 大特規路徑")
    resolve_parser.add_argument(
        "--project",
        type=str,
        default="myapp",
        help="專案名稱 (預設: myapp)",
    )
    resolve_parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="環境變數前綴 (預設: APP)",
    )
    resolve_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出路徑",
    )

    # 3. eval-config 子命令
    config_parser = subparsers.add_parser("eval-config", help="評估 5 層配置覆蓋結果與來源追溯")
    config_parser.add_argument(
        "--project",
        type=str,
        default="myapp",
        help="專案名稱",
    )
    config_parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式輸出配置與來源追溯",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "check":
        root = args.root.resolve()
        violations = check_codebase(root_dir=root, project_slug=args.project)
        if args.json:
            out = {
                "root": str(root),
                "total_violations": len(violations),
                "violations": [v.to_dict() for v in violations],
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            if not violations:
                print(f"✅ 掃描通過：在 {root} 未發現任何 FHS 反模式違規。")
            else:
                print(f"❌ 發現 {len(violations)} 項架構反模式違規：\n")
                for v in violations:
                    print(f"  [{v.pattern_type.value}] {v.file_path}:{v.line_number}")
                    print(f"    行內容: {v.line_content.strip()}")
                    print(f"    說明:   {v.message}\n")
        return 1 if violations else 0

    elif args.subcommand == "resolve":
        paths = resolve_app_paths(project_name=args.project, prefix=args.prefix)
        p_dict = paths.as_dict()
        if args.json:
            print(json.dumps(p_dict, ensure_ascii=False, indent=2))
        else:
            print(f"=== FHS 6 大特規路徑解析 ({args.project}) ===")
            for k, v in p_dict.items():
                print(f"  {k:<24} = {v}")
            print("\n  [可寫目錄 / Volume]:", [str(p) for p in paths.get_writable_directories()])
            print("  [唯讀目錄 / Rootfs]:", [str(p) for p in paths.get_read_only_directories()])
        return 0

    elif args.subcommand == "eval-config":
        paths = resolve_app_paths(project_name=args.project)
        hierarchy = ConfigHierarchy(project_name=args.project, paths=paths)
        layered = hierarchy.evaluate()
        if args.json:
            print(json.dumps(layered.get_provenance(), ensure_ascii=False, indent=2))
        else:
            print(f"=== 5 層配置覆蓋階層評估結果 ({args.project}) ===")
            prov = layered.get_provenance()
            for k, info in prov.items():
                print(f"  {k} = {info['value']} (Layer {info['layer_level']}: {info['layer_name']}, 來源: {info['source']})")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
