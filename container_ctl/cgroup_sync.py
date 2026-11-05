"""
cgroup_sync.py — 容器 Cgroup 資源限制（MemoryMax, TasksMax）全域同步與治理模組

依據系統最新容器設計規範：
- dev-tier: MemoryMax=2G, TasksMax=2048
- prod-tier: MemoryMax=1G
- infra-tier: MemoryMax=1G
"""
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple

from container_ctl.lifecycle_adapter import get_default_manager
from container_ctl import paths

logger = logging.getLogger("container_ctl.cgroup_sync")

DEFAULT_SERVICES_DIR = paths.SERVICES_DIR
DEFAULT_SYSTEMD_QUADLET_DIR = Path.home() / ".config/containers/systemd"

# 排序重啟優先級：資料庫優先，業務服務次之（未列出者＝2），基礎設施入口最後
SERVICE_RESTART_PRIORITY = {
    "postgres": 1,
    "db": 1,
    "redis": 1,
    "monitor": 3,
    "cloudflared": 4,
    "nginx": 5,
}


class SyncResult(NamedTuple):
    file_path: Path
    container_name: str
    tier: str
    modified: bool
    memory_max: str
    tasks_max: str | None
    swap_max: str | None
    stable_id: str = ""


def detect_tier_from_content(content: str, file_path: Path) -> str:
    """依據檔案內容與路徑探測容器所屬 tier (dev, prod, infra, rescue)。"""
    # 1. 檢查 ingress.tier 或 <LABEL_NS>.tier 等標籤
    m_tier = re.search(r"^\s*Label\s*=.*?(?:ingress\.tier|tier)\s*=\s*([a-zA-Z0-9_-]+)", content, re.MULTILINE)
    if m_tier:
        val = m_tier.group(1).lower()
        if val in ("dev", "prod", "infra", "rescue"):
            return val

    # 2. 檢查 platform.tier 或 platform.domain
    if "platform.domain=infrastructure" in content or "/infra/" in str(file_path):
        return "infra"

    # 3. 檢查 Description 中的 tier 標註
    m_desc = re.search(r"Description=.*?\((dev|prod|infra|rescue)\)", content, re.IGNORECASE)
    if m_desc:
        return m_desc.group(1).lower()

    # 4. 檔名或路徑特徵（受控 legacy 單元，嚴格限定精確檔名防範寬鬆放寬）
    fname = file_path.name.lower()
    if file_path.name in ("svc-sshd.container", "sshd.container"):
        return "rescue"
    elif "svc-dev-" in fname or "-dev" in fname:
        return "dev"
    elif "svc-prod-" in fname or "-prod" in fname:
        return "prod"
    elif "cloudflared" in fname or "nginx" in fname or "monitor" in fname or "infra" in fname:
        return "infra"

    # 預設保護原則：回退至 dev (嚴格限制 2G)
    return "dev"


def _append_missing_directives(
    new_lines: list[str],
    target_slice: str | None, has_slice: bool,
    target_guard: str | None, has_guard: bool,
    target_mem: str, has_mem: bool,
    target_high: str | None, has_high: bool,
    target_swap: str | None, has_swap: bool,
    target_tasks: str | None, has_tasks: bool,
) -> None:
    if target_slice and not has_slice:
        new_lines.append(f"Slice={target_slice}")
    if target_guard and not has_guard:
        new_lines.append(target_guard)
    if target_high and not has_high:
        new_lines.append(f"MemoryHigh={target_high}")
    if not has_mem:
        new_lines.append(f"MemoryMax={target_mem}")
    if target_swap and not has_swap:
        new_lines.append(f"MemorySwapMax={target_swap}")
    if target_tasks and not has_tasks:
        new_lines.append(f"TasksMax={target_tasks}")


