"""A partial infra deployment must preserve networks owned by other services."""
from unittest.mock import patch
from container_ctl.dag_resolver import DeployPlan, ServiceAction
from container_ctl.quadlet_gen import generate_units


def test_adding_database_preserves_existing_infra_network(tmp_path):
    services = tmp_path / "services"
    project = services / "infra"
    project.mkdir(parents=True)
    existing = project / "net-cloudflared.network"
    content = "[Network]\nNetworkName=net-cloudflared\nDriver=bridge\n"
    existing.write_text(content)
    quadlets = tmp_path / "quadlets"
    quadlets.mkdir()
    link = quadlets / existing.name
    link.symlink_to(existing)
    plan = DeployPlan("infra", "v1", [ServiceAction("dev-postgres", "NEW", {
        "name": "dev-postgres", "tier": "dev", "scope": "shared",
        "image": "example.invalid/postgres@sha256:" + "0" * 64,
        "networks": ["net-dev-db"], "lifecycle_policy": {"mode": "always_on"},
    })])
    with patch("container_ctl.quadlet_gen.SYSTEMD_QUADLET_DIR", quadlets), \
         patch("container_ctl.quadlet_gen.resolve_extension_volumes", return_value=[]), \
         patch("container_ctl.quadlet_gen.subprocess.run"):
        generate_units(plan, str(services))
    assert existing.exists(), "partial deployment removed another service's network"
    assert existing.read_text() == content
    assert link.is_symlink() and link.resolve() == existing
    assert (project / "net-dev-db.network").is_file()
