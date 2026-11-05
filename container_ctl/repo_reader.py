"""
TICKET-003: Bare Repo .deploy/ 讀取器
從 Bare Repo 透過 git show 讀取，禁止 checkout。
"""
import json
import subprocess
from pathlib import Path
from container_ctl import paths

DEFAULT_GIT_REPOS_DIR = paths.GIT_REPOS_DIR


class DeployConfigNotFoundError(Exception):
    pass


class PinnedDigestRequiredError(Exception):
    pass


def _git_show(repo_path: str, ref: str, file_path: str) -> str:
    """執行 git show <ref>:<file_path>，不執行 checkout。"""
    result = subprocess.run(
        ["git", "-C", repo_path, "show", f"{ref}:{file_path}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # 嘗試列出可用 ref
        refs_result = subprocess.run(
            ["git", "-C", repo_path, "tag", "--sort=-creatordate"],
            capture_output=True,
            text=True,
        )
        available = refs_result.stdout.strip()[:200] if refs_result.returncode == 0 else "(無法取得)"
        raise DeployConfigNotFoundError(
            f"無法讀取 {repo_path} 的 {ref}:{file_path}\n"
            f"stderr: {result.stderr.strip()}\n"
            f"可用 tags（最近）:\n{available}"
        )
    return result.stdout


def read_manifest(repo_path: str, ref: str) -> dict:
    """讀取並解析 .deploy/manifest.json，並進行基本 Schema 驗證與正規化。"""
    content = _git_show(repo_path, ref, ".deploy/manifest.json")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise DeployConfigNotFoundError(f"manifest.json 解析失敗: {e}")

    if not isinstance(data, dict):
        raise DeployConfigNotFoundError("manifest.json 格式錯誤：根節點必須是 JSON 物件 (dict)")

    project_name = data.get("project_name") or data.get("name")
    if not project_name:
        raise DeployConfigNotFoundError(
            "manifest.json 缺少必要欄位 'project_name'。"
            "請確認 .deploy/manifest.json 包含 'project_name'（或 'name'）。"
        )
    data["project_name"] = str(project_name)

    if not data.get("version"):
        raise DeployConfigNotFoundError(
            "manifest.json 缺少必要欄位 'version'。"
            "請確認 .deploy/manifest.json 包含 'version' 欄位（例如 'v1.0.0'）。"
        )

    if "dependencies" not in data and "containers" in data and isinstance(data["containers"], list):
        data["dependencies"] = [
            c["name"] if isinstance(c, dict) and "name" in c else str(c)
            for c in data["containers"]
        ]

    return data


def read_container_config(repo_path: str, ref: str, name: str, env: str | None = None) -> dict:
    """讀取 .deploy/containers/<name>.json（base）疊上環境 overlay，並驗證 Pinned Digest。

    env 預設 DEFAULT_ENV（未設定＝無環境，只用 base）；無 overlay 檔時結果即 base（ADR-009 P1）。
    """
    from container_ctl.env_overlay import DEFAULT_ENV, OverlayError, merge_overlay, overlay_path, read_overlay
    content = _git_show(repo_path, ref, f".deploy/containers/{name}.json")
    try:
        config = json.loads(content)
    except json.JSONDecodeError as e:
        raise DeployConfigNotFoundError(f"containers/{name}.json 解析失敗: {e}")
    try:
        overlay_env = env or DEFAULT_ENV
        config = merge_overlay(config, read_overlay(repo_path, ref, overlay_env, name), overlay_path(overlay_env, name))
    except OverlayError as e:
        raise DeployConfigNotFoundError(str(e)) from e

    image = config.get("image", "")
    if "@sha256:" not in image:
        raise PinnedDigestRequiredError(
            f"容器 '{name}' 的 image 必須包含 @sha256: Pinned Digest，"
            f"實際值: '{image}'。禁止使用 :latest。"
        )
    return config


def _service_tier(repo_path: str, ref: str, name: str) -> str | None:
    """取得服務 tier：優先 JSON "tier" 欄位，否則依 dev-/prod- 名稱前綴。"""
    try:
        tier = json.loads(_git_show(repo_path, ref, f".deploy/containers/{name}.json")).get("tier")
    except Exception:
        tier = None
    if tier in ("dev", "prod", "infra"):
        return tier
    for t in ("dev", "prod"):
        if name.startswith(f"{t}-"):
            return t
    return None


def list_container_configs(repo_path: str, ref: str, tier: str | None = None, env: str | None = None) -> list[dict]:
    """依 manifest.dependencies 順序讀取容器配置；tier 指定時僅讀取該 tier。"""
    manifest = read_manifest(repo_path, ref)
    configs = []
    for dep_name in manifest.get("dependencies", []):
        if tier and _service_tier(repo_path, ref, dep_name) != tier:
            continue
        config = read_container_config(repo_path, ref, dep_name, env)
        configs.append(config)
    return configs


def repo_path_for(project_name: str, repos_dir: str = DEFAULT_GIT_REPOS_DIR) -> str:
    """取得專案的 Bare Repo 完整路徑。"""
    path_with_ext = Path(repos_dir) / f"{project_name}.git"
    if path_with_ext.exists():
        return str(path_with_ext)
    path_without_ext = Path(repos_dir) / project_name
    if path_without_ext.exists():
        return str(path_without_ext)
    return str(path_with_ext)
