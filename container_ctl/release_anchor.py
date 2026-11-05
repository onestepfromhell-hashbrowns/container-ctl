"""prod 版本錨定檢查（DESIGN-prod-version-anchor-20261003 D1/D4/D5）。

純函式：讀 Bare Repo 的 release tag，比對 prod 宣告。接線到 validate／redeploy 另行處理。
strict=False（validate，運行中舊 prod 過渡）：缺錨定只 WARN；strict=True（新部署／redeploy）：ERROR。
digest 與 tag 記錄不符一律 ERROR（同版本內容已凍結）。
"""

from __future__ import annotations

from container_ctl.cli.image_prod import read_release_record, release_tag


def check_release_anchor(
    repo: str,
    configs: list[dict],
    default_version: str,
    *,
    strict: bool = False,
) -> tuple[bool, list[str]]:
    messages: list[str] = []
    ok = True
    soft = "[ERROR]" if strict else "[WARN]"
    for cfg in configs:
        if cfg.get("tier") != "prod":
            continue
        name = cfg.get("name", "unknown")
        version = str(cfg.get("version") or default_version)
        image = cfg.get("image", "")
        if "@sha256:" not in image:
            messages.append(f"{soft} {name}: prod 映像必須以 @sha256 digest 錨定（目前 {image}）")
            ok = ok and not strict
            continue
        record = read_release_record(repo, name, version)
        if record is None:
            messages.append(f"{soft} {name}: 版本 {version} 尚無 {release_tag(name, version)} 發布記錄（prepare-prod／publish-prod 或補登）")
            ok = ok and not strict
            continue
        digest = "sha256:" + image.rsplit("@sha256:", 1)[1]
        if record.get("image_id") != digest:
            messages.append(
                f"[ERROR] {name}: 版本 {version} 已凍結為 {record.get('image_id')}，宣告為 {digest}；內容變更請升版本號"
            )
            ok = False
    return ok, messages
