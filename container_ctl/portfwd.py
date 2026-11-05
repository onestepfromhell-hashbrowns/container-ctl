"""
host-portfwd：把 dev 容器的 127.0.0.1:<port> 暫時中繼到宿主機網卡，供區網裝置（手機/平板）做實測。

設計約束：
- 不經任何容器，不改 manifest/Quadlet，不碰被轉發的 listener；只在宿主機多開一個 socat 程序。
- 只准轉發 dev 的 127.0.0.1:<port>；prod 的 127.0.0.2 一律拒絕。
- 只綁指定網卡 IP（預設 eth0），禁止 0.0.0.0 / loopback。
- 以 systemd --user 暫時性 unit 執行，RuntimeMaxSec 到期自動關閉。
- 開啟/關閉/到期都寫入 state.db lifecycle_events。
"""
import ipaddress
import json
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from container_ctl import agent_exec
from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl.lifecycle_adapter import get_default_manager
from container_ctl import paths

LISTEN_PORT_RANGE = (9000, 9499)
ALLOWED_TARGET_HOST = "127.0.0.1"
DEFAULT_TTL = "2h"
UNIT_PREFIX = "portfwd-"
SERVICES_DIR = Path(paths.SERVICES_DIR)
_LISTEN_LABEL_RE = re.compile(rf"^\s*Label={re.escape(paths.PORTFWD_LABEL)}=(\d+)\s*$", re.M)
_CONTAINER_NAME_RE = re.compile(r"^\s*ContainerName=(\S+)\s*$", re.M)
CTL_PATH = str(Path(__file__).resolve().parent.parent / "container-ctl")


class PortfwdError(Exception):
    pass


def parse_ttl(text: str) -> int:
    m = re.fullmatch(r"(\d+)([smh])", text.strip())
    if not m or int(m.group(1)) <= 0:
        raise PortfwdError(f"TTL 格式錯誤：{text!r}（例：30m、2h、900s）")
    seconds = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
    if seconds > 24 * 3600:
        raise PortfwdError("TTL 不可超過 24h")
    return seconds


def parse_target(text: str) -> tuple:
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise PortfwdError(f"--to 格式錯誤：{text!r}（例：127.0.0.1:8081）")
    if host != ALLOWED_TARGET_HOST:
        raise PortfwdError(f"只允許轉發 dev 的 {ALLOWED_TARGET_HOST}；拒絕目標 {host}（prod 為 127.0.0.2）")
    return host, int(port)


def validate_listen_port(port: int) -> None:
    lo, hi = LISTEN_PORT_RANGE
    if not lo <= port <= hi:
        raise PortfwdError(f"監聽埠必須在 {lo}-{hi}，收到 {port}")


def validate_bind(addr: str) -> str:
    ip = ipaddress.ip_address(addr)
    if ip.is_unspecified or ip.is_loopback:
        raise PortfwdError(f"--bind 不可為 {addr}（禁止 0.0.0.0 / loopback）；請指定網卡 IP")
    return str(ip)


def validate_allow(cidr: Optional[str]) -> Optional[str]:
    if cidr is None:
        return None
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except ValueError as exc:
        raise PortfwdError(f"--allow 不是合法 CIDR：{cidr!r}") from exc


_REAL_RUN = subprocess.run  # 預設 run 的識別基準（agent 模式只在呼叫端沒注入自訂 run 時接管）


def _port_open(host: str, port: int) -> bool:
    if agent_exec.agent_socket():
        try:
            return agent_exec.port_open(host, port)
        except agent_exec.AgentError as exc:
            raise PortfwdError(str(exc)) from exc
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def scan_declared_ports(services_dir: Optional[Path] = None) -> dict:
    """掃描 .services/*/*.container 的 Label=<LABEL_NS>.portfwd.listen，回傳 {port: [容器名稱, ...]}。"""
    root = Path(services_dir) if services_dir is not None else SERVICES_DIR
    declared: dict = {}
    if not root.exists():
        return declared
    for cfile in sorted(root.glob("*/*.container")):
        try:
            content = cfile.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _LISTEN_LABEL_RE.search(content)
        if not m:
            continue
        name = _CONTAINER_NAME_RE.search(content)
        declared.setdefault(int(m.group(1)), []).append(name.group(1) if name else cfile.stem)
    return declared


