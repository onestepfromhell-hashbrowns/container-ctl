"""
TICKET-004: 依賴 DAG 解析器
解析 manifest.dependencies，查詢 SQLite，生成排序後的 DeployPlan。
TICKET-025: 依 tier 排序（infra > prod > dev）。
"""
import sqlite3
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ServiceAction:
    name: str
    action: Literal["NEW", "REUSE", "CONFLICT"]
    config: dict
    existing_instance_id: str | None = None
    existing_version: str | None = None


@dataclass
class DeployPlan:
    project_name: str
    version: str
    actions: list[ServiceAction] = field(default_factory=list)

    @property
    def has_conflict(self) -> bool:
        return any(a.action == "CONFLICT" for a in self.actions)

    @property
    def new_count(self) -> int:
        return sum(1 for a in self.actions if a.action == "NEW")

    @property
    def reuse_count(self) -> int:
        return sum(1 for a in self.actions if a.action == "REUSE")


def resolve(
    manifest: dict,
    container_configs: list[dict],
    db_conn: sqlite3.Connection,
) -> DeployPlan:
    """
    解析依賴並生成 DeployPlan。
    - shared scope：查詢 SQLite shared_refs 判斷 REUSE / CONFLICT / NEW
    - dedicated scope：一律 NEW
    順序保持 manifest.dependencies 原始拓撲順序。
    """
    project_name = manifest.get("project_name") or manifest.get("name")
    if not project_name:
        raise ValueError("manifest 缺少必要欄位 'project_name'")
    version = manifest.get("version", "v1.0.0")

    plan = DeployPlan(
        project_name=project_name,
        version=version,
    )

    for config in container_configs:
        name = config["name"]
        scope = config.get("scope", "dedicated")
        config_version = config.get("version", manifest["version"])

        if scope == "shared":
            row = db_conn.execute(
                """
                SELECT service_id, version, status
                FROM shared_refs
                WHERE service_name = ?
                  AND status NOT IN ('stopped', 'destroyed')
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (name,),
            ).fetchone()

            if row:
                existing_ver = row["version"]
                if existing_ver == config_version:
                    plan.actions.append(
                        ServiceAction(
                            name=name,
                            action="REUSE",
                            config=config,
                            existing_instance_id=row["service_id"],
                            existing_version=existing_ver,
                        )
                    )
                else:
                    plan.actions.append(
                        ServiceAction(
                            name=name,
                            action="CONFLICT",
                            config=config,
                            existing_instance_id=row["service_id"],
                            existing_version=existing_ver,
                        )
                    )
            else:
                plan.actions.append(ServiceAction(name=name, action="NEW", config=config))
        else:
            # dedicated：按專案完全隔離，一律 NEW
            plan.actions.append(ServiceAction(name=name, action="NEW", config=config))

    # TICKET-025: 依 tier 排序（infra 先，prod 次，dev 最後）
    _TIER_ORDER = {"infra": 0, "prod": 1, "dev": 2}
    plan.actions.sort(key=lambda a: _TIER_ORDER.get(a.config.get("tier", "prod"), 1))

    return plan
