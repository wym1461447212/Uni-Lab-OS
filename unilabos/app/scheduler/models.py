"""Edge Scheduler 最小数据模型。

这个模块只描述调度需要的 DAG、设备锁、物料锁和优先级事实，不承担
云端工作流或库存服务的持久化。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WorkflowState(StrEnum):
    RUNNING = "running"
    WAITING_MATERIAL = "waiting_for_material"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class MaterialRequirement:
    """一个节点的数量型物料需求。"""

    lot_id: str
    quantity: float = 1.0
    unit: str = ""


@dataclass
class WorkflowNode:
    id: str
    device_id: str = ""
    action_name: str = ""
    action_type: str = "goal"
    param: dict[str, Any] = field(default_factory=dict)
    material_requirements: list[MaterialRequirement] = field(default_factory=list)
    resource_lock_keys: tuple[str, ...] = ()
    disabled: bool = False
    node_type: str = "ILab"

    @property
    def device_action_key(self) -> str:
        return f"/devices/{self.device_id}/{self.action_name}"

    @property
    def device_lock_key(self) -> str:
        return f"/devices/{self.device_id}"

    def is_manual_confirm(self) -> bool:
        return self.action_name == "manual_confirm"


@dataclass(frozen=True)
class WorkflowEdge:
    uuid: str
    source_node_id: str
    target_node_id: str


@dataclass
class WorkflowSpec:
    workflow_id: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge] = field(default_factory=list)
    priority: Any = "normal"
    submitted_at: float = field(default_factory=time.time)
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = self.workflow_id

    def material_requirements_by_node(self) -> dict[str, list[MaterialRequirement]]:
        return {
            node.id: list(node.material_requirements)
            for node in self.nodes
            if node.material_requirements and not node.disabled
        }


@dataclass
class ReadyTask:
    workflow_id: str
    node: WorkflowNode
    priority_weight: float
    submitted_at: float
    is_resource_unblocking: bool = False
    resource_lock_keys: tuple[str, ...] = ()
    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = self.workflow_id


@dataclass
class DispatchedJob:
    job_id: str
    workflow_id: str
    node_id: str
    device_action_key: str
    device_id: str
    action_name: str
    resource_lock_keys: tuple[str, ...] = ()


PRIORITY_WEIGHTS: dict[str, float] = {
    "urgent": 300.0,
    "high": 200.0,
    "normal": 100.0,
    "low": 50.0,
}


def priority_weight(priority: Any) -> float:
    if isinstance(priority, str):
        if priority in PRIORITY_WEIGHTS:
            return PRIORITY_WEIGHTS[priority]
        return float(priority)
    return float(priority)


def node_from_dict(data: dict[str, Any]) -> WorkflowNode:
    requirements = [
        MaterialRequirement(
            lot_id=str(item.get("lot_id") or item.get("lotId") or ""),
            quantity=float(item.get("quantity", 1.0)),
            unit=str(item.get("unit") or ""),
        )
        for item in data.get("material_requirements", [])
        if isinstance(item, dict)
    ]
    return WorkflowNode(
        id=str(data["id"]),
        device_id=str(data.get("device_id") or ""),
        action_name=str(data.get("action_name") or ""),
        action_type=str(data.get("action_type") or "goal"),
        param=dict(data.get("param") or {}),
        material_requirements=requirements,
        resource_lock_keys=tuple(
            str(item)
            for item in data.get("resource_lock_keys", [])
            if str(item)
        ),
        disabled=bool(data.get("disabled", False)),
        node_type=str(data.get("node_type") or data.get("nodeType") or "ILab"),
    )


def spec_from_dict(data: dict[str, Any]) -> WorkflowSpec:
    """从本地 JSON 兼容格式构造 workflow spec。"""
    nodes = [
        item if isinstance(item, WorkflowNode) else node_from_dict(item)
        for item in data.get("nodes", [])
    ]
    edges = [
        item
        if isinstance(item, WorkflowEdge)
        else WorkflowEdge(
            uuid=str(item.get("uuid") or item.get("id") or f"edge-{index}"),
            source_node_id=str(
                item.get("source_node_id")
                or item.get("source")
                or item.get("sourceNodeId")
            ),
            target_node_id=str(
                item.get("target_node_id")
                or item.get("target")
                or item.get("targetNodeId")
            ),
        )
        for index, item in enumerate(data.get("edges", []))
    ]
    return WorkflowSpec(
        workflow_id=str(data.get("workflow_id") or data.get("id") or ""),
        nodes=nodes,
        edges=edges,
        priority=data.get("priority", "normal"),
        submitted_at=float(data.get("submitted_at") or data.get("submittedAt") or 0)
        or time.time(),
        run_id=str(data.get("run_id") or data.get("runId") or ""),
    )
