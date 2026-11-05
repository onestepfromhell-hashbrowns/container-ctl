"""governance_port: the only module in container_ctl that imports memory_governor (D-01).

memory_governor is an optional external plugin and may be absent. Callers import the governor's
types and functions from here instead of from memory_governor.

When memory_governor is installed the names are the governor's own objects, so behaviour is unchanged.
When it is absent:
  - pause checks go through ``GovernanceLedger``, a ledger that never exists and holds no pauses, so
    every workload is admitted (D-01a);
  - anything that needs the governor's policy model (compiling a capacity policy, syncing cgroups)
    raises ``GovernorUnavailable`` (code ``GOVERNOR_UNAVAILABLE``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

ERROR_CODE = "GOVERNOR_UNAVAILABLE"


class GovernorUnavailable(RuntimeError):
    """memory_governor is not installed but the requested operation needs it."""

    code = ERROR_CODE

    def __init__(self, what: str = "this operation") -> None:
        super().__init__(f"{ERROR_CODE}: memory_governor is not installed; {what} needs it")


try:
    from memory_governor.ledger import GovernanceLedger, PauseRecord
    from memory_governor.models import GuestMemorySample
    from memory_governor.policy import (
        CapacityPolicy,
        OwnerBudget,
        TierBudget,
        WorkloadBudgetPolicy,
        load_policy_from_file,
        parse_capacity_policy_dict,
        validate_capacity_policy,
    )

    AVAILABLE = True
except ImportError:
    AVAILABLE = False

    @dataclass
    class PauseRecord:  # type: ignore[no-redef]
        stable_id: str
        incident_id: str = ""
        reason: str = ""

    class GovernanceLedger:  # type: ignore[no-redef]
        """No governor: no ledger, no pauses, everything admitted."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def exists(self) -> bool:
            return False

        def get_pauses(self) -> List[PauseRecord]:
            return []

    def _unavailable_type(name: str) -> type:
        def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
            raise GovernorUnavailable(name)

        return type(name, (), {"__init__": __init__})

    GuestMemorySample = _unavailable_type("GuestMemorySample")  # type: ignore[misc]
    CapacityPolicy = _unavailable_type("CapacityPolicy")  # type: ignore[misc]
    OwnerBudget = _unavailable_type("OwnerBudget")  # type: ignore[misc]
    TierBudget = _unavailable_type("TierBudget")  # type: ignore[misc]
    WorkloadBudgetPolicy = _unavailable_type("WorkloadBudgetPolicy")  # type: ignore[misc]

    def load_policy_from_file(path: str) -> Any:  # type: ignore[misc]
        raise GovernorUnavailable("compiling a capacity policy")

    def parse_capacity_policy_dict(data: Dict[str, Any]) -> Any:  # type: ignore[misc]
        raise GovernorUnavailable("compiling a capacity policy")

    def validate_capacity_policy(policy: Any, guest_sample: Optional[Any] = None) -> Any:  # type: ignore[misc]
        raise GovernorUnavailable("validating a capacity policy")


def paused_stable_ids(ledger: Any = None) -> set:
    """stable_ids with an active pause; empty when the governor or its ledger is absent."""
    try:
        led = ledger or GovernanceLedger()
        if led.exists():
            return {p.stable_id for p in led.get_pauses()}
    except Exception:
        pass
    return set()
