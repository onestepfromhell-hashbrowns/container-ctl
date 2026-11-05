import hashlib
from types import SimpleNamespace

from container_ctl import supply_chain as sc

D = "sha256:" + "b" * 64


def _which(present):
    return lambda t: f"/usr/bin/{t}" if t in present else None


def test_collect_without_tools_is_all_null(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.shutil, "which", _which(set()))
    got = sc.collect("P", "svc", "1.0.0", {"manifest_digest": D, "source_commit": "s", "config_commit": "c"}, root=str(tmp_path))
    assert got["sbom"] is None and got["signature"] is None and got["provenance"]["source_commit"] == "s"


def test_sbom_generated_when_syft_present(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.shutil, "which", _which({"syft"}))
    monkeypatch.setattr(sc.artifact_store, "enabled", lambda: True)

    def run(cmd, **kw):
        assert cmd[0] == "syft" and cmd[1].startswith("oci-dir:")
        open(cmd[3].removeprefix("spdx-json="), "w").write('{"spdxVersion":"SPDX-2.3"}')
        return SimpleNamespace(returncode=0, stderr="")
    sbom = sc.generate_sbom("P", "svc", "1.0.0", root=str(tmp_path), run=run)
    assert sbom["format"] == "spdx-json" and sbom["sha256"] == "sha256:" + hashlib.sha256(b'{"spdxVersion":"SPDX-2.3"}').hexdigest()
    ok, msgs = sc.verify_evidence({"sbom": sbom})
    assert ok and any("SBOM 雜湊一致" in m for m in msgs)
    open(sbom["path"], "w").write("tampered")
    assert not sc.verify_evidence({"sbom": sbom})[0]


def test_sign_needs_key_and_cosign(monkeypatch, tmp_path):
    monkeypatch.setattr(sc.artifact_store, "enabled", lambda: True)
    monkeypatch.setattr(sc.shutil, "which", _which({"cosign"}))
    monkeypatch.delenv("CONTAINER_CTL_COSIGN_KEY", raising=False)
    assert sc.sign("P", "svc", "1", D, root=str(tmp_path)) is None  # 無金鑰不簽
    monkeypatch.setenv("CONTAINER_CTL_COSIGN_KEY", "/k.key")
    monkeypatch.setenv("CONTAINER_CTL_COSIGN_PUB", "/k.pub")
    got = sc.sign("P", "svc", "1", D, root=str(tmp_path), run=lambda c, **k: SimpleNamespace(returncode=0, stderr=""))
    assert got["public_key"] == "/k.pub" and got["digest"] == D


def test_require_signature(monkeypatch):
    monkeypatch.setattr(sc.shutil, "which", _which(set()))
    assert sc.verify_evidence({})[0]  # 預設不強制
    ok, msgs = sc.verify_evidence({}, require_signature=True)
    assert not ok and "無簽章" in msgs[-1]
    sig = {"tool": "cosign", "digest": D, "layout": "/l", "public_key": "/k.pub"}
    ok, msgs = sc.verify_evidence({"signature": sig}, require_signature=True)
    assert not ok and "無法驗證" in msgs[-1]  # 缺 cosign：fail closed
    monkeypatch.setattr(sc.shutil, "which", _which({"cosign"}))
    good = sc.verify_evidence({"signature": sig}, require_signature=True, run=lambda c, **k: SimpleNamespace(returncode=0, stderr=""))
    bad = sc.verify_evidence({"signature": sig}, require_signature=True, run=lambda c, **k: SimpleNamespace(returncode=1, stderr="no match"))
    assert good[0] and not bad[0]
