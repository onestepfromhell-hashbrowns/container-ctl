import hashlib
import json
from types import SimpleNamespace

import pytest

from container_ctl import artifact_store as st

IMG = "sha256:" + "e" * 64


def _fake_push(layout_blob=b'{"schemaVersion":2}', tamper=False):
    def run(cmd, **kw):
        assert cmd[:3] == ["podman", "--remote=false", "push"] and cmd[5] == IMG
        assert "CONTAINER_HOST" not in kw["env"]
        dest = cmd[6].removeprefix("oci:")
        layout, _, version = dest.rpartition(":")
        digest = "sha256:" + hashlib.sha256(layout_blob).hexdigest()
        from pathlib import Path
        blobs = Path(layout) / "blobs" / "sha256"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / digest.split(":")[1]).write_bytes(layout_blob + (b"x" if tamper else b""))
        (Path(layout) / "index.json").write_text(json.dumps({"schemaVersion": 2, "manifests": [
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": digest, "size": len(layout_blob),
             "annotations": {"org.opencontainers.image.ref.name": version}}]}))
        Path(cmd[4]).write_text(digest + "\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_put_verify_roundtrip_and_immutable(tmp_path):
    d = st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push())
    assert d == "sha256:" + hashlib.sha256(b'{"schemaVersion":2}').hexdigest()
    assert st.verify("P", "svc", "1.0.0", d, root=str(tmp_path)) == (True, d)
    ok, msg = st.verify("P", "svc", "1.0.0", "sha256:" + "0" * 64, root=str(tmp_path))
    assert not ok and "不符" in msg
    with pytest.raises(st.ArtifactStoreError, match="拒絕覆寫"):
        st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push())
    assert not st.verify("P", "svc", "9.9.9", root=str(tmp_path))[0]


def test_tampered_blob_detected(tmp_path):
    with pytest.raises(st.ArtifactStoreError, match="損毀"):
        st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push(tamper=True))


def test_push_failure_and_bad_image_id(tmp_path):
    with pytest.raises(st.ArtifactStoreError, match="podman push 失敗"):
        st.put("P", "svc", "1", IMG, root=str(tmp_path), run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    with pytest.raises(st.ArtifactStoreError, match="格式無效"):
        st.put("P", "svc", "1", "--evil", root=str(tmp_path))


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CONTAINER_CTL_OCI_STORE", "off")
    assert not st.enabled()
    monkeypatch.setenv("CONTAINER_CTL_OCI_STORE", "/x")
    assert st.enabled()


def test_discard_removes_only_that_ref(tmp_path):
    st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push())
    assert st.discard("P", "svc", "1.0.0", root=str(tmp_path)) is True
    assert st.discard("P", "svc", "1.0.0", root=str(tmp_path)) is False
    assert st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push())  # 殘留清掉後可重發


def test_put_and_load_force_local_podman_even_with_container_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTAINER_HOST", "unix:///run/user/1000/podman/podman.sock")
    monkeypatch.setenv("CONTAINER_CONNECTION", "x")
    st.put("P", "svc", "1.0.0", IMG, root=str(tmp_path), run=_fake_push())
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    st.load("P", "svc", "1.0.0", root=str(tmp_path), run=run)
    assert seen["cmd"][:3] == ["podman", "--remote=false", "pull"]
    assert "CONTAINER_HOST" not in seen["env"] and "CONTAINER_CONNECTION" not in seen["env"]
