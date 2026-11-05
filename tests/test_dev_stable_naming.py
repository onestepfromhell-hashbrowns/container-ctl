from container_ctl.dag_resolver import ServiceAction
from container_ctl.quadlet_gen import _container_name, _db_instance_id, _inst_id, _render_container_unit


def test_dev_workspace_name_stays_stable_across_manifest_versions():
    action = ServiceAction(name="dev-app-hub", action="NEW", config={"tier": "dev"})

    for version in ("v0.1.0", "v0.2.0"):
        assert _inst_id(action, "AppHub", version) == "dev-app-hub"
        assert _container_name(action, "AppHub", version) == "svc-dev-app-hub"
        assert _db_instance_id(action, "AppHub", version) == "AppHub-dev-app-hub"
        unit = _render_container_unit(action, "localhost/hub:dev", "AppHub", version)
        assert "ContainerName=svc-dev-app-hub" in unit


def test_prod_and_explicit_shared_scope_keep_distinct_identity():
    prod = ServiceAction(name="prod-app-hub", action="NEW", config={"tier": "prod"})
    shared_dev = ServiceAction(
        name="dev-postgres", action="NEW", config={"tier": "dev", "scope": "shared"}
    )

    assert _container_name(prod, "AppHub", "v1") == "svc-prod-app-hub-v1"
    assert _db_instance_id(prod, "AppHub", "v1") == "AppHub-prod-app-hub-v1"
    assert _container_name(shared_dev, "AppHub", "v1") == "svc-dev-postgres-shared"


def test_prod_service_version_overrides_manifest_for_database_and_container():
    action = ServiceAction("prod-app", "NEW", {"tier": "prod", "version": "0.2.0"})
    assert _container_name(action, "app-proj", "0.1.0") == "svc-prod-app-0.2.0"
    assert _db_instance_id(action, "app-proj", "0.1.0") == "app-proj-prod-app-0.2.0"
