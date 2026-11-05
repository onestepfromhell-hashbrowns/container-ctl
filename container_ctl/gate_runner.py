"""把 extscope-tools gate(static gate) 接進部署流程：只掃宣告 json（不掃 dev-images 的 wheel，避免二進位誤報）。"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

def gate_findings(configs: list[dict], runner=subprocess.run, gate_bin: str | None = None) -> list[str]:
    """對宣告執行 static gate(extscope-tools gate;JSON 輸出,有違規 exit 1)。prod 違規 → ERROR;dev 違規 → WARN;工具缺失 → WARN(不假裝已掃)。"""
    gate = gate_bin or shutil.which("extscope-tools")
    if not gate:
        return ["[WARN] 找不到 extscope-tools,static gate 未執行(未驗證明文機密)"]
    out: list[str] = []
    with tempfile.TemporaryDirectory(prefix="container-ctl-gate-") as tmp:
        root = Path(tmp)
        (root / ".deploy" / "containers").mkdir(parents=True)
        tiers: dict[str, str] = {}
        for cfg in configs:
            name = str(cfg.get("name") or "unknown")
            (root / ".deploy" / "containers" / f"{name}.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            tiers[f"{name}.json"] = str(cfg.get("tier") or "prod")
        res = runner([gate, "gate", str(root)], capture_output=True, text=True, check=False, timeout=120)
        try:
            report = json.loads(res.stdout or "")
        except ValueError:
            report = None
        if res.returncode == 0 and isinstance(report, dict) and report.get("ok") is True:
            return []
        hits = 0
        for v in (report or {}).get("violations", []) if isinstance(report, dict) else []:
            hits += 1
            fname = Path(str(v.get("file", ""))).name
            level = "ERROR" if tiers.get(fname, "prod") == "prod" else "WARN"
            out.append(f"[{level}] {fname[:-5]}: static_gate {v.get('rule')}(行 {v.get('line')})→ 改用 *_FILE 契約;詳情:extscope-tools gate")
        if not hits:
            out.append(f"[ERROR] static_gate 結束碼 {res.returncode} 但無法解析違規項目:{(res.stderr or res.stdout or '')[-200:].strip()}")
    return out
