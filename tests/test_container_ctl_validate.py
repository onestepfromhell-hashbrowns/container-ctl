"""
test_container_ctl_validate.py — TICKET-02: container-ctl validate 子命令測試
涵蓋:
- 有效 manifest + pinned digest + 存在的 volume → exit 0
- 缺失非自動建立目錄 → exit 1 + 路徑列出
- :latest tag 或未包含 @sha256: → exit 1 + digest 錯誤
- 損毀或缺失 manifest → exit 1
- --output json 輸出格式驗證
- 省略 ref 時使用 HEAD，支援指定明確 ref
- --quiet 抑制非錯誤輸出
- Zero side-effects 驗證
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import shutil

needs_podman = pytest.mark.skipif(shutil.which("podman") is None, reason="requires podman")

from container_ctl.cli.validate import run_validation, validate
from container_ctl.cli.main import build_parser

SAMPLE_SHA256 = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
def fake_repo_setup(tmp_path):
    repos_dir = tmp_path / "repos"
    repos_dir.mkdir()
    repo_dir = repos_dir / "ExampleProj.git"
    repo_dir.mkdir()
    return {"repos_dir": str(repos_dir), "repo_dir": str(repo_dir)}


class TestContainerCtlValidate:
    @needs_podman
    def test_validate_ok_exit_0(self, fake_repo_setup, tmp_path, capsys):
        manifest_data = {
            "schema_version": 1,
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        # Host volume that exists in read roots
        host_vol = tmp_path / "data"
        host_vol.mkdir()
        container_data = {
            "name": "api",
            "image": f"registry.example.com/app:1.0.0@{SAMPLE_SHA256}",
            "volumes": [
                {
                    "host_path": str(host_vol),
                    "container_path": "/app/data",
                    "options": "rw",
                }
            ],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]), \
             patch("container_ctl.cli.validate.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)):

            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="table", quiet=False, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 0

            captured = capsys.readouterr()
            assert "[OK]" in captured.out

    def test_validate_missing_non_autocreatable_volume_exit_1(self, fake_repo_setup, tmp_path, capsys):
        manifest_data = {
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        missing_vol = tmp_path / "non_existent_vol"
        # do not create missing_vol!
        container_data = {
            "name": "api",
            "image": f"registry.example.com/app:1.0.0@{SAMPLE_SHA256}",
            "volumes": [
                {
                    "host_path": str(missing_vol),
                    "container_path": "/app/data",
                    "options": "rw",
                }
            ],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]), \
             patch("container_ctl.cli.validate.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)):

            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="table", quiet=False, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert str(missing_vol) in captured.out or str(missing_vol) in captured.err

    def test_validate_latest_tag_exit_1(self, fake_repo_setup, tmp_path, capsys):
        manifest_data = {
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        container_data = {
            "name": "api",
            "image": "registry.example.com/app:latest",
            "volumes": [],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]):

            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="table", quiet=False, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "Image Pinned Digest 違規" in captured.out or "Image Pinned Digest 違規" in captured.err

    def test_validate_missing_manifest_exit_1(self, fake_repo_setup, capsys):
        from container_ctl.repo_reader import DeployConfigNotFoundError

        with patch("container_ctl.cli.validate.read_manifest", side_effect=DeployConfigNotFoundError("manifest.json 不存在")):
            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="table", quiet=False, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 1

            captured = capsys.readouterr()
            assert "manifest.json 不存在" in captured.out or "manifest.json 不存在" in captured.err

    @needs_podman

    def test_validate_json_output_format(self, fake_repo_setup, tmp_path, capsys):
        manifest_data = {
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        container_data = {
            "name": "api",
            "image": f"registry.example.com/app:1.0.0@{SAMPLE_SHA256}",
            "volumes": [],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]):

            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="json", quiet=False, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 0

            captured = capsys.readouterr()
            parsed = json.loads(captured.out)
            assert parsed["project"] == "ExampleProj"
            assert parsed["ref"] == "HEAD"
            assert parsed["ok"] is True
            assert isinstance(parsed["messages"], list)

    def test_validate_ref_defaults_to_head_and_explicit_ref(self, fake_repo_setup):
        parser = build_parser()
        args1 = parser.parse_args(["validate", "ExampleProj"])
        assert args1.ref == "HEAD"

        args2 = parser.parse_args(["validate", "ExampleProj", "v1.2.3"])
        assert args2.ref == "v1.2.3"

    @needs_podman

    def test_validate_quiet_suppresses_stdout(self, fake_repo_setup, tmp_path, capsys):
        manifest_data = {
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        container_data = {
            "name": "api",
            "image": f"registry.example.com/app:1.0.0@{SAMPLE_SHA256}",
            "volumes": [],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]):

            with pytest.raises(SystemExit) as exc_info:
                validate("ExampleProj", ref="HEAD", output="table", quiet=True, repos_dir=fake_repo_setup["repos_dir"])
            assert exc_info.value.code == 0

            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == ""

    @needs_podman

    def test_validate_zero_side_effects(self, fake_repo_setup, tmp_path):
        """驗證 validate 不會在 storage 目錄補建任何資料夾"""
        manifest_data = {
            "project_name": "ExampleProj",
            "version": "v1.0.0",
            "dependencies": ["api"],
        }
        storage_dir = tmp_path / "storage" / "volumes"
        target_vol = storage_dir / "ExampleProj" / "api"
        container_data = {
            "name": "api",
            "image": f"registry.example.com/app:1.0.0@{SAMPLE_SHA256}",
            "volumes": [
                {
                    "container_path": "/app/data",
                }
            ],
        }

        with patch("container_ctl.cli.validate.read_manifest", return_value=manifest_data), \
             patch("container_ctl.cli.validate.list_container_configs", return_value=[container_data]), \
             patch("container_ctl.volume_resolver.DEFAULT_VOLUMES_DIR", storage_dir), \
             patch("container_ctl.volume_resolver.ALLOWED_MOUNT_READ_ROOTS", (tmp_path.resolve(),)), \
             patch("container_ctl.volume_resolver.ALLOWED_AUTO_CREATE_ROOTS", (storage_dir.resolve(),)):

            ok, messages = run_validation("ExampleProj", ref="HEAD", repos_dir=fake_repo_setup["repos_dir"])
            assert ok is True
            # Zero side-effects: storage_dir must NOT be created
            assert not target_vol.exists()


def test_list_container_configs_tier_filter():
    import json
    from unittest.mock import patch
    from container_ctl import repo_reader

    files = {
        ".deploy/manifest.json": {"project_name": "p", "version": "v1", "dependencies": ["dev-a", "prod-a"]},
        ".deploy/containers/dev-a.json": {"image": "x@sha256:1"},
        ".deploy/containers/prod-a.json": {"image": "x:1"},
    }
    with patch.object(repo_reader, "_git_show", side_effect=lambda r, ref, path: json.dumps(files[path])):
        assert [c["image"] for c in repo_reader.list_container_configs("r", "m", tier="dev")] == ["x@sha256:1"]
        try:
            repo_reader.list_container_configs("r", "m")
            assert False
        except repo_reader.PinnedDigestRequiredError:
            pass
