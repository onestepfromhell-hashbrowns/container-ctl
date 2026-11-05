"""
container_ctl.cli.validate — 容器專案靜態預檢（零副作用）
TICKET-02: 單一真實來源 (SSOT) 靜態預檢子命令
"""
import json
import sys
from pathlib import Path
from typing import Any

from container_ctl.declaration_guard import check_declarations
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.preflight import _check_image_digests
from container_ctl.release_anchor import check_release_anchor
from container_ctl.image_volume_guard import check_image_volumes
from container_ctl.repo_reader import (
    DEFAULT_GIT_REPOS_DIR,
    DeployConfigNotFoundError,
    PinnedDigestRequiredError,
    list_container_configs,
    read_manifest,
    repo_path_for,
)
from container_ctl.volume_resolver import (
    ALLOWED_MOUNT_READ_ROOTS,
    validate_volumes,
)


def run_validation(
    project: str,
    ref: str = "HEAD",
    repos_dir: str | None = None,
    tier: str | None = None,
) -> tuple[bool, list[str]]:
    """
    執行無副作用靜態預檢：
    1. Bare repo 與 manifest 讀取
    2. Container configs 讀取與 digest 驗證
    3. Volume 掛載預檢
    回傳: (ok, messages)
    """
    messages: list[str] = []
    repo, kind = resolve_deploy_repo(project, repos_dir)
    if not Path(repo).exists():
        return False, [f"[ERROR] Bare repo 不存在: {repo}"]

    try:
        manifest = read_manifest(repo, ref)
    except DeployConfigNotFoundError as e:
        return False, [f"[ERROR] {e}"]
    except Exception as e:
        return False, [f"[ERROR] 無法讀取 manifest: {e}"]

    # 檢查 project 名稱是否相符
    manifest_name = manifest.get("project_name")
    if manifest_name and manifest_name != project:
        messages.append(f"[WARN] CLI 專案名稱 ({project}) 與 Manifest 宣告名稱 ({manifest_name}) 不一致")

    # 讀取 containers
    try:
        configs = list_container_configs(repo, ref, tier=tier)
    except (DeployConfigNotFoundError, PinnedDigestRequiredError) as e:
        return False, [f"[ERROR] {e}"]
    except Exception as e:
        return False, [f"[ERROR] 容器配置讀取失敗: {e}"]

    # 驗證 image digests
    digests_ok, digest_msg = _check_image_digests(configs)
    if not digests_ok:
        messages.append(digest_msg)
        return False, messages

    # 驗證 digest 格式與 tier 對應的 Host IP
    try:
        from container_ctl.env_overlay import read_profile
        profile = read_profile(repo, ref)
    except Exception as e:  # noqa: BLE001
        return False, messages + [f"[ERROR] 環境 profile 無法讀取: {e}"]
    decl_ok, decl_msgs = check_declarations(configs, profile)
    messages.extend(decl_msgs)
    if not decl_ok:
        return False, messages

    # 驗證 volumes (唯讀純查詢)
    vols_ok, vol_msgs, _ = validate_volumes(configs, project=project)
    messages.extend(vol_msgs)
    if not vols_ok:
        return False, messages

    image_ok, image_msgs = check_image_volumes(configs, require_image=False)
    messages.extend(image_msgs)
    if not image_ok:
        return False, messages

    # prod 版本錨定：運行中舊 prod 缺 release tag 只 WARN；digest 與凍結記錄不符一律 ERROR
    anchor_ok, anchor_msgs = check_release_anchor(repo, configs, str(manifest.get("version", "")))
    messages.extend(anchor_msgs)
    if not anchor_ok:
        return False, messages

    if not messages:
        messages.append("[OK]   所有預檢項目通過")

    from container_ctl.contract import ContractError, check_contract, read_contract
    try:
        contract = read_contract(repo, ref)
    except ContractError as e:
        return False, messages + [f"[ERROR] {e}"]
    contract_msgs = [m for cfg in configs for m in check_contract(cfg, contract)]
    messages.extend(contract_msgs)
    if any(m.startswith("[ERROR]") for m in contract_msgs):
        return False, messages

    if repos_dir is None:  # 測試以 repos_dir 隔離時不查正式工作樹
        from container_ctl.cli.config import ineffective_source_messages
        messages.extend(ineffective_source_messages(project))

    if kind == "legacy":
        messages.append(f"[WARN] {project} 仍為 legacy 模式（宣告與程式碼同 repo）；可用 `container-ctl config init {project}` 遷移到設定 repo")

    return True, messages


def validate(
    project: str,
    ref: str = "HEAD",
    output: str = "table",
    quiet: bool = False,
    repos_dir: str | None = None,
    tier: str | None = None,
) -> None:
    ok, messages = run_validation(project, ref, repos_dir=repos_dir, tier=tier)

    result_data = {
        "project": project,
        "ref": ref,
        "ok": ok,
        "messages": messages,
    }

    if output == "json":
        if not quiet:
            print(json.dumps(result_data, indent=2, ensure_ascii=False))
        elif not ok:
            for msg in messages:
                print(msg, file=sys.stderr)
    else:
        # table / plain text
        if not quiet:
            for msg in messages:
                print(msg)
        elif not ok:
            for msg in messages:
                print(msg, file=sys.stderr)

    if not ok:
        sys.exit(1)
    sys.exit(0)
