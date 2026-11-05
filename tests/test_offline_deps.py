from container_ctl.offline_deps import check_dockerfile_offline, verify_wheelhouse
import hashlib


def test_warns_when_pip_install_without_no_index():
    text = "FROM python:3.11\nRUN pip install -r requirements.txt\n"
    warns = check_dockerfile_offline(text, "Dockerfile.dev")
    assert len(warns) == 1 and "--no-index" in warns[0]


def test_no_warn_with_no_index_even_across_continuation():
    text = "RUN pip install --no-cache-dir \\\n    --no-index --find-links /wh -r r.txt\n"
    assert check_dockerfile_offline(text) == []


def test_ignores_comments_and_non_run():
    text = "# RUN pip install foo\nCOPY pip install /x\n"
    assert check_dockerfile_offline(text) == []


def test_python_m_pip_detected():
    assert check_dockerfile_offline("RUN python3 -m pip install foo\n")


def test_verify_wheelhouse(tmp_path):
    (tmp_path / "a.whl").write_bytes(b"x")
    (tmp_path / "SHA256SUMS").write_text(f"{hashlib.sha256(b'x').hexdigest()}  a.whl\n")
    assert verify_wheelhouse(tmp_path) == []
    (tmp_path / "a.whl").write_bytes(b"y")
    assert verify_wheelhouse(tmp_path)
    assert verify_wheelhouse(tmp_path / "none")
