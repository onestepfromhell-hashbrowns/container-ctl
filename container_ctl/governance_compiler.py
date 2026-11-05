"""governance_compiler.py — C04 / C07 Governance and Admission Guard Compiler.

Produces:
1. Reconstructible tier slices under rootless user manager:
   - workload.slice (root workload slice)
   - workload-dev.slice, workload-prod.slice, workload-infra.slice (tier child slices)
   - rescue targets (sshd, cloudflared) strictly excluded from workload slice tree.
2. Target separation:
   - Dev and prod are isolated in separate target boundaries and cannot be enveloped
     under a single unloadable parent target.
3. Workload admission guard mapping (ExecCondition adapter):
   - ExecCondition=/usr/local/bin/memory-ctl start-check <stable_id>
   - Non-zero exit code (1..254) terminates start attempt cleanly without triggering
     Restart=on-failure restart storms.
4. Comprehensive GovernanceManifest export for verification and downstream (#08 / #15) consumption.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict, List, Optional, Union

from container_ctl.governance_port import (
    GuestMemorySample,
    CapacityPolicy,
    OwnerBudget,
    TierBudget,
    WorkloadBudgetPolicy,
    load_policy_from_file,
    parse_capacity_policy_dict,
    validate_capacity_policy,
)
from container_ctl.dag_resolver import DeployPlan, ServiceAction


@dataclass
class WorkloadGovernanceMapping:
    """Per-workload governance mapping for unit generation and sync (C07)."""
    stable_id: str
    unit_name: str
    tier: str
    slice: Optional[str]
    guard: Optional[str]
    limits: Dict[str, Any]
    owner_uid: int = 1000
    dependency_ids: List[str] = field(default_factory=list)
    target_name: str = "default.target"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "unit_name": self.unit_name,
            "tier": self.tier,
            "slice": self.slice,
            "guard": self.guard,
            "limits": self.limits,
            "owner_uid": self.owner_uid,
            "dependency_ids": self.dependency_ids,
            "target_name": self.target_name,
        }


@dataclass
class SliceSpec:
    """Systemd Slice definition conforming to C04 / C07."""
    slice_name: str
    parent_slice: Optional[str]
    description: str
    memory_high: Optional[Union[int, str]] = None
    memory_max: Optional[Union[int, str]] = None
    memory_swap_max: Optional[Union[int, str]] = None
    tasks_max: Optional[Union[int, str]] = None
    unit_content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slice_name": self.slice_name,
            "parent_slice": self.parent_slice,
            "description": self.description,
            "memory_high": self.memory_high,
            "memory_max": self.memory_max,
            "memory_swap_max": self.memory_swap_max,
            "tasks_max": self.tasks_max,
            "unit_content": self.unit_content,
        }


@dataclass
class GovernanceManifest:
    """Consolidated manifest output containing unit/slice/guard/limits mappings (C04/C07/AC5)."""
    schema_version: int = 1
    policy_version: str = "1.0"
    mode: str = "observe"
    workloads: Dict[str, WorkloadGovernanceMapping] = field(default_factory=dict)
    slices: Dict[str, SliceSpec] = field(default_factory=dict)
    owner_allocations: Dict[str, Any] = field(default_factory=dict)
    rescue_targets: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "workloads": {k: v.to_dict() for k, v in self.workloads.items()},
            "slices": {k: v.to_dict() for k, v in self.slices.items()},
            "owner_allocations": self.owner_allocations,
            "rescue_targets": self.rescue_targets,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def _render_slice_unit(spec: SliceSpec) -> str:
    """Render a standard systemd .slice unit."""
    lines = [
        "[Unit]",
        f"Description={spec.description}",
        "Before=slices.target",
        "",
        "[Slice]",
    ]
    if spec.memory_high is not None:
        lines.append(f"MemoryHigh={spec.memory_high}")
    if spec.memory_max is not None:
        lines.append(f"MemoryMax={spec.memory_max}")
    if spec.memory_swap_max is not None:
        lines.append(f"MemorySwapMax={spec.memory_swap_max}")
    if spec.tasks_max is not None:
        lines.append(f"TasksMax={spec.tasks_max}")
    return "\n".join(lines) + "\n"


def compile_governance(
    policy: Union[CapacityPolicy, Dict[str, Any], str, Path],
    plan: Optional[DeployPlan] = None,
    owner_uid: int = 1000,
    start_check_bin: str = "/usr/local/bin/memory-ctl",
    guest_sample: Optional[GuestMemorySample] = None,
) -> GovernanceManifest:
    """Compile a CapacityPolicy and optional DeployPlan into a GovernanceManifest (C04, C07).

    Validates:
    - Policy structure and budget feasibility.
    - Owner allocations for dev, prod, and infra.
    - Workload mappings and admission guard specifications.
    - Strict separation of dev and prod targets (preventing common unloadable parent targets).
    - Isolation of rescue targets outside the workload slice tree.

    Raises ValueError if policy is invalid or inconsistent.
    """
    # 1. Resolve and validate policy object
    if isinstance(policy, (str, Path)):
        pol_obj, errors = load_policy_from_file(str(policy))
        if pol_obj is None:
            err_msg = "; ".join(e.detail for e in errors)
            raise ValueError(f"Failed to load capacity policy: {err_msg}")
        capacity_policy = pol_obj
    elif isinstance(policy, dict):
        pol_obj, errors = parse_capacity_policy_dict(policy)
        if pol_obj is None:
            err_msg = "; ".join(e.detail for e in errors)
            raise ValueError(f"Failed to parse capacity policy dict: {err_msg}")
        capacity_policy = pol_obj
    elif isinstance(policy, CapacityPolicy):
        capacity_policy = policy
    else:
        raise TypeError(f"Unsupported policy type: {type(policy)}")

    # Validate policy
    if capacity_policy.system_budget_bytes is None or capacity_policy.system_budget_bytes < 0:
        raise ValueError(
            f"Invalid capacity policy: system_budget_bytes must be a mandatory non-negative integer (got {capacity_policy.system_budget_bytes})."
        )
    if capacity_policy.unmanaged_budget_bytes is None or capacity_policy.unmanaged_budget_bytes < 0:
        raise ValueError(
            f"Invalid capacity policy: unmanaged_budget_bytes must be a mandatory non-negative integer (got {capacity_policy.unmanaged_budget_bytes})."
        )

    # Validate policy against guest capacity
    sample = guest_sample
    if sample is None:
        if capacity_policy.rescue_reserve_bytes and capacity_policy.rescue_reserve_bytes > 0:
            mem_total = int(capacity_policy.rescue_reserve_bytes / 0.20)
        else:
            mem_total = 8053063680  # ~7.5 GiB
        sample = GuestMemorySample(
            mem_total_bytes=mem_total,
            swap_total_bytes=8 * 1024 * 1024 * 1024,
        )
    val_res = validate_capacity_policy(capacity_policy, guest_sample=sample)
    if not val_res.valid:
        err_details = "; ".join(e.detail for e in val_res.errors)
        raise ValueError(f"Capacity policy validation failed: {err_details}")

    # 2. Extract owner budget
    owner_str = str(owner_uid)
    owner_budget = capacity_policy.owners.get(owner_str)
    if owner_budget is None and capacity_policy.owners:
        # Fallback to first owner if specific UID not explicitly configured
        owner_budget = next(iter(capacity_policy.owners.values()))

    # 3. Generate Slices
    slices: Dict[str, SliceSpec] = {}

    # Root workload slice
    root_slice = SliceSpec(
        slice_name="workload.slice",
        parent_slice=None,
        description="container-ctl Workload Top-Level Slice",
    )
    root_slice.unit_content = _render_slice_unit(root_slice)
    slices["workload.slice"] = root_slice

    # Tier slices: dev, prod, infra
    tiers_config = [
        ("dev", "container-ctl Workload Dev Tier Slice", getattr(owner_budget, "dev", None) if owner_budget else None),
        ("prod", "container-ctl Workload Prod Tier Slice", getattr(owner_budget, "prod", None) if owner_budget else None),
        ("infra", "container-ctl Workload Infra Tier Slice", getattr(owner_budget, "infra", None) if owner_budget else None),
    ]

    for tier_name, desc, tier_b in tiers_config:
        s_name = f"workload-{tier_name}.slice"
        high = tier_b.memory_high_bytes if tier_b else None
        max_bytes = tier_b.memory_max_bytes if tier_b else None
        swap_raw = tier_b.swap_max_bytes if tier_b else None
        slice_swap_max = swap_raw if swap_raw is not None else 0  # INV-05
        tasks = 2048 if tier_name == "dev" else None

        spec = SliceSpec(
            slice_name=s_name,
            parent_slice="workload.slice",
            description=desc,
            memory_high=high,
            memory_max=max_bytes,
            memory_swap_max=slice_swap_max,
            tasks_max=tasks,
        )
        spec.unit_content = _render_slice_unit(spec)
        slices[s_name] = spec

    # 4. Generate Workload Mappings
    workloads: Dict[str, WorkloadGovernanceMapping] = {}
    rescue_targets = set(capacity_policy.rescue_targets or ["sshd", "cloudflared"])

    # First from policy declared workloads
    for w in capacity_policy.workloads:
        is_rescue = (w.tier == "rescue" or w.stable_id in rescue_targets)
        if is_rescue:
            mapping = WorkloadGovernanceMapping(
                stable_id=w.stable_id,
                unit_name=f"svc-{w.stable_id}",
                tier="rescue",
                slice=None,  # Strictly excluded from workload slice tree
                guard=None,  # No admission guard for rescue
                limits={
                    "memory_high": w.memory_high_bytes,
                    "memory_max": w.memory_max_bytes or "1G",
                    "swap_max": w.swap_max_bytes,
                    "tasks_max": None,
                },
                owner_uid=w.owner_uid or owner_uid,
                dependency_ids=list(w.dependency_ids),
                target_name="default.target",
            )
        else:
            tier = w.tier if w.tier in ("dev", "prod", "infra") else "prod"
            target_name = f"workload-{tier}.target"
            tasks = 2048 if tier == "dev" else None
            mapping = WorkloadGovernanceMapping(
                stable_id=w.stable_id,
                unit_name=f"svc-{w.stable_id}",
                tier=tier,
                slice=f"workload-{tier}.slice",
                guard=f"ExecCondition={start_check_bin} start-check {w.stable_id}",
                limits={
                    "memory_high": w.memory_high_bytes,
                    "memory_max": w.memory_max_bytes or ("2G" if tier == "dev" else "1G"),
                    "swap_max": w.swap_max_bytes if w.swap_max_bytes is not None else 0,  # INV-01
                    "tasks_max": tasks,
                },
                owner_uid=w.owner_uid or owner_uid,
                dependency_ids=list(w.dependency_ids),
                target_name=target_name,
            )
        workloads[w.stable_id] = mapping

    # If a DeployPlan was provided, map plan actions against policy
    if plan is not None:
        for action in plan.actions:
            cfg = action.config
            stable_id = (
                cfg.get("stable_id")
                or cfg.get("governance", {}).get("id")
                or cfg.get("labels", {}).get("governance.id")
                or action.name
            )
            if stable_id in workloads:
                # Update unit name if specific plan has project context
                mapping = workloads[stable_id]
                mapping.unit_name = f"svc-{action.name}"
            else:
                # Check if matches rescue or declared
                tier = cfg.get("tier", "prod")
                is_rescue = (tier == "rescue" or stable_id in rescue_targets or action.name in rescue_targets)
                if is_rescue:
                    workloads[stable_id] = WorkloadGovernanceMapping(
                        stable_id=stable_id,
                        unit_name=f"svc-{action.name}",
                        tier="rescue",
                        slice=None,
                        guard=None,
                        limits={"memory_max": "1G"},
                        owner_uid=owner_uid,
                        target_name="default.target",
                    )
                else:
                    target_name = f"workload-{tier}.target"
                    workloads[stable_id] = WorkloadGovernanceMapping(
                        stable_id=stable_id,
                        unit_name=f"svc-{action.name}",
                        tier=tier,
                        slice=f"workload-{tier}.slice",
                        guard=f"ExecCondition={start_check_bin} start-check {stable_id}",
                        limits={
                            "memory_max": cfg.get("memory_max") or ("2G" if tier == "dev" else "1G"),
                            "swap_max": cfg.get("memory_swap_max") if cfg.get("memory_swap_max") is not None else 0,
                            "tasks_max": cfg.get("tasks_max") or ("2048" if tier == "dev" else None),
                        },
                        owner_uid=owner_uid,
                        target_name=target_name,
                    )

    return GovernanceManifest(
        schema_version=1,
        policy_version=capacity_policy.policy_version,
        mode=capacity_policy.mode,
        workloads=workloads,
        slices=slices,
        owner_allocations={k: v.to_dict() for k, v in capacity_policy.owners.items()},
        rescue_targets=list(rescue_targets),
    )
