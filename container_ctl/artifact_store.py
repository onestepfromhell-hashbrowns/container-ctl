"""Artifact store：OCI Image Layout（ADR-009 P3；OCI Image Spec v1）。

每個 (project, service) 一個 layout 目錄 `<store>/<project>/<service>/`，ref name＝version。
身分＝OCI manifest digest（sha256 of manifest blob），與本機 image ID（config digest）並存於 release 記錄。
不需 registry；日後換 OCI Distribution registry 只需替換本模組。`CONTAINER_CTL_OCI_STORE=off` 停用（測試／無 podman push 環境）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from container_ctl import paths

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REAL_RUN = subprocess.run  # 預設 run 的識別基準：呼叫端注入自訂 run（測試）時不接管


class ArtifactStoreError(Exception):
    pass


def _local_podman_env() -> dict[str, str]:
    """oci: transport 只在本機模式可用；container-ctl 主程式會設 CONTAINER_HOST（遠端模式），這裡須排除。"""
    return {k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")}


def store_root() -> str:
    return os.environ.get("CONTAINER_CTL_OCI_STORE") or paths.OCI_STORE


def enabled() -> bool:
    return store_root().lower() != "off"


def layout_dir(project: str, service: str, root: str | None = None) -> Path:
    return Path(root or store_root()) / project / service


def _index_entry(layout: Path, version: str) -> dict | None:
    try:
        index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for m in index.get("manifests", []):
        if (m.get("annotations") or {}).get("org.opencontainers.image.ref.name") == version:
            return m
    return None


def verify(project: str, service: str, version: str, expected: str | None = None, *, root: str | None = None) -> tuple[bool, str]:
    """index.json 有該 ref，且 manifest blob 的 sha256 確實等於其 digest（≠ expected 則失敗）。"""
    layout = layout_dir(project, service, root)
    entry = _index_entry(layout, version)
    if entry is None:
        return False, f"OCI store 沒有 {service}:{version}（{layout}）"
    digest = str(entry.get("digest", ""))
    if not DIGEST_RE.fullmatch(digest):
        return False, f"index.json 的 digest 無效：{digest!r}"
    if expected and digest != expected:
        return False, f"store digest {digest} 與 release 記錄 {expected} 不符"
    blob = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
    try:
        actual = "sha256:" + hashlib.sha256(blob.read_bytes()).hexdigest()
    except OSError as exc:
        return False, f"manifest blob 無法讀取：{exc}"
    if actual != digest:
        return False, f"manifest blob 內容雜湊 {actual} 與 digest {digest} 不符（store 損毀）"
    return True, digest


def discard(project: str, service: str, version: str, *, root: str | None = None) -> bool:
    """移除 index.json 內某 ref（失敗發布的殘留；blob 留給 podman 覆寫同內容）。呼叫端須確保該版本無 release tag。"""
    layout = layout_dir(project, service, root)
    try:
        index = json.loads((layout / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    kept = [m for m in index.get("manifests", []) if (m.get("annotations") or {}).get("org.opencontainers.image.ref.name") != version]
    if len(kept) == len(index.get("manifests", [])):
        return False
    index["manifests"] = kept
    (layout / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return True


def _cp_api():
    """控制面容器（CONTAINER_CTL_HOST_AGENT 存在）：podman 遠端 API 不支援 oci: transport，改走 cp_core.artifact_api（save/load oci-archive）。
    延後匯入以避開循環；匯入失敗即報錯，不回退本機 push/pull。"""
    from container_ctl import agent_exec
    if not agent_exec.agent_socket():
        return None
    try:
        from cp_core import artifact_api
    except ImportError as exc:
        raise ArtifactStoreError(f"控制面模式需要 cp_core.artifact_api（PYTHONPATH 未含專案 repo）：{exc}") from exc
    return artifact_api


def put(project: str, service: str, version: str, image_id: str, *, root: str | None = None, run=subprocess.run) -> str:
    """把本機映像推進 OCI layout 並回傳 manifest digest。同 ref 已存在則必須同 digest（不可覆寫）。"""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ArtifactStoreError(f"image_id 格式無效：{image_id!r}")
    api = _cp_api() if run is _REAL_RUN else None
    if api is not None:
        return api.put(project, service, version, image_id, root=root)
    layout = layout_dir(project, service, root)
    existing = _index_entry(layout, version)
    layout.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        raise ArtifactStoreError(f"OCI store 已有 {service}:{version}（{existing.get('digest')}）；拒絕覆寫")
    with tempfile.TemporaryDirectory(prefix="container-ctl-oci-") as tmp:
        digestfile = Path(tmp) / "digest"
        res = run(["podman", "--remote=false", "push", "--digestfile", str(digestfile), image_id, f"oci:{layout}:{version}"],
                  capture_output=True, text=True, env=_local_podman_env())
        if res.returncode:
            raise ArtifactStoreError(f"podman push 失敗：{(res.stderr or '').strip()[:300]}")
        digest = digestfile.read_text(encoding="utf-8").strip()
    if not DIGEST_RE.fullmatch(digest):
        raise ArtifactStoreError(f"取得的 manifest digest 無效：{digest!r}")
    ok, msg = verify(project, service, version, digest, root=root)
    if not ok:
        raise ArtifactStoreError(msg)
    return digest


def load(project: str, service: str, version: str, *, root: str | None = None, run=subprocess.run) -> str:
    """驗證後把 layout 中的映像載入本機 containers-storage（缺映像時還原用）。回傳 manifest digest。"""
    api = _cp_api() if run is _REAL_RUN else None
    if api is not None:
        return api.load(project, service, version, root=root)
    ok, msg = verify(project, service, version, root=root)
    if not ok:
        raise ArtifactStoreError(msg)
    res = run(["podman", "--remote=false", "pull", "-q", f"oci:{layout_dir(project, service, root)}:{version}"],
              capture_output=True, text=True, env=_local_podman_env())
    if res.returncode:
        raise ArtifactStoreError(f"podman pull 失敗：{(res.stderr or '').strip()[:300]}")
    return msg


def export_layout(project: str, service: str, version: str, dest: str | Path, *, root: str | None = None) -> str:
    """把單一 ref 的 manifest＋config＋layers 複製成獨立 OCI layout（不帶 store 內其他版本的 blob）。回傳 manifest digest。"""
    import shutil

    ok, digest = verify(project, service, version, root=root)
    if not ok:
        raise ArtifactStoreError(digest)
    src = layout_dir(project, service, root)
    blobs = src / "blobs" / "sha256"
    manifest = json.loads((blobs / digest.split(":", 1)[1]).read_text(encoding="utf-8"))
    needed = [digest, str((manifest.get("config") or {}).get("digest", ""))] + [str(x.get("digest", "")) for x in manifest.get("layers", [])]
    dest = Path(dest)
    (dest / "blobs" / "sha256").mkdir(parents=True, exist_ok=True)
    for d in needed:
        if not DIGEST_RE.fullmatch(d):
            raise ArtifactStoreError(f"manifest 含無效 digest：{d!r}")
        shutil.copyfile(blobs / d.split(":", 1)[1], dest / "blobs" / "sha256" / d.split(":", 1)[1])
    entry = dict(_index_entry(src, version) or {})
    (dest / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [entry]}, indent=2), encoding="utf-8")
    (dest / "oci-layout").write_text('{"imageLayoutVersion": "1.0.0"}\n', encoding="utf-8")
    return digest
