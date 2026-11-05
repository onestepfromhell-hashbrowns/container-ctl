import json

import importlib.util

import pytest

from container_ctl.cli.config import config_check, config_set
from container_ctl.cli.image import ImagePrepareError
from container_ctl.contract import check_contract, contract_hash
from container_ctl.image_checks import image_findings
from tests.test_env_overlay import BASE, _commit, cfg_repo  # noqa: F401  (fixture)

CONTRACT = {"schema_version": 1, "env": {
    "type": "object", "required": ["AUTH_URL", "KEY_FILE"], "additionalProperties": True,
    "properties": {"AUTH_URL": {"type": "string", "pattern": "^https?://"},
                   "KEY_FILE": {"type": "string", "x-secret-file": True}}}}
CFG = dict(BASE, env={"AUTH_URL": "https://a.example", "KEY_FILE": "/etc/opt/x/secrets/k.pem"},
           volumes=[{"host_path": "/h", "container_path": "/etc/opt/x/secrets"}])

needs_jsonschema = pytest.mark.skipif(importlib.util.find_spec("jsonschema") is None, reason="requires jsonschema (pip install -e '.[schema]')")


@needs_jsonschema
def test_valid_missing_pattern_and_secret_rules():
    assert check_contract(CFG, CONTRACT) == []
    msgs = check_contract(dict(CFG, env={"AUTH_URL": "ftp://x"}), CONTRACT)
    assert any("AUTH_URL" in m and m.startswith("[ERROR]") for m in msgs) and any("KEY_FILE" in m for m in msgs)
    assert any("不在任何宣告的掛載點" in m for m in check_contract(dict(CFG, env=dict(CFG["env"], KEY_FILE="/tmp/k")), CONTRACT))
    bad = {"schema_version": 1, "env": {"type": "object", "properties": {"KEY": {"type": "string", "x-secret-file": True}}}}
    assert any("_FILE 結尾" in m for m in check_contract(dict(CFG, env={}), bad))


def test_missing_contract_warns_only_for_prod():
    assert check_contract(CFG, None)[0].startswith("[WARN]")
    assert check_contract(dict(CFG, tier="dev"), None) == []


def test_hash_stable():
    assert contract_hash(CONTRACT) == contract_hash(json.loads(json.dumps(CONTRACT)))


@needs_jsonschema
def test_config_check_and_config_set_enforce_contract(cfg_repo):
    tmp, work, bare = cfg_repo
    kw = dict(repos_dir=str(tmp / ".git_repos"), config_dir=str(tmp / ".config_repos"))
    _commit(work, bare, {".deploy/contract.json": CONTRACT,
                         ".deploy/containers/prod-s.json": dict(BASE, env={"AUTH_URL": "https://a", "KEY_FILE": "/k/f"},
                                                                 volumes=[{"host_path": "/h", "container_path": "/k"}])})
    ok, out = config_check("ProjX", **kw)
    assert ok and any("契約 sha256:" in m for m in out)
    with pytest.raises(ImagePrepareError, match="AUTH_URL"):
        config_set("ProjX", "prod-s", {"AUTH_URL": "not-a-url"}, [], **kw)
    config_set("ProjX", "prod-s", {"AUTH_URL": "https://b"}, [], **kw)  # 合法值通過（預覽）
    _commit(work, bare, {".deploy/envs/vps/containers/prod-s.json": {"env": {"AUTH_URL": "nope"}}})
    ok, out = config_check("ProjX", "vps", **kw)
    assert not ok and any("AUTH_URL" in m for m in out)


def test_image_env_scan_flags_addresses_and_secrets_not_benign():
    img = {"Config": {"WorkingDir": "/opt/x", "Env": ["PATH=/usr/bin", "API_URL=https://x.example", "DB=10.0.0.5:5432",
                                                       "SVC=db:5432", "DB_PASSWORD=pw", "DB_PASSWORD_FILE=/s/p", "MODE=prod"]}}
    out = image_findings(img, "svc")
    flagged = " ".join(out)
    assert "API_URL" in flagged and "DB " in flagged + " " and "SVC" in flagged and "DB_PASSWORD " in flagged + " "
    assert "PATH" not in flagged and "DB_PASSWORD_FILE" not in flagged and "MODE" not in flagged
    assert all(m.startswith("[WARN]") for m in out)
