"""
container_params.config — 5 層配置覆蓋階層解析模型 (Configuration Hierarchy)

實現清晰的 5 層優先權：
[最高] Layer 4: 執行時注入與動態鑑權 (Runtime CLI / Injected Env)
       Layer 3: 宣告式容器清冊 (.deploy/containers/*.json, Quadlet)
       Layer 2: 外部環境變數與 FHS 設定檔 (/etc/opt/<slug>/*.json, .env)
       Layer 1: Path Resolver 自動探測 (Candidate Repo vs /opt/<slug>)
[最低] Layer 0: 代碼底層安全預設 (Code Defaults)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import FHSPaths, resolve_app_paths


class ConfigLayer(IntEnum):
    LAYER_0_CODE_DEFAULT = 0
    LAYER_1_PATH_RESOLVER = 1
    LAYER_2_FHS_AND_ENV_FILE = 2
    LAYER_3_CONTAINER_MANIFEST = 3
    LAYER_4_RUNTIME_INJECTION = 4


@dataclass
class ConfigEntry:
    key: str
    value: Any
    layer: ConfigLayer
    source: str


@dataclass
class LayeredConfig:
    project_name: str
    entries: dict[str, ConfigEntry] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        entry = self.entries.get(key)
        return entry.value if entry is not None else default

    def __getitem__(self, key: str) -> Any:
        if key not in self.entries:
            raise KeyError(key)
        return self.entries[key].value

    def as_dict(self) -> dict[str, Any]:
        return {k: v.value for k, v in self.entries.items()}

    def get_provenance(self) -> dict[str, dict[str, Any]]:
        """回傳各設定值的來源階層與檔案/來源名稱，供審查與除錯。"""
        return {
            k: {
                "value": v.value,
                "layer_level": int(v.layer),
                "layer_name": v.layer.name,
                "source": v.source,
            }
            for k, v in self.entries.items()
        }


class ConfigHierarchy:
    """5 層配置評估與載入器。"""

    def __init__(
        self,
        project_name: str = "myapp",
        paths: Optional[FHSPaths] = None,
        code_defaults: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.project_name = project_name
        self.paths = paths or resolve_app_paths(project_name=project_name)
        self.code_defaults = dict(code_defaults or {})

    def evaluate(
        self,
        env_files: Optional[list[Path]] = None,
        manifest_files: Optional[list[Path]] = None,
        runtime_env: Optional[Mapping[str, str]] = None,
        runtime_overrides: Optional[Mapping[str, Any]] = None,
    ) -> LayeredConfig:
        config = LayeredConfig(project_name=self.project_name)

        # Layer 0: 代碼預設值
        for k, v in self.code_defaults.items():
            config.entries[k] = ConfigEntry(
                key=k, value=v, layer=ConfigLayer.LAYER_0_CODE_DEFAULT, source="CodeBase Defaults"
            )

        # Layer 1: Path Resolver 自動解析的路徑
        for k, v in self.paths.as_dict().items():
            config.entries[k] = ConfigEntry(
                key=k, value=v, layer=ConfigLayer.LAYER_1_PATH_RESOLVER, source="PathResolver"
            )

        # Layer 2: 外部環境變數與 FHS 設定檔 (/etc/opt/<slug>/*.json, .env)
        candidate_config_dirs = [self.paths.config_dir]
        for cfg_dir in candidate_config_dirs:
            if cfg_dir.is_dir():
                for json_file in sorted(cfg_dir.glob("*.json")):
                    try:
                        data = json.loads(json_file.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            for k, v in data.items():
                                config.entries[k] = ConfigEntry(
                                    key=k,
                                    value=v,
                                    layer=ConfigLayer.LAYER_2_FHS_AND_ENV_FILE,
                                    source=str(json_file),
                                )
                    except (json.JSONDecodeError, OSError):
                        pass

        if env_files:
            for ef in env_files:
                if ef.is_file():
                    self._load_dotenv_file(ef, config, ConfigLayer.LAYER_2_FHS_AND_ENV_FILE)

        # Layer 3: 宣告式容器清冊 (.deploy/containers/*.json)
        if manifest_files:
            for mf in manifest_files:
                if mf.is_file():
                    try:
                        m_data = json.loads(mf.read_text(encoding="utf-8"))
                        # 若清冊包含 environments 欄位
                        envs = m_data.get("environments", {})
                        if isinstance(envs, dict):
                            for k, v in envs.items():
                                config.entries[k] = ConfigEntry(
                                    key=k,
                                    value=v,
                                    layer=ConfigLayer.LAYER_3_CONTAINER_MANIFEST,
                                    source=str(mf),
                                )
                    except (json.JSONDecodeError, OSError):
                        pass

        # Layer 4: 執行時注入與動態鑑權
        env_map = runtime_env if runtime_env is not None else os.environ
        for k, v in env_map.items():
            # 僅注入專案相關或已知變數，或全部環境變數
            if k.startswith(self.project_name.upper()) or k.startswith("APP_") or k in config.entries:
                config.entries[k] = ConfigEntry(
                    key=k,
                    value=v,
                    layer=ConfigLayer.LAYER_4_RUNTIME_INJECTION,
                    source="Process Environment",
                )

        if runtime_overrides:
            for k, v in runtime_overrides.items():
                config.entries[k] = ConfigEntry(
                    key=k,
                    value=v,
                    layer=ConfigLayer.LAYER_4_RUNTIME_INJECTION,
                    source="Runtime CLI Override",
                )

        return config

    def _load_dotenv_file(self, path: Path, config: LayeredConfig, layer: ConfigLayer) -> None:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                config.entries[k] = ConfigEntry(key=k, value=v, layer=layer, source=str(path))
        except OSError:
            pass
