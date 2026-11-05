"""供應鏈掛點（ADR-009 P7）：SBOM（SPDX）、cosign 簽章、provenance 欄位。預設關閉。

工具（syft／cosign）未安裝時不報錯：release 記錄的 sbom／signature／provenance 皆為 null，`image verify` 照常。
安裝須使用者另行批准（離線政策 SPEC §21）。啟用條件：
  SBOM     — PATH 有 syft
  簽章     — PATH 有 cosign 且 CONTAINER_CTL_COSIGN_KEY 指向私鑰檔（密碼走 COSIGN_PASSWORD 環境，本模組不讀不印）
`image verify --require-signature`：記錄無簽章、或無法驗證（缺 cosign／公鑰）一律失敗。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from container_ctl import artifact_store


def available(tool: str) -> bool:
    return shutil.which(tool) is not None


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_dir(project: str, service: str, version: str, root: str | None = None) -> Path:
    return artifact_store.layout_dir(project, service, root).parent / f"{service}.evidence" / version


def generate_sbom(project: str, service: str, version: str, *, root: str | None = None, run=subprocess.run) -> dict | None:
    if not available("syft") or not artifact_store.enabled():
        return None
    layout = artifact_store.layout_dir(project, service, root)
    out = evidence_dir(project, service, version, root)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "sbom.spdx.json"
    res = run(["syft", f"oci-dir:{layout}", "-o", f"spdx-json={target}"], capture_output=True, text=True)
    if res.returncode or not target.exists():
        return None
    return {"format": "spdx-json", "path": str(target), "sha256": _sha256_file(target)}


def sign(project: str, service: str, version: str, manifest_digest: str, *, root: str | None = None, run=subprocess.run) -> dict | None:
    key = os.environ.get("CONTAINER_CTL_COSIGN_KEY")
    if not (key and available("cosign") and manifest_digest and artifact_store.enabled()):
        return None
    layout = artifact_store.layout_dir(project, service, root)
    res = run(["cosign", "sign", "--yes", "--key", key, "--tlog-upload=false", f"oci:{layout}@{manifest_digest}"],
              capture_output=True, text=True)
    if res.returncode:
        return None
    pub = os.environ.get("CONTAINER_CTL_COSIGN_PUB")
    return {"tool": "cosign", "digest": manifest_digest, "layout": str(layout), "public_key": pub}


def provenance(source_commit: str, config_commit: str) -> dict:
    """最小 in-toto 風格 provenance 欄位：誰（工具）從哪個 commit 建出此 artifact。完整 SLSA 證明待簽章流程就位。"""
    return {"builder": "container-ctl", "source_commit": source_commit, "config_commit": config_commit}


def collect(project: str, service: str, version: str, record: dict, *, root: str | None = None, run=subprocess.run) -> dict:
    """回傳要併入 release 記錄的欄位；工具不在則對應欄位為 None。"""
    return {
        "sbom": generate_sbom(project, service, version, root=root, run=run),
        "signature": sign(project, service, version, str(record.get("manifest_digest") or ""), root=root, run=run),
        "provenance": provenance(str(record.get("source_commit") or ""), str(record.get("config_commit") or "")),
    }


def verify_evidence(record: dict, *, require_signature: bool = False, run=subprocess.run) -> tuple[bool, list[str]]:
    msgs: list[str] = []
    ok = True
    sbom = record.get("sbom")
    if sbom:
        try:
            actual = _sha256_file(Path(sbom["path"]))
        except OSError as exc:
            ok = False
            msgs.append(f"[ERROR] SBOM 檔無法讀取：{exc}")
        else:
            if actual != sbom.get("sha256"):
                ok = False
                msgs.append(f"[ERROR] SBOM 雜湊 {actual} 與記錄 {sbom.get('sha256')} 不符")
            else:
                msgs.append("[OK]   SBOM 雜湊一致")
    sig = record.get("signature")
    if not sig:
        if require_signature:
            ok = False
            msgs.append("[ERROR] release 記錄無簽章（--require-signature）")
        else:
            msgs.append("[INFO] 無簽章（未啟用 cosign）")
        return ok, msgs
    pub = sig.get("public_key")
    if not (available("cosign") and pub):
        if require_signature:
            ok = False
            msgs.append("[ERROR] 有簽章記錄但無法驗證（缺 cosign 或公鑰）")
        else:
            msgs.append("[WARN] 有簽章記錄但本機缺 cosign／公鑰，未驗證")
        return ok, msgs
    layout = sig.get("layout")
    ref = f"oci:{layout}@{sig['digest']}" if layout else sig["digest"]
    res = run(["cosign", "verify", "--key", pub, "--insecure-ignore-tlog=true", ref], capture_output=True, text=True)
    if res.returncode:
        return False, msgs + [f"[ERROR] cosign 驗證失敗：{(res.stderr or '').strip()[:200]}"]
    return ok, msgs + ["[OK]   cosign 簽章驗證通過"]
