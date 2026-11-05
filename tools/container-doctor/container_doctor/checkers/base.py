"""
Abstract Base Checker for Container Doctor.
"""

from abc import ABC, abstractmethod
from typing import List

from container_doctor.core.context import ExecutionContext
from container_doctor.core.models import CheckResult


class BaseChecker(ABC):
    """
    Abstract base class for all domain checkers.
    """

    @property
    @abstractmethod
    def domain(self) -> str:
        """Domain identifier (e.g., 'mounts', 'paths', 'network', 'resources', 'ssh')."""
        pass

    @abstractmethod
    def check(self, context: ExecutionContext) -> List[CheckResult]:
        """
        Execute domain-specific checks against the provided ExecutionContext.
        Must catch internal errors and return CheckResult items without raising.
        """
        pass