def apply_cgroup_limits_to_text(
    content: str,
    tier: str,
    policy: any = None,
    stable_id: str | None = None,
    governance_mapping: any = None,
    start_check_bin: str = "/usr/local/bin/memory-ctl",
) -> tuple[str, bool, str, str | None, str | None]:
    """
    分析 Quadlet 文本，補齊 [Service] 區段的 Slice, MemoryMax, MemoryHigh, TasksMax 與 ExecCondition。
    遵循 C04 / C07，與 Quadlet Generator 輸出完全對齊，支援冪等重跑不重複 directive。
    回傳: (updated_text, was_modified, applied_mem, applied_tasks, applied_swap)
    """
    from container_ctl.governance_compiler import compile_governance

    # 1. 探測 stable_id
    if not stable_id:
        m_id = re.search(rf"^\s*Label\s*=\s*(?:{re.escape(paths.LABEL_NS)}\.)?governance\.id\s*=\s*([a-zA-Z0-9_-]+)", content, re.MULTILINE)
        if m_id:
            stable_id = m_id.group(1).strip()
        else:
            m_cname = re.search(r"^\s*ContainerName\s*=\s*(?:svc-)?([a-zA-Z0-9_.-]+)", content, re.MULTILINE)
            if m_cname:
                stable_id = m_cname.group(1).strip()

    # 2. 解析 governance_mapping（若提供 policy 則強制精確編譯，編譯失敗 fail-closed 拋出異常）
    gov_map = governance_mapping
    if gov_map is None and policy is not None:
        manifest = compile_governance(policy, start_check_bin=start_check_bin)
        if stable_id:
            gov_map = manifest.workloads.get(stable_id)

    # 3. 確定目標規範
    target_slice: str | None = None
    target_guard: str | None = None
    target_high: str | None = None
    target_swap: str | None = None

    if gov_map:
        target_slice = gov_map.slice
        target_guard = gov_map.guard
        target_mem = str(gov_map.limits.get("memory_max") or ("2G" if tier == "dev" else "1G"))
        target_high = str(gov_map.limits["memory_high"]) if gov_map.limits.get("memory_high") is not None else None
        gov_swap = gov_map.limits.get("swap_max")
        if gov_swap is not None:
            target_swap = str(gov_swap)
        elif tier == "rescue":
            target_swap = None
        else:
            target_swap = "0"
        target_tasks = str(gov_map.limits["tasks_max"]) if gov_map.limits.get("tasks_max") is not None else ("2048" if tier == "dev" else None)
    else:
        # Fallback to defaults
        target_mem = "2G" if tier == "dev" else "1G"
        target_tasks = "2048" if tier == "dev" else None
        target_swap = None if tier == "rescue" else "0"   # INV-04
        if policy is not None and tier != "rescue":
            target_slice = f"workload-{tier}.slice"
            if stable_id:
                target_guard = f"ExecCondition={start_check_bin} start-check {stable_id}"

    # 4. 分析當前內容
    has_service = "[Service]" in content
    has_slice = bool(re.search(r"^\s*Slice\s*=", content, re.MULTILINE))
    cur_slice_m = re.search(r"^\s*Slice\s*=\s*(\S+)", content, re.MULTILINE)
    cur_slice = cur_slice_m.group(1) if cur_slice_m else None

    has_guard = bool(re.search(r"^\s*ExecCondition\s*=.*start-check", content, re.MULTILINE))
    cur_guard_m = re.search(r"^\s*(ExecCondition\s*=.*start-check\S*)", content, re.MULTILINE)
    cur_guard = cur_guard_m.group(1) if cur_guard_m else None

    has_mem = bool(re.search(r"^\s*MemoryMax\s*=", content, re.MULTILINE))
    cur_mem_m = re.search(r"^\s*MemoryMax\s*=\s*(\S+)", content, re.MULTILINE)
    cur_mem = cur_mem_m.group(1) if cur_mem_m else target_mem

    has_high = bool(re.search(r"^\s*MemoryHigh\s*=", content, re.MULTILINE))
    cur_high_m = re.search(r"^\s*MemoryHigh\s*=\s*(\S+)", content, re.MULTILINE)
    cur_high = cur_high_m.group(1) if cur_high_m else None

    has_swap = bool(re.search(r"^\s*MemorySwapMax\s*=", content, re.MULTILINE))
    cur_swap_m = re.search(r"^\s*MemorySwapMax\s*=\s*(\S+)", content, re.MULTILINE)
    cur_swap = cur_swap_m.group(1) if cur_swap_m else None

    has_tasks = bool(re.search(r"^\s*TasksMax\s*=", content, re.MULTILINE))
    cur_tasks_m = re.search(r"^\s*TasksMax\s*=\s*(\S+)", content, re.MULTILINE)
    cur_tasks = cur_tasks_m.group(1) if cur_tasks_m else None

    # 若 dev 容器存在舊版 TasksMax=128，需升級為 2048
    needs_tasks_upgrade = (tier == "dev" and cur_tasks == "128")

    # 檢查是否已完全符合（冪等性判定）
    slice_compliant = (target_slice is None) or (has_slice and cur_slice == target_slice)
    guard_compliant = (target_guard is None) or (has_guard and (target_guard in content or cur_guard == target_guard))
    swap_compliant = (target_swap is None) or (has_swap and cur_swap == target_swap)
    if policy is None:
        mem_compliant = has_mem
        high_compliant = True
        tasks_compliant = (tier != "dev") or (has_tasks and not needs_tasks_upgrade)
    else:
        mem_compliant = has_mem and (cur_mem == target_mem)
        high_compliant = (target_high is None) or (has_high and cur_high == target_high)
        tasks_compliant = not needs_tasks_upgrade and ((target_tasks is None) or (has_tasks and cur_tasks == target_tasks))

    if slice_compliant and guard_compliant and mem_compliant and high_compliant and swap_compliant and tasks_compliant:
        final_swap = cur_swap if has_swap else target_swap
        return content, False, cur_mem or target_mem, cur_tasks, final_swap

    # 需要補齊或修正
    if has_service:
        lines = content.splitlines()
        new_lines = []
        in_service = False
        inserted = False

        for line in lines:
            stripped = line.strip()
            if stripped == "[Service]":
                in_service = True
                new_lines.append(line)
                continue

            if in_service and stripped.startswith("[") and stripped.endswith("]"):
                # 進入下個區段前補齊
                if not inserted:
                    _append_missing_directives(
                        new_lines, target_slice, has_slice,
                        target_guard, has_guard,
                        target_mem, has_mem,
                        target_high, has_high,
                        target_swap, has_swap,
                        target_tasks, has_tasks,
                    )
                    inserted = True
                in_service = False
                new_lines.append(line)
                continue

            if in_service:
                # 替換不符的 directive
                if target_slice and stripped.startswith("Slice="):
                    new_lines.append(f"Slice={target_slice}")
                    continue
                if target_guard and stripped.startswith("ExecCondition=") and "start-check" in stripped:
                    new_lines.append(target_guard)
                    continue
                if target_high and stripped.startswith("MemoryHigh="):
                    new_lines.append(f"MemoryHigh={target_high}")
                    continue
                if target_mem and stripped.startswith("MemoryMax=") and policy is not None:
                    new_lines.append(f"MemoryMax={target_mem}")
                    continue
                if target_swap and stripped.startswith("MemorySwapMax="):
                    new_lines.append(f"MemorySwapMax={target_swap}")
                    continue
                if stripped.startswith("TasksMax="):
                    if needs_tasks_upgrade or (target_tasks and policy is not None):
                        new_lines.append(f"TasksMax={target_tasks}")
                        continue

            new_lines.append(line)

        if in_service and not inserted:
            _append_missing_directives(
                new_lines, target_slice, has_slice,
                target_guard, has_guard,
                target_mem, has_mem,
                target_high, has_high,
                target_swap, has_swap,
                target_tasks, has_tasks,
            )

        trailing_newline = "\n" if content.endswith("\n") else ""
        final_tasks = target_tasks if (target_tasks and (not has_tasks or needs_tasks_upgrade)) else cur_tasks
        final_swap = target_swap if target_swap is not None else cur_swap
        return "\n".join(new_lines) + trailing_newline, True, target_mem, final_tasks, final_swap
    else:
        # 完全無 [Service]，在結尾追加
        additions = [
            "",
            "[Service]",
            "Restart=on-failure",
            "RestartSec=10s",
            "RestartPreventExitStatus=125",
        ]
        if target_slice:
            additions.append(f"Slice={target_slice}")
        if target_guard:
            additions.append(target_guard)
        if target_high:
            additions.append(f"MemoryHigh={target_high}")
        additions.append(f"MemoryMax={target_mem}")
        if target_swap:
            additions.append(f"MemorySwapMax={target_swap}")
        if target_tasks:
            additions.append(f"TasksMax={target_tasks}")

        return content.rstrip() + "\n" + "\n".join(additions) + "\n", True, target_mem, target_tasks, target_swap


