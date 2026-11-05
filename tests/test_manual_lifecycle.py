from container_ctl.dag_resolver import ServiceAction
from container_ctl.quadlet_gen import _render_container_unit


def test_render_container_unit_manual_mode():
    action = ServiceAction(
        name="test-relay-server",
        action="NEW",
        config={
            "tier": "infra",
            "lifecycle_policy": {"mode": "manual"},
        },
    )
    unit_content = _render_container_unit(action, "localhost/tmp_relay_agent:latest")
    assert "[Install]" not in unit_content
    assert "WantedBy=default.target" not in unit_content


def test_render_container_unit_always_on_mode():
    action = ServiceAction(
        name="dev-relay-server",
        action="NEW",
        config={
            "tier": "infra",
            "lifecycle_policy": {"mode": "always_on"},
        },
    )
    unit_content = _render_container_unit(action, "localhost/tmp_relay_agent:latest")
    assert "[Install]" in unit_content
    assert "WantedBy=default.target" in unit_content


def test_render_container_unit_with_comment():
    action = ServiceAction(
        name="dev-relay-server",
        action="NEW",
        config={
            "tier": "infra",
            "comment": [
                "開發除錯專用中繼伺服器",
                "HOST_HOSTNAME 與 HOST_IP 為宿主機網路設定",
            ],
            "lifecycle_policy": {"mode": "always_on"},
        },
    )
    unit_content = _render_container_unit(action, "localhost/tmp_relay_agent:latest")
    assert "# 【容器藍圖備註 (Blueprint Comment)】" in unit_content
    assert "# 開發除錯專用中繼伺服器" in unit_content
    assert "# HOST_HOSTNAME 與 HOST_IP 為宿主機網路設定" in unit_content
    assert "[Unit]" in unit_content


def test_render_container_unit_dev_tier_default_manual():
    action = ServiceAction(
        name="dev-my-service",
        action="NEW",
        config={
            "tier": "dev",
        },
    )
    unit_content = _render_container_unit(action, "localhost/myservice_dev:latest")
    assert "[Install]" not in unit_content
    assert "WantedBy=default.target" not in unit_content
    assert "RunInit=true" in unit_content


def test_render_container_unit_run_init_modes():
    # 1. sleep infinity triggers RunInit=true
    action_sleep = ServiceAction(
        name="custom-sandbox",
        action="NEW",
        config={
            "tier": "custom",
            "command": "sleep infinity",
        },
    )
    assert "RunInit=true" in _render_container_unit(action_sleep, "localhost/img:latest")

    # 2. explicit run_init=True triggers RunInit=true
    action_explicit = ServiceAction(
        name="prod-service-with-workers",
        action="NEW",
        config={
            "tier": "prod",
            "run_init": True,
            "command": "python worker.py",
        },
    )
    assert "RunInit=true" in _render_container_unit(action_explicit, "localhost/img:latest")

    # 3. standard prod service without run_init does NOT have RunInit=true
    action_prod = ServiceAction(
        name="prod-normal",
        action="NEW",
        config={
            "tier": "prod",
            "command": "python app.py",
        },
    )
    assert "RunInit=true" not in _render_container_unit(action_prod, "localhost/img:latest")



