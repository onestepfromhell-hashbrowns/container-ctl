"""exec_run is shipped as a standalone module; it must stay free of intra-package imports."""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "container_ctl" / "exec_run.py"


def test_exec_run_imports_nothing_from_the_package():
    bad = []
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").split(".")[0] == "container_ctl"):
            bad.append(node.module or ".")
        if isinstance(node, ast.Import):
            bad += [a.name for a in node.names if a.name.split(".")[0] == "container_ctl"]
    assert not bad, f"exec_run imports package modules: {bad}"