def sync_quadlet_files(
    services_dir: str = DEFAULT_SERVICES_DIR,
    quadlet_dir: Path = DEFAULT_SYSTEMD_QUADLET_DIR,
    dry_run: bool = False,
    policy: any = None,
    start_check_bin: str = "/usr/local/bin/memory-ctl",
) -> list[SyncResult]:
    """掃描所有 .container 檔案並確保包含規範之 cgroup 設定與 symlink。"""
    services_path = Path(services_dir)
    results: list[SyncResult] = []

    if not services_path.exists():
        return results

    if not dry_run:
        quadlet_dir.mkdir(parents=True, exist_ok=True)

    gov_manifest = None
    if policy is not None:
        from container_ctl.governance_compiler import compile_governance
        # Fail closed: policy 明確提供時，編譯失敗直接拋出異常，阻斷任何寫入與服務處置
        gov_manifest = compile_governance(policy, start_check_bin=start_check_bin)

    for cfile in sorted(services_path.rglob("*.container")):
        content = cfile.read_text(encoding="utf-8")
        tier = detect_tier_from_content(content, cfile)

        cname_m = re.search(r"^\s*ContainerName\s*=\s*(\S+)", content, re.MULTILINE)
        cname = cname_m.group(1) if cname_m else cfile.stem

        # Resolve stable_id
        m_id = re.search(rf"^\s*Label\s*=\s*(?:{re.escape(paths.LABEL_NS)}\.)?governance\.id\s*=\s*([a-zA-Z0-9_-]+)", content, re.MULTILINE)
        stable_id = m_id.group(1).strip() if m_id else (cname[4:] if cname.startswith("svc-") else cname)

        gov_map = gov_manifest.workloads.get(stable_id) if gov_manifest else None

        updated_text, modified, mem, tasks, swap = apply_cgroup_limits_to_text(
            content,
            tier=tier,
            policy=policy,
            stable_id=stable_id,
            governance_mapping=gov_map,
            start_check_bin=start_check_bin,
        )

        if modified and not dry_run:
            cfile.write_text(updated_text, encoding="utf-8")

        # 確保 symlink 存在且正確指向
        link_target = quadlet_dir / cfile.name
        if not dry_run:
            if link_target.exists() or link_target.is_symlink():
                try:
                    if link_target.resolve() != cfile.resolve():
                        link_target.unlink()
                        link_target.symlink_to(cfile.resolve())
                except Exception:
                    link_target.unlink(missing_ok=True)
                    link_target.symlink_to(cfile.resolve())
            else:
                link_target.symlink_to(cfile.resolve())

        results.append(
            SyncResult(
                file_path=cfile,
                container_name=cname,
                tier=tier,
                modified=modified,
                memory_max=mem,
                tasks_max=tasks,
                swap_max=swap,
                stable_id=stable_id,
            )
        )

    return results


