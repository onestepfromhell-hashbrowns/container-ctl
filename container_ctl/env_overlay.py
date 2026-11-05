"""環境 overlay 與 profile（ADR-009 P1；對標 Kustomize base/overlay 的保守子集）。

設定 repo 結構：
  .deploy/containers/<svc>.json                 base：artifact 身分＋環境無關欄位
  .deploy/envs/<env>/containers/<svc>.json     overlay：只含該環境差異（白名單鍵）
  .deploy/envs/<env>/profile.json              環境規則（Host IP、…）；缺則用預設
合併：dict 遞迴合併、其餘（含 list）整體取代。overlay 不得動 image／version／name／tier／command 等 artifact 與安全屬性。
無 overlay 時結果與 base 完全相同（向後相容）。
"""

from __future__ import annotations

import json
import os
import subprocess
from container_ctl import paths

# No default environment: without --env or CONTAINER_CTL_ENV (env var or conf file) only the base declaration applies.
DEFAULT_ENV = paths.setting("CONTAINER_CTL_ENV", "") or None

# overlay 允許覆蓋的鍵（其餘一律拒絕：artifact 身分與安全屬性由 base／映像決定）
OVERRIDABLE = frozenset(
    "env ports volumes networks network_alias network_aliases memory memory_max memory_swap_max tasks_max resources "
    "health_cmd health_interval health_timeout health_retries health_start_period healthcheck public_files secrets "
    "labels smoke portfwd portfwd_port tmpfs publish_exceptions lifecycle_policy update_strategy comment notes".split()
)
DEFAULT_PROFILE = {"host_ip": {"dev": "127.0.0.1", "prod": "127.0.0.2"}}
# 環境層真值（.environments/<env>/profile.json）；缺檔時退回 DEFAULT_PROFILE（向後相容）
ENVIRONMENTS_DIR = paths.ENVIRONMENTS_DIR


class OverlayError(Exception):
    pass


def check_overlay_keys(overlay: dict, where: str = "overlay") -> list[str]:
    if not isinstance(overlay, dict):
        return [f"{where} 必須為 JSON object"]
    return [f"{where} 不得覆蓋 '{k}'（artifact 身分或安全屬性；允許：env、ports、volumes、networks、資源、探針時序…）"
            for k in sorted(overlay) if k not in OVERRIDABLE]


def _merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = _merge(base[key], value) if key in base else value
        return out
    return over


def merge_overlay(base: dict, overlay: dict | None, where: str = "overlay") -> dict:
    if not overlay:
        return base
    errors = check_overlay_keys(overlay, where)
    if errors:
        raise OverlayError("；".join(errors))
    return _merge(base, overlay)


def _read_json(repo: str, ref: str, path: str) -> dict | None:
    found = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True)
    if found.returncode != 0:  # 沒有該檔＝沒有 overlay（base 讀取已負責報告 repo／ref 錯誤）
        return None
    try:
        data = json.loads(found.stdout)
    except ValueError as exc:
        raise OverlayError(f"{path} 不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise OverlayError(f"{path} 必須為 JSON object")
    return data


def overlay_path(env: str | None, name: str) -> str:
    return f".deploy/envs/{env}/containers/{name}.json"


def read_overlay(repo: str, ref: str, env: str | None, name: str) -> dict | None:
    if not env:
        return None
    return _read_json(repo, ref, overlay_path(env, name))


def environment_dir(env: str | None = None) -> str:
    env = env or DEFAULT_ENV
    if not env:
        raise ValueError("no environment selected: pass --env or set CONTAINER_CTL_ENV")
    return os.path.join(ENVIRONMENTS_DIR, env)


def read_environment_profile(env: str | None = None) -> dict:
    """讀環境層 profile.json；缺檔或損毀回 {}（不阻擋）。未選環境時回 {}。"""
    if not (env or DEFAULT_ENV):
        return {}
    try:
        with open(os.path.join(environment_dir(env), "profile.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_profile(repo: str, ref: str, env: str | None = None) -> dict:
    """優先序：DEFAULT_PROFILE < 環境層 profile < 專案層 envs/<env>/profile.json。"""
    env = env or DEFAULT_ENV
    data = (_read_json(repo, ref, f".deploy/envs/{env}/profile.json") if env else None) or {}
    return _merge(_merge(DEFAULT_PROFILE, read_environment_profile(env)), data)
