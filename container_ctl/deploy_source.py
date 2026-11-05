"""部署宣告來源解析（DESIGN-deploy-config-repo-20261003）。

設定 repo（`<config_dir>/<project>.git`）存在時，宣告從它讀；不存在才回退讀程式碼 Bare Repo 的
`.deploy/`（legacy）。設定 repo 存在但讀不到 ref 時不靜默回退，由呼叫端的 `_git_show` 報錯，避免讀到過時宣告。
"""

from __future__ import annotations

from pathlib import Path

from container_ctl.repo_reader import DEFAULT_GIT_REPOS_DIR, repo_path_for
from container_ctl import paths

DEFAULT_CONFIG_REPOS_DIR = paths.CONFIG_REPOS_DIR


def config_repo_path(project: str, repos_dir: str | None = None, config_dir: str | None = None) -> str:
    """設定 repo 路徑（不檢查是否存在）。給了 repos_dir 而沒給 config_dir 時，取其同層的 `.config_repos`（測試隔離）。"""
    if config_dir:
        root = Path(config_dir)
    elif repos_dir and str(repos_dir) != DEFAULT_GIT_REPOS_DIR:
        root = Path(repos_dir).parent / ".config_repos"
    else:
        root = Path(DEFAULT_CONFIG_REPOS_DIR)
    return str(root / f"{project}.git")


def resolve_deploy_repo(
    project: str,
    repos_dir: str | None = None,
    config_dir: str | None = None,
) -> tuple[str, str]:
    """回傳 (repo 路徑, kind)；kind 為 "config" 或 "legacy"。"""
    config = config_repo_path(project, repos_dir, config_dir)
    if Path(config).is_dir():
        return config, "config"
    legacy = repo_path_for(project, repos_dir) if repos_dir else repo_path_for(project)
    return legacy, "legacy"