def find_listen_conflicts(services_dir: Optional[Path] = None) -> dict:
    """回傳被多個容器重複宣告的埠號 {port: [容器名稱, ...]}。"""
    return {p: n for p, n in scan_declared_ports(services_dir).items() if len(n) > 1}


def get_next_available_port(
    port_range: tuple = LISTEN_PORT_RANGE,
    services_dir: Optional[Path] = None,
    port_open: Callable = _port_open,
) -> int:
    """回傳範圍內第一個「未被宣告且宿主機目前未被占用」的埠號。"""
    lo, hi = port_range
    taken = set(scan_declared_ports(services_dir))
    for port in range(lo, hi + 1):
        if port not in taken and not port_open("127.0.0.1", port):
            return port
    raise PortfwdError(f"{lo}-{hi} 已無可用埠號")


def detect_bind_ip(iface: str = "eth0") -> str:
    if agent_exec.agent_socket():
        try:
            out = agent_exec.addr_show(iface)
        except agent_exec.AgentError as exc:
            raise PortfwdError(str(exc)) from exc
    else:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", iface], capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
    if not m:
        raise PortfwdError(f"找不到網卡 {iface} 的 IPv4；請用 --bind 指定")
    return m.group(1)


def _record(db_path: str, instance_id: str, event_type: str, triggered_by: str, detail: dict) -> None:
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO lifecycle_events (instance_id, event_type, triggered_by, detail, occurred_at) VALUES (?, ?, ?, ?, ?)",
        (instance_id, event_type, triggered_by, json.dumps(detail, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _lookup_instance(db_path: str, target_port: int) -> str:
    conn = init_db(db_path)
    row = conn.execute(
        "SELECT id FROM instances WHERE tier='dev' AND status != 'destroyed' AND blueprint LIKE ? LIMIT 1",
        (f'%:{target_port}:{target_port}"%',),
    ).fetchone()
    conn.close()
    return row["id"] if row else "host"


def resolve_declaration(
    target_ident: Optional[str] = None,
    to: Optional[str] = None,
    listen: Optional[int] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[str, int, str]:
    resolved_to = to
    resolved_listen = listen
    instance_id = target_ident or "host"

    # 1. 優先查核 state.db 實例與 blueprint 宣告
    if Path(db_path).exists():
        try:
            conn = init_db(db_path)
            query = "SELECT id, container_name, blueprint FROM instances WHERE tier='dev' AND status != 'destroyed'"
            params = []
            if target_ident:
                query += " AND (id = ? OR container_name = ? OR service_name = ? OR project_name = ?)"
                params = [target_ident, target_ident, target_ident, target_ident]
            elif to:
                _, t_port = parse_target(to)
                query += " AND blueprint LIKE ?"
                params = [f'%:{t_port}:{t_port}"%']
            row = conn.execute(query + " LIMIT 1", params).fetchone()
            conn.close()
            if row:
                instance_id = row["id"]
                try:
                    bp = json.loads(row["blueprint"])
                    if not resolved_listen:
                        pf = bp.get("portfwd", {})
                        resolved_listen = (
                            pf.get("listen") if isinstance(pf, dict) else None
                        ) or bp.get("portfwd_port") or bp.get("labels", {}).get(f"{paths.LABEL_NS}.portfwd.listen")
                    if not resolved_to:
                        for p in bp.get("ports", []):
                            if p.startswith("127.0.0.1:"):
                                resolved_to = p.rsplit(":", 1)[0]
                                break
                except Exception:
                    pass
        except Exception:
            pass

    # 2. 查核 .services/ Quadlet 容器宣告 (.container)
    services_dir = Path(paths.SERVICES_DIR)
    if (not resolved_listen or not resolved_to) and services_dir.exists():
        for cfile in services_dir.glob("*/*.container"):
            try:
                content = cfile.read_text(encoding="utf-8")
                matched = False
                if target_ident:
                    if (
                        target_ident in cfile.name
                        or f"ContainerName={target_ident}" in content
                        or f"instance {target_ident}" in content
                        or f"Label={paths.LABEL_NS}.governance.id={target_ident}" in content
                    ):
                        matched = True
                elif to:
                    if f"PublishPort={to}:" in content or f"PublishPort={to}" in content:
                        matched = True
                if matched:
                    if not resolved_listen:
                        m_listen = re.search(rf"Label={re.escape(paths.PORTFWD_LABEL)}=(\d+)", content)
                        if m_listen:
                            resolved_listen = int(m_listen.group(1))
                    if not resolved_to:
                        m_port = re.search(r"PublishPort=(127\.0\.0\.1:\d+)", content)
                        if m_port:
                            resolved_to = m_port.group(1)
                    if instance_id == "host":
                        instance_id = cfile.stem.replace("svc-", "")
                    if resolved_listen and resolved_to:
                        break
            except Exception:
                pass

    # 3. 查核 .deploys/ 宣告 JSON
    deploys_dir = Path(paths.DEPLOYS_DIR)
    if (not resolved_listen or not resolved_to) and deploys_dir.exists():
        for jfile in deploys_dir.glob("*/*.json"):
            try:
                data = json.loads(jfile.read_text(encoding="utf-8"))
                for svc in data.get("services", []):
                    cfg = svc.get("config", {})
                    name = cfg.get("name", "")
                    if target_ident and target_ident not in (name, svc.get("name"), data.get("project_name")):
                        continue
                    if not resolved_listen:
                        pf = cfg.get("portfwd", {})
                        resolved_listen = pf.get("listen") if isinstance(pf, dict) else cfg.get("portfwd_port")
                    if not resolved_to:
                        for p in cfg.get("ports", []):
                            if p.startswith("127.0.0.1:"):
                                resolved_to = p.rsplit(":", 1)[0]
                                break
                    if resolved_listen and resolved_to:
                        if instance_id == "host":
                            instance_id = name
                        break
            except Exception:
                pass

    if not resolved_to:
        raise PortfwdError("無法解析目標 dev 容器埠號（請傳入 --to 127.0.0.1:<port> 或指定有效容器實例）")
    if not resolved_listen:
        raise PortfwdError(
            f"容器 {target_ident or resolved_to} 未宣告固定 host portfwd 埠號，請使用 --listen 指定（{LISTEN_PORT_RANGE[0]}-{LISTEN_PORT_RANGE[1]}）或在容器宣告中設定 Label={paths.LABEL_NS}.portfwd.listen=<PORT>"
        )

    return resolved_to, int(resolved_listen), instance_id


def build_socat_args(bind: str, listen: int, target_port: int, allow: Optional[str]) -> list:
    listen_spec = f"TCP-LISTEN:{listen},bind={bind},fork,reuseaddr"
    if allow:
        listen_spec += f",range={allow}"
    return ["socat", listen_spec, f"TCP:{ALLOWED_TARGET_HOST}:{target_port}"]


def open_forward(
    to: Optional[str] = None,
    listen: Optional[int] = None,
    reason: str = "",
    ttl: str = DEFAULT_TTL,
    bind: Optional[str] = None,
    allow: Optional[str] = None,
    lan: Optional[str] = None,
    instance: Optional[str] = None,
    actor: str = "host_portfwd",
    db_path: str = DEFAULT_DB_PATH,
    run: Callable = subprocess.run,
    port_open: Callable = _port_open,
) -> dict:
    if not reason or not reason.strip():
        raise PortfwdError("必須提供 --reason")
    to, listen, resolved_instance_id = resolve_declaration(
        target_ident=instance, to=to, listen=listen, db_path=db_path
    )
    instance_id = resolved_instance_id or _lookup_instance(db_path, parse_target(to)[1])
    _, target_port = parse_target(to)
    validate_listen_port(listen)
    seconds = parse_ttl(ttl)
    allow = validate_allow(allow)
    lan = validate_allow(lan)
    bind = validate_bind(bind) if bind else validate_bind(detect_bind_ip())
    if not port_open(ALLOWED_TARGET_HOST, target_port):
        raise PortfwdError(f"{ALLOWED_TARGET_HOST}:{target_port} 沒有 listener；請先確認 dev 容器已啟動")
    if port_open(bind, listen):
        raise PortfwdError(f"{bind}:{listen} 已被佔用")
    unit = f"{UNIT_PREFIX}{listen}"
    if agent_exec.agent_socket() and run is _REAL_RUN:
        try:
            res = agent_exec.portfwd_open(listen, target_port, bind, seconds, instance_id, allow)
        except agent_exec.AgentError as exc:
            raise PortfwdError(str(exc)) from exc
    else:
        res = get_default_manager().run_transient(
            unit,
            f"host-portfwd {bind}:{listen}->{to}",
            [
                f"RuntimeMaxSec={seconds}",
                f"ExecStopPost={CTL_PATH} portfwd _stopped --listen {listen} --instance {instance_id} --target {to} --bind {bind}",
            ],
            build_socat_args(bind, listen, target_port, allow),
            run=run,
        )
    if res.returncode != 0:
        raise PortfwdError(f"systemd-run 失敗：{(res.stderr or '').strip()}")
    detail = {"unit": unit, "bind": bind, "listen": listen, "target": to, "ttl_sec": seconds,
              "allow": allow, "lan": lan, "reason": reason.strip()}
    _record(db_path, instance_id, "portfwd_open", actor, detail)
    return detail


def close_forward(
    listen: Optional[int] = None,
    instance: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
    run: Callable = subprocess.run,
) -> int:
    if listen is None and instance:
        _, listen, _ = resolve_declaration(target_ident=instance, db_path=db_path)
    if listen is None:
        raise PortfwdError("必須指定 --listen 或提供已宣告的容器實例")
    validate_listen_port(listen)
    res = get_default_manager().stop_unit_blocking(f"{UNIT_PREFIX}{listen}.service", run=None if run is _REAL_RUN else run)
    if res.returncode != 0:
        raise PortfwdError(f"停止失敗（可能未開啟）：{(res.stderr or '').strip()}")
    return listen


def list_forwards(run: Callable = subprocess.run) -> str:
    return get_default_manager().list_units(f"{UNIT_PREFIX}*.service", run=None if run is _REAL_RUN else run)


def record_stopped(listen: int, instance_id: str, target: str, bind: str, result: str,
                   db_path: str = DEFAULT_DB_PATH) -> None:
    """ExecStopPost 呼叫：手動 close 記 portfwd_close，RuntimeMaxSec 到期（result=timeout）記 portfwd_expire。"""
    event = "portfwd_expire" if result == "timeout" else "portfwd_close"
    _record(db_path, instance_id, event, "systemd_execstoppost",
            {"listen": listen, "target": target, "bind": bind, "service_result": result})


def windows_hint(bind: str, listen: int, allow_remote: Optional[str] = None) -> str:
    fw_remote = f" remoteip={allow_remote}" if allow_remote else ""
    return (
        "Windows（管理員 PowerShell）需要另外執行，本工具無法代勞：\n"
        f"  netsh interface portproxy add v4tov4 listenport={listen} listenaddress=0.0.0.0 connectport={listen} connectaddress={bind}\n"
        f"  netsh advfirewall firewall add rule name=\"container-ctl-portfwd-{listen}\" dir=in action=allow protocol=TCP localport={listen}{fw_remote}\n"
        "收尾：\n"
        f"  netsh interface portproxy delete v4tov4 listenport={listen} listenaddress=0.0.0.0\n"
        f"  netsh advfirewall firewall delete rule name=\"container-ctl-portfwd-{listen}\""
    )
