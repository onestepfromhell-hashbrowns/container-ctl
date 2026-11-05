"""Verify live prod declarations and persist evidence after deployment."""
import json
import subprocess

from container_ctl.lifecycle_adapter import get_default_manager
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.cli.deploy import _log_event
from container_ctl.cli.image import ImagePrepareError
from container_ctl.cli.image_prod import audit_prod_image
from container_ctl.cli.validate import run_validation
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.db import init_db
from container_ctl.deploy_source import resolve_deploy_repo
from container_ctl.quadlet_gen import find_container_unit_drift, _db_instance_id, _container_name
from container_ctl.repo_reader import list_container_configs, read_manifest
from container_ctl import paths

REPORTS_ROOT = Path(paths.DEPLOYS_DIR)


def verify_prod(project, ref, only=None):
    ok, messages = run_validation(project, ref, tier='prod')
    if not ok:
        raise ImagePrepareError('\n'.join(messages))
    repo, _ = resolve_deploy_repo(project)
    manifest = read_manifest(repo, ref)
    configs = list_container_configs(repo, ref, tier='prod')
    if only:
        if set(only) - {c['name'] for c in configs}:
            raise ImagePrepareError('requested service is not a declared prod service')
        configs = [c for c in configs if c['name'] in only]
    if not configs:
        raise ImagePrepareError('no prod services selected')
    plan = DeployPlan(project, manifest['version'], [ServiceAction(c['name'], 'NEW', c) for c in configs])
    drift = find_container_unit_drift(plan)
    if drift:
        raise ImagePrepareError('Quadlet declaration drift: ' + ', '.join(sorted(drift)))
    verified = []
    for action in plan.actions:
        cfg = action.config
        version = str(cfg.get('version') or manifest['version'])
        ok, messages = audit_prod_image(project, action.name, version, ref)
        if not ok:
            raise ImagePrepareError('\n'.join(messages))
        name = _container_name(action, project, manifest['version'])
        active_ok = get_default_manager().is_active(name + '.service', timeout=5.0)
        result = subprocess.run(['podman', 'inspect', name], capture_output=True, text=True, check=True)
        live = json.loads(result.stdout)[0]
        health = live['State'].get('Health') or live['State'].get('Healthcheck') or {}
        if not active_ok or not live['State']['Running'] or health.get('Status') != 'healthy':
            raise ImagePrepareError(f'{name}: requires active, Running and healthy')
        if cfg.get('read_only') and not live['HostConfig']['ReadonlyRootfs']:
            raise ImagePrepareError(f'{name}: read-only rootfs is not active')
        actual_mounts = {m['Destination']: m for m in live['Mounts']}
        for volume in cfg.get('volumes', []):
            mounted = actual_mounts.get(volume['container_path'])
            expected_rw = 'ro' not in volume.get('options', 'rw').split(',')
            if not mounted or mounted['Type'] != 'bind' or mounted['Source'] != volume['host_path'] or mounted['RW'] != expected_rw:
                raise ImagePrepareError(f'{name}: live mount differs for {volume["container_path"]}')
        env = dict(item.split('=', 1) for item in live['Config'].get('Env', []) if '=' in item)
        if any(env.get(k) != str(v) for k, v in cfg.get('env', {}).items() if not k.startswith('_')):
            raise ImagePrepareError(f'{name}: live environment differs from declaration')
        # Quadlet generates boot dependencies in the runtime generator tree.
        owner = int(cfg.get('owner_uid', 1000))
        wants = Path(f'/run/user/{owner}/systemd/generator/default.target.wants') / (name + '.service')
        if cfg.get('lifecycle_policy', {}).get('mode', 'always_on') == 'always_on' and not wants.is_symlink():
            raise ImagePrepareError(f'{name}: missing generated boot dependency')
        verified.append({'instance_id': _db_instance_id(action, project, manifest['version']),
                         'service': action.name, 'version': version, 'container_name': name,
                         'image': cfg['image'], 'healthy': True, 'read_only': live['HostConfig']['ReadonlyRootfs'],
                         'started_at': live['State']['StartedAt'], 'boot_dependency': str(wants)})
    now = datetime.now(timezone.utc).isoformat()
    commit = subprocess.check_output(['git', '--git-dir', str(repo), 'rev-parse', ref], text=True).strip()
    report = {'project': project, 'config_commit': commit, 'verified_at': now, 'services': verified}
    root = REPORTS_ROOT / project
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'verified-prod.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    conn = init_db()
    for item in verified:
        for event in ('provision', 'start'):
            exists = conn.execute('SELECT 1 FROM lifecycle_events WHERE instance_id=? AND event_type=?',
                                  (item['instance_id'], event)).fetchone()
            if not exists:
                _log_event(conn, item['instance_id'], event, 'deploy_verify',
                           {'observed': True, 'started_at': item['started_at'], 'report': str(path)}, now)
        _log_event(conn, item['instance_id'], 'verify', 'deploy_verify', {'report': str(path), 'healthy': True}, now)
    conn.commit()
    conn.close()
    print(f'[OK] {project}: {len(verified)} prod services verified; {path}')
    return report
