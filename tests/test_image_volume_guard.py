import json
from unittest.mock import patch

from container_ctl.image_volume_guard import check_image_volumes, runtime_image_ref


IMAGE_ID = "a" * 64
IMAGE = f"localhost/example@sha256:{IMAGE_ID}"


def test_local_quadlet_ref_uses_pinned_id():
    assert runtime_image_ref(IMAGE) == f"sha256:{IMAGE_ID}"
    assert runtime_image_ref("registry.example/api@sha256:123") == "registry.example/api@sha256:123"


def inspect_result(volumes, image_id=IMAGE_ID):
    return type("Result", (), {
        "returncode": 0,
        "stdout": json.dumps({"Id": f"sha256:{image_id}", "Config": {"Volumes": volumes}}),
        "stderr": "",
    })()


def test_prod_image_volume_must_be_explicit():
    config = {"name": "api", "tier": "prod", "image": IMAGE, "volumes": []}
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=inspect_result({"/var/lib/api": {}})):
        ok, messages = check_image_volumes([config])
    assert not ok
    assert "/var/lib/api" in messages[0]


def test_explicit_volume_and_tmpfs_cover_image_destinations():
    config = {"name": "api", "tier": "prod", "image": IMAGE,
              "volumes": [{"container_path": "/var/lib/api"}], "tmpfs": ["/run/api:size=8M"]}
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=inspect_result({"/var/lib/api": {}, "/run/api": {}})) as inspect:
        ok, _ = check_image_volumes([config])
    assert ok
    assert inspect.call_args.args[0][3] == f"sha256:{IMAGE_ID}"


def test_local_tag_must_match_pinned_id():
    config = {"name": "api", "tier": "prod", "image": IMAGE}
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=inspect_result({}, "b" * 64)):
        ok, messages = check_image_volumes([config])
    assert not ok
    assert "digest 不符" in messages[0]


def test_missing_image_fails_apply_but_warns_during_validate():
    config = {"name": "api", "tier": "prod", "image": IMAGE}
    result = type("Result", (), {"returncode": 125, "stdout": "", "stderr": "image not known"})()
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=result):
        assert check_image_volumes([config])[0] is False
        ok, messages = check_image_volumes([config], require_image=False)
    assert ok
    assert messages[0].startswith("[WARN]")


def test_missing_dev_pinned_image_fails_validate():
    config = {"name": "dev-api", "tier": "dev", "image": IMAGE}
    result = type("Result", (), {"returncode": 125, "stdout": "", "stderr": "image not known"})()
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=result):
        ok, messages = check_image_volumes([config], require_image=False)
    assert not ok
    assert messages[0].startswith("[ERROR]")


def test_dev_image_requires_infra_scope_label():
    config = {"name": "dev-api", "tier": "dev", "image": IMAGE, "volumes": []}
    with patch("container_ctl.image_volume_guard.subprocess.run", return_value=inspect_result({})):
        ok, messages = check_image_volumes([config], require_image=False)
    assert not ok
    assert any("dev-infra" in msg for msg in messages)
