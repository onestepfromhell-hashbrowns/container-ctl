#!/usr/bin/env python3
"""
TICKET-NETGATE: Quadlet Network Tier Conformance Scanner
掃描 .services/ 與 ~/.config/containers/systemd 中的 Quadlet 配置，
檢查容器的 Tier (dev/prod) 與所綁定的 Bridge 網路命名是否一致。
依據原則：僅輸出警告訊息 (Non-blocking Warning)，不阻斷流程。
"""
import argparse
import re
import sys
from pathlib import Path
from container_ctl import paths

DEFAULT_QUADLET_DIR = Path.home() / ".config/containers/systemd"
DEFAULT_SERVICES_DIR = Path(paths.SERVICES_DIR)


def parse_quadlet_file(path: Path) -> dict:
    content = path.read_text(encoding="utf-8", errors="ignore")
    info = {"path": str(path), "labels": {}, "networks": [], "container_name": ""}
    
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith("ContainerName="):
            info["container_name"] = line.split("=", 1)[1].strip()
        elif line.startswith("Network="):
            net = line.split("=", 1)[1].strip()
            if net:
                info["networks"].append(net)
        elif line.startswith("Label="):
            label_part = line.split("=", 1)[1].strip()
            if "=" in label_part:
                k, v = label_part.split("=", 1)
                info["labels"][k.strip()] = v.strip()
    return info


def scan_network_tier_conformance(target_dirs: list[Path]) -> list[str]:
    warnings = []
    
    # 收集所有的 .network 檔案及其 NetworkName
    network_names = {}
    for d in target_dirs:
        if not d.exists():
            continue
        for net_file in d.glob("**/*.network"):
            content = net_file.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                if line.strip().startswith("NetworkName="):
                    network_names[net_file.name] = line.split("=", 1)[1].strip()

    # 掃描所有 .container 檔案
    for d in target_dirs:
        if not d.exists():
            continue
        for ctn_file in d.glob("**/*.container"):
            info = parse_quadlet_file(ctn_file)
            cname = info["container_name"] or ctn_file.stem
            tier = info["labels"].get("ingress.tier", "")
            
            # 推導 Tier
            if not tier:
                if "dev" in cname.lower():
                    tier = "dev"
                elif "prod" in cname.lower():
                    tier = "prod"
            
            for net_ref in info["networks"]:
                actual_net_name = network_names.get(net_ref, net_ref)
                
                # 排除 common / inbound / infra 共享網段
                if any(k in actual_net_name.lower() for k in ["cloudflared", "infra", "podman", "host"]):
                    continue
                if "relay" in cname.lower() and "relay" in actual_net_name.lower():
                    continue

                if tier == "dev" and actual_net_name.startswith("net-prod-"):
                    warnings.append(
                        f"[WARN] 網路分軌邊界提示: Dev 容器 '{cname}' (檔案: {ctn_file.name}) 綁定了 Prod 網段 '{actual_net_name}'。建議修正為 'net-dev-...'。"
                    )
                elif tier == "prod" and actual_net_name.startswith("net-dev-"):
                    warnings.append(
                        f"[WARN] 網路分軌邊界提示: Prod 容器 '{cname}' (檔案: {ctn_file.name}) 綁定了 Dev 網段 '{actual_net_name}'。建議修正為 'net-prod-...'。"
                    )

    return warnings


def main():
    parser = argparse.ArgumentParser(description="Scan Quadlet Network Tier Conformance")
    parser.add_argument(
        "--dirs",
        nargs="+",
        default=[str(DEFAULT_QUADLET_DIR), str(DEFAULT_SERVICES_DIR)],
        help="掃描目錄清單",
    )
    args = parser.parse_args()
    
    target_paths = [Path(p) for p in args.dirs]
    warnings = scan_network_tier_conformance(target_paths)
    
    if warnings:
        print(f"=== 網路分軌靜態門禁檢查：發現 {len(warnings)} 項潛在邊界提示 (不阻斷) ===")
        for w in warnings:
            print(w)
    else:
        print("[OK]   網路分軌邊界檢查通過 (No Tier Mismatch Found)")
    
    # 始終回傳 0 (Non-blocking)
    sys.exit(0)


if __name__ == "__main__":
    main()
