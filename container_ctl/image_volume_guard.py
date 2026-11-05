"""Check image-declared volumes against explicit Quadlet mounts before deployment."""

import json
import subprocess
from container_ctl import paths


def runtime_image_ref(image: str) -> str:
    """Use the pinned local image ID that Podman can run and inspect."""
    if image.startswith("localhost/") and "@sha256:" in image:
        return "sha256:" + image.rsplit("@sha256:", 1)[1]
    return image


def check_image_volumes(configs: list[dict], *, require_image: bool = True) -> tuple[bool, list[str]]:
    messages: list[str] = []
    ok = True
    for cfg in configs:
        image = cfg.get("image", "")
        name = cfg.get("name", "unknown")
        # Local build digests are image IDs. Inspect the exact ID used by Quadlet.
        runtime_image = runtime_image_ref(image)
        result = subprocess.run(
            ["podman", "image", "inspect", runtime_image, "--format", "{{json .}}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            image_required = require_image or cfg.get("tier") == "dev"
            message = f"[ERROR] {name}: 無法檢查部署映像 {runtime_image}: {result.stderr.strip()}"
            messages.append(message if image_required else message.replace("[ERROR]", "[WARN]", 1))
            ok = ok and not image_required
            continue
        try:
            metadata = json.loads(result.stdout)
            declared = set((metadata.get("Config") or {}).get("Volumes") or {})
            actual_id = metadata.get("Id", "").removeprefix("sha256:")
        except (ValueError, AttributeError, TypeError) as exc:
            messages.append(f"[ERROR] {name}: 映像 metadata 無法解析: {exc}")
            ok = False
            continue
        if image.startswith("localhost/") and "@sha256:" in image:
            pinned_id = image.rsplit("@sha256:", 1)[1]
            if actual_id != pinned_id:
                messages.append(f"[ERROR] {name}: 本機映像 ID 與宣告 digest 不符: {runtime_image}")
                ok = False
        if cfg.get("tier") == "dev":
            labels = metadata.get("Labels") or (metadata.get("Config") or {}).get("Labels") or {}
            if labels.get(f"{paths.OCI_LABEL_NS}.image-scope") != "dev-infra":
                messages.append(f"[ERROR] {name}: dev 映像缺少 dev-infra 來源標籤；請用 image prepare-dev 建置隔離基礎映像")
                ok = False
        explicit = {v.get("container_path") for v in cfg.get("volumes", []) if isinstance(v, dict)}
        temporary = {v.split(":", 1)[0] for v in cfg.get("tmpfs", []) if isinstance(v, str)}
        missing = declared - explicit - temporary
        if missing:
            messages.append(f"[ERROR] {name}: 映像 VOLUME 未在 volumes/tmpfs 明確宣告: {', '.join(sorted(missing))}")
            ok = False
    if ok and not messages:
        messages.append("[OK]   生產映像 VOLUME 均有明確掛載，或映像未宣告 VOLUME")
    return ok, messages
