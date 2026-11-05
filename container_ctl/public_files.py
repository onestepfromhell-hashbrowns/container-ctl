"""宣告 `public_files`：把「公開」憑證（如 CA）由來源複製到容器要掛載的專用目錄（agent 不手動搬檔）。

宣告：  "public_files": [{"src": "/abs/.../ca.crt", "dest": "/abs/.../certs/ca.crt", "kind": "ca-cert"}]
安全規則：
  kind 只接受 ca-cert（只放公開憑證）。來源內容必須是 PEM，且只含 CERTIFICATE 區塊；
  含任何 PRIVATE KEY、或其他區塊、或超過 64KB → 拒絕，不寫出任何東西。
  dest 檔名不得為 *.key／*.pem 私鑰名；dest 須在白名單根目錄內且不在受保護路徑。
  以 `podman unshare` 讀寫（來源目錄可能屬映射 uid）；寫入為暫存檔 + mv，0644；內容相同則不動。
這讓消費者不必再從含 server.key 的目錄掛單一檔案（最小暴露）。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

MAX_BYTES = 64 * 1024
_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")


def validate_ca_pem(text: str) -> str | None:
    """合法回傳 None；否則回傳原因。"""
    if len(text.encode()) > MAX_BYTES:
        return "來源超過 64KB"
    kinds = _BLOCK.findall(text)
    if not kinds:
        return "不是 PEM 憑證"
    if any("PRIVATE KEY" in k for k in kinds) or "PRIVATE KEY" in text:
        return "含 PRIVATE KEY 區塊"
    if set(kinds) != {"CERTIFICATE"}:
        return f"含非 CERTIFICATE 區塊：{sorted(set(kinds) - {'CERTIFICATE'})}"
    return None


def check_declaration(name: str, entry: dict, allowed_roots, forbidden) -> list[str]:
    errs = []
    src, dest = str(entry.get("src", "")), str(entry.get("dest", ""))
    if entry.get("kind") != "ca-cert":
        errs.append(f"[ERROR] {name}: public_files.kind 只接受 ca-cert（收到 {entry.get('kind')!r}）")
    if not os.path.isabs(src) or not os.path.isabs(dest):
        errs.append(f"[ERROR] {name}: public_files 的 src／dest 必須是絕對路徑")
        return errs
    if Path(dest).suffix in (".key",) or Path(dest).name.endswith(("-key.pem", ".key.pem")):
        errs.append(f"[ERROR] {name}: dest 檔名 {Path(dest).name} 像私鑰，拒絕")
    d = Path(os.path.abspath(dest))
    if not any(d == r or d.is_relative_to(r) for r in allowed_roots):
        errs.append(f"[ERROR] {name}: public_files.dest {dest} 不在允許的根目錄內")
    if any(d == r or d.is_relative_to(r) for r in forbidden):
        errs.append(f"[ERROR] {name}: public_files.dest {dest} 位於受保護路徑")
    return errs


def _unshare(*args: str) -> list[str]:
    return ["podman", "--remote=false", "unshare", *args]


def _publish_via_agent(src: str, dest: str) -> str:
    from container_ctl import agent_exec
    try:
        r = agent_exec.unshare_publish_ca(src, dest)
    except agent_exec.AgentError as exc:
        return f"[ERROR] {src} 發布失敗：{exc}"
    if r.returncode == 0:
        if (r.stdout or "").strip() == "UNCHANGED":
            return f"[OK]   {dest} 已是最新，未改動"
        return f"[OK]   已發布公開憑證 {src} → {dest}（0644，已驗證僅含 CERTIFICATE）"
    err = (r.stderr or "").strip()[:100]
    return f"[ERROR] {src} {err}" if r.returncode == 4 else f"[ERROR] 發布 {src} → {dest} 失敗：{err}"


def publish(src: str, dest: str, run=subprocess.run) -> str:
    """回傳 [OK]/[ERROR] 訊息。內容為公開憑證，才會經 stdin 傳遞。"""
    from container_ctl import agent_exec
    if agent_exec.agent_socket() and run is subprocess.run:
        return _publish_via_agent(src, dest)
    env = {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}
    r = run(_unshare("cat", src), capture_output=True, env=env)
    if r.returncode:
        return f"[ERROR] 讀不到來源 {src}"
    try:
        text = r.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return f"[ERROR] {src} 不是文字 PEM，拒絕"
    reason = validate_ca_pem(text)
    if reason:
        return f"[ERROR] {src} 拒絕發布：{reason}"
    cur = run(_unshare("cat", dest), capture_output=True, env=env)
    if cur.returncode == 0 and hashlib.sha256(cur.stdout).digest() == hashlib.sha256(r.stdout).digest():
        return f"[OK]   {dest} 已是最新，未改動"
    script = 'set -e; t="$2.tmp.$$"; cat > "$t"; chmod 0644 "$t"; mv -f "$t" "$2"'
    w = run(_unshare("sh", "-c", script, "sh", src, dest), input=r.stdout, capture_output=True, env=env)
    if w.returncode:
        return f"[ERROR] 寫入 {dest} 失敗：{(w.stderr or b'').decode()[:100]}"
    return f"[OK]   已發布公開憑證 {src} → {dest}（0644，已驗證僅含 CERTIFICATE）"
