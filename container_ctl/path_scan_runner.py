"""把 08 的 scan-path-refs 接進 prod doctor：只掃 prod 宣告 json（R-001：prod 配置不得含 /workspace）。

整包應用碼掃描不在此阻擋：核心規則 R-006 對站點的 `/var/opt/<site>/...` 路徑有誤報。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SCAN_TOOL = os.environ.get("CONTAINER_CTL_SCAN_PATH_REFS") or str(Path(__file__).resolve().parents[1] / "tools" / "scan-path-refs" / "scan-path-refs")
_RE = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+): \[VIOLATION\] (?P<rule>R-\d+)")


def path_scan_findings(configs: list[dict], runner=subprocess.run, tool: str = SCAN_TOOL) -> list[str]:
    if not Path(tool).exists():
        return ["[WARN] 找不到 scan-path-refs，路徑掃描未執行"]
    prod = [c for c in configs if str(c.get("tier") or "prod") == "prod"]
    if not prod:
        return []
    out: list[str] = []
    with tempfile.TemporaryDirectory(prefix="container-ctl-pathscan-") as tmp:
        root = Path(tmp)
        for cfg in prod:
            (root / f"{cfg.get('name', 'unknown')}.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        res = runner(["python3", tool, "--target", str(root), "--ext", "json"], capture_output=True, text=True,
                     check=False, timeout=60, cwd=str(root))
        if res.returncode == 0:
            return []
        for line in (res.stdout or "").splitlines():
            m = _RE.match(line.strip())
            if m:
                out.append(f"[ERROR] {Path(m['file']).stem}: scan-path-refs {m['rule']}（行 {m['line']}）prod 宣告含禁用路徑（/workspace 等）")
        if not out:
            out.append(f"[ERROR] scan-path-refs 結束碼 {res.returncode}：{(res.stderr or res.stdout or '')[-200:].strip()}")
    return out
