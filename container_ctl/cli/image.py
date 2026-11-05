"""Prepare a dev image from an immutable Bare Repo commit, without deploying it."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess

from container_ctl.lifecycle_adapter import get_default_manager
import tarfile
import tempfile
import uuid
from pathlib import Path

from container_ctl.offline_deps import check_dockerfile_offline
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.repo_reader import DeployConfigNotFoundError, _git_show, read_manifest, repo_path_for
from container_ctl.db import DEFAULT_DB_PATH
from container_ctl.quadlet_gen import DEFAULT_SERVICES_DIR, SYSTEMD_QUADLET_DIR
from container_ctl import paths

DEFAULT_STAGE_ROOT = paths.DEPLOYS_DIR
# RAM-backed roots: build/publish contexts must never resolve under these (ISSUE-004).
# /run/user is tmpfs; /var/tmp is disk-backed and intentionally excluded.
_STAGE_RAM_ROOTS = (Path("/tmp"), Path("/dev/shm"), Path("/run/user"))


class ImagePrepareError(Exception):
    """A safe image preparation prerequisite was not met."""


def _ctx_stage_root() -> str:
    """Disk-backed parent for build/publish contexts (ISSUE-004: /tmp is RAM and contexts scale with repo size).

    CONTAINER_CTL_STAGE_ROOT overrides; checks realpath against RAM-backed roots before
    creating directories, sets 0700 only on directories it creates (existing ones, e.g. the shared .deploys, are untouched), and raises ImagePrepareError on failure.
    """
    import os as _os

    raw = _os.environ.get("CONTAINER_CTL_STAGE_ROOT") or DEFAULT_STAGE_ROOT
    real = Path(_os.path.realpath(raw))
    for ram in _STAGE_RAM_ROOTS:
        if real == ram or ram in real.parents:
            raise ImagePrepareError(f"建置暫存目錄不得位於 RAM 檔案系統（{ram}）：{real}")
    try:
        existed = _os.path.isdir(real)
        _os.makedirs(real, mode=0o700, exist_ok=True)
        if not existed:
            _os.chmod(real, 0o700)  # 既有目錄(如共用的 .deploys)不改權限;建置 context 由 TemporaryDirectory 建立(本身 0700)
    except OSError as exc:
        raise ImagePrepareError(f"無法建立或設定建置暫存目錄 {real}：{exc}") from exc
    return str(real)


def _validate_name(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ImagePrepareError(f"project/service 名稱無效：{value!r}")


def _run(args: list[str], *, stream: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=not stream, text=True)
    if result.returncode:
        detail = (result.stderr or "").strip() if not stream else f"exit {result.returncode}"
        raise ImagePrepareError(f"{' '.join(args[:3])} 失敗：{detail}")
    return result


def _source_commit(repo: str, ref: str) -> str:
    result = _run(["git", "--git-dir", repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"])
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ImagePrepareError(f"無效 commit：{commit!r}")
    return commit


def _context_hash(repo: str, commit: str, service: str) -> str:
    """Content address of the dev image build context at a commit (mirrors _dev_build_context precedence).

    The image label carries this instead of the whole-project commit, so unrelated commits do not
    change the image config (and therefore its Id) when the build context is byte-identical.
    """
    for path, kind in ((f".deploy/dev-images/{service}", "tree"), ("Dockerfile.dev", "blob")):
        found = subprocess.run(
            ["git", "--git-dir", repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}"],
            capture_output=True, text=True,
        )
        value = found.stdout.strip()
        if found.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value):
            return f"{kind}:{value}"
    raise ImagePrepareError("來源缺少 Dockerfile.dev 或 .deploy/dev-images/<service>/，無法計算 context hash")


def _source_config(repo: str, commit: str, service: str) -> dict:
    try:
        config = json.loads(_git_show(repo, commit, f".deploy/containers/{service}.json"))
    except (ValueError, DeployConfigNotFoundError) as exc:
        raise ImagePrepareError(f"{service} 配置 JSON 無效：{exc}") from exc
    if not isinstance(config, dict):
        raise ImagePrepareError(f"{service} 配置必須為 JSON object")
    return config


def _archive_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
    try:
        return tarfile.data_filter(member, dest_path)
    except (tarfile.LinkOutsideDestinationError, tarfile.AbsoluteLinkError):
        return None


def _archive_to_context(repo: str, commit: str, context: Path) -> None:
    archive = subprocess.Popen(
        ["git", "--git-dir", repo, "archive", "--format=tar", commit],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert archive.stdout is not None
        with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
            tar.extractall(context, filter=_archive_filter)
        if archive.wait() != 0:
            raise ImagePrepareError(f"git archive 失敗：{archive.stderr.read().decode(errors='replace').strip()}")
    except Exception:
        archive.kill()
        archive.wait()
        raise
    finally:
        if archive.stdout:
            archive.stdout.close()
        if archive.stderr:
            archive.stderr.close()


def _dev_build_context(archive_root: Path, service: str, isolated_root: Path) -> Path:
    """Never pass the application workspace as a dev image build context."""
    dedicated = archive_root / ".deploy" / "dev-images" / service
    if dedicated.is_dir():
        if dedicated.is_symlink():
            raise ImagePrepareError("dev 映像專用 context 不得為 symlink")
        build_context = dedicated
        forbidden_dirs = {"src", "app", "packages", "ui", "tests", "web"}
        for path in dedicated.rglob("*"):
            if path.is_symlink():
                raise ImagePrepareError(f"dev 映像專用 context 不得含 symlink：{path}")
            if path.is_dir() and path.name in forbidden_dirs:
                raise ImagePrepareError(f"dev 映像專用 context 含專案原始碼目錄：{path}")
    else:
        dockerfile = archive_root / "Dockerfile.dev"
        if not dockerfile.is_file():
            raise ImagePrepareError("來源缺少 Dockerfile.dev 或 .deploy/dev-images/<service>/Dockerfile.dev")
        if dockerfile.is_symlink():
            raise ImagePrepareError("Dockerfile.dev 不得為 symlink")
        isolated_root.mkdir()
        shutil.copy2(dockerfile, isolated_root / "Dockerfile.dev")
        build_context = isolated_root

    dockerfile = build_context / "Dockerfile.dev"
    if not dockerfile.is_file():
        raise ImagePrepareError(f"dev 映像專用 context 缺少 Dockerfile.dev：{build_context}")
    instructions = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    for _msg in check_dockerfile_offline(instructions, "Dockerfile.dev"):
        print(_msg)
    for line in instructions.splitlines():
        if re.match(r"^\s*(COPY|ADD)\b", line, re.IGNORECASE) and "/workspace" in line:
            raise ImagePrepareError("Dockerfile.dev 不得 COPY/ADD 內容到 /workspace；dev workspace 由掛載提供")
    if build_context == isolated_root and re.search(r"^\s*(COPY|ADD)\b", instructions, re.IGNORECASE | re.MULTILINE):
        raise ImagePrepareError("根目錄 Dockerfile.dev 含 COPY/ADD；請將僅含工具與依賴素材的 context 移至 .deploy/dev-images/<service>/")
    return build_context


def prepare_dev_image(
    project: str,
    service: str,
    ref: str,
    output_dir: str,
    *,
    repos_dir: str | None = None,
    pull_policy: str = "never",
) -> Path:
    """Build only a dev image, then emit a pinned config and provenance receipt."""
    if pull_policy not in {"never", "missing", "always"}:
        raise ImagePrepareError(f"無效 pull policy：{pull_policy}")
    _validate_name(project)
    _validate_name(service)
    repo, _kind = resolve_deploy_repo(project, repos_dir)  # config 模式：設定 repo 即宣告與 dev context 的來源
    if not Path(repo).is_dir():
        raise ImagePrepareError(f"Bare Repo 不存在：{repo}")
    commit = _source_commit(repo, ref)
    tree = _run(["git", "--git-dir", repo, "ls-tree", "-r", commit]).stdout
    if any(line.startswith("160000 commit ") for line in tree.splitlines()):
        raise ImagePrepareError("來源包含 Git submodule；git archive 不含其內容，拒絕不完整建置")
    try:
        manifest = read_manifest(repo, commit)
    except DeployConfigNotFoundError as exc:
        raise ImagePrepareError(f"來源 manifest 無法讀取：{exc}") from exc
    if manifest["project_name"] != project or service not in manifest.get("dependencies", []):
        raise ImagePrepareError(f"{project}/{service} 不在該 ref 的 manifest dependencies 中")
    config = _source_config(repo, commit, service)
    if config.get("tier") != "dev":
        raise ImagePrepareError(f"{service} 不是 dev 容器")
    original_image = config.get("image")
    if not isinstance(original_image, str) or not original_image.startswith("localhost/"):
        raise ImagePrepareError("目前僅支援 localhost/ dev 映像")
    image_name = original_image.split("@sha256:", 1)[0]
    if ":" in image_name.rsplit("/", 1)[-1]:
        image_name = image_name.rsplit(":", 1)[0]
    build_tag = f"{image_name}:prepare-{commit[:12]}-{uuid.uuid4().hex[:8]}"
    context_hash = _context_hash(repo, commit, service)

    with tempfile.TemporaryDirectory(prefix="container-ctl-build-", dir=_ctx_stage_root()) as context_name:
        root = Path(context_name)
        archive_root = root / "archive"
        archive_root.mkdir()
        _archive_to_context(repo, commit, archive_root)
        context = _dev_build_context(archive_root, service, root / "isolated")
        context_source = (
            f".deploy/dev-images/{service}" if context != root / "isolated" else "Dockerfile.dev (isolated)"
        )
        dockerfile = context / "Dockerfile.dev"
        try:
            _run([
                "podman", "build", f"--pull={pull_policy}",
                "--label", f"{paths.OCI_LABEL_NS}.context-hash={context_hash}",
                "--label", f"{paths.OCI_LABEL_NS}.project={project}",
                "--label", f"{paths.OCI_LABEL_NS}.service={service}",
                "--label", f"{paths.OCI_LABEL_NS}.image-scope=dev-infra",
                "-f", str(dockerfile), "-t", build_tag, str(context),
            ], stream=True)
        except ImagePrepareError as exc:
            if pull_policy == "never":
                raise ImagePrepareError(
                    f"{exc}；若基底映像不在本機（`podman image exists <FROM 映像>`），可加 --pull-policy missing 拉取（須使用者同意）"
                ) from exc
            raise

    inspected = _run(["podman", "image", "inspect", build_tag, "--format", "{{json .}}"])
    try:
        metadata = json.loads(inspected.stdout)
        image_id = str(metadata["Id"]).removeprefix("sha256:")
    except (ValueError, KeyError, TypeError) as exc:
        raise ImagePrepareError(f"無法解析建置映像 ID：{exc}") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        raise ImagePrepareError(f"建置映像 ID 無效：{image_id!r}")
    labels = metadata.get("Labels") or (metadata.get("Config") or {}).get("Labels") or {}
    if labels.get(f"{paths.OCI_LABEL_NS}.context-hash") != context_hash or labels.get(f"{paths.OCI_LABEL_NS}.project") != project or labels.get(f"{paths.OCI_LABEL_NS}.service") != service or labels.get(f"{paths.OCI_LABEL_NS}.image-scope") != "dev-infra":
        raise ImagePrepareError("建置映像缺少來源標籤；拒絕產出無法核對的 pin")
    _run(["podman", "image", "inspect", f"sha256:{image_id}", "--format", "{{.Id}}"])
    # 穩定名稱：pin 映像除短暫的 prepare tag 外也有 pin tag，避免被 prune 當成 dangling 清掉（W2）
    _run(["podman", "tag", f"sha256:{image_id}", f"{image_name}:pin-{image_id[:12]}"])

    config["image"] = f"{image_name}@sha256:{image_id}"
    receipt = {
        "project": project,
        "service": service,
        "source_ref": ref,
        "source_commit": commit,
        "context_hash": context_hash,
        "dockerfile": "Dockerfile.dev",
        "build_context": context_source,
        "pull_policy": pull_policy,
        "build_tag": build_tag,
        "image_id": f"sha256:{image_id}",
        "pinned_image": config["image"],
    }
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifact = Path(tempfile.mkdtemp(prefix=f"{project}-{service}-{commit[:12]}-", dir=root))
    try:
        (artifact / f"{service}.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (artifact / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(artifact)
        raise
    return artifact


def publish_dev_image(
    artifact_dir: str,
    branch: str,
    *,
    yes: bool = False,
    repos_dir: str | None = None,
) -> str:
    """Commit only the prepared image pin to an unchanged Bare Repo branch."""
    artifact = Path(artifact_dir).expanduser().resolve()
    try:
        receipt = json.loads((artifact / "receipt.json").read_text(encoding="utf-8"))
        project = receipt["project"]
        service = receipt["service"]
        _validate_name(project)
        _validate_name(service)
        source_commit = receipt["source_commit"]
        context_hash = receipt["context_hash"]
        image_id = receipt["image_id"]
        prepared_config = json.loads((artifact / f"{service}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise ImagePrepareError(f"待發布 artifact 不完整：{exc}") from exc
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", source_commit):
        raise ImagePrepareError("artifact 的 source_commit 無效")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ImagePrepareError("artifact 的 image_id 無效")
    _run(["git", "check-ref-format", "--branch", branch])
    repo, _kind = resolve_deploy_repo(project, repos_dir)  # config 模式：設定 repo 即宣告與 dev context 的來源
    if not Path(repo).is_dir():
        raise ImagePrepareError(f"Bare Repo 不存在：{repo}")
    branch_ref = f"refs/heads/{branch}"
    current_commit = _run(["git", "--git-dir", repo, "rev-parse", "--verify", branch_ref]).stdout.strip()
    if current_commit != source_commit:
        # 分支前進本身不要求重建：來源須是祖先，且 build context 內容（hash）未變。
        ancestor = subprocess.run(
            ["git", "--git-dir", repo, "merge-base", "--is-ancestor", source_commit, current_commit],
            capture_output=True, text=True,
        )
        if ancestor.returncode != 0:
            raise ImagePrepareError(f"{branch_ref} 已變更且來源 {source_commit} 不是其祖先：目前 {current_commit}")
        if _context_hash(repo, current_commit, service) != context_hash:
            raise ImagePrepareError(f"{branch_ref} 已變更：build context 與映像不符，請重新 prepare-dev")

    original_config = _source_config(repo, current_commit, service)
    if original_config.get("tier") != "dev":
        raise ImagePrepareError(f"{service} 不是 dev 容器")
    expected_config = dict(original_config)
    expected_config["image"] = receipt.get("pinned_image")
    image_name = original_config["image"].split("@sha256:", 1)[0]
    if ":" in image_name.rsplit("/", 1)[-1]:
        image_name = image_name.rsplit(":", 1)[0]
    if prepared_config != expected_config or expected_config["image"] != f"{image_name}@{image_id}":
        raise ImagePrepareError("待發布 JSON 與來源宣告的差異不只 image pin，或 pin 與映像 ID 不符")
    inspected = _run(["podman", "image", "inspect", image_id, "--format", "{{json .}}"])
    try:
        metadata = json.loads(inspected.stdout)
        actual_id = str(metadata["Id"]).removeprefix("sha256:")
    except (ValueError, TypeError, KeyError) as exc:
        raise ImagePrepareError(f"映像 metadata 無法解析：{exc}") from exc
    if actual_id != image_id.removeprefix("sha256:"):
        raise ImagePrepareError("本機映像 ID 與 artifact 不符")
    labels = (metadata.get("Labels") or (metadata.get("Config") or {}).get("Labels") or {})
    if labels.get(f"{paths.OCI_LABEL_NS}.context-hash") != context_hash or labels.get(f"{paths.OCI_LABEL_NS}.project") != project or labels.get(f"{paths.OCI_LABEL_NS}.service") != service or labels.get(f"{paths.OCI_LABEL_NS}.image-scope") != "dev-infra":
        raise ImagePrepareError("映像缺少與 artifact 相符的建置來源標籤")

    if not yes:
        try:
            answer = input(f"只更新 {project}/{service} 的 image pin 並推送 {branch_ref}？(y/N): ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            raise ImagePrepareError("已取消發布")

    with tempfile.TemporaryDirectory(prefix="container-ctl-publish-", dir=_ctx_stage_root()) as temp_name:
        temp = Path(temp_name)
        checkout = temp / "checkout"
        _run(["git", "clone", "--branch", branch, "--single-branch", repo, str(checkout)])
        if _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip() != current_commit:
            raise ImagePrepareError("clone 後來源分支已變更，請重新準備映像")
        config_path = checkout / ".deploy" / "containers" / f"{service}.json"
        config_path.write_text(json.dumps(prepared_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _run(["git", "-C", str(checkout), "add", "--", str(config_path.relative_to(checkout))])
        _run(["git", "-C", str(checkout), "commit", "-m", f"Pin {service} dev image {image_id[:19]}"])
        new_commit = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()

        # Run the same read-only validation against the candidate commit before push.
        temp_repos = temp / "repos"
        temp_repos.mkdir()
        _run(["git", "clone", "--bare", str(checkout), str(temp_repos / f"{project}.git")])
        from container_ctl.cli.validate import run_validation
        ok, messages = run_validation(project, new_commit, repos_dir=str(temp_repos))
        if not ok:
            raise ImagePrepareError("候選提交 validate 未通過：\n" + "\n".join(messages))
        _run(["git", "-C", str(checkout), "push", "origin", f"HEAD:{branch_ref}"])
        return new_commit


def audit_dev_image(
    project: str,
    service: str,
    ref: str,
    *,
    repos_dir: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
    services_dir: str = DEFAULT_SERVICES_DIR,
    quadlet_dir: Path = SYSTEMD_QUADLET_DIR,
) -> tuple[bool, list[str]]:
    """Read-only comparison of Bare pin, generated Quadlet, DB, and live container."""
    _validate_name(project)
    _validate_name(service)
    repo, _kind = resolve_deploy_repo(project, repos_dir)  # config 模式：設定 repo 即宣告與 dev context 的來源
    commit = _source_commit(repo, ref)
    config = _source_config(repo, commit, service)
    image = config.get("image", "")
    if config.get("tier") != "dev" or not isinstance(image, str) or not image.startswith("localhost/") or "@sha256:" not in image:
        raise ImagePrepareError("audit-dev 僅支援有本機 pinned image 的 dev 服務")
    expected = image.rsplit("@sha256:", 1)[1]
    messages = [f"[INFO] Bare ref={commit} pinned=sha256:{expected}"]
    ok = True
    with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM instances WHERE project_name=? AND service_name=? AND status!='destroyed'",
            (project, service),
        ).fetchall()
    if len(rows) > 1:
        # redeploy 以固定 dev ID 寫新列，並把舊規則（含版本號）的列標為 stopped 退役；只審核現行 ID。
        canonical = [r for r in rows if r["id"] == f"{project}-{service}"]
        retired = [r for r in rows if r["id"] != f"{project}-{service}" and r["status"] == "stopped"]
        if len(canonical) == 1 and len(canonical) + len(retired) == len(rows):
            messages.append(f"[INFO] 忽略 {len(retired)} 筆已退役（stopped）舊 ID 實例：{', '.join(r['id'] for r in retired)}")
            rows = canonical
    if len(rows) != 1:
        return False, messages + [f"[ERROR] state.db 實例數應為 1，實際 {len(rows)}"]
    row = rows[0]
    cname = row["container_name"]
    try:
        db_image = json.loads(row["blueprint"] or "{}").get("image")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ImagePrepareError(f"state.db blueprint 無法解析：{exc}") from exc
    if db_image != image:
        messages.append(f"[ERROR] state.db image={db_image}，Bare 宣告 image={image}")
        ok = False
    unit = Path(services_dir) / project / f"{cname}.container"
    link = Path(quadlet_dir) / unit.name
    if not unit.is_file() or not link.is_symlink() or link.resolve() != unit.resolve():
        messages.append(f"[ERROR] Quadlet 或 systemd 連結缺失／不一致：{unit}")
        ok = False
    else:
        rendered = next((line.split("=", 1)[1].strip() for line in unit.read_text().splitlines() if line.startswith("Image=")), None)
        if rendered != f"sha256:{expected}":
            messages.append(f"[ERROR] Quadlet Image={rendered}，預期 sha256:{expected}")
            ok = False

    pinned = subprocess.run(["podman", "image", "inspect", f"sha256:{expected}", "--format", "{{json .}}"], capture_output=True, text=True)
    if pinned.returncode:
        messages.append(f"[ERROR] pinned 映像不在本機：sha256:{expected}")
        ok = False
    else:
        try:
            pin_meta = json.loads(pinned.stdout)
            pin_labels = pin_meta.get("Labels") or (pin_meta.get("Config") or {}).get("Labels") or {}
        except (ValueError, AttributeError, TypeError) as exc:
            raise ImagePrepareError(f"pinned 映像 metadata 無法解析：{exc}") from exc
        if pin_labels.get(f"{paths.OCI_LABEL_NS}.image-scope") != "dev-infra":
            messages.append("[ERROR] pinned dev 映像缺少 dev-infra 來源標籤")
            ok = False
        image_hash = pin_labels.get(f"{paths.OCI_LABEL_NS}.context-hash")
        if image_hash is None:
            messages.append("[WARN] pinned 映像沒有 context-hash 標籤（舊式映像），無法核對 build context 是否仍與來源一致；下次 prepare-dev 重建後即有")
        else:
            try:
                source_hash = _context_hash(repo, commit, service)
            except ImagePrepareError as exc:
                messages.append(f"[ERROR] {exc}")
                ok = False
            else:
                if image_hash != source_hash:
                    messages.append(f"[ERROR] 映像 context-hash={image_hash}，來源 ref 的 context={source_hash}：映像已過時，請重新 prepare-dev／publish-dev")
                    ok = False
    live = subprocess.run(["podman", "inspect", cname, "--format", "{{json .}}"], capture_output=True, text=True)
    try:
        blueprint = json.loads(row["blueprint"] or "{}")
    except (ValueError, TypeError):
        blueprint = {}
    manual = blueprint.get("lifecycle_policy", {}).get("mode", "manual") == "manual"
    unit_state = get_default_manager().unit_state(f"{cname}.service")
    not_started = live.returncode != 0 and unit_state != "active"
    if live.returncode and manual and not_started:
        messages.append(f"[INFO] manual 實例尚未啟動（預期）：{cname}；以 container-ctl service start 啟動")
    elif live.returncode:
        messages.append(f"[ERROR] 容器不存在：{cname}")
        ok = False
    else:
        try:
            live_meta = json.loads(live.stdout)
            live_id = str(live_meta.get("Image", "")).removeprefix("sha256:")
            running = bool((live_meta.get("State") or {}).get("Running"))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ImagePrepareError(f"容器 inspect 無法解析：{exc}") from exc
        if live_id != expected or not running:
            messages.append(f"[ERROR] 運行容器 {cname}: image=sha256:{live_id} running={running}，預期 sha256:{expected}")
            ok = False
    if row["status"] == "stopped" and live.returncode == 0:
        messages.append(f"[ERROR] state.db 記為 stopped，但 Podman 容器存在：{cname}")
        ok = False
    if unit_state != "active" and not (manual and not_started):
        messages.append(f"[ERROR] systemd 狀態：{cname}.service={unit_state}")
        ok = False
    if ok:
        messages.append(f"[OK]   {project}/{service}/{cname} 的來源、Quadlet、DB 與運行映像一致")
    return ok, messages
