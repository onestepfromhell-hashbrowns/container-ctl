# ExecSpec 介面（`container_ctl/exec_run.py`）

依據 ExecSpec v1/v2 契約。入口 `run_exec(spec) -> result`，不認識特定 agent 平台，不寫帳本。

- 加固:`--read-only`、`--cap-drop all`、`no-new-privileges`、`--init`、`--pids-limit 512`、`--pull=never`、記憶體上限、HOME/tmp/secrets 皆 tmpfs。
- 憑證:只經 stdin 進 tmpfs(檔案 0600、env 檔 0600),不進 argv。
- `egress` 只用名稱;名稱到網路的對應由呼叫端以 `networks` 傳入,未知名稱 fail closed(`EGRESS_UNKNOWN`)。
- 冪等:同一個 `exec_id` 已有容器時不啟動第二個(`EXEC_ID_IN_USE`)。
- 一定銷毀:無論成功、逾時或失敗,最後都 `podman rm -f`。
- `rw`:host 目錄複製兩份(a 原樣、b 可寫),容器只掛 b,結束後 git 格式 diff 產生 patch(`git apply -p1` 可用);host 目錄從不被改。拒絕符號連結、系統根目錄與超過 256 MiB 的來源;patch 超過 1 MiB 回 `PATCH_TOO_LARGE`,不回截斷的 patch。實測 `git apply --check` 通過。
- 實測(2026-10-29,rootless podman 5.7):rootfs 唯讀、憑證檔 0600、env 變數注入、`--network none` 只剩 lo、逾時回 `timeout`、結束後無殘留容器。
