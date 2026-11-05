"""container-ctl prod doctor — prod 部署前的唯讀體檢：一次列出全部阻塞與修法（SPEC 方案 P0）。

重用 validate（結構、digest、volume、release 錨定）並加上「prod 能不能啟動」的規則：
env 必填、明文 http、唯讀 rootfs 相對路徑、宿主目錄／secret 檔的存在與映射擁有者、生命週期與健康檢查宣告。
不連資料庫、不讀 secret 內容、不啟動任何容器。
"""
import getpass
import json
import os
import sys
from pathlib import Path

from container_ctl.cli.validate import run_validation
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.image_volume_guard import runtime_image_ref
from container_ctl.release_anchor import check_release_anchor
from container_ctl.repo_reader import list_container_configs, read_manifest

_PATH_SUFFIXES = ("_DIR", "_PATH", "_FILE", "_ROOT")


def _subuid_start(user: str | None = None, subuid_file: str = "/etc/subuid") -> int | None:
    user = user or getpass.getuser()
    try:
        for line in Path(subuid_file).read_text().splitlines():
            name, start, _count = line.split(":")
            if name == user:
                return int(start)
    except (OSError, ValueError):
        return None
    return None


def _mapped_host_uid(container_uid: int, subuid_start: int | None) -> int | None:
    """rootless 且無 keep-id：容器 uid 0→宿主 uid，uid N(N>=1)→subuid_start+N-1。"""
    if container_uid == 0:
        return os.getuid()
    if subuid_start is None:
        return None
    return subuid_start + container_uid - 1


def _container_uid(cfg: dict) -> int | None:
    user = cfg.get("user")
    if user is None:
        return None
    try:
        return int(str(user).split(":")[0])
    except ValueError:
        return None


def _stat_path(path: Path, expected_host_uid: int | None, container_uid: int | None) -> tuple[bool, bool | None, int | None]:
    """回傳 (存在, 擁有者是否正確或 None=未知, 權限位元)。目錄屬映射 uid 時宿主 stat 會 EACCES，
    改用 `podman unshare stat`（只讀中繼資料，不讀內容）；其輸出的 uid 為容器命名空間視角。"""
    import subprocess

    try:
        st = path.stat()
        owner_ok = None if expected_host_uid is None else st.st_uid == expected_host_uid
        return True, owner_ok, st.st_mode & 0o777
    except FileNotFoundError:
        return False, None, None
    except PermissionError:
        from container_ctl import agent_exec
        if agent_exec.agent_socket():
            try:
                r = agent_exec.unshare_stat(str(path))
            except agent_exec.AgentError:
                return False, None, None
        else:
            r = subprocess.run(["podman", "--remote=false", "unshare", "stat", "-c", "%u %a", str(path)],
                               capture_output=True, text=True,
                               env={k: v for k, v in os.environ.items() if k not in ("CONTAINER_HOST", "CONTAINER_CONNECTION")})
        if r.returncode != 0:
            return False, None, None
        uid_s, mode_s = r.stdout.split()
        owner_ok = None if container_uid is None else int(uid_s) == container_uid
        return True, owner_ok, int(mode_s, 8)


def _host_for_container_path(cfg: dict, cpath: str) -> Path | None:
    best = None
    for vol in cfg.get("volumes", []):
        if not isinstance(vol, dict) or not vol.get("host_path"):
            continue
        base = vol["container_path"].rstrip("/")
        if cpath == base or cpath.startswith(base + "/"):
            if best is None or len(base) > len(best[0]):
                best = (base, Path(vol["host_path"]) / cpath[len(base):].lstrip("/"))
    return best[1] if best else None


