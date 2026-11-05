"""Dockerfile.prod 來源稽核：FROM 必須以 @sha256 固定；receipt 記錄該檔最後修改的 commit 與作者。

規則（prod 映像不可變）：
  literal FROM 無 @sha256 → ERROR（上游 tag 會漂移，同版本重建結果不同）
  FROM ${VAR}            → WARN（無法靜態判斷；請在 ARG 預設值固定 digest）
  FROM scratch／前一階段別名 → 略過
"""
from __future__ import annotations

import re
import subprocess

_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*$", re.IGNORECASE)


def from_findings(text: str, name: str = "Dockerfile.prod") -> list[str]:
    out: list[str] = []
    stages: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _FROM.match(line)
        if not m:
            continue
        ref, alias = m.group(1), m.group(2)
        if alias:
            stages.add(alias.lower())
        if ref.lower() == "scratch" or ref.lower() in stages:
            continue
        if "$" in ref:
            out.append(f"[WARN] {name}:{lineno} FROM {ref} 含變數，無法確認 digest；請在 ARG 預設值使用 @sha256 固定")
        elif "@sha256:" not in ref:
            out.append(f"[ERROR] {name}:{lineno} FROM {ref} 未以 @sha256 固定；prod 映像必須可重現。"
                       f"修法：podman image inspect {ref} --format '{{{{.Digest}}}}' 取得 digest，改寫為 {ref.split(':')[0]}:<tag>@sha256:<digest>")
    return out


def last_change(repo: str, commit: str, path: str = "Dockerfile.prod") -> dict | None:
    """回傳 {commit, author, date} 或 None（唯讀 git log）。"""
    r = subprocess.run(["git", "--git-dir", repo, "log", "-1", "--format=%H%x09%an <%ae>%x09%aI", commit, "--", path],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        return None
    h, who, date = r.stdout.strip().split("\t", 2)
    return {"commit": h, "author": who, "date": date}


def doctor_findings(repo: str, ref: str = "HEAD") -> list[str]:
    r = subprocess.run(["git", "--git-dir", repo, "show", f"{ref}:Dockerfile.prod"], capture_output=True, text=True)
    return from_findings(r.stdout) if r.returncode == 0 else []
