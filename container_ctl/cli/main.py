#!/usr/bin/env python3
"""
container-ctl — 容器依賴編排與生命週期管理 CLI 入口
"""
import argparse
import os
from pathlib import Path
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="container-ctl",
        description="容器依賴編排與生命週期管理工具",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ── deploy ──────────────────────────────────────────────
    dp = sub.add_parser("deploy", help="部署專案容器依賴")
    dsub = dp.add_subparsers(dest="subcommand", required=True)

    plan_p = dsub.add_parser("plan", help="預覽部署計畫；會寫 Blueprint 並協調狀態 DB，不啟動容器")
    plan_p.add_argument("project", help="專案名稱")
    plan_p.add_argument("ref", help="Git ref（tag / commit / branch）")
    plan_p.add_argument("--output", choices=["table", "json"], default="table")
    plan_p.add_argument("--tier", choices=["dev", "prod", "infra"], default=None, help="僅處理指定 tier 的服務")

    apply_p = dsub.add_parser("apply", help="執行部署計畫；檢查映像 VOLUME 與明確掛載",
                              description="部署前檢查實際映像的 VOLUME；未在 volumes/tmpfs 宣告即中止。")
    apply_p.add_argument("project", help="專案名稱")
    apply_p.add_argument("ref", help="Git ref（tag / commit / branch）")
    apply_p.add_argument("--yes", "-y", action="store_true", help="跳過互動確認（agent 模式）")
    apply_p.add_argument("--tier", choices=["dev", "prod", "infra"], default=None,
                         help="僅處理指定 tier 的服務（其餘 tier 的宣告不讀、映像不檢）")

    stop_p = dsub.add_parser("stop", help="優雅停止專案服務")
    stop_p.add_argument("project", help="專案名稱")
    stop_p.add_argument("--yes", "-y", action="store_true", help="跳過確認")

    destroy_p = dsub.add_parser("destroy", help="停止並移除 Quadlet 單元")
    destroy_p.add_argument("project", help="專案名稱")
    destroy_p.add_argument("--yes", "-y", action="store_true", help="跳過確認")

    redeploy_p = dsub.add_parser("redeploy", help="依整份 manifest 重生成單元；檢查映像 VOLUME 與明確掛載",
                                 description="重部署前檢查實際映像的 VOLUME；未在 volumes/tmpfs 宣告即中止。")
    redeploy_p.add_argument("project", help="專案名稱")
    redeploy_p.add_argument("ref", help="新 Git ref（支持回滚）")
    redeploy_p.add_argument("--service", action="append", dest="services", metavar="NAME",
                            help="只處理指定服務（可重複）；其餘服務不比對、不生成、不重啟")
    redeploy_p.add_argument("--tier", choices=["dev", "prod", "infra"], default=None,
                            help="僅處理指定 tier 的服務（其餘 tier 的宣告不讀）")
    redeploy_p.add_argument("--yes", "-y", action="store_true", help="跳過確認")

    migrate_p = dsub.add_parser("migrate-postgres", help="備份並離線遷移未納管 PostgreSQL 至 prod Quadlet；失敗回復原容器")
    migrate_p.add_argument("project")
    migrate_p.add_argument("ref")
    migrate_p.add_argument("--service", required=True)
    migrate_p.add_argument("--source-container", required=True)
    migrate_p.add_argument("--yes", action="store_true")

    verify_p = dsub.add_parser("verify", help="核對 prod 錨定、Quadlet、實際掛載、healthy 與開機依賴，保存驗收紀錄")
    verify_p.add_argument("project")
    verify_p.add_argument("ref")
    verify_p.add_argument("--service", action="append", dest="services")

    # ── status ──────────────────────────────────────────────
    sp = sub.add_parser("status", help="查詢實例狀態")
    ssub = sp.add_subparsers(dest="subcommand", required=False)

    list_p = ssub.add_parser("list", help="列出所有實例")
    list_p.add_argument("--tier", choices=["infra", "prod", "dev"], help="依 tier 過濾")
    list_p.add_argument("--output", choices=["table", "json"], default="table")

    show_p = ssub.add_parser("show", help="顯示指定專案詳細狀態")
    show_p.add_argument("project", help="專案名稱")

    # ── service lifecycle ───────────────────────────────────
    svc_p = sub.add_parser(
        "service",
        help="操作單一既有 manual 實例；不重生成 Quadlet 或處理整份 manifest",
    )
    svc_sub = svc_p.add_subparsers(dest="subcommand", required=True)
    for operation in ("start", "restart"):
        op_p = svc_sub.add_parser(
            operation,
            help=f"{operation} 一個既有 manual 實例（需指定精確 container name）",
            description=(
                f"只操作指定的既有 manual 實例；檢查 state.db、Quadlet/symlink、"
                "pinned image 與治理准入，不重生成 Quadlet，也不讀取/部署整份 manifest。"
            ),
        )
        op_p.add_argument("project", help="專案名稱")
        op_p.add_argument("service", help="state.db 中的服務名稱")
        op_p.add_argument("--container-name", required=True, help="state.db 中精確的實例容器名稱")
        op_p.add_argument("--yes", "-y", action="store_true", help="跳過操作確認")
        op_p.add_argument("--with-app", action="store_true",
                          help="容器 active 後執行宣告的 app_hooks.start 並以 readiness_probe 確認")

    image_p = sub.add_parser("image", help="從 Bare Repo 準備映像與 pinned 宣告")
    image_sub = image_p.add_subparsers(dest="subcommand", required=True)
    prepare_p = image_sub.add_parser(
        "prepare-dev",
        help="由乾淨 Git ref 建置 dev 映像，輸出 pinned JSON 與來源紀錄",
        description=(
            "從 Bare Repo 指定 ref 的 Git archive 擷取隔離建置 context；"
            "產出待提交的 .deploy/containers/<service>.json 與 receipt.json。"
            "不修改 Bare Repo、Quadlet 或運行容器。"
        ),
    )
    prepare_p.add_argument("project", help="專案名稱")
    prepare_p.add_argument("service", help="manifest dependencies 中的 dev 服務名稱")
    prepare_p.add_argument("ref", help="Git ref；工具先解析為不可變 commit")
    prepare_p.add_argument("--output-dir", required=True, help="存放待審核宣告與 receipt 的目錄")
    prepare_p.add_argument("--pull-policy", choices=["never", "missing", "always"], default="never",
                           help="Podman 基底映像拉取策略（預設 never）")
    publish_p = image_sub.add_parser(
        "publish-dev",
        help="驗證 prepare-dev 產物，僅提交 image pin 並推送指定 Bare Repo 分支",
        description="比對來源 commit、JSON 唯一變更與映像標籤；候選提交 validate 通過後才非強制推送。",
    )
    publish_p.add_argument("artifact_dir", help="prepare-dev 產出的資料夾")
    publish_p.add_argument("--branch", required=True, help="來源 commit 所在的 Bare Repo 分支")
    publish_p.add_argument("--yes", "-y", action="store_true", help="跳過發布確認")
    prod_p = image_sub.add_parser(
        "prepare-prod",
        help="由乾淨 Git commit 建置 prod 映像（Dockerfile.prod），輸出 pinned JSON 與 receipt",
        description="不寫 tag、不修改 Bare Repo 或容器。release tag 已存在則拒絕（同版本內容凍結）。",
    )
    prod_p.add_argument("project", help="專案名稱")
    prod_p.add_argument("service", help="manifest dependencies 中的 prod 服務名稱")
    prod_p.add_argument("version", help="要凍結的版本號")
    prod_p.add_argument("ref", help="Git ref；工具先解析為不可變 commit")
    prod_p.add_argument("--output-dir", required=True, help="存放 pinned 宣告與 receipt 的目錄")
    prod_p.add_argument("--pull-policy", choices=["never", "missing", "always"], default="never",
                        help="Podman 基底映像拉取策略（預設 never）")
    pubprod_p = image_sub.add_parser(
        "publish-prod",
        help="驗證 prepare-prod 產物後，在 Bare Repo 寫入 release/<service>/<version> tag",
        description="只寫 annotated tag（digest＋source commit）；拒絕覆寫既有 tag。宣告檔 pin 另行提交。",
    )
    pubprod_p.add_argument("artifact_dir", help="prepare-prod 產出的資料夾")
    pubprod_p.add_argument("--yes", "-y", action="store_true", help="跳過發布確認")
    adopt_p = image_sub.add_parser(
        "adopt-prod",
        help="補登運行中的舊 prod 版本：只寫 release tag（adopted=true）",
        description="宣告須已 pin digest，且運行容器映像等於該 digest；不建置、不啟停容器。",
    )
    adopt_p.add_argument("project", help="專案名稱")
    adopt_p.add_argument("service", help="prod 服務名稱")
    adopt_p.add_argument("version", help="運行中的版本號")
    adopt_p.add_argument("ref", help="宣告所在的 Git ref（tag 指向其解析出的 commit）")
    adopt_p.add_argument("--yes", "-y", action="store_true", help="跳過確認")
    auditprod_p = image_sub.add_parser(
        "audit-prod",
        help="唯讀比對 release tag、Bare 宣告、state.db 與運行容器的映像 digest",
        description="任一不符即以非零狀態結束；不啟停或修改容器。",
    )
    auditprod_p.add_argument("project", help="專案名稱")
    auditprod_p.add_argument("service", help="prod 服務名稱")
    auditprod_p.add_argument("version", help="版本號")
    auditprod_p.add_argument("ref", help="Bare Repo Git ref")
    verify_p = image_sub.add_parser("verify", help="唯讀：release 的 manifest digest 對 OCI store（重算 blob 雜湊）")
    verify_p.add_argument("project", help="專案名稱")
    verify_p.add_argument("service", help="prod 服務名稱")
    verify_p.add_argument("version", help="版本號")
    verify_p.add_argument("--require-signature", action="store_true", help="release 記錄須有可驗證的 cosign 簽章，否則失敗")
    audit_p = image_sub.add_parser(
        "audit-dev",
        help="唯讀比對 Bare pin、state.db、Quadlet、systemd 與運行容器映像",
        description="發現任何映像或狀態漂移即以非零狀態結束；不啟停或修改容器。",
    )
    audit_p.add_argument("project", help="專案名稱")
    audit_p.add_argument("service", help="dev 服務名稱")
    audit_p.add_argument("ref", help="Bare Repo Git ref")

    config_p = sub.add_parser("config", help="部署設定 repo（.config_repos）管理")
    config_sub = config_p.add_subparsers(dest="subcommand", required=True)
    cinit_p = config_sub.add_parser("init", help="由程式碼 Bare Repo 的 .deploy 建立設定 repo（不改既有 repo／容器）")
    cinit_p.add_argument("project", help="專案名稱")
    cinit_p.add_argument("--ref", default="HEAD", help="程式碼 Bare Repo 的 ref（預設 HEAD）")
    cstat_p = config_sub.add_parser("status", help="唯讀比對設定 repo、程式碼 repo 與工作樹的 .deploy")
    cstat_p.add_argument("project", help="專案名稱")
    cchk_p = config_sub.add_parser("check", help="唯讀：以 .deploy/contract.json 驗證指定環境的有效宣告")
    cchk_p.add_argument("project", help="專案名稱")
    cchk_p.add_argument("--env", dest="env_name", default=None, metavar="ENV", help="環境（預設：CONTAINER_CTL_ENV；未設定則只用 base）")
    cchk_p.add_argument("--ref", default="HEAD", help="設定 repo ref")
    cshow_p = config_sub.add_parser("show", help="唯讀：印出 base ⊕ 環境 overlay 的有效宣告")
    cshow_p.add_argument("project", help="專案名稱")
    cshow_p.add_argument("service", help="服務名稱")
    cshow_p.add_argument("--env", dest="env_name", default=None, metavar="ENV", help="環境（預設：CONTAINER_CTL_ENV；未設定則只用 base）")
    cset_p = config_sub.add_parser("set", help="修改設定 repo 中服務的 env 或 volumes（驗證→預覽→提交→稽核；不重啟容器）")
    cset_p.add_argument("project", help="專案名稱")
    cset_p.add_argument("service", help="服務名稱")
    cset_p.add_argument("assignments", nargs="*", metavar="KEY=VALUE", help="要設定的 env")
    cset_p.add_argument("--unset", action="append", default=[], metavar="KEY", help="移除 env 鍵（可重複）")
    cset_p.add_argument("--volume", action="append", default=[], metavar="CPATH=HOST[:OPTS]",
                        help="以 container_path 為鍵設定掛載：已存在則換 host_path（保留原 options，除非另給），不存在則新增（可重複）")
    cset_p.add_argument("--unset-volume", action="append", default=[], metavar="CPATH", help="移除該 container_path 的掛載（可重複）")
    cset_p.add_argument("--env", dest="env_name", default=None, metavar="ENV", help="寫入環境 overlay（.deploy/envs/<ENV>/）；省略且無 overlay 時寫 base")
    cset_p.add_argument("--reason", default="", help="寫入提交與稽核紀錄的理由")
    cset_p.add_argument("--yes", action="store_true", help="確認提交（未帶只預覽）")

    csync_p = config_sub.add_parser("sync-image-context", help="把程式碼 repo 指定 ref 的 .deploy/dev-images/<服務>/ 同步進設定 repo（驗證→預覽→提交→稽核；不建置）")
    csync_p.add_argument("project", help="專案名稱")
    csync_p.add_argument("service", help="dev 服務名稱")
    csync_p.add_argument("--from-ref", required=True, metavar="REF", help="程式碼 Bare Repo 的 ref（必填，不給預設以免誤同步）")
    csync_p.add_argument("--reason", default="", help="寫入提交與稽核紀錄的理由")
    csync_p.add_argument("--yes", action="store_true", help="確認提交（未帶只預覽）")

    # ── rollout / reconcile（ADR-009 P4）─────────────────────
    ro_p = sub.add_parser("rollout", help="把設定 repo 現行宣告套用到宿主；紀錄、回滾（apply／status／history／undo）")
    ro_sub = ro_p.add_subparsers(dest="rollout_cmd", required=True)
    for _name, _help in (("apply", "套用現行宣告（改 env／埠等配置的正規路徑；預設預覽）"),
                         ("status", "最近的 rollout 紀錄"), ("history", "同 status"),
                         ("undo", "回到最近一個實際健康的 rollout（預設預覽）")):
        _p = ro_sub.add_parser(_name, help=_help)
        _p.add_argument("project", help="專案名稱")
        _p.add_argument("service", help="prod 服務名稱")
        if _name == "apply":
            _p.add_argument("--ref", default="HEAD")
            _p.add_argument("--env", default=None, metavar="ENV", help="環境（預設：CONTAINER_CTL_ENV；未設定則只用 base）")
        if _name in ("apply", "undo"):
            _p.add_argument("--yes", action="store_true", help="確認執行（未帶只預覽）")
            _p.add_argument("--timeout", type=int, default=180, help="等待 healthy 的秒數")
        if _name == "undo":
            _p.add_argument("--to", type=int, default=None, metavar="ID", help="指定 rollout id（須為 healthy）")
            _p.add_argument("--mode", choices=["both", "image", "config"], default="both", help="還原範圍（預設整份宣告）")
    rc_p = sub.add_parser("reconcile", help="唯讀：desired（設定 repo＋release）vs actual（podman）漂移報告")
    rc_p.add_argument("project", help="專案名稱")
    rc_p.add_argument("service", help="prod 服務名稱")
    rc_p.add_argument("--ref", default="HEAD")
    rc_p.add_argument("--env", default=None, metavar="ENV")

    # ── db ──────────────────────────────────────────────────
    db_p = sub.add_parser("db", help="資料庫管理")
    dbsub = db_p.add_subparsers(dest="subcommand", required=True)
    dbsub.add_parser("init", help="初始化 state.db（冪等）")

    # ── backup ─────────────────────────────────────────────
    bp = sub.add_parser("backup", help="備份狀態 DB 或容器 Volume")
    bsub = bp.add_subparsers(dest="subcommand", required=True)
    bsub.add_parser("state", help="備份 state.db（SQLite .backup API）")
    bvol_p = bsub.add_parser("volumes", help="tar 打包容器 Volume")
    bvol_p.add_argument("project", help="專案名稱，或 'all' 備份所有")

    # ── query ───────────────────────────────────────────────
    qp = sub.add_parser("query", help="查詢工具（供方案 3 整合使用）")
    qsub = qp.add_subparsers(dest="subcommand", required=True)
    cn_p = qsub.add_parser("container-name", help="查詢容器的實際名稱")
    cn_p.add_argument("project", help="專案名稱")
    cn_p.add_argument("service", help="服務名稱")

    # ── wsl-shutdown / wsl-restart ───────────────────────────────────
    ws_p = sub.add_parser("wsl-shutdown", help="安全關閉 WSL（停止所有容器 + 備份）")
    ws_p.add_argument("--yes", "-y", action="store_true", help="跳過互動確認（agent 模式）")

    wr_p = sub.add_parser("wsl-restart", help="安全重啟 WSL（停止所有容器 + 備份）")
    wr_p.add_argument("--yes", "-y", action="store_true", help="跳過互動確認（agent 模式）")

    # ── validate ──────────────────────────────────────────
    val_p = sub.add_parser("validate", help="預檢 Manifest、掛載與本機映像 VOLUME（映像缺失時警告；零副作用）",
                           description="唯讀檢查 Bare Repo 宣告、明確掛載與本機映像 VOLUME。映像未在本機時提出警告；apply/redeploy 會中止。")
    val_p.add_argument("project", help="專案名稱")
    val_p.add_argument("ref", nargs="?", default="HEAD", help="Git ref（預設 HEAD）")
    val_p.add_argument("--output", choices=["table", "json"], default="table")
    val_p.add_argument("--quiet", "-q", action="store_true")
    val_p.add_argument("--tier", choices=["dev", "prod"], default=None, help="僅驗證指定 tier 的服務")

    # ── prod ─────────────────────────────────────────────────
    prod_p = sub.add_parser("prod", help="prod 部署流程工具")
    prod_sub = prod_p.add_subparsers(dest="subcommand", required=True)
    doctor_p = prod_sub.add_parser(
        "doctor", help="唯讀體檢：一次列出 prod 部署前的全部阻塞與修法",
        description="重用 validate 並加上 prod 啟動規則（env 空值、相對路徑、宿主目錄與 secret 擁有者、release 錨定）。不連 DB、不讀 secret 內容。")
    doctor_p.add_argument("project", help="專案名稱")
    doctor_p.add_argument("ref", nargs="?", default="HEAD", help="Git ref（預設 HEAD）")
    doctor_p.add_argument("--service", action="append", help="只檢查指定 prod 服務（可重複）")
    doctor_p.add_argument("--output", choices=["table", "json"], default="table")

    sec_p = sub.add_parser("secret", help="機密檔供應（由工具產生，agent 不碰內容）")
    sec_sub = sec_p.add_subparsers(dest="subcommand", required=True)
    sprov_p = sec_sub.add_parser(
        "provision", help="依宣告 secrets[] 產生缺少的 password/token 檔（只建不覆寫，內容不輸出；預設預覽）")
    sprov_p.add_argument("project", help="專案名稱")
    sprov_p.add_argument("ref", nargs="?", default="HEAD", help="Git ref（預設 HEAD）")
    sprov_p.add_argument("--service", action="append", help="只處理指定服務（可重複）")
    sprov_p.add_argument("--tier", choices=["dev", "prod"], default="prod")
    sprov_p.add_argument("--apply", action="store_true", help="實際產生（預設僅預覽）")

    dprov_p = dbsub.add_parser(
        "provision", help="依消費者 .deploy/pg-hub.json 發放 database／角色／密碼檔（預設 dry-run）",
        description="委派 PG-Hub bin/create-service-db；密碼由工具產生、寫檔、chown，不輸出。已存在的密碼檔不覆寫。")
    dprov_p.add_argument("project", help="消費者專案名稱")
    dprov_p.add_argument("--env", choices=["dev", "prod"], required=True)
    dprov_p.add_argument("--mode", choices=["render", "dry-run", "apply"], default="dry-run")

    dsnap_p = dbsub.add_parser("snapshot", help="升級前備份：pg_dump -Fc＋globals＋MANIFEST（.backups/<P>/<ts>/，0600）")
    dsnap_p.add_argument("project")
    dsnap_p.add_argument("--env", choices=["dev", "prod"], default="prod")
    dsnap_p.add_argument("--reason", default="manual")
    ddrill_p = dbsub.add_parser("restore-drill", help="把最新快照還原到用後即棄 PG（同映像、無網路），套用待遷移並比對列數")
    ddrill_p.add_argument("project")
    ddrill_p.add_argument("--env", choices=["dev", "prod"], default="prod")
    ddrill_p.add_argument("--snapshot", help="指定快照目錄（預設最新）")
    ddrill_p.add_argument("--code-ref", default="HEAD", help="migration 來源的程式碼 ref／tag")
    dmig_p = dbsub.add_parser("migrate", help="依 .deploy/db-migrate.json 套用待遷移 SQL（預設只列計畫；--apply 先快照再套用）")
    dmig_p.add_argument("project")
    dmig_p.add_argument("--env", choices=["dev", "prod"], default="prod")
    dmig_p.add_argument("--code-ref", default="HEAD")
    dmig_p.add_argument("--apply", action="store_true")
    dmig_p.add_argument("--no-snapshot", action="store_true")

    host_p = sub.add_parser("host", help="宿主環境準備")
    host_sub = host_p.add_subparsers(dest="subcommand", required=True)
    hprep_p = host_sub.add_parser(
        "prepare", help="依宣告建立缺少的宿主目錄、巢狀掛載點並對映 uid（預設預覽）",
        description="只新建、不刪、不改既有目錄與檔案；加 --apply 才執行。完成後以 prod doctor 複查。")
    hprep_p.add_argument("project", help="專案名稱")
    hprep_p.add_argument("ref", nargs="?", default="HEAD", help="Git ref（預設 HEAD）")
    hprep_p.add_argument("--service", action="append", help="只處理指定服務（可重複）")
    hprep_p.add_argument("--tier", choices=["dev", "prod"], default="prod")
    hprep_p.add_argument("--apply", action="store_true", help="實際執行（預設僅預覽）")
    hprep_p.add_argument("--output", choices=["table", "json"], default="table")

    hchk_p = host_sub.add_parser(
        "check", help="唯讀：對照環境 policy.json 檢查已宣告的 Quadlet 單元",
        description="讀 .environments/<env>/{profile,policy}.json 與 quadlet 目錄；advisory 強度下違規只 WARN，--strict 才回非 0。")
    hchk_p.add_argument("--env", help="環境名稱（預設 CONTAINER_CTL_ENV；host check 必須有環境）")
    hchk_p.add_argument("--units-dir", help="覆寫 Quadlet 目錄（預設取 profile.host.quadlet_dir）")
    hchk_p.add_argument("--output", choices=["table", "json"], default="table")
    hchk_p.add_argument("--strict", action="store_true", help="任何違規回非 0")
    hchk_p.add_argument("--verbose", action="store_true", help="列出全部違規單元（預設每條規則只列前 3 個）")

    up_p = prod_sub.add_parser(
        "up", help="單一指令完成 prod 服務部署（doctor→host prepare→prepare→publish→redeploy→health→verify）",
        description="未帶 --yes 只做體檢與預覽；帶 --yes 才建置、發布 release tag、部署。可重跑續做。")
    up_p.add_argument("project", help="專案名稱")
    up_p.add_argument("service", help="prod 服務名稱")
    up_p.add_argument("--ver", required=True, help="版本號（release tag 版本）")
    up_p.add_argument("--code-ref", default="HEAD", help="程式碼 repo 的建置 ref（預設 HEAD）")
    up_p.add_argument("--ref", default="HEAD", help="設定 repo ref（預設 HEAD）")
    up_p.add_argument("--yes", action="store_true", help="實際執行（預設僅預覽）")
    up_p.add_argument("--timeout", type=int, default=180, help="等待 healthy 秒數")
    up_p.add_argument("--rollback-on-fail", action="store_true", help="redeploy 後 health/smoke/verify 失敗時，自動回到前一個已發布版本")
    up_p.add_argument("--no-snapshot", action="store_true", help="略過升級前資料庫快照（不建議；僅在已手動備份時）")
    rb_p = prod_sub.add_parser(
        "rollback", help="回到已發布的舊版（向前提交 image pin，redeploy→health→smoke→verify；預設預覽）",
        description="把設定 repo 的 image/version 換回 release/<service>/<ver> 凍結內容並以新 commit 推進；不改寫歷史、不刪映像。")
    rb_p.add_argument("project", help="專案名稱")
    rb_p.add_argument("service", help="prod 服務名稱")
    rb_p.add_argument("--to", required=True, help="目標版本（須已有 release tag）")
    rb_p.add_argument("--ref", default="HEAD", help="設定 repo ref（預設 HEAD）")
    rb_p.add_argument("--yes", action="store_true", help="實際執行（預設僅預覽）")
    rb_p.add_argument("--timeout", type=int, default=180, help="等待 healthy 秒數")

    # ── cgroup-sync ──────────────────────────────────────────
    cs_p = sub.add_parser("cgroup-sync", help="跨專案同步 Quadlet Cgroup 限制；預設重啟掃描到的 active 服務")
    cs_p.add_argument("--no-restart", action="store_true", help="僅同步 Quadlet 檔案，不重啟 active 服務")
    cs_p.add_argument("--dry-run", action="store_true", help="預覽變更但不寫入")
    cs_p.add_argument("--policy", help="容量政策設定檔路徑（選填）")
    cs_p.add_argument("--services-dir", help="自訂 services 目錄（選填）", default=None)
    cs_p.add_argument("--quadlet-dir", help="自訂 systemd quadlet 掃描目錄（選填）", default=None)

    # ── portfwd ──────────────────────────────────────────
    pf_p = sub.add_parser("portfwd", help="把 dev 容器 127.0.0.1:<port> 暫時中繼到宿主機網卡（socat；不經容器）")
    pf_sub = pf_p.add_subparsers(dest="subcommand", required=True)
    pf_open = pf_sub.add_parser("open", help="開啟中繼（systemd --user 暫時性 unit，TTL 到期自動關閉）")
    pf_open.add_argument("instance", nargs="?", default=None, help="目標實例或容器名稱（例：dev-app；可省略 --to 與 --listen 自動由容器宣告解析）")
    pf_open.add_argument("--to", default=None, help="目標，僅限 127.0.0.1:<port>（dev；未指定時由宣告解析）")
    pf_open.add_argument("--listen", default=None, type=int, help="宿主機監聽埠（9000-9499；未指定時由宣告解析）")
    pf_open.add_argument("--instance", "-i", dest="opt_instance", default=None, help="目標實例或容器名稱（例：dev-app）")
    pf_open.add_argument("--reason", required=True, help="用途（寫入操作紀錄）")
    pf_open.add_argument("--ttl", default="2h", help="存活時間，例 30m、2h、8h（預設 2h，上限 24h）")
    pf_open.add_argument("--bind", default=None, help="綁定的網卡 IP（預設 eth0；禁止 0.0.0.0）")
    pf_open.add_argument("--allow", default=None, help="可選：僅允許此來源 CIDR（WSL2 經 portproxy 時看到的是 Windows 端 IP）")
    pf_open.add_argument("--lan", default=None, help="可選：區網來源 CIDR；只用於產生 Windows 防火牆 remoteip= 指令（socat 看不到手機真實 IP）")
    pf_sub.add_parser("list", help="列出目前的中繼")
    pf_sub.add_parser("next", help="掃描 .services 已宣告的 portfwd.listen，推薦下一個可用埠號（9000-9499）")
    pf_sub.add_parser("check", help="防撞檢查：同一 portfwd.listen 被多個容器宣告時回傳非 0")
    pf_close = pf_sub.add_parser("close", help="關閉中繼")
    pf_close.add_argument("instance", nargs="?", default=None, help="目標實例或容器名稱（未指定 --listen 時由宣告解析）")
    pf_close.add_argument("--listen", default=None, type=int, help="宿主機監聽埠（可由實例名稱解析）")
    pf_close.add_argument("--instance", "-i", dest="opt_instance", default=None, help="目標實例或容器名稱")
    pf_stop = pf_sub.add_parser("_stopped", help=argparse.SUPPRESS)
    pf_stop.add_argument("--listen", required=True, type=int)
    pf_stop.add_argument("--instance", required=True)
    pf_stop.add_argument("--target", required=True)
    pf_stop.add_argument("--bind", required=True)

    # ── menu ─────────────────────────────────────────────
    sub.add_parser("menu", help="互動式主選單")

    return p