def prod_findings(cfg: dict, *, subuid_start: int | None = None) -> list[str]:
    name = cfg.get("name", "unknown")
    out: list[str] = []
    env = cfg.get("env") or {}
    mode = (cfg.get("lifecycle_policy") or {}).get("mode", "always_on")
    if mode == "manual":
        out.append(f"[WARN] {name}: prod 宣告 lifecycle_policy.mode=manual，redeploy 不會啟動它 → 改 always_on")
    if not (cfg.get("healthcheck") or cfg.get("health_cmd")):
        out.append(f"[WARN] {name}: prod 未宣告 healthcheck，podman 無健康狀態 → 加 healthcheck{{test,interval,timeout,retries,start_period}}")
    for key, value in env.items():
        if isinstance(value, str) and value.strip() == "":
            out.append(f"[ERROR] {name}: env {key} 為空字串（prod 模式常拒絕空值，例：ALLOWED_ORIGINS）→ 填入 prod 值或移除該鍵")
        elif isinstance(value, str) and value.startswith("http://") and "127.0.0.1" not in value and "localhost" not in value:
            out.append(f"[WARN] {name}: env {key}={value} 為明文 http；確認應用 prod 模式是否要求 https")
        if (cfg.get("read_only") or cfg.get("read_only_rootfs")) and key.endswith(_PATH_SUFFIXES):
            if isinstance(value, str) and value and not value.startswith("/") and "://" not in value:
                out.append(f"[ERROR] {name}: read_only rootfs 下 env {key}={value} 為相對路徑 → 改絕對路徑並確保位於可寫掛載")
    uid = _container_uid(cfg)
    expected = _mapped_host_uid(uid, subuid_start) if uid is not None else None
    for vol in cfg.get("volumes", []):
        if not isinstance(vol, dict) or not vol.get("host_path"):
            continue
        host = Path(vol["host_path"])
        exists, owner_ok, _mode = _stat_path(host, expected, uid)
        if not exists:
            out.append(f"[ERROR] {name}: 宿主目錄不存在 {host} → mkdir -p {host}（/var/opt 不會自動補建）")
            continue
        if owner_ok is False and "rw" in str(vol.get("options", "rw")).split(","):
            out.append(
                f"[ERROR] {name}: 可寫掛載 {host} 擁有者與容器 user={cfg.get('user')} 不符 "
                f"→ podman unshare chown -R {cfg.get('user')} {host}"
            )
    for key, value in env.items():
        if key.endswith("_FILE") and isinstance(value, str) and value.startswith("/"):
            host = _host_for_container_path(cfg, value)
            if host is None:
                out.append(f"[ERROR] {name}: env {key}={value} 不在任何宿主掛載內，容器內不會有此檔")
                continue
            exists, owner_ok, mode = _stat_path(host, expected, uid)
            if not exists:
                out.append(f"[ERROR] {name}: env {key} 指向的宿主檔不存在 {host}（password/token 可宣告 secrets[] 由 `secret provision` 產生；憑證／私鑰須事先就位）")
            elif owner_ok is False:
                out.append(f"[ERROR] {name}: secret 檔 {host} 擁有者與容器 user={cfg.get('user')} 不符，容器讀不到 → podman unshare chown {cfg.get('user')} {host}")
            elif mode is not None and mode & 0o077:
                out.append(f"[WARN] {name}: secret 檔 {host} 權限 {oct(mode)} 過寬 → chmod 0400")
    return out


def run_prod_doctor(project: str, ref: str = "HEAD", services: list[str] | None = None,
                    repos_dir: str | None = None, anchor_strict: bool = True) -> tuple[bool, list[str]]:
    ok, messages = run_validation(project, ref, repos_dir=repos_dir)
    messages = list(messages)
    repo, _kind = resolve_deploy_repo(project, repos_dir)
    try:
        manifest = read_manifest(repo, ref)
        configs = [c for c in list_container_configs(repo, ref) if c.get("tier") == "prod"]
    except Exception as exc:  # 已由 validate 回報
        return False, messages + [f"[ERROR] 無法讀取 prod 宣告：{exc}"]
    if services:
        configs = [c for c in configs if c.get("name") in services]
    if not configs:
        return False, messages + ["[ERROR] 找不到符合的 prod 服務宣告"]
    anchor_ok, anchor_msgs = check_release_anchor(repo, configs, str(manifest.get("version", "")), strict=anchor_strict)
    messages.extend(m for m in anchor_msgs if m not in messages)
    start = _subuid_start()
    findings: list[str] = []
    for cfg in configs:
        findings.extend(prod_findings(cfg, subuid_start=start))
    from container_ctl.image_checks import image_findings, inspect_image
    for cfg in configs:
        ref_img = str(cfg.get("image") or "")
        runtime = runtime_image_ref(ref_img) if ref_img else ""
        inspected = inspect_image(runtime) if runtime else None
        if inspected is None:
            findings.append(f"[WARN] {cfg.get('name')}: 本機找不到宣告的映像，略過映像 WORKDIR／ENV 檢查")
        else:
            # 上游映像（非 localhost/ 自建，如 postgres:15-alpine）WORKDIR 不適用，只查 /workspace 與 debugpy
            findings.extend(image_findings(inspected, str(cfg.get("name")),
                                           check_workdir=str(cfg.get("image") or "").startswith("localhost/")))
    from container_ctl.dockerfile_provenance import doctor_findings
    from container_ctl.repo_reader import repo_path_for
    if any(str(c.get("image") or "").startswith("localhost/") for c in configs):
        findings.extend(doctor_findings(repo_path_for(project)))
    from container_ctl.gate_runner import gate_findings
    findings.extend(gate_findings(configs))
    from container_ctl.path_scan_runner import path_scan_findings
    findings.extend(path_scan_findings(configs))
    messages.extend(findings)
    all_ok = ok and anchor_ok and not any(m.startswith("[ERROR]") for m in messages)
    messages.append("[INFO] 未檢查（需人工或另案）：資料庫可達性與角色、TLS 憑證內容與到期、應用層 prod 設定語意；以 prepare-prod 的冒煙驗證補強。")
    messages.append("[OK]   prod doctor 無阻塞" if all_ok else "[FAIL] prod doctor 發現阻塞，修正上列 [ERROR] 後重跑")
    return all_ok, messages


def prod_doctor(project: str, ref: str = "HEAD", services: list[str] | None = None,
                output: str = "table", repos_dir: str | None = None) -> None:
    ok, messages = run_prod_doctor(project, ref, services, repos_dir)
    if output == "json":
        print(json.dumps({"project": project, "ref": ref, "ok": ok, "messages": messages}, indent=2, ensure_ascii=False))
    else:
        for msg in messages:
            print(msg)
    sys.exit(0 if ok else 1)
