"""
TICKET-005: Blueprint 生成器
將 DeployPlan 序列化為版本化 Blueprint JSON，儲存至 .deploys/。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from container_ctl.dag_resolver import DeployPlan
from container_ctl import paths

DEFAULT_DEPLOYS_DIR = paths.DEPLOYS_DIR


def generate_blueprint(
    plan: DeployPlan,
    output_dir: str = DEFAULT_DEPLOYS_DIR,
) -> str:
    """
    生成 Blueprint JSON 並儲存至 .deploys/<project>/<ver>.json。
    若相同版本已存在則覆寫（冪等）。
    回傳 Blueprint 的完整路徑。
    """
    blueprint = {
        "schema_version": 1,
        "project_name": plan.project_name,
        "version": plan.version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "services": [
            {
                "name": a.name,
                "action": a.action,
                "config": a.config,
                "existing_instance_id": a.existing_instance_id,
                "existing_version": a.existing_version,
            }
            for a in plan.actions
        ],
    }

    dest_dir = Path(output_dir) / plan.project_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{plan.version}.json"
    dest_file.write_text(
        json.dumps(blueprint, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(dest_file)


def load_blueprint(path: str) -> dict:
    """從磁碟載入既有 Blueprint（供 release deploy / redeploy 使用）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))
