"""
container_params — 通用 FHS 容器標準參數與配置檢查套件

基於第一性原理，提供：
1. 6 大 FHS 特規路徑動態解析引擎 (FHSPathResolver)
2. 5 層配置覆蓋階層解析模型 (ConfigHierarchy)
3. 5 大架構反模式靜態/動態檢查器 (AntiPatternChecker, AntiPatternViolation)
"""

from .paths import FHSPathResolver, FHSPaths, resolve_app_paths
from .config import ConfigHierarchy, ConfigLayer, LayeredConfig
from .checker import (
    AntiPatternChecker,
    AntiPatternType,
    AntiPatternViolation,
    check_codebase,
)

__version__ = "1.0.0"

__all__ = [
    "FHSPathResolver",
    "FHSPaths",
    "resolve_app_paths",
    "ConfigHierarchy",
    "ConfigLayer",
    "LayeredConfig",
    "AntiPatternChecker",
    "AntiPatternType",
    "AntiPatternViolation",
    "check_codebase",
]
