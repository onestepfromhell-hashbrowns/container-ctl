"""Governed, single-instance lifecycle operations for existing Quadlets."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.db import DEFAULT_DB_PATH, init_db
from container_ctl.image_volume_guard import runtime_image_ref
from container_ctl.lifecycle_adapter import (
    get_default_manager,
    precise_restart_workload,
    precise_start_workload,
)
from container_ctl.dag_resolver import ServiceAction
from container_ctl import paths
from container_ctl.quadlet_gen import (
    DEFAULT_SERVICES_DIR,
    SYSTEMD_QUADLET_DIR,
    _render_container_unit,
    resolve_extension_volumes,
)

_DRIFT_KEYS = ("exec", "volume", "environment", "publishport", "network", "tmpfs", "podmanargs")


def _fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def _quadlet_image(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*Image\s*=\s*(.*?)\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _check_pinned_image(config: dict, unit_path: Path) -> str:
    declared = config.get("image", "")
    if "@sha256:" not in declared:
        _fail("state.db blueprint 未固定 image digest；拒絕啟動既有單元。")

    expected = runtime_image_ref(declared)
    rendered = _quadlet_image(unit_path)
    if not rendered:
        _fail(f"Quadlet 缺少 Image=：{unit_path}")
    if rendered != expected:
        _fail(
            "Quadlet Image 與 state.db blueprint 的 pinned image 不一致："
            f"Quadlet={rendered}，pinned={expected}。請先以受治理部署更新宣告與單元。"
        )

    result = subprocess.run(
        ["podman", "image", "inspect", expected, "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        _fail(f"pinned image 不存在或無法檢查：{expected}: {result.stderr.strip()}")
    try:
        metadata = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        _fail(f"pinned image metadata 無法解析：{exc}")

    if declared.startswith("localhost/"):
        pinned_id = declared.rsplit("@sha256:", 1)[1]
        actual_id = str(metadata.get("Id", "")).removeprefix("sha256:")
        if actual_id != pinned_id:
            _fail(
                "本機映像 ID 與 state.db blueprint 的 pinned digest 不符："
                f"pinned={pinned_id}，actual={actual_id}。"
            )
    return expected


def _unit_keylines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if "=" in line and key in _DRIFT_KEYS:
            out.append(line.strip())
    return sorted(out)


def _check_unit_drift(config: dict, row, unit_path: Path, services_dir: str, expected_image: str) -> None:
    """Quadlet 的 Exec/Volume/Environment/Port/Network 必須與 blueprint 渲染結果一致。"""
    action = ServiceAction(config.get("name", row["service_name"]), "NEW", config)
    ext = resolve_extension_volumes(row["project_name"], services_dir, read_only=True)
    expected = _render_container_unit(
        action, expected_image, row["project_name"], row["version"], extra_volumes=ext
    )
    actual = unit_path.read_text(encoding="utf-8")
    if _unit_keylines(expected) != _unit_keylines(actual):
        _fail(
            f"Quadlet 與 state.db blueprint 渲染結果漂移（Exec/Volume/Env/Port/Network）：{unit_path}。"
            "請先以受治理部署（redeploy --service）更新單元。"
        )


def _wait_active(container_name: str, timeout: float = 60.0) -> bool:
    """systemd unit active 且 podman 容器 Running 才算成功。"""
    deadline = time.monotonic() + timeout
    unit = f"{container_name}.service"
    while time.monotonic() < deadline:
        unit_ok = get_default_manager().is_active(unit, timeout=5.0)
        if unit_ok:
            res = subprocess.run(
                ["podman", "inspect", "-f", "{{.State.Running}}", container_name],
                capture_output=True, text=True, check=False,
            )
            if res.returncode == 0 and res.stdout.strip() == "true":
                return True
        time.sleep(1.0)
    return False


def _exec_hook(container_name: str, hook, timeout: float) -> tuple[bool, str]:
    cmd = ["sh", "-c", hook] if isinstance(hook, str) else list(hook)
    res = subprocess.run(
        ["podman", "exec", container_name, *cmd],
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    return res.returncode == 0, (res.stdout + res.stderr).strip()[-400:]


def _run_app_hooks(container_name: str, hooks: dict) -> None:
    """啟動容器內 app（app_hooks.start），再以 readiness_probe 輪詢確認。"""
    start = hooks.get("start")
    if not start:
        _fail("宣告缺少 app_hooks.start，無法使用 --with-app。")
    ok, out = _exec_hook(container_name, start, timeout=float(hooks.get("start_timeout", 120)))
    if not ok:
        _fail(f"app_hooks.start 失敗：{out}")
    probe = hooks.get("readiness_probe")
    if not probe:
        print("[WARN] 未宣告 app_hooks.readiness_probe，僅確認 start 指令成功。")
        return
    deadline = time.monotonic() + float(hooks.get("probe_timeout", 60))
    last = ""
    while time.monotonic() < deadline:
        ok, last = _exec_hook(container_name, probe, timeout=15)
        if ok:
            print("[OK] app readiness probe 通過。")
            return
        time.sleep(2.0)
    _fail(f"app readiness probe 逾時：{last}")


def operate_manual_instance(
    operation: str,
    project: str,
    service: str,
    container_name: str,
    *,
    yes: bool = False,
    db_path: str = DEFAULT_DB_PATH,
    services_dir: str = DEFAULT_SERVICES_DIR,
    quadlet_dir: Path = SYSTEMD_QUADLET_DIR,
    with_app: bool = False,
) -> None:
    """Start or restart one explicitly named, already generated manual instance."""
    if operation not in {"start", "restart"}:
        raise ValueError(f"unsupported operation: {operation}")

    conn = init_db(db_path)
    rows = conn.execute(
        """SELECT * FROM instances
           WHERE project_name = ? AND service_name = ? AND container_name = ?
             AND status != 'destroyed'
           ORDER BY updated_at DESC""",
        (project, service, container_name),
    ).fetchall()
    if len(rows) != 1:
        conn.close()
        _fail(
            f"找不到唯一的既有實例 {project}/{service}/{container_name} "
            f"（匹配筆數：{len(rows)}）。"
        )
    row = rows[0]
    try:
        config = json.loads(row["blueprint"] or "{}")
    except (TypeError, ValueError) as exc:
        conn.close()
        _fail(f"state.db blueprint 無法解析：{exc}")
    conn.close()

    default_mode = "manual" if row["tier"] == "dev" else "always_on"
    lifecycle_mode = config.get("lifecycle_policy", {}).get("mode", default_mode)
    if lifecycle_mode == "always_on":
        # always_on 由 redeploy 啟動；此入口僅提供單服務 restart（例：更換 secret 檔後重載），不碰其他服務
        if operation != "restart":
            _fail("always_on 實例由 redeploy 啟動；此入口只允許 restart。")
        if with_app:
            _fail("always_on 實例的應用由 entrypoint 啟動，不適用 --with-app。")
    elif lifecycle_mode != "manual":
        _fail(f"此入口只允許 manual 實例（或 always_on 的 restart）；目前 lifecycle mode={lifecycle_mode}。")

    unit_path = Path(services_dir) / project / f"{container_name}.container"
    link_path = Path(quadlet_dir) / unit_path.name
    if not unit_path.is_file():
        _fail(f"既有 Quadlet 不存在：{unit_path}")
    if not link_path.is_symlink() or link_path.resolve() != unit_path.resolve():
        _fail(f"systemd Quadlet 連結不存在或指向不符：{link_path}")

    expected_image = _check_pinned_image(config, unit_path)
    _check_unit_drift(config, row, unit_path, services_dir, expected_image)
    hooks = config.get("app_hooks") or {}
    if with_app and not hooks.get("start"):
        _fail("宣告缺少 app_hooks.start，無法使用 --with-app。")
    if not yes:
        try:
            answer = input(f"確認 {operation} {container_name}.service？(y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            print("[CANCELLED] 未送出 systemd 工作。")
            return

    stable_id = (
        config.get("stable_id")
        or (config.get("governance", {}).get("id") if isinstance(config.get("governance"), dict) else None)
        or (config.get("labels", {}).get(f"{paths.LABEL_NS}.governance.id") if isinstance(config.get("labels"), dict) else None)
        or (config.get("labels", {}).get("governance.id") if isinstance(config.get("labels"), dict) else None)
        or service
    )
    owner_uid = int(config.get("owner_uid", 1000))
    dispatch = precise_start_workload if operation == "start" else precise_restart_workload
    allowed, result, reason = dispatch(
        stable_id,
        f"{container_name}.service",
        owner_uid=owner_uid,
    )
    if not allowed:
        _fail(f"治理准入或 systemd {operation} 失敗：{reason}")

    if not _wait_active(container_name):
        conn = init_db(db_path)
        conn.execute(
            """INSERT INTO lifecycle_events
                 (instance_id, event_type, triggered_by, detail, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                row["id"], f"{operation}_failed", f"container_ctl_service_{operation}",
                json.dumps({"container_name": container_name, "reason": "not_active"}, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
        _fail(f"{container_name}.service 送出 {operation} 後未在時限內進入 active/Running；state.db 狀態未更新。")

    now = datetime.now(timezone.utc).isoformat()
    conn = init_db(db_path)
    updated = conn.execute(
        "UPDATE instances SET status = 'provisioned', updated_at = ? WHERE id = ? AND status != 'destroyed'",
        (now, row["id"]),
    )
    if updated.rowcount != 1:
        conn.close()
        _fail("systemd 已收到工作，但 state.db 實例已被移除或銷毀；未覆寫其狀態。")
    conn.execute(
        """INSERT INTO lifecycle_events
             (instance_id, event_type, triggered_by, detail, occurred_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            row["id"],
            f"{operation}_requested",
            f"container_ctl_service_{operation}",
            json.dumps({"container_name": container_name, "job_submitted": True}, ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    conn.close()
    print(f"[OK] {operation} 完成：{container_name}.service active 且容器 Running。")
    if with_app:
        _run_app_hooks(container_name, hooks)
    elif lifecycle_mode == "manual":
        print("[NOTE] 容器內 app 未啟動；需 control.sh start，或加 --with-app（宣告 app_hooks）。")
