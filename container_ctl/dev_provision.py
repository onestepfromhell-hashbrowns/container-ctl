"""dev 部署前置：與 `prod up` 對等的三步（host prepare／secret provision／db provision），由 redeploy／apply 在 --yes 時自動呼叫。

只處理本次計畫內 tier=dev 的服務；全部沿用既有工具（冪等：只建缺少的、不覆寫、不輪替）。
宿主 agent 執行；容器內執行由 host_guard 拒絕。
"""
from __future__ import annotations

import sys


def run_dev_provision(project: str, ref: str, configs: list[dict], yes: bool) -> bool:
    """回傳 True 可繼續部署；False 應中止。"""
    dev = [c for c in configs if c.get("tier") == "dev"]
    if not dev:
        return True
    from container_ctl.cli.host_prepare import apply_actions, plan_host_prepare
    from container_ctl.secret_provision import apply_secret_provision, plan_secret_provision
    actions, errors = plan_host_prepare(dev)
    s_actions, s_msgs = plan_secret_provision(dev)
    from container_ctl.db_provision import has_declaration
    needs_db = any(k.endswith("_PG_PASSWORD_FILE") for c in dev for k in (c.get("env") or {})) and has_declaration(project)
    for m in errors + s_msgs:
        print(m, file=sys.stderr)
    if errors or any(m.startswith("[ERROR]") for m in s_msgs):
        print("[ABORT] dev 前置檢查有 [ERROR]，未動任何東西。", file=sys.stderr)
        return False
    pending = [a for a in s_actions if a["state"] == "missing"]
    if not yes:
        if actions or pending or needs_db:
            print(f"[INFO] dev 前置（--yes 時自動執行）：host prepare {len(actions)} 項、secret provision {len(pending)} 個檔"
                  + ("、db provision" if needs_db else ""), file=sys.stderr)
        return True
    ok = True
    if actions:
        for line in apply_actions(actions):
            print(line, file=sys.stderr)
            ok &= not line.startswith("[ERROR]")
    if ok and pending:
        for line in apply_secret_provision(s_actions):
            print(line, file=sys.stderr)
            ok &= not line.startswith("[ERROR]")
    if ok and needs_db:
        from container_ctl.db_provision import db_provision
        ok = db_provision(project, "dev", "apply") == 0
    if not ok:
        print("[ABORT] dev 前置失敗；修正後重跑（工具冪等，可直接重跑）。", file=sys.stderr)
    return ok
