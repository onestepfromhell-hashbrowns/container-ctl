"""test_quadlet_ssot_mount.py — 測試 Quadlet 生成器 SSOT Extension Mount 合流與白名單驗證"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import container_ctl.quadlet_gen as quadlet_gen
from container_ctl.dag_resolver import DeployPlan, ServiceAction


class TestQuadletSSOTMount(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.services_dir = os.path.join(self.tmpdir, ".services")
        self.proj_dir = os.path.join(self.services_dir, "TestProj")
        os.makedirs(self.proj_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_resolve_extension_volumes_empty_if_no_file(self):
        """當 ext-default.json 不存在時回傳空清單"""
        vols = quadlet_gen.resolve_extension_volumes("NonExistent", self.services_dir)
        self.assertEqual(vols, [])

    @unittest.skipUnless(shutil.which("extscope"), "requires the extscope CLI")

    def test_resolve_extension_volumes_whitelist_rejection(self):
        """當 Scope 包含非法路徑（如 /etc/secret）時拋出 PermissionError"""
        bad_scope = {
            "$schema": "https://site.internal/schemas/extension-v1.json",
            "uuid": "00000001-1111-4000-8000-000000000001",
            "name": "bad-scope",
            "enabled": True,
            "include_items": [
                {
                    "src": "/etc/shadow",
                    "dst_fhs": "/opt/ext/fhs/shadow",
                    "mode": "ro",
                    "type": "mount",
                }
            ]
        }
        scope_path = os.path.join(self.proj_dir, "ext-default.json")
        with open(scope_path, "w", encoding="utf-8") as f:
            json.dump(bad_scope, f)

        with self.assertRaises(PermissionError):
            quadlet_gen.resolve_extension_volumes("TestProj", self.services_dir)

    def test_render_container_unit_injects_extra_volumes_for_dev(self):
        """驗證 dev tier 容器能正確注入 extra_volumes 並自動去重"""
        action = ServiceAction(
            name="dev-app",
            action="NEW",
            config={
                "name": "dev-app",
                "tier": "dev",
                "image": "localhost/app:dev",
                "volumes": [
                    {
                        "host_path": "/srv/site/projects/.worktrees/TestProj",
                        "container_path": "/workspace",
                        "options": "rw",
                    },
                    {
                        "host_path": "/srv/site/projects/.agent_brains/TestProj/brain",
                        "container_path": "/opt/ext/fhs/antigravity/brain",
                        "options": "rw",
                    }
                ]
            }
        )
        extra_vols = [
            {
                "host_path": "/srv/site/projects/.agent_brains/TestProj/brain",
                "container_path": "/opt/ext/fhs/antigravity/brain",
                "options": "ro",
            },
            {
                "host_path": "/var/opt/container-ctl/cache/ms-playwright",
                "container_path": "/opt/ext/fhs/browsers/ms-playwright",
                "options": "ro",
            }
        ]
        with unittest.mock.patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (Path("/srv/site"), Path("/var/opt/container-ctl"))), \
             unittest.mock.patch("container_ctl.volume_resolver.ALLOWED_AUTO_CREATE_ROOTS", (Path("/srv/site/projects"),)):
            rendered = quadlet_gen._render_container_unit(
                action,
                image="localhost/app:dev",
                project="TestProj",
                version="v1.0.0",
                extra_volumes=extra_vols,
            )

        # 驗證包含了 ms-playwright 掛載
        self.assertIn("Volume=/var/opt/container-ctl/cache/ms-playwright:/opt/ext/fhs/browsers/ms-playwright:ro", rendered)
        # 驗證原本已有的 brain 沒有重複出現兩次
        self.assertEqual(rendered.count("/opt/ext/fhs/antigravity/brain"), 1)

    @unittest.skipUnless(shutil.which("extscope"), "requires the extscope CLI")

    def test_resolve_extension_volumes_host_adaptation_rejection(self):
        """Ticket 03: 當 Scope 宣告為 Host Adaptation (adaptation_target: 'host') 時拋出 HostAdaptationBindingError"""
        host_scope = {
            "$schema": "https://site.internal/schemas/extension-v1.json",
            "uuid": "00000001-2222-4000-8000-000000000001",
            "name": "host-adaptation-scope",
            "adaptation_target": "host",
            "enabled": True,
            "include_items": [
                {
                    "src": "/var/opt/container-ctl/policies/host-quota.json",
                    "dst_fhs": "/etc/opt/container-ctl/policies/host-quota.json",
                    "mode": "ro",
                    "type": "mount",
                }
            ],
        }
        scope_path = os.path.join(self.proj_dir, "ext-default.json")
        with open(scope_path, "w", encoding="utf-8") as f:
            json.dump(host_scope, f)

        with self.assertRaises(quadlet_gen.HostAdaptationBindingError) as cm:
            quadlet_gen.resolve_extension_volumes("TestProj", self.services_dir)

        self.assertIn("Host Adaptation", str(cm.exception))
        self.assertIn("adaptation_target: 'host'", str(cm.exception))

    def test_render_container_unit_rejects_host_adaptation_volumes(self):
        """Ticket 03: 傳入標記有 adaptation_target: 'host' 之 volume 時 _render_container_unit 拒絕渲染"""
        action = ServiceAction(
            name="prod-service",
            action="NEW",
            config={
                "name": "prod-service",
                "tier": "prod",
                "image": "localhost/app:prod",
                "volumes": [],
            },
        )
        bad_vols = [
            {
                "host_path": "/var/opt/container-ctl/policies/host-quota.json",
                "container_path": "/etc/opt/container-ctl/policies/host-quota.json",
                "options": "ro",
                "adaptation_target": "host",
            }
        ]
        with self.assertRaises(quadlet_gen.HostAdaptationBindingError):
            quadlet_gen._render_container_unit(
                action,
                image="localhost/app:prod",
                project="TestProj",
                version="v1.0.0",
                extra_volumes=bad_vols,
            )
