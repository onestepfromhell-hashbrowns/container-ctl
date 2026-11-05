"""
Container Control Package (container_control)
Standardized in-container process lifecycle supervision, triple-verification, and scaffolding.
"""

from .config import ServiceConfig, load_service_config
from .process import ProcessManager
from .port import PortDetector
from .probe import ReadinessProbe
from .controller import ServiceController

__version__ = "2.0.0"
__all__ = [
    "ServiceConfig",
    "load_service_config",
    "ProcessManager",
    "PortDetector",
    "ReadinessProbe",
    "ServiceController",
]