def _setup_xdg() -> None:
    """
    TICKET-022: 在 CLI 入口統一設定 XDG 隔離，所有子指令繼承。
    """
    uid = os.getuid()
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/tmp/run-{os.environ.get('USER', uid)}")
    os.environ.setdefault("CONTAINER_HOST", f"unix:///run/user/{uid}/podman/podman.sock")

    # 驗證 /run/user/<uid> 權限為 710
    run_user = f"/run/user/{uid}"
    from pathlib import Path
    p = Path(run_user)
    if p.exists():
        mode = oct(p.stat().st_mode)[-3:]
        if mode != "700" and mode != "710":
            print(
                f"[WARN] {run_user} 權限為 {mode}，建議執行："
                f" chmod 710 {run_user}",
                file=sys.stderr,
            )


def main() -> None:
    _setup_xdg()
    parser = build_parser()
    args = parser.parse_args()
    from container_ctl.host_guard import refuse_in_container
    refuse_in_container()

    if args.command == "deploy":
        from container_ctl.cli.deploy import apply, destroy, plan, redeploy, stop
        if args.subcommand == "plan":
            plan(args.project, args.ref, output_json=(args.output == "json"), tier=args.tier)
        elif args.subcommand == "apply":
            apply(args.project, args.ref, yes=args.yes, tier=args.tier)
        elif args.subcommand == "stop":
            stop(args.project, yes=args.yes)
        elif args.subcommand == "destroy":
            destroy(args.project, yes=args.yes)
        elif args.subcommand == "redeploy":
            redeploy(args.project, args.ref, yes=args.yes, only=args.services, tier=args.tier)
        elif args.subcommand == "migrate-postgres":
            from container_ctl.pg_migration import migrate_postgres
            from container_ctl.cli.image import ImagePrepareError
            try:
                migrate_postgres(args.project, args.ref, args.service, args.source_container, yes=args.yes)
            except ImagePrepareError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                sys.exit(1)
        elif args.subcommand == "verify":
            from container_ctl.prod_verify import verify_prod
            from container_ctl.cli.image import ImagePrepareError
            try:
                verify_prod(args.project, args.ref, args.services)
            except ImagePrepareError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "status":
        from container_ctl.cli.status import list_instances, show_instance
        sub = getattr(args, "subcommand", None) or "list"
        if sub == "list":
            list_instances(
                tier=getattr(args, "tier", None),
                output_json=(getattr(args, "output", "table") == "json"),
            )
        elif sub == "show":
            show_instance(args.project)

    elif args.command == "service":
        from container_ctl.cli.service import operate_manual_instance
        operate_manual_instance(
            args.subcommand,
            args.project,
            args.service,
            args.container_name,
            yes=args.yes,
            with_app=args.with_app,
        )

    elif args.command == "image":
        from container_ctl.cli.image import ImagePrepareError, audit_dev_image, prepare_dev_image, publish_dev_image
        try:
            if args.subcommand == "prepare-dev":
                artifact = prepare_dev_image(
                    args.project, args.service, args.ref, args.output_dir,
                    pull_policy=args.pull_policy,
                )
                print(f"[OK] 已準備 dev 映像與待提交宣告：{artifact}")
            elif args.subcommand == "publish-dev":
                commit = publish_dev_image(args.artifact_dir, args.branch, yes=args.yes)
                print(f"[OK] 已推送 pinned 宣告：{commit}")
            elif args.subcommand == "prepare-prod":
                from container_ctl.cli.image_prod import prepare_prod_image
                artifact = prepare_prod_image(
                    args.project, args.service, args.version, args.ref, args.output_dir,
                    pull_policy=args.pull_policy,
                )
                print(f"[OK] 已準備 prod 映像與待提交宣告：{artifact}")
            elif args.subcommand == "publish-prod":
                from container_ctl.cli.image_prod import publish_prod_image
                print(f"[OK] 已寫入 {publish_prod_image(args.artifact_dir, yes=args.yes)}")
            elif args.subcommand == "adopt-prod":
                from container_ctl.cli.image_prod import adopt_prod_release
                print(f"[OK] 已補登 {adopt_prod_release(args.project, args.service, args.version, args.ref, yes=args.yes)}")
            elif args.subcommand == "verify":
                from container_ctl.cli.image_prod import verify_release_artifact
                ok, messages = verify_release_artifact(args.project, args.service, args.version, require_signature=args.require_signature)
                for message in messages:
                    print(message)
                if not ok:
                    sys.exit(1)
            elif args.subcommand == "audit-prod":
                from container_ctl.cli.image_prod import audit_prod_image
                ok, messages = audit_prod_image(args.project, args.service, args.version, args.ref)
                for message in messages:
                    print(message)
                if not ok:
                    sys.exit(1)
            elif args.subcommand == "audit-dev":
                ok, messages = audit_dev_image(args.project, args.service, args.ref)
                for message in messages:
                    print(message)
                if not ok:
                    sys.exit(1)
        except ImagePrepareError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "config":
        from container_ctl.cli.config import _parse_assignments, config_init, config_set, config_status
        from container_ctl.cli.image import ImagePrepareError
        try:
            if args.subcommand == "init":
                print(f"[OK] 已建立設定 repo：{config_init(args.project, args.ref)}")
            elif args.subcommand == "check":
                from container_ctl.cli.config import config_check
                ok, messages = config_check(args.project, args.env_name, args.ref)
                for message in messages:
                    print(message)
                if not ok:
                    sys.exit(1)
            elif args.subcommand == "show":
                from container_ctl.cli.config import config_show
                print(config_show(args.project, args.service, args.env_name))
            elif args.subcommand == "sync-image-context":
                from container_ctl.cli.config import sync_image_context
                _ok, messages = sync_image_context(args.project, args.service, args.from_ref, yes=args.yes, reason=args.reason)
                for message in messages:
                    print(message)
            elif args.subcommand == "set":
                if not (args.assignments or args.unset or args.volume or args.unset_volume):
                    raise ImagePrepareError("至少給一個 KEY=VALUE、--unset KEY、--volume 或 --unset-volume")
                from container_ctl.cli.config import _parse_volume
                parsed = [_parse_volume(v) for v in args.volume]
                _ok, messages = config_set(args.project, args.service, _parse_assignments(args.assignments),
                                           args.unset, vol_sets={c: (h, o) for c, h, o in parsed}, vol_unsets=args.unset_volume,
                                           yes=args.yes, reason=args.reason, env_name=args.env_name)
                for message in messages:
                    print(message)
            else:
                ok, messages = config_status(args.project)
                for message in messages:
                    print(message)
                if not ok:
                    sys.exit(1)
        except ImagePrepareError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "db":
        if args.subcommand == "init":
            from container_ctl.db import init_db
            conn = init_db()
            conn.close()
            print("[OK] state.db 初始化完成。")
        elif args.subcommand == "provision":
            from container_ctl.db_provision import db_provision
            sys.exit(db_provision(args.project, args.env, args.mode))
        elif args.subcommand in ("snapshot", "restore-drill", "migrate"):
            from container_ctl import db_upgrade as _u
            try:
                if args.subcommand == "snapshot":
                    _u.snapshot(args.project, args.env, reason=args.reason)
                elif args.subcommand == "restore-drill":
                    sys.exit(_u.restore_drill(args.project, args.env, __import__('pathlib').Path(args.snapshot) if args.snapshot else None, args.code_ref))
                else:
                    sys.exit(_u.migrate(args.project, args.env, args.apply, args.code_ref, not args.no_snapshot))
            except _u.UpgradeError as e:
                print(f"[ERROR] {e}")
                sys.exit(1)

    elif args.command == "query":
        if args.subcommand == "container-name":
            from container_ctl.cli.status import query_container_name
            name = query_container_name(args.project, args.service)
            if name:
                print(name)
            else:
                print(f"[ERROR] 找不到 {args.project}/{args.service} 的容器", file=sys.stderr)
                sys.exit(1)


    elif args.command == "backup":
        from container_ctl.backup import backup_state, backup_volumes
        if args.subcommand == "state":
            backup_state()
        elif args.subcommand == "volumes":
            backup_volumes(args.project)

    elif args.command == "portfwd":
        from container_ctl import portfwd
        try:
            if args.subcommand == "open":
                target_inst = getattr(args, "opt_instance", None) or args.instance
                d = portfwd.open_forward(
                    to=args.to,
                    listen=args.listen,
                    reason=args.reason,
                    ttl=args.ttl,
                    bind=args.bind,
                    allow=args.allow,
                    lan=args.lan,
                    instance=target_inst,
                    actor=os.environ.get("USER", "host_portfwd"),
                )
                print(f"[OK] 中繼已開啟：{d['bind']}:{d['listen']} -> {d['target']}（TTL {d['ttl_sec']}s，unit {d['unit']}）")
                print(portfwd.windows_hint(d["bind"], d["listen"], d["lan"]))
            elif args.subcommand == "list":
                print(portfwd.list_forwards() or "(無)")
            elif args.subcommand == "next":
                print(portfwd.get_next_available_port())
            elif args.subcommand == "check":
                conflicts = portfwd.find_listen_conflicts()
                if conflicts:
                    raise portfwd.PortfwdError("; ".join(f"{p} 被重複宣告：{', '.join(n)}" for p, n in sorted(conflicts.items())))
                print("[OK] 無重複 portfwd.listen 宣告")
            elif args.subcommand == "close":
                target_inst = getattr(args, "opt_instance", None) or args.instance
                closed_port = portfwd.close_forward(listen=args.listen, instance=target_inst)
                print(f"[OK] 已關閉 {closed_port}")
            elif args.subcommand == "_stopped":
                portfwd.record_stopped(args.listen, args.instance, args.target, args.bind,
                                       os.environ.get("SERVICE_RESULT", "unknown"))
        except (portfwd.PortfwdError, ValueError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "wsl-shutdown":
        from container_ctl.wsl_shutdown import wsl_shutdown
        wsl_shutdown(yes=args.yes)

    elif args.command == "wsl-restart":
        from container_ctl.wsl_shutdown import wsl_restart
        wsl_restart(yes=args.yes)

    elif args.command == "validate":
        from container_ctl.cli.validate import validate
        validate(
            project=args.project,
            ref=args.ref,
            output=args.output,
            quiet=args.quiet,
            tier=args.tier,
        )

    elif args.command == "secret":
        from container_ctl.secret_provision import secret_provision
        sys.exit(secret_provision(args.project, args.ref, args.service, args.tier, args.apply))

    elif args.command == "host":
        if args.subcommand == "check":
            from container_ctl.host_check import host_check
            sys.exit(host_check(args.env, args.units_dir, args.output, args.strict, args.verbose))
        from container_ctl.cli.host_prepare import host_prepare
        host_prepare(args.project, args.ref, args.service, args.tier, args.apply, args.output)

    elif args.command == "rollout":
        from container_ctl.cli.rollout_cmd import main_entry as ro_entry
        ro_entry(args)

    elif args.command == "reconcile":
        from container_ctl.cli.rollout_cmd import reconcile_entry
        reconcile_entry(args)

    elif args.command == "prod":
        from container_ctl.cli.prod_doctor import prod_doctor
        if args.subcommand == "up":
            from container_ctl.cli.prod_up import main_entry
            main_entry(args)
        if args.subcommand == "rollback":
            from container_ctl.cli.prod_rollback import main_entry as rb_entry
            rb_entry(args)
        prod_doctor(args.project, args.ref, args.service, args.output)

    elif args.command == "cgroup-sync":
        from container_ctl.cgroup_sync import (
            sync_quadlet_files,
            reload_and_restart_services,
            DEFAULT_SERVICES_DIR,
            DEFAULT_SYSTEMD_QUADLET_DIR,
        )
        services_dir = getattr(args, "services_dir", None) or DEFAULT_SERVICES_DIR
        quadlet_dir = getattr(args, "quadlet_dir", None) or DEFAULT_SYSTEMD_QUADLET_DIR
        if isinstance(quadlet_dir, str):
            quadlet_dir = Path(quadlet_dir)

        print("[1/3] 掃描並同步所有 Quadlet 單元 Cgroup 限制 (MemoryMax / MemorySwapMax / TasksMax)...")
        try:
            results = sync_quadlet_files(
                services_dir=services_dir,
                quadlet_dir=quadlet_dir,
                dry_run=args.dry_run,
                policy=getattr(args, "policy", None),
            )
        except Exception as e:
            print(f"[ERROR] cgroup-sync 執行失敗 (Fail-Closed): {e}", file=sys.stderr)
            sys.exit(1)

        modified_count = sum(1 for r in results if r.modified)

        header = f"{'CONTAINER':<30} {'TIER':<8} {'MEM_MAX':<10} {'SWAP_MAX':<10} {'TASKS':<8} {'MODIFIED'}"
        print(header)
        print("-" * len(header))
        for r in results:
            swap_str = r.swap_max if r.swap_max is not None else "-"
            tasks_str = r.tasks_max if r.tasks_max is not None else "-"
            mod_str = "yes" if r.modified else "no"
            print(f"{r.container_name:<30} {r.tier:<8} {r.memory_max:<10} {swap_str:<10} {tasks_str:<8} {mod_str}")

        print(f"\n[2/3] Quadlet 掃描完成：共檢查 {len(results)} 個單元，更新 {modified_count} 個。")
        if not args.dry_run:
            print("[3/3] 執行 daemon-reload 並依依賴順序套用變更至 active 服務...")
            restarted = reload_and_restart_services(results, restart=(not args.no_restart))
            print(f"[OK] Cgroup 同步完成，重啟了 {len(restarted)} 個服務。")
        else:
            print("[INFO] --dry-run 模式，跳過 daemon-reload 與服務重啟。")

    elif args.command == "menu":
        from container_ctl.cli.menu import run_menu
        run_menu()


if __name__ == "__main__":
    main()
