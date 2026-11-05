#!/bin/sh
# git-repo-setup.sh — 容器內專案 Git 倉庫環境初始化及 Worktree 自癒腳本
set -e

# 裸庫根目錄（同 container-ctl 的 CONTAINER_CTL_GIT_REPOS_DIR；預設同 container_ctl.paths.GIT_REPOS_DIR）
GIT_REPOS_DIR="${CONTAINER_CTL_GIT_REPOS_DIR:-${CONTAINER_CTL_DATA_ROOT:-/var/lib/container-ctl}/git-repos}"

# 1. 配置 Git 系統層級安全目錄，防止跨容器使用者權限問題 (dubious ownership)
if command -v git >/dev/null 2>&1; then
    git config --system --add safe.directory /workspace 2>/dev/null || true
    git config --system --add safe.directory '*' 2>/dev/null || true
fi

# 2. 檢查並自癒 /workspace/.git 工作區參照
if [ -f /workspace/.git ]; then
    gitdir_line=$(cat /workspace/.git 2>/dev/null || true)
    if [ -n "$gitdir_line" ]; then
        target_dir=$(echo "$gitdir_line" | sed -E 's/^gitdir: *//')
        # 若目標目錄在容器內不存在，但容器有掛載 /repo 或 /opt/ext/fhs/repo
        if [ ! -d "$target_dir" ]; then
            if [ -d /repo ]; then
                echo "gitdir: /repo" > /workspace/.git
            elif [ -d /opt/ext/fhs/repo ]; then
                echo "gitdir: /opt/ext/fhs/repo" > /workspace/.git
            elif [ -d "${GIT_REPOS_DIR}" ]; then
                :
            fi
        fi
    fi
fi

# 3. 確保 /workspace 綁定 origin 為中央 SSOT Bare Repo
if [ -d /workspace ] && command -v git >/dev/null 2>&1; then
    if [ -f /workspace/.git ] || [ -d /workspace/.git ]; then
        if ! git -C /workspace remote get-url origin >/dev/null 2>&1; then
            # 自動偵測可用的裸庫路徑
            repo_candidate=""
            if [ -n "$PROJECT_NAME" ] && [ -d "${GIT_REPOS_DIR}/${PROJECT_NAME}.git" ]; then
                repo_candidate="${GIT_REPOS_DIR}/${PROJECT_NAME}.git"
            elif [ -d "/repo" ]; then
                repo_candidate="/repo"
            else
                for r in "${GIT_REPOS_DIR}"/*.git; do
                    if [ -d "$r" ]; then
                        repo_candidate="$r"
                        break
                    fi
                done
            fi

            if [ -n "$repo_candidate" ]; then
                git -C /workspace remote add origin "$repo_candidate" 2>/dev/null || true
                current_branch=$(git -C /workspace rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
                git -C /workspace branch --set-upstream-to="origin/$current_branch" "$current_branch" 2>/dev/null || true
            fi
        fi
    fi
fi

exit 0
