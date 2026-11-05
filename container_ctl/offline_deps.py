"""離線依賴規範檢查：映像需要套件時預設走共用離線來源，不連網下載（SPEC §21）。

Python 第三方套件預設來源：paths.WHEELHOUSE_DIR（附 SHA256SUMS；站點用 CONTAINER_CTL_WHEELHOUSE_DIR 指定）。
本模組只做靜態檢查並回傳 WARN；不阻擋建置。
"""
import re
from pathlib import Path
from container_ctl import paths

WHEELHOUSE_DIR = Path(paths.WHEELHOUSE_DIR)

_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b|\bpython3?\s+-m\s+pip\s+install\b")


def check_dockerfile_offline(text: str, name: str = "Dockerfile") -> list[str]:
    """回傳 pip install 未帶 --no-index 的 WARN 訊息；續行（\\）合併後逐 RUN 指令檢查。"""
    joined = re.sub(r"\\\s*\n", " ", text)
    warns = []
    for lineno, line in enumerate(joined.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.upper().startswith("RUN"):
            continue
        if _PIP_INSTALL.search(stripped) and "--no-index" not in stripped:
            warns.append(
                f"[WARN] {name}: pip install 未帶 --no-index（會連 PyPI）。"
                f"請改用 --no-index --find-links <wheelhouse>（共用庫 {WHEELHOUSE_DIR}，SPEC §21）：{stripped[:100]}"
            )
    return warns


def verify_wheelhouse(directory: Path = WHEELHOUSE_DIR) -> list[str]:
    """核對 SHA256SUMS；回傳錯誤訊息（空 list＝通過或目錄不存在則回報缺少）。"""
    import hashlib

    sums = Path(directory) / "SHA256SUMS"
    if not sums.is_file():
        return [f"[ERROR] 缺少 {sums}"]
    errors = []
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, _, fname = line.partition("  ")
        target = Path(directory) / fname.strip().lstrip("*")
        if not target.is_file():
            errors.append(f"[ERROR] wheelhouse 缺檔：{fname}")
            continue
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            errors.append(f"[ERROR] wheelhouse sha256 不符：{fname}")
    return errors


def inject_wheelhouse(context: Path, dockerfile_text: str, source: Path = WHEELHOUSE_DIR) -> dict | None:
    """Dockerfile 引用 `wheelhouse` 而 build context（git archive）沒有該目錄時，由工具從共用庫注入。

    注入前核對 SHA256SUMS（錯誤則丟 ValueError）；回傳 {"sha256sums": <SHA256SUMS 檔的 sha256>} 供 receipt 記錄，
    不引用或 repo 已自帶 wheelhouse/ 則回 None。專案不必把數十 MB 的 wheel 提交進 repo。
    """
    import hashlib
    import shutil

    if "wheelhouse" not in dockerfile_text or (Path(context) / "wheelhouse").exists():
        return None
    errors = verify_wheelhouse(source)
    if errors:
        raise ValueError("共用 wheelhouse 驗證失敗：" + "; ".join(errors[:3]))
    shutil.copytree(source, Path(context) / "wheelhouse")
    return {"sha256sums": hashlib.sha256((Path(source) / "SHA256SUMS").read_bytes()).hexdigest()}
