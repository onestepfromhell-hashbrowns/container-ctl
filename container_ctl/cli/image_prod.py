"""prod 映像建置與版本錨定（DESIGN-prod-version-anchor-20261003 D2/D3/D6）。

權威記錄是 Bare Repo 的 annotated tag `release/<service>/<version>`，內容為 JSON：
digest、source commit、version。已存在的 release tag 永不覆寫。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from container_ctl.offline_deps import check_dockerfile_offline
from container_ctl.cli.image import (
    ImagePrepareError,
    _archive_to_context,
    _ctx_stage_root,
    _run,
    _source_commit,
    _source_config,
    _validate_name,
)
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import DeployConfigNotFoundError, read_manifest, repo_path_for
from container_ctl import paths

_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


# 建置與錨定 release 映像是「映像來源可重現」的需求，與角色無關：prod 與 infra 都走這條路。
# tier 只決定生命週期／啟停順序／網路規則（SPEC §18、ADR-009）；dev 映像另走 prepare-dev。
RELEASE_BUILD_TIERS = ("prod", "infra")


def release_tag(service: str, version: str) -> str:
    return f"release/{service}/{version}"


def read_release_record(repo: str, service: str, version: str) -> dict | None:
    """回傳 release tag 記錄；不存在回 None。"""
    ref = f"refs/tags/{release_tag(service, version)}"
    found = subprocess.run(["git", "--git-dir", repo, "cat-file", "tag", ref], capture_output=True, text=True)
    if found.returncode:
        return None
    message = found.stdout.split("\n\n", 1)[-1]
    try:
        record = json.loads(message)
    except ValueError as exc:
        raise ImagePrepareError(f"{release_tag(service, version)} 記錄不是有效 JSON：{exc}") from exc
    return record if isinstance(record, dict) else None


def _tag_exists(repo: str, service: str, version: str) -> bool:
    ref = f"refs/tags/{release_tag(service, version)}"
    return subprocess.run(["git", "--git-dir", repo, "show-ref", "--verify", "--quiet", ref]).returncode == 0


def _validate_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ImagePrepareError(f"version 無效：{version!r}")


def prepare_prod_image(
    project: str,
    service: str,
    version: str,
    ref: str,
    output_dir: str,
    *,
    repos_dir: str | None = None,
    pull_policy: str = "never",
) -> Path:
    """由乾淨 Git commit 建置 prod 映像，輸出 pinned JSON 與 receipt。不寫 tag、不動容器。"""
    if pull_policy not in {"never", "missing", "always"}:
        raise ImagePrepareError(f"無效 pull policy：{pull_policy}")
    _validate_name(project)
    _validate_name(service)
    _validate_version(version)
    repo = repo_path_for(project, repos_dir) if repos_dir else repo_path_for(project)  # 程式碼 repo（建置來源）
    if not Path(repo).is_dir():
        raise ImagePrepareError(f"Bare Repo 不存在：{repo}")
    deploy_repo, kind = resolve_deploy_repo(project, repos_dir)  # 宣告與 release tag 所在
    if _tag_exists(deploy_repo, service, version):
        raise ImagePrepareError(f"{release_tag(service, version)} 已存在；同版本內容已凍結，變更請升版本號")
    commit = _source_commit(repo, ref)
    # legacy：宣告與程式碼同 commit；config：宣告讀設定 repo 的 HEAD
    decl_commit = commit if kind == "legacy" else _source_commit(deploy_repo, "HEAD")
    tree = _run(["git", "--git-dir", repo, "ls-tree", "-r", commit]).stdout
    if any(line.startswith("160000 commit ") for line in tree.splitlines()):
        raise ImagePrepareError("來源包含 Git submodule；git archive 不含其內容，拒絕不完整建置")
    try:
        manifest = read_manifest(deploy_repo, decl_commit)
    except DeployConfigNotFoundError as exc:
        raise ImagePrepareError(f"來源 manifest 無法讀取：{exc}") from exc
    if manifest["project_name"] != project or service not in manifest.get("dependencies", []):
        raise ImagePrepareError(f"{project}/{service} 不在該 ref 的 manifest dependencies 中")
    config = _source_config(deploy_repo, decl_commit, service)
    if config.get("tier") not in RELEASE_BUILD_TIERS:
        raise ImagePrepareError(f"{service} 不是 prod／infra 容器（tier={config.get('tier')!r}）")
    original_image = config.get("image")
    if not isinstance(original_image, str) or not original_image.startswith("localhost/"):
        raise ImagePrepareError("目前僅支援 localhost/ prod 映像")
    image_name = original_image.split("@sha256:", 1)[0]
    if ":" in image_name.rsplit("/", 1)[-1]:
        image_name = image_name.rsplit(":", 1)[0]
    build_tag = f"{image_name}:prepare-{commit[:12]}-{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory(prefix="container-ctl-prod-build-", dir=_ctx_stage_root()) as context_name:
        context = Path(context_name)
        _archive_to_context(repo, commit, context)
        dockerfile = context / "Dockerfile.prod"
        if not dockerfile.is_file() or dockerfile.is_symlink():
            raise ImagePrepareError("來源缺少 Dockerfile.prod（或為 symlink）")
        _dtext = dockerfile.read_text(encoding="utf-8", errors="replace")
        for _msg in check_dockerfile_offline(_dtext, "Dockerfile.prod"):
            print(_msg)
        from container_ctl.dockerfile_provenance import from_findings, last_change
        _from_msgs = from_findings(_dtext)
        for _msg in _from_msgs:
            print(_msg)
        if any(_m.startswith("[ERROR]") for _m in _from_msgs):
            raise ImagePrepareError("Dockerfile.prod 的 FROM 未固定 digest；修正後重跑（prod 映像必須可重現）")
        _prov = last_change(repo, commit)
        from container_ctl.offline_deps import inject_wheelhouse
        try:
            _wheelhouse = inject_wheelhouse(context, _dtext)
        except ValueError as exc:
            raise ImagePrepareError(str(exc)) from exc
        if _wheelhouse:
            print(f"[INFO] 已從共用庫注入 wheelhouse/（SHA256SUMS {_wheelhouse['sha256sums'][:12]}）")
        _df_text = dockerfile.read_text(encoding="utf-8")
        # Dockerfile 含 `# <LABEL_NS>: squash-all` → 壓成單層（刪除檔案才真的變小）；引用 wheelhouse → 無網路建置（SPEC §21 離線）
        _extra = (["--squash-all"] if re.search(rf"^#\s*{re.escape(paths.LABEL_NS)}:\s*squash-all\s*$", _df_text, re.M) else []) + \
                 (["--network", "none"] if _wheelhouse or "wheelhouse" in _df_text else [])
        _created = _run(["git", "--git-dir", repo, "show", "-s", "--format=%cI", commit]).stdout.strip()  # 以 commit 時間而非 now，維持可重現
        _run([
            "podman", "build", f"--pull={pull_policy}", *_extra,
            "--label", f"org.opencontainers.image.revision={commit}",
            "--label", f"org.opencontainers.image.version={version}",
            "--label", f"org.opencontainers.image.title={service}",
            "--label", f"org.opencontainers.image.created={_created}",
            "--label", f"{paths.OCI_LABEL_NS}.source-commit={commit}",
            "--label", f"{paths.OCI_LABEL_NS}.version={version}",
            "--label", f"{paths.OCI_LABEL_NS}.project={project}",
            "--label", f"{paths.OCI_LABEL_NS}.service={service}",
            "--label", f"{paths.OCI_LABEL_NS}.image-scope=prod",
            "-f", str(dockerfile), "-t", build_tag, str(context),
        ], stream=True)

    image_id = _inspect_id(build_tag)
    from container_ctl.image_checks import image_findings, inspect_image
    _built = inspect_image(f"sha256:{image_id}")
    _img_msgs = image_findings(_built, service) if _built else []
    for _m in _img_msgs:
        print(_m)
    if any(_m.startswith("[ERROR]") for _m in _img_msgs):
        raise ImagePrepareError("映像違反 prod runtime 規範（SPEC §23.1），已停止；修正 Dockerfile.prod 後重建")
    _run(["podman", "tag", f"sha256:{image_id}", f"{image_name}:pin-{image_id[:12]}"])
    config["image"] = f"{image_name}@sha256:{image_id}"
    receipt = {
        "project": project,
        "service": service,
        "version": version,
        "source_ref": ref,
        "source_commit": commit,
        "deploy_kind": kind,
        "config_commit": decl_commit,
        "dockerfile": "Dockerfile.prod",
        "dockerfile_last_change": _prov,
        "wheelhouse": _wheelhouse,
        "pull_policy": pull_policy,
        "build_tag": build_tag,
        "image_id": f"sha256:{image_id}",
        "pinned_image": config["image"],
    }
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifact = Path(tempfile.mkdtemp(prefix=f"{project}-{service}-{version}-", dir=root))
    try:
        (artifact / f"{service}.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (artifact / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(artifact)
        raise
    return artifact


def _inspect_id(ref: str) -> str:
    inspected = _run(["podman", "image", "inspect", ref, "--format", "{{json .}}"])
    try:
        image_id = str(json.loads(inspected.stdout)["Id"]).removeprefix("sha256:")
    except (ValueError, KeyError, TypeError) as exc:
        raise ImagePrepareError(f"無法解析映像 ID：{exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        raise ImagePrepareError(f"映像 ID 無效：{image_id!r}")
    return image_id


def publish_prod_image(artifact_dir: str, *, repos_dir: str | None = None, yes: bool = False) -> str:
    """驗證 receipt 與本機映像後，在 Bare Repo 寫入 release tag。拒絕覆寫既有 tag。"""
    artifact = Path(artifact_dir)
    try:
        receipt = json.loads((artifact / "receipt.json").read_text(encoding="utf-8"))
        project, service, version = receipt["project"], receipt["service"], receipt["version"]
        commit, image_id = receipt["source_commit"], receipt["image_id"]
    except (OSError, ValueError, KeyError) as exc:
        raise ImagePrepareError(f"receipt 無效：{exc}") from exc
    _validate_name(project)
    _validate_name(service)
    _validate_version(version)
    # receipt 是磁碟上的輸入：image_id 會當作 podman 參數，須先限定格式（防選項注入）
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ImagePrepareError(f"receipt 的 image_id 格式無效：{image_id!r}")
    repo = repo_path_for(project, repos_dir) if repos_dir else repo_path_for(project)  # 程式碼 repo
    deploy_repo, kind = resolve_deploy_repo(project, repos_dir)
    if _tag_exists(deploy_repo, service, version):
        raise ImagePrepareError(f"{release_tag(service, version)} 已存在；拒絕覆寫")
    if _source_commit(repo, commit) != commit:
        raise ImagePrepareError("receipt 的 source_commit 在 Bare Repo 中不存在")
    metadata = json.loads(_run(["podman", "image", "inspect", image_id, "--format", "{{json .}}"]).stdout)
    if "sha256:" + str(metadata.get("Id", "")).removeprefix("sha256:") != image_id:
        raise ImagePrepareError("本機映像 ID 與 receipt 不符")
    labels = metadata.get("Labels") or (metadata.get("Config") or {}).get("Labels") or {}
    expected = {
        f"{paths.OCI_LABEL_NS}.source-commit": commit,
        f"{paths.OCI_LABEL_NS}.version": version,
        f"{paths.OCI_LABEL_NS}.project": project,
        f"{paths.OCI_LABEL_NS}.service": service,
        f"{paths.OCI_LABEL_NS}.image-scope": "prod",
    }
    if any(labels.get(k) != v for k, v in expected.items()):
        raise ImagePrepareError("映像標籤與 receipt 不符；拒絕發布")
    if not yes:
        answer = input(f"在 {repo} 建立 {release_tag(service, version)} → {image_id[:19]}？[y/N] ")
        if answer.strip().lower() != "y":
            raise ImagePrepareError("使用者取消")
    # 先寫 OCI store 再提交 pin：store 失敗時設定 repo 不留「有 pin 無 release tag」的半成品。
    # 此時 release tag 必不存在（上面已查），store 內同 ref 只可能是先前失敗發布的殘留，丟棄後重寫。
    manifest_digest = None
    from container_ctl import artifact_store
    if artifact_store.enabled():
        try:
            artifact_store.discard(project, service, version)
            manifest_digest = artifact_store.put(project, service, version, image_id)
        except artifact_store.ArtifactStoreError as exc:
            raise ImagePrepareError(f"OCI artifact store 寫入失敗，未提交 pin、未建立 release tag：{exc}") from exc
    tag_target = commit
    if kind == "config":
        tag_target = _commit_pin_to_config_repo(deploy_repo, project, service, version, receipt, artifact)
    record = {"schema": 2, "service": service, "version": version, "image_id": image_id, "source_commit": commit,
              "config_commit": tag_target}
    if manifest_digest:
        record["manifest_digest"] = manifest_digest
    from container_ctl import supply_chain
    record.update(supply_chain.collect(project, service, version, record))
    from container_ctl.contract import ContractError, contract_hash, read_contract
    try:
        _contract = read_contract(deploy_repo, tag_target)
    except ContractError:
        _contract = None
    if _contract:
        record["contract_hash"] = contract_hash(_contract)
    _run([
        "git", "--git-dir", deploy_repo, "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost",
        "tag", "-a", release_tag(service, version), tag_target, "-m", json.dumps(record, sort_keys=True),
    ])
    return release_tag(service, version)


def _commit_pin_to_config_repo(deploy_repo: str, project: str, service: str, version: str, receipt: dict, artifact: Path) -> str:
    """config 模式：只把 image pin 提交進設定 repo 的現行分支並回傳新 commit（release tag 指向它）。"""
    branch = _run(["git", "--git-dir", deploy_repo, "symbolic-ref", "--short", "HEAD"]).stdout.strip()
    head = _source_commit(deploy_repo, "HEAD")
    if receipt.get("config_commit") != head:
        raise ImagePrepareError("設定 repo 已前進（與 prepare-prod 當時不同），請重新 prepare-prod")
    prepared = json.loads((artifact / f"{service}.json").read_text(encoding="utf-8"))
    original = _source_config(deploy_repo, head, service)
    expected = dict(original, image=receipt.get("pinned_image"))
    if {k: v for k, v in prepared.items() if k != "version"} != {k: v for k, v in expected.items() if k != "version"}:
        raise ImagePrepareError("待發布 JSON 與設定 repo 宣告的差異不只 image pin")
    prepared = dict(prepared, version=version)  # 版本錨定：宣告的 version 須與 release tag 同步，否則 anchor 比對舊版凍結 digest
    with tempfile.TemporaryDirectory(prefix="container-ctl-prod-publish-", dir=_ctx_stage_root()) as name:
        temp = Path(name)
        checkout = temp / "checkout"
        _run(["git", "clone", "-q", "--branch", branch, "--single-branch", deploy_repo, str(checkout)])
        path = checkout / ".deploy" / "containers" / f"{service}.json"
        path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git = ["git", "-C", str(checkout), "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost"]
        _run([*git, "add", "--", str(path.relative_to(checkout))])
        _run([*git, "commit", "-q", "-m", f"Pin {service} prod image {receipt['image_id'][:19]} for {version}"])
        new_commit = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
        _run(["git", "-C", str(checkout), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"])
    return new_commit


def audit_prod_image(
    project: str,
    service: str,
    version: str,
    ref: str,
    *,
    repos_dir: str | None = None,
    db_path: str | None = None,
) -> tuple[bool, list[str]]:
    """唯讀：release tag 記錄＝Bare 宣告＝state.db blueprint＝運行容器映像 ID，任一不符即 ERROR（D6）。"""
    import sqlite3

    from container_ctl.db import DEFAULT_DB_PATH

    _validate_name(project)
    _validate_name(service)
    _validate_version(version)
    repo, _kind = resolve_deploy_repo(project, repos_dir)  # 宣告與 release tag 所在；ref 為該 repo 的 ref
    commit = _source_commit(repo, ref)
    record = read_release_record(repo, service, version)
    if record is None:
        return False, [f"[ERROR] 無 {release_tag(service, version)} 發布記錄"]
    frozen = record.get("image_id", "")
    messages = [f"[INFO] {release_tag(service, version)} 凍結為 {frozen}（commit {record.get('source_commit', '?')[:12]}）"]
    ok = True

    config = _source_config(repo, commit, service)
    image = config.get("image", "")
    declared = "sha256:" + image.rsplit("@sha256:", 1)[1] if "@sha256:" in image else None
    if config.get("tier") not in RELEASE_BUILD_TIERS:
        return False, messages + [f"[ERROR] {service} 不是 prod／infra 容器"]
    if declared != frozen:
        messages.append(f"[ERROR] Bare 宣告 image={image}，與凍結記錄不符")
        ok = False

    with sqlite3.connect(f"file:{Path(db_path or DEFAULT_DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM instances WHERE project_name=? AND service_name=? AND version=? AND status!='destroyed'",
            (project, service, version),
        ).fetchall()
    if len(rows) != 1:
        return False, messages + [f"[ERROR] state.db 中 {service} {version} 的實例數應為 1，實際 {len(rows)}"]
    row = rows[0]
    try:
        db_image = json.loads(row["blueprint"] or "{}").get("image", "")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImagePrepareError(f"state.db blueprint 無法解析：{exc}") from exc
    db_digest = "sha256:" + db_image.rsplit("@sha256:", 1)[1] if "@sha256:" in db_image else None
    if db_digest != frozen:
        messages.append(f"[ERROR] state.db image={db_image}，與凍結記錄不符")
        ok = False

    cname = row["container_name"]
    live = subprocess.run(["podman", "inspect", cname, "--format", "{{json .}}"], capture_output=True, text=True)
    if live.returncode:
        messages.append(f"[ERROR] 容器不存在：{cname}")
        ok = False
    else:
        try:
            live_id = "sha256:" + str(json.loads(live.stdout).get("Image", "")).removeprefix("sha256:")
        except (ValueError, AttributeError, TypeError) as exc:
            raise ImagePrepareError(f"容器 inspect 無法解析：{exc}") from exc
        if live_id != frozen:
            messages.append(f"[ERROR] 運行容器 {cname} image={live_id}，與凍結記錄不符")
            ok = False
    if ok:
        messages.append(f"[OK]   {project}/{service} {version}：發布記錄、Bare 宣告、DB 與運行容器一致")
    return ok, messages


def adopt_prod_release(
    project: str,
    service: str,
    version: str,
    ref: str,
    *,
    repos_dir: str | None = None,
    db_path: str | None = None,
    yes: bool = False,
) -> str:
    """補登運行中的舊 prod（D5）：只寫 release tag（adopted=true），不建置、不動容器。

    條件：Bare 宣告為 prod 且 @sha256 pin；state.db 該版本唯一實例；運行容器映像 ID 等於宣告 digest。
    """
    import sqlite3

    from container_ctl.db import DEFAULT_DB_PATH

    _validate_name(project)
    _validate_name(service)
    _validate_version(version)
    repo, _kind = resolve_deploy_repo(project, repos_dir)  # 宣告與 release tag 所在；ref 為該 repo 的 ref
    if _tag_exists(repo, service, version):
        raise ImagePrepareError(f"{release_tag(service, version)} 已存在；拒絕覆寫")
    commit = _source_commit(repo, ref)
    config = _source_config(repo, commit, service)
    image = config.get("image", "")
    if config.get("tier") not in RELEASE_BUILD_TIERS or "@sha256:" not in image:
        raise ImagePrepareError("補登僅支援已 pin digest 的 prod／infra 宣告")
    declared = "sha256:" + image.rsplit("@sha256:", 1)[1]
    with sqlite3.connect(f"file:{Path(db_path or DEFAULT_DB_PATH).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT container_name FROM instances WHERE project_name=? AND service_name=? AND version=? AND status!='destroyed'",
            (project, service, version),
        ).fetchall()
    if len(rows) != 1:
        raise ImagePrepareError(f"state.db 中 {service} {version} 的實例數應為 1，實際 {len(rows)}")
    cname = rows[0]["container_name"]
    live = subprocess.run(["podman", "inspect", cname, "--format", "{{json .}}"], capture_output=True, text=True)
    if live.returncode:
        raise ImagePrepareError(f"容器不存在：{cname}")
    live_id = "sha256:" + str(json.loads(live.stdout).get("Image", "")).removeprefix("sha256:")
    if live_id != declared:
        raise ImagePrepareError(f"運行容器映像 {live_id} 與宣告 {declared} 不符；拒絕補登")
    if not yes:
        answer = input(f"補登 {release_tag(service, version)} → {declared[:19]}（commit {commit[:12]}，adopted）？[y/N] ")
        if answer.strip().lower() != "y":
            raise ImagePrepareError("使用者取消")
    record = {"service": service, "version": version, "image_id": declared, "source_commit": commit, "adopted": True}
    _run([
        "git", "--git-dir", repo, "-c", "user.name=container-ctl", "-c", "user.email=container-ctl@localhost",
        "tag", "-a", release_tag(service, version), commit, "-m", json.dumps(record, sort_keys=True),
    ])
    return release_tag(service, version)


def verify_release_artifact(project: str, service: str, version: str, *, repos_dir: str | None = None,
                            require_signature: bool = False) -> tuple[bool, list[str]]:
    """唯讀：release 記錄的 manifest digest ＝ OCI store 內容（含 blob 雜湊重算）。舊記錄（無 manifest_digest）回報略過。"""
    from container_ctl import artifact_store
    _validate_name(project)
    _validate_name(service)
    _validate_version(version)
    repo, _kind = resolve_deploy_repo(project, repos_dir)
    record = read_release_record(repo, service, version)
    if record is None:
        return False, [f"[ERROR] 無 {release_tag(service, version)} 發布記錄"]
    digest = record.get("manifest_digest")
    if not digest:
        return not require_signature, [f"[WARN] {release_tag(service, version)} 為舊格式（無 manifest_digest）；只有 image_id 錨定，無 OCI 身分"]
    ok, msg = artifact_store.verify(project, service, version, digest)
    messages = [f"[OK]   {service}:{version} manifest {digest} 與 OCI store 一致"] if ok else [f"[ERROR] {msg}"]
    from container_ctl import supply_chain
    ev_ok, ev_msgs = supply_chain.verify_evidence(record, require_signature=require_signature)
    return ok and ev_ok, messages + ev_msgs
