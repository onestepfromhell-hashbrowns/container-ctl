import json
from unittest.mock import Mock
from pathlib import Path

import pytest

from container_ctl import prod_verify as pv
from container_ctl.cli.image import ImagePrepareError
from container_ctl.db import init_db


@pytest.fixture
def verification(monkeypatch, tmp_path):
    cfg = {'name': 'api', 'tier': 'prod', 'version': '1.0', 'read_only': True,
           'image': 'localhost/api@sha256:' + 'a' * 64, 'env': {'APP_DATA_DIR': '/var/data'},
           'volumes': [{'host_path': '/var/opt/container-ctl/projects/P/var', 'container_path': '/var/data', 'options': 'rw'}]}
    live = {'State': {'Running': True, 'StartedAt': 'now', 'Health': {'Status': 'healthy'}},
            'HostConfig': {'ReadonlyRootfs': True}, 'Config': {'Env': ['APP_DATA_DIR=/var/data']},
            'Mounts': [{'Destination': '/var/data', 'Source': '/var/opt/container-ctl/projects/P/var', 'RW': True, 'Type': 'bind'}]}
    monkeypatch.setattr(pv, 'run_validation', lambda *a, **k: (True, []))
    monkeypatch.setattr(pv, 'resolve_deploy_repo', lambda *a: ('repo', 'config'))
    monkeypatch.setattr(pv, 'read_manifest', lambda *a: {'version': '1.0'})
    monkeypatch.setattr(pv, 'list_container_configs', lambda *a, **k: [cfg])
    monkeypatch.setattr(pv, 'find_container_unit_drift', lambda *a: set())
    monkeypatch.setattr(pv, 'audit_prod_image', lambda *a: (True, []))
    monkeypatch.setattr(pv, 'REPORTS_ROOT', tmp_path / 'reports')
    monkeypatch.setattr(pv, 'init_db', lambda: init_db(str(tmp_path / 'state.db')))
    monkeypatch.setattr(Path, 'is_symlink', lambda p: True)
    monkeypatch.setattr(pv.subprocess, 'run', lambda args, **kw: Mock(returncode=0, stdout=json.dumps([live])))
    monkeypatch.setattr(pv.subprocess, 'check_output', lambda *a, **k: 'abc123\n')
    return live, tmp_path


@pytest.mark.parametrize('damage', ['unhealthy', 'writable_root', 'wrong_mount', 'wrong_env'])
def test_runtime_drift_does_not_write_success_evidence(verification, damage):
    live, tmp = verification
    if damage == 'unhealthy': live['State']['Health']['Status'] = 'unhealthy'
    elif damage == 'writable_root': live['HostConfig']['ReadonlyRootfs'] = False
    elif damage == 'wrong_mount': live['Mounts'][0]['Source'] = '/srv/dev'
    elif damage == 'wrong_env': live['Config']['Env'] = ['APP_DATA_DIR=/workspace']
    with pytest.raises(ImagePrepareError): pv.verify_prod('P', 'HEAD')
    assert not (tmp / 'reports').exists()
    assert not (tmp / 'state.db').exists()


def test_verified_runtime_persists_observed_lifecycle_evidence(verification):
    _, tmp = verification
    result = pv.verify_prod('P', 'HEAD')
    assert result['services'][0]['healthy']
    assert (tmp / 'reports/P/verified-prod.json').exists()
    conn = init_db(str(tmp / 'state.db'))
    assert [r[0] for r in conn.execute('SELECT event_type FROM lifecycle_events')] == ['provision', 'start', 'verify']
    conn.close()
