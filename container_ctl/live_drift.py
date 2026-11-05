"""宣告 vs 運行中容器的實際設定比對（唯讀）。

`redeploy` 原本只比對 state.db 的 blueprint；一次 redeploy 沒成功重啟後，state.db 已是新設定，
之後永遠看不到差異。此模組直接比對 `podman inspect`，只檢查「宣告中明確寫了」的鍵（子集比對，避免誤報）。
"""
import json
import os
import subprocess


def inspect_container(name: str) -> dict | None:
    r = subprocess.run(["podman", "inspect", name, "--format", "{{json .}}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _port_binding(spec) -> tuple[str, str, str] | None:
    parts = str(spec).split("/")[0].split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return None


def find_live_drift(config: dict, live: dict, extra_volumes: list[dict] | None = None) -> list[str]:
    out: list[str] = []
    host = live.get("HostConfig") or {}
    conf = live.get("Config") or {}
    image = config.get("image", "")
    if "@sha256:" in image:
        declared = "sha256:" + image.rsplit("@sha256:", 1)[1]
        actual = "sha256:" + str(live.get("Image", "")).removeprefix("sha256:")
        if declared != actual:
            out.append(f"image 運行 {actual[:19]}，宣告 {declared[:19]}")
    if config.get("user") is not None and str(conf.get("User", "")) != str(config["user"]):
        out.append(f"user 運行 {conf.get('User')!r}，宣告 {config['user']!r}")
    for key in ("read_only", "read_only_rootfs"):
        if key in config and bool(host.get("ReadonlyRootfs")) != bool(config[key]):
            out.append(f"read_only 運行 {bool(host.get('ReadonlyRootfs'))}，宣告 {bool(config[key])}")
            break
    if config.get("run_init") and not host.get("Init"):
        out.append("run_init 宣告為 true，運行中未啟用 init")
    bindings = host.get("PortBindings") or {}
    for spec in config.get("ports") or []:
        parsed = _port_binding(spec)
        if not parsed:
            continue
        ip, hport, cport = parsed
        if "-" in hport or "-" in cport:  # 埠範圍（UDP 例外）不逐一比對
            continue
        entries = bindings.get(f"{cport}/tcp") or bindings.get(f"{cport}/udp") or []
        if not any(e.get("HostPort") == hport and (not ip or e.get("HostIp") in (ip, "")) for e in entries):
            out.append(f"port {spec} 未在運行容器上")
    mounts = {m.get("Destination"): m for m in live.get("Mounts") or []}
    for vol in (config.get("volumes") or []) + list(extra_volumes or []):
        if not isinstance(vol, dict) or not vol.get("host_path"):
            continue
        m = mounts.get(vol["container_path"])
        if m is None:
            out.append(f"volume {vol['container_path']} 未掛載")
            continue
        want_rw = "ro" not in str(vol.get("options", "rw")).split(",")
        if bool(m.get("RW")) != want_rw:
            out.append(f"volume {vol['container_path']} 讀寫模式不符（宣告 {'rw' if want_rw else 'ro'}）")
        if os.path.realpath(m.get("Source", "")) != os.path.realpath(vol["host_path"]):
            out.append(f"volume {vol['container_path']} 來源 {m.get('Source')} ≠ 宣告 {vol['host_path']}")
    live_env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in (conf.get("Env") or []) if "=" in e}
    for key, value in (config.get("env") or {}).items():
        if isinstance(value, str) and live_env.get(key) != value:
            out.append(f"env {key} 運行值與宣告不同")
    if (config.get("healthcheck") or config.get("health_cmd")) and not conf.get("Healthcheck"):
        out.append("宣告了 healthcheck，運行容器沒有")
    return out


def find_quadlet_dropins(quadlet_path) -> list[str]:
    """受管 Quadlet 旁的 `<unit>.container.d/*.conf`：工具生成器不產生，屬於繞過宣告的手工覆寫。"""
    from pathlib import Path

    d = Path(str(quadlet_path) + ".d")
    return sorted(str(f) for f in d.glob("*.conf")) if d.is_dir() else []
