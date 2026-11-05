import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "ADR-0001-manual-fallback-anchors.md"
ANCHOR = re.compile(r"`(container_ctl/[\w/]+\.py)::(\w+)`")


def test_every_anchor_symbol_exists():
    anchors = ANCHOR.findall(ADR.read_text(encoding="utf-8"))
    assert len(anchors) >= 11  # M5/M5d/M6 已移除現行錨點
    for rel, symbol in anchors:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert re.search(rf"^(def|class) {symbol}\b", text, re.M), f"{rel}::{symbol} 不存在，請同步 ADR-0001"


def test_manual_fallback_ids_have_anchors():
    adr = ADR.read_text(encoding="utf-8")
    ref = (ROOT / "docs/manual-fallback.md").read_text(encoding="utf-8")
    for step_id in re.findall(r"^## (M\d+)\b", ref, re.M):
        assert f"| {step_id} |" in adr, f"{step_id} 未在 ADR 錨定表"
