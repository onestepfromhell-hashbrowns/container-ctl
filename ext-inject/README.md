# ext-inject

由 Extension Scope（`ext-system-git-repo`、`ext-system-network-proxy`、`biz-git-worktree-connect`）注入容器的輔助腳本：
- `git-repo-setup.sh`：容器內 Git 安全目錄與 `/workspace/.git` 參照初始化。
- `proxy-compat-setup.sh`：容器缺乏原生 `ss` 或 `socat` 時的 POSIX 相容補全腳本。
- `ide-compat-setup.sh` 位於 `ide-inject/ide/`。
