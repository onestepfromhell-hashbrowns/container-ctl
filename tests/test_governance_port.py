import importlib
import re
import sys

import pytest

from container_ctl import governance_port as gp


def test_port_is_the_only_importer_of_memory_governor():
    import pathlib
    root = pathlib.Path(gp.__file__).parent
    hits = [p.name for p in root.rglob("*.py")
            if p.name != "governance_port.py" and re.search(r"^\s*(from|import) memory_governor", p.read_text(), re.M)]
    assert not hits, hits


def test_no_pauses_without_ledger(tmp_path):
    class L:
        def exists(self):
            return False

    assert gp.paused_stable_ids(L()) == set()


def test_ledger_errors_admit():
    class L:
        def exists(self):
            raise OSError("boom")

    assert gp.paused_stable_ids(L()) == set()


def _without_governor(monkeypatch):
    for m in [m for m in sys.modules if m == "memory_governor" or m.startswith("memory_governor.")]:
        monkeypatch.delitem(sys.modules, m)
    monkeypatch.setitem(sys.modules, "memory_governor", None)
    return importlib.reload(gp)


def test_absent_governor_admits_and_compile_fails_loudly(monkeypatch):
    try:
        g = _without_governor(monkeypatch)
        assert g.AVAILABLE is False
        assert g.GovernanceLedger().exists() is False
        assert g.paused_stable_ids() == set()
        with pytest.raises(g.GovernorUnavailable) as e:
            g.load_policy_from_file("/x")
        assert "GOVERNOR_UNAVAILABLE" in str(e.value)
        with pytest.raises(g.GovernorUnavailable):
            g.GuestMemorySample(mem_total_bytes=1, swap_total_bytes=1)
    finally:
        monkeypatch.undo()
        importlib.reload(gp)
