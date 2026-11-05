"""redeploy 前的宿主埠衝突偵測（唯讀）：新單元要發布的 ip:port 已被其他運行中容器佔用 → 新單元會 address already in use。"""

from __future__ import annotations

import json
import subprocess

_WILD = ("", "0.0.0.0", "::")


def declared_binds(ports) -> list[tuple[str, int, str]]:
    """`[ip:][host:]container[/proto]` → (ip, host_port, proto)；省略 host 埠（隨機）或為範圍者略過。"""
    out = []
    for port in ports or []:
        body, _, proto = str(port).partition("/")
        parts = body.split(":")
        if len(parts) < 2 or "-" in parts[-2]:
            continue
        ip = ":".join(parts[:-2]) if len(parts) > 2 else ""
        if parts[-2].isdigit():
            out.append((ip, int(parts[-2]), proto or "tcp"))
    return out


def running_binds(ps_json: list[dict]) -> list[tuple[str, int, str, str]]:
    out = []
    for c in ps_json or []:
        name = (c.get("Names") or ["?"])[0] if isinstance(c.get("Names"), list) else str(c.get("Names"))
        for p in c.get("Ports") or []:
            if not p.get("host_port"):
                continue
            span = int(p.get("range") or 1)
            for hp in range(int(p["host_port"]), int(p["host_port"]) + span):
                out.append((p.get("host_ip", ""), hp, p.get("protocol", "tcp"), name))
    return out


def find_port_conflicts(ports, own_names: set[str], ps_json: list[dict]) -> list[str]:
    taken = running_binds(ps_json)
    found = []
    for ip, port, proto in declared_binds(ports):
        for t_ip, t_port, t_proto, holder in taken:
            if t_port == port and t_proto == proto and holder not in own_names and (ip in _WILD or t_ip in _WILD or ip == t_ip):
                found.append(f"{ip or '*'}:{port}/{proto} 已被運行中的 {holder} 佔用")
    return found


def podman_ps() -> list[dict]:
    r = subprocess.run(["podman", "ps", "--format", "json"], capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except ValueError:
        return []


def needs_stop_first(old_name: str | None, new_name: str, cfg: dict) -> bool:
    """舊實例名稱與新單元不同、且新單元宣告固定宿主埠 → 新舊會搶埠，須先停舊再啟新（明示停機，ADR-009 P5）。"""
    return bool(old_name) and old_name != new_name and bool(declared_binds(cfg.get("ports")))


def precheck_own_names(old_name: str | None, new_name: str, cfg: dict) -> tuple[set[str], str | None]:
    """預檢時不算衝突的單元名：新單元自己，以及「redeploy 會先停掉的本服務舊單元」（needs_stop_first）。
    回傳 (own, 停機提示或 None)；提示供預覽明示。其他服務／對不上名稱的佔用者仍算衝突。"""
    own = {new_name}
    if needs_stop_first(old_name, new_name, cfg):
        own.add(str(old_name))
        return own, f"redeploy 會先停舊單元 {old_name}（固定宿主埠，新舊不可並存；此期間服務中斷）"
    return own, None