def reload_and_restart_services(
    results: list[SyncResult],
    restart: bool = True,
    ledger: any = None,
) -> list[str]:
    """執行 daemon-reload 並依依賴優先順序重啟已更新且處於 active 的服務（嚴格跳過 paused 目標）。"""
    # 1. 執行 systemctl --user daemon-reload
    get_default_manager().daemon_reload()

    restarted: list[str] = []
    if not restart:
        return restarted

    # 查詢治理暫停狀態 (AC3: cgroup-sync 變更後不能 restart paused 目標)
    from container_ctl.governance_port import paused_stable_ids
    paused_ids: set[str] = paused_stable_ids(ledger)

    # 取得當前所有 active 服務清單
    active_services = set()
    result_by_name = {r.container_name: r for r in results}

    for r in results:
        svc_name = f"{r.container_name}.service"
        # 排除專屬的 WSL Remote Tunnel (絕不重啟)
        if "wsl-remote" in svc_name or svc_name == "cloudflared.service":
            continue
        if get_default_manager().is_active(svc_name, timeout=5.0):
            active_services.add(r.container_name)

    # 排序重啟順序
    def sort_key(name: str) -> int:
        for keyword, prio in SERVICE_RESTART_PRIORITY.items():
            if keyword in name.lower():
                return prio
        return 2

    ordered_to_restart = sorted(active_services, key=sort_key)

    for cname in ordered_to_restart:
        r = result_by_name.get(cname)
        sid = r.stable_id if (r and r.stable_id) else (cname[4:] if cname.startswith("svc-") else cname)
        svc_name = f"{cname}.service"

        if sid in paused_ids or cname in paused_ids:
            print(f"  [SKIP] 服務 {svc_name} (stable_id: {sid}) 處於治理暫停狀態，跳過重啟。", file=sys.stderr)
            continue

        print(f"  [RESTART] 正在重啟 {svc_name} 以套用最新 Cgroup 限制...", file=sys.stderr)
        res = get_default_manager().restart_unit(svc_name, no_block=False)
        if res.job_submitted:
            restarted.append(cname)
        else:
            print(f"  [WARN] 重啟 {svc_name} 失敗: {res.error}", file=sys.stderr)

    return restarted

