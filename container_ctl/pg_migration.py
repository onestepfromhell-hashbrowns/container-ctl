"""Offline cutover of a legacy PostgreSQL container into declared Quadlet storage.

The old stopped container and its data are retained for rollback. Never copy
live PGDATA, overwrite target data, or initialize a replacement cluster.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

from container_ctl.lifecycle_adapter import get_default_manager
import time
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.cli.deploy import redeploy, _log_event
from container_ctl.cli.image import ImagePrepareError
from container_ctl.cli.validate import run_validation
from container_ctl.dag_resolver import ServiceAction
from container_ctl.db import init_db
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.quadlet_gen import _container_name, _db_instance_id
from container_ctl.release_anchor import check_release_anchor
from container_ctl.repo_reader import list_container_configs, read_manifest
from container_ctl import paths

BACKUPS_ROOT = Path(paths.PG_BACKUPS_DIR)


def _run(args):
    env = os.environ.copy()
    if args[:2] == ['podman', 'unshare']:
        # unshare is a local-only command; the CLI normally selects the
        # rootless API socket for other Podman operations.
        env.pop('CONTAINER_HOST', None)
        env.pop('CONTAINER_CONNECTION', None)
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
    if result.returncode:
        # Never echo stdout: database backups and config can contain secrets.
        raise ImagePrepareError(f"command failed: {args[0]} {args[1]} (exit {result.returncode})")
    return result.stdout


def _inspect(name):
    return json.loads(_run(["podman", "inspect", name]))[0]


def migrate_postgres(project, ref, service, source, *, yes=False):
    if not yes:
        raise ImagePrepareError("offline cutover requires --yes")
    repo, _ = resolve_deploy_repo(project)
    manifest = read_manifest(repo, ref)
    configs = list_container_configs(repo, ref)
    cfg = next((c for c in configs if c['name'] == service), None)
    if not cfg or cfg.get('tier') != 'prod' or cfg.get('scope') != 'shared':
        raise ImagePrepareError("cutover requires a declared shared prod service")
    ok, messages = run_validation(project, ref)
    if not ok:
        raise ImagePrepareError('\n'.join(messages))
    ok, messages = check_release_anchor(str(repo), [cfg], manifest['version'], strict=True)
    if not ok:
        raise ImagePrepareError('\n'.join(messages))
    target = _container_name(ServiceAction(service, 'NEW', cfg), project, manifest['version'])
    if source == target:
        raise ImagePrepareError("source and target containers must differ")
    if subprocess.run(['podman', 'container', 'exists', target], capture_output=True).returncode == 0:
        raise ImagePrepareError("target container already exists; refusing repeat cutover")
    original = _inspect(source)
    if not original['State']['Running']:
        raise ImagePrepareError("source must be running for the logical backup")
    if (original['Config'].get('Labels') or {}).get('PODMAN_SYSTEMD_UNIT'):
        raise ImagePrepareError("this entry is only for unmanaged legacy containers")
    mounts = {m['Destination']: m['Source'] for m in original['Mounts']}
    declared = {v['container_path']: v['host_path'] for v in cfg.get('volumes', [])}
    copies = []
    for destination in ['/var/lib/postgresql/data', '/var/lib/neutral-postgres-state']:
        src, dst = mounts.get(destination), declared.get(destination)
        if not src or not dst or Path(src).resolve() == Path(dst).resolve():
            raise ImagePrepareError("missing or non-isolated data/state migration mounts")
        if not str(Path(dst).resolve()).startswith(f'{paths.PROJECTS_CODE_ROOT.rstrip("/")}/{project}/'):
            raise ImagePrepareError("target data must use the prod project root")
        nonempty = _run(['podman', 'unshare', 'find', dst, '-mindepth', '1', '-maxdepth', '1', '-print', '-quit'])
        if nonempty.strip():
            raise ImagePrepareError("target data/state is nonempty; refusing overwrite")
        copies.append((src, dst))
    version = _run(['podman', 'exec', source, 'cat', '/var/lib/postgresql/data/PG_VERSION']).strip()
    expected = str(cfg.get('postgres_major', ''))
    if version != expected:
        raise ImagePrepareError("source PostgreSQL major differs from declared postgres_major")
    print(f'PLAN: backup {source}; stop; copy data/state; deploy {project}/{service} -> {target}', flush=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BACKUPS_ROOT / f'{project}-{service}-{stamp}'
    backup.mkdir(parents=True, mode=0o700)
    dump = backup / 'cluster.sql'
    with dump.open('xb') as out:
        os.chmod(dump, 0o600)
        result = subprocess.run(['podman', 'exec', '--user', 'postgres', source,
                                 'pg_dumpall', '-U', 'infra_admin'], stdout=out, stderr=subprocess.PIPE, timeout=120)
    if result.returncode or dump.stat().st_size == 0:
        raise ImagePrepareError('logical backup failed; source untouched')
    record = {'project': project, 'service': service, 'ref': ref, 'source': source,
              'target': target, 'source_image': original['Image'], 'postgres_major': version,
              'copies': copies, 'dump_sha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
              'status': 'backed_up'}
    receipt = backup / 'receipt.json'
    receipt.write_text(json.dumps(record, indent=2) + '\n')
    stopped = False
    try:
        _run(['podman', 'stop', '--time', '60', source])
        stopped = True
        if _inspect(source)['State']['Running']:
            raise ImagePrepareError('source failed to stop; refusing live PGDATA copy')
        record['archives'] = []
        for index, (src, dst) in enumerate(copies):
            archive = backup / f'volume-{index}.tar'
            _run(['podman', 'unshare', 'tar', '-cpf', str(archive), '-C', src, '.'])
            _run(['podman', 'unshare', 'tar', '-tf', str(archive)])
            _run(['podman', 'unshare', 'cp', '-a', src + '/.', dst])
            _run(['podman', 'unshare', 'diff', '-qr', src, dst])
            record['archives'].append({'path': str(archive),
                'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()})
        record['status'] = 'copied'
        receipt.write_text(json.dumps(record, indent=2) + '\n')
        redeploy(project, ref, yes=True, only=[service])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                live = _inspect(target)
                health = live['State'].get('Health') or live['State'].get('Healthcheck') or {}
                if live['State']['Running'] and health.get('Status') == 'healthy':
                    now = datetime.now(timezone.utc).isoformat()
                    action = ServiceAction(service, 'NEW', cfg)
                    instance_id = _db_instance_id(action, project, manifest['version'])
                    conn = init_db()
                    for event in ('provision', 'start'):
                        _log_event(conn, instance_id, event, 'deploy_migrate_postgres',
                                   {'container_name': target, 'health': 'healthy', 'receipt': str(receipt)}, now)
                    conn.commit()
                    conn.close()
                    record['status'] = 'healthy'
                    receipt.write_text(json.dumps(record, indent=2) + '\n')
                    print(f'[OK] migrated: {target} healthy; backup and rollback source retained at {backup}')
                    return
            except ImagePrepareError:
                pass
            time.sleep(2)
        raise ImagePrepareError('replacement failed health verification')
    except BaseException:
        if stopped:
            # A failed preflight can leave no unit at all. Failure to stop a
            # nonexistent target must never prevent recovery of the source.
            exists = subprocess.run(['podman', 'container', 'exists', target], capture_output=True).returncode == 0
            if exists:
                try:
                    if get_default_manager().stop_unit_blocking(f'{target}.service', timeout=120).returncode:
                        raise ImagePrepareError('command failed: systemctl stop')
                except ImagePrepareError:
                    _run(['podman', 'stop', '--time', '60', target])
            _run(['podman', 'start', source])
            record['status'] = 'rolled_back'
            receipt.write_text(json.dumps(record, indent=2) + '\n')
            print('[ROLLBACK] original PostgreSQL restarted; copied target retained for inspection')
        raise
