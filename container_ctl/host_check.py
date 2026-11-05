"""container-ctl host check — 唯讀：對照環境 policy.json 檢查宿主已宣告的 Quadlet 單元。

資料來源：.environments/<env>/{profile,policy}.json（環境層真值）與 profile.host.quadlet_dir 下的 *.container。
強度：policy.default_strength=advisory 時違規為 WARN、退出碼 0；--strict 或 mandatory 時違規退出碼 1。
只讀單元檔與路徑存在性；不改檔案、不連 podman。沒有檢查器的規則回報 skipped。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

from container_ctl.env_overlay import DEFAULT_ENV, environment_dir
from container_ctl import paths

_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """極簡 INI：section -> key -> [values]（鍵可重複，如 Volume=、PublishPort=）。"""
    out: dict[str, dict[str, list[str]]] = {}
    sec = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            sec = m[1]
            out.setdefault(sec, {})
            continue
        if sec and "=" in line:
            k, v = line.split("=", 1)
            out[sec].setdefault(k.strip(), []).append(v.strip())
    return out


def to_bytes(v: str) -> float | None:
    v = v.strip()
    if v == "infinity":
        return float("inf")
    m = re.fullmatch(r"(\d+)([KMGT]?)", v)
    return int(m[1]) * _UNITS[m[2]] if m else None


def _last(sec: dict, key: str) -> str | None:
    vals = sec.get(key)
    return vals[-1] if vals else None


def check_loopback(name: str, unit: dict, params: dict) -> list[str]:
    cont = unit.get("Container", {})
    found = []
    if "host" in cont.get("Network", []) and name not in params.get("allow_network_host_units", []):
        found.append("Network=host")
    for pp in cont.get("PublishPort", []):
        parts = pp.split(":")
        ip = parts[0] if len(parts) >= 3 else ""
        if not ip.startswith("127."):
            found.append(f"PublishPort={pp}（非 loopback）")
    return found


def check_cgroup(name: str, unit: dict, params: dict) -> list[str]:
    """params 值為 None 或缺省＝該項不檢查（由環境宣告決定門檻）。"""
    svc = unit.get("Service", {})
    found = []
    mem_cap = params.get("memory_max")
    if mem_cap is not None:
        mem = _last(svc, "MemoryMax")
        mb = to_bytes(mem) if mem else None
        if mem is None:
            found.append("MemoryMax 未設")
        elif mb is None or mb > (to_bytes(mem_cap) or 0):
            found.append(f"MemoryMax={mem} > {mem_cap}")
    cpu_cap = params.get("cpu_quota")
    if cpu_cap is not None:
        cpu = _last(svc, "CPUQuota")
        if cpu is None:
            found.append("CPUQuota 未設")
        else:
            try:
                if float(cpu.rstrip("%")) > float(str(cpu_cap).rstrip("%")):
                    found.append(f"CPUQuota={cpu} > {cpu_cap}")
            except ValueError:
                found.append(f"CPUQuota={cpu} 無法解析")
    swap_cap = params.get("memory_swap_max")
    if swap_cap is not None:
        swap = _last(svc, "MemorySwapMax")
        sb = to_bytes(swap) if swap else None
        if sb is None or sb > (to_bytes(str(swap_cap)) or 0):
            found.append(f"MemorySwapMax={swap or '未設'} > {swap_cap}")
    return found


def check_fhs(name: str, unit: dict, params: dict) -> list[str]:
    found = []
    prefixes = params.get("forbidden_mount_prefixes", list(paths.FORBIDDEN_MOUNT_PREFIXES))
    for v in unit.get("Container", {}).get("Volume", []):
        src = v.split(":")[0]
        if any(src == p or src.startswith(p.rstrip("/") + "/") for p in prefixes):
            found.append(f"Volume 來源 {src}")
    return found


CHECKERS = {"loopback-publish": check_loopback, "cgroup-guard": check_cgroup, "fhs-paths": check_fhs}


def run_check(env: str | None = None, units_dir: str | None = None) -> dict:
    env = env or DEFAULT_ENV
    base = Path(environment_dir(env))
    profile = json.loads((base / "profile.json").read_text(encoding="utf-8"))
    policy = json.loads((base / "policy.json").read_text(encoding="utf-8"))
    qdir = Path(os.path.expanduser(units_dir or profile.get("host", {}).get("quadlet_dir", "")))
    units = {f.stem: parse_unit(f.read_text(encoding="utf-8")) for f in sorted(qdir.glob("*.container"))}
    results = []
    for rule in policy.get("rules", []):
        fn = CHECKERS.get(rule["id"])
        if fn is None:
            results.append({"rule": rule["id"], "status": "skipped", "findings": {}})
            continue
        params = rule.get("params", {})
        found = {n: r for n, u in units.items() if (r := fn(n, u, params))}
        results.append({"rule": rule["id"], "status": "violation" if found else "ok", "findings": found})
    deferred = policy.get("deferred", [])
    missing = [f"{k}: {v}" for k, v in profile.get("paths", {}).items() if not Path(v).exists()]
    return {"env": env, "strength": policy.get("default_strength", "advisory"),
            "units": len(units), "results": results, "profile_findings": missing, "deferred": deferred}


def render(report: dict, verbose: bool = False) -> str:
    lines = [f"[host check] env={report['env']} strength={report['strength']} units={report['units']}"]
    tags = {"ok": "[OK]  ", "skipped": "[SKIP]", "violation": "[WARN]"}
    for r in report["results"]:
        n = len(r["findings"])
        lines.append(f"{tags[r['status']]} {r['rule']}" + (f"：{n} 個單元" if n else ""))
        shown = list(r["findings"].items())
        for unit, items in shown if verbose else shown[:3]:
            lines.append(f"       {unit}: " + "；".join(items))
        if not verbose and len(shown) > 3:
            lines.append(f"       …其餘 {len(shown) - 3} 個單元（--verbose 全列）")
    for m in report["profile_findings"]:
        lines.append(f"[WARN] profile.paths 不存在 {m}")
    today = date.today().isoformat()
    for d in report.get("deferred", []):
        exp = d.get("expires", "")
        tag = "[WARN] 暫緩已過期" if exp < today else "[NOTE] 暫緩中"
        lines.append(f"{tag} {d.get('id')}（效期 {exp}）：{d.get('note', '')}")
    return "\n".join(lines)


def host_check(env: str | None, units_dir: str | None, output: str, strict: bool, verbose: bool = False) -> int:
    try:
        report = run_check(env, units_dir)
    except (OSError, ValueError, KeyError) as e:
        print(f"[ERROR] host check：無法載入環境檔 — {e}")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if output == "json" else render(report, verbose))
    bad = any(r["status"] == "violation" for r in report["results"]) or bool(report["profile_findings"])
    return 1 if bad and (strict or report["strength"] == "mandatory") else 0
