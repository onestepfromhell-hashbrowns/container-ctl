import json
from pathlib import Path
from container_params.config import ConfigHierarchy, ConfigLayer
from container_params.paths import FHSPaths


def test_config_layer_hierarchy(tmp_path):
    # 建立設定檔結構
    config_dir = tmp_path / "etc" / "opt" / "demo"
    config_dir.mkdir(parents=True)
    (config_dir / "default.json").write_text(json.dumps({"PORT": 8000, "DB_NAME": "from_fhs_json"}))

    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("PORT=8080\nAPI_KEY=from_dotenv")

    manifest_file = tmp_path / "container.json"
    manifest_file.write_text(json.dumps({"environments": {"PORT": 9000, "REGION": "us-east-1"}}))

    paths = FHSPaths(
        project_name="demo",
        app_root=tmp_path,
        config_dir=config_dir,
        runtime_dir=tmp_path / "run",
        log_dir=tmp_path / "logs",
        data_dir=tmp_path / "data",
        frontend_dir=tmp_path / "frontend",
        static_vendors_dir=tmp_path / "static_vendors",
    )

    code_defaults = {"PORT": 3000, "TIMEOUT": 30}

    hierarchy = ConfigHierarchy(project_name="demo", paths=paths, code_defaults=code_defaults)

    # 1. 僅 Layer 0 + Layer 1 + Layer 2 (FHS JSON)
    cfg1 = hierarchy.evaluate()
    assert cfg1.get("TIMEOUT") == 30
    assert cfg1.get("DB_NAME") == "from_fhs_json"
    assert cfg1.get("PORT") == 8000

    # 2. 加入 Layer 2 (.env) 覆寫
    cfg2 = hierarchy.evaluate(env_files=[dotenv_file])
    assert cfg2.get("PORT") == "8080"
    assert cfg2.get("API_KEY") == "from_dotenv"

    # 3. 加入 Layer 3 (Container Manifest) 覆寫
    cfg3 = hierarchy.evaluate(env_files=[dotenv_file], manifest_files=[manifest_file])
    assert cfg3.get("PORT") == 9000
    assert cfg3.get("REGION") == "us-east-1"

    # 4. 加入 Layer 4 (Runtime CLI Override)
    cfg4 = hierarchy.evaluate(
        env_files=[dotenv_file],
        manifest_files=[manifest_file],
        runtime_overrides={"PORT": 9999},
    )
    assert cfg4.get("PORT") == 9999

    prov = cfg4.get_provenance()
    assert prov["PORT"]["layer_level"] == ConfigLayer.LAYER_4_RUNTIME_INJECTION
    assert prov["TIMEOUT"]["layer_level"] == ConfigLayer.LAYER_0_CODE_DEFAULT
