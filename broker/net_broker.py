#!/usr/bin/env python3
"""
TICKET-015: Dynamic Network Broker
訂閱 Podman 網路事件，偵測 net-prod-<project> Bridge 建立，
廣播讓被動容器（如 shared Relay）串流加入 Bridge。

執行方式：
  python3 net_broker.py [--config /path/to/config.json]

日誌：透過 systemd-cat 輸出至 journald（由 systemd unit 包裝）。
"""
import argparse
import fnmatch
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

# ── 日誌設定（stdout → journald via systemd-cat）──────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s [broker-netattach] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("broker-netattach")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run by path: the package root is the sibling of broker/
from container_ctl import paths  # noqa: E402

DEFAULT_CONFIG = Path(paths.BROKER_CONFIG)


def load_config(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        log.warning("找不到配置檔：%s，使用空配置", config_path)
        return {"passive_attach": [], "network_prefix": "net-prod-"}
    return json.loads(p.read_text(encoding="utf-8"))


def get_running_containers() -> set[str]:
    """取得目前所有 Running 容器名稱（for reconcile）。"""
    result = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def get_container_networks(container: str) -> set[str]:
    """取得容器已連接的網路清單。"""
    result = subprocess.run(
        ["podman", "inspect", "--format", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}", container],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def attach_container_to_network(container: str, network: str) -> bool:
    """執行 podman network connect，回傳是否成功。"""
    result = subprocess.run(
        ["podman", "network", "connect", network, container],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("已將 %s 加入 %s", container, network)
        return True
    else:
        err = result.stderr.strip()
        if "already connected" in err.lower():
            log.debug("%s 已在 %s 中（跳過）", container, network)
            return True
        log.warning("無法將 %s 加入 %s：%s", container, network, err)
        return False


def detach_container_from_network(container: str, network: str) -> bool:
    """執行 podman network disconnect，回傳是否成功。"""
    result = subprocess.run(
        ["podman", "network", "disconnect", network, container],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        log.info("已將 %s 退出 %s", container, network)
        return True
    else:
        err = result.stderr.strip()
        log.warning("無法將 %s 退出 %s：%s", container, network, err)
        return False


def resolve_passive_targets(patterns: list[str], running: set[str]) -> set[str]:
    """根據 passive patterns (支援萬用字元如 svc-dev-relay-server-*) 解析出目前運行的容器清單。"""
    resolved = set()
    for pattern in patterns:
        if "*" in pattern or "?" in pattern:
            matched = {c for c in running if fnmatch.fnmatch(c, pattern)}
            resolved.update(matched)
        elif pattern in running:
            resolved.add(pattern)
    return resolved


def get_configured_prefixes(config: dict) -> tuple[str, ...]:
    """取得允許自動附加的網路前綴元組。"""
    raw_prefix = config.get("network_prefixes") or config.get("network_prefix", "net-prod-")
    if isinstance(raw_prefix, list):
        return tuple(raw_prefix)
    return (str(raw_prefix),)


def get_rule_prefixes(rule: dict, global_prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """取得特定規則允許的網路前綴。"""
    raw = rule.get("network_prefixes") or rule.get("network_prefix")
    if raw is not None:
        if isinstance(raw, list):
            return tuple(raw)
        return (str(raw),)
    return global_prefixes


def reconcile(config: dict) -> None:
    """
    Broker 重啟後重新掃描，修復遺漏的 attach 並清理跨 tier 違規連線。
    對每個被動容器規則，檢查匹配其指定前綴的網路並補接，同時斷開不匹配的受管前綴網段。
    """
    global_prefixes = get_configured_prefixes(config)
    passive_rules = config.get("passive_attach", [])

    if not passive_rules:
        return

    # 取得所有網路
    result = subprocess.run(
        ["podman", "network", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning("無法列出網路：%s", result.stderr.strip())
        return

    all_networks = [n.strip() for n in result.stdout.splitlines() if n.strip()]
    running = get_running_containers()

    for rule in passive_rules:
        target_pattern = rule.get("container")
        if not target_pattern:
            continue
        rule_prefixes = get_rule_prefixes(rule, global_prefixes)
        targets = resolve_passive_targets([target_pattern], running)
        allowed_networks = [n for n in all_networks if n.startswith(rule_prefixes)]

        fixed_nets = rule.get("fixed_networks", [])

        for container in targets:
            current_nets = get_container_networks(container)
            
            # 1. 確保固定網段 (如 net-cloudflared) 已連接
            for fnet in fixed_nets:
                if fnet not in current_nets and fnet in all_networks:
                    log.info("[reconcile] 連接固定網段：%s → %s", container, fnet)
                    attach_container_to_network(container, fnet)

            # 2. 補接匹配該規則前綴的網路
            for network in allowed_networks:
                if network not in current_nets:
                    log.info("[reconcile] 修復遺漏：%s → %s", container, network)
                    attach_container_to_network(container, network)
            
            # 3. 清理受管但超出該規則允許前綴且非固定網段的專案網路
            for net in current_nets:
                if net in fixed_nets:
                    continue
                if net.startswith(global_prefixes) and not net.startswith(rule_prefixes):
                    log.info("[reconcile] 隔離邊界清理：%s 退出 %s", container, net)
                    detach_container_from_network(container, net)


def subscribe_events(config: dict) -> None:
    """
    訂閱 Podman 事件，偵測 network 建立與 container 啟動並觸發 attach / reconcile。
    阻塞執行（由 systemd 管理重啟）。
    """
    global_prefixes = get_configured_prefixes(config)
    passive_rules = config.get("passive_attach", [])

    log.info("啟動 Network Broker，全域前綴：%s...", global_prefixes)

    proc = subprocess.Popen(
        [
            "podman",
            "events",
            "--format",
            "json",
            "--filter",
            "type=network",
            "--filter",
            "type=container",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = (event.get("Type") or event.get("type") or "").lower()
            action = (event.get("Status") or event.get("Action") or event.get("status") or event.get("event") or "").lower()
            actor = event.get("Actor") or {}
            if not isinstance(actor, dict):
                actor = {}
            attributes = actor.get("Attributes") or event.get("Attributes") or {}
            if not isinstance(attributes, dict):
                attributes = {}
            name = event.get("Name") or attributes.get("name", "")

            # 1. 處理 network create 事件
            if event_type == "network" and action == "create":
                if not name.startswith(global_prefixes):
                    continue

                log.info("偵測到新 Bridge 網路：%s", name)
                running = get_running_containers()

                for rule in passive_rules:
                    target_pattern = rule.get("container")
                    if not target_pattern:
                        continue
                    rule_prefixes = get_rule_prefixes(rule, global_prefixes)
                    if name.startswith(rule_prefixes):
                        targets = resolve_passive_targets([target_pattern], running)
                        for container in targets:
                            attach_container_to_network(container, name)

            # 2. 處理 container start / init 事件（消除 Boot Race，被動容器或業務容器上線即補接）
            elif event_type == "container" and action in ("start", "init"):
                # 檢查是否為被動容器或具備前綴網段的容器
                is_passive_target = False
                for rule in passive_rules:
                    pattern = rule.get("container", "")
                    if pattern and (fnmatch.fnmatch(name, pattern) or name == pattern):
                        is_passive_target = True
                        break

                if is_passive_target:
                    log.info("偵測到受管被動服務容器啟動：%s，執行自動補掛接 (reconcile)...", name)
                    reconcile(config)
                elif name.startswith("svc-") or any(p in name for p in ("dev", "prod")):
                    # 業務容器啟動，確保所有被動服務皆已接入其所屬網段
                    log.debug("偵測到業務容器啟動：%s，執行 reconcile 檢查...", name)
                    reconcile(config)

    except KeyboardInterrupt:
        log.info("Network Broker 收到中斷信號，正在退出...")
    finally:
        proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dynamic Network Broker")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置檔路徑（預設：%(default)s）",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # 啟動時先 reconcile（修復重啟前遺漏的 attach）
    log.info("執行 startup reconcile...")
    reconcile(config)

    # 訂閱事件（阻塞）
    subscribe_events(config)


if __name__ == "__main__":
    main()
