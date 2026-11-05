import json
from unittest.mock import Mock

import pytest

from container_ctl import pg_migration as pg
from container_ctl import paths
from container_ctl.cli.image import ImagePrepareError
from container_ctl.db import init_db
from pathlib import Path


@pytest.fixture
def cutover(monkeypatch, tmp_path):
    cfg = {'name': 'prod-postgres', 'tier': 'prod', 'scope': 'shared',
           'postgres_major': '15', 'volumes': [
               {'container_path': d, 'host_path': paths.PROJECTS_CODE_ROOT + '/P/var/' + str(i)}
               for i, d in enumerate(['/var/lib/postgresql/data', '/var/lib/neutral-postgres-state'])]}
    source = {'Image': 'old', 'State': {'Running': True}, 'Config': {'Labels': {}},
              'Mounts': [{'Destination': v['container_path'], 'Source': '/srv/old/' + str(i)}
                         for i, v in enumerate(cfg['volumes'])]}
    monkeypatch.setattr(pg, 'resolve_deploy_repo', lambda p: ('repo', 'config'))
    monkeypatch.setattr(pg, 'read_manifest', lambda *a: {'version': '1'})
    monkeypatch.setattr(pg, 'list_container_configs', lambda *a: [cfg])
    monkeypatch.setattr(pg, 'run_validation', lambda *a: (True, []))
    monkeypatch.setattr(pg, 'check_release_anchor', lambda *a, **k: (True, []))
    monkeypatch.setattr(pg, '_container_name', lambda *a: 'new')
    monkeypatch.setattr(pg, 'BACKUPS_ROOT', tmp_path)
    monkeypatch.setattr(pg, 'init_db', lambda: init_db(str(tmp_path / 'state.db')))
    inspections = Mock(side_effect=[source, dict(source, State={'Running': False}),
                                    {'State': {'Running': True, 'Health': {'Status': 'healthy'}}}])
    monkeypatch.setattr(pg, '_inspect', inspections)
    def command(a):
        if '-cpf' in a:
            Path(a[a.index('-cpf') + 1]).write_bytes(b'archive')
        return '15\n' if a[:3] == ['podman', 'exec', 'old'] else ''

    commands = Mock(side_effect=command)
    monkeypatch.setattr(pg, '_run', commands)

    def subprocess_run(args, **kwargs):
        if 'pg_dumpall' in args:
            kwargs['stdout'].write(b'-- complete backup\n')
            return Mock(returncode=0)
        return Mock(returncode=1)  # target does not exist

    monkeypatch.setattr(pg.subprocess, 'run', subprocess_run)
    deploy = Mock()
    monkeypatch.setattr(pg, 'redeploy', deploy)
    return source, commands, deploy, tmp_path


def test_offline_copy_follows_verified_stop_and_preserves_source(cutover):
    _, commands, deploy, tmp = cutover
    pg.migrate_postgres('P', 'HEAD', 'prod-postgres', 'old', yes=True)
    args = [c.args[0] for c in commands.call_args_list]
    stop = args.index(['podman', 'stop', '--time', '60', 'old'])
    copy = next(i for i, a in enumerate(args) if a[:4] == ['podman', 'unshare', 'cp', '-a'])
    assert stop < copy
    assert not any(a[:2] == ['podman', 'rm'] for a in args)
    deploy.assert_called_once_with('P', 'HEAD', yes=True, only=['prod-postgres'])
    assert json.loads(next(tmp.rglob('receipt.json')).read_text())['status'] == 'healthy'


def test_deploy_failure_restarts_original_source(cutover):
    _, commands, deploy, tmp = cutover
    deploy.side_effect = ImagePrepareError('failed')
    with pytest.raises(ImagePrepareError, match='failed'):
        pg.migrate_postgres('P', 'HEAD', 'prod-postgres', 'old', yes=True)
    assert commands.call_args_list[-1].args[0] == ['podman', 'start', 'old']
    assert json.loads(next(tmp.rglob('receipt.json')).read_text())['status'] == 'rolled_back'


def test_nonempty_target_never_stops_source(cutover):
    _, commands, deploy, _ = cutover
    commands.side_effect = lambda a: 'existing-data\n' if 'find' in a else ''
    with pytest.raises(ImagePrepareError, match='nonempty'):
        pg.migrate_postgres('P', 'HEAD', 'prod-postgres', 'old', yes=True)
    assert not any('stop' in c.args[0] for c in commands.call_args_list)
    deploy.assert_not_called()


def test_managed_source_is_rejected_before_stop(cutover):
    source, commands, deploy, _ = cutover
    source['Config']['Labels']['PODMAN_SYSTEMD_UNIT'] = 'old.service'
    with pytest.raises(ImagePrepareError, match='unmanaged'):
        pg.migrate_postgres('P', 'HEAD', 'prod-postgres', 'old', yes=True)
    commands.assert_not_called()


def test_unapproved_cutover_rejected(cutover):
    _, commands, _, _ = cutover
    with pytest.raises(ImagePrepareError, match='--yes'):
        pg.migrate_postgres('P', 'HEAD', 'prod-postgres', 'old')
    commands.assert_not_called()


def test_unshare_uses_local_podman_even_when_cli_sets_api_socket(monkeypatch):
    monkeypatch.setenv('CONTAINER_HOST', 'unix:///run/user/1000/podman/podman.sock')
    run = Mock(return_value=Mock(returncode=0, stdout=''))
    monkeypatch.setattr(pg.subprocess, 'run', run)
    pg._run(['podman', 'unshare', 'true'])
    assert 'CONTAINER_HOST' not in run.call_args.kwargs['env']
