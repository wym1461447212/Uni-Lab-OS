"""S09 用 TIP 动作前的换 TIP 盒动作序列。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from scripts.run_workflow_local import WorkflowNode
from unilabos.app.scheduler.service import build_tip_box_change_workflow


S09_LIQUID_NODE_ID = "w03_add_liquid_s09"
S09_DENSITY_NODE_ID = "w05_measure_density_s09"
# 这些动作会从 TIP 库存取头。盒空时都要先换盒，再认领。
TIP_INVENTORY_METHODS = frozenset({
    "add_liquid_with_reusable_tip",
    "measure_density",
})
# 加液动作把取放料架写进参数；测密度执行时直接读换盒后的库存。
TIP_RACK_PARAMETER_METHODS = frozenset({
    "add_liquid_with_reusable_tip",
})
STATION_DEVICE_ID = "szlab_mixer_pipetting_station"
ROBOT_DEVICE_ID = "szlab_mixer_robot"
TIP_BOX_CHANGE_TEMPLATE_ID = "tip_box_change"
TIP_BOX_CHANGE_TEMPLATE_NAME = "S09 换 TIP 盒"
TIP_BOX_CHANGE_WORKFLOW_FILE = (
    Path(__file__).resolve().parents[1]
    / "unilabos/devices/workstation/szlab_poly_studio/workflows/szlab_tip_box_change_workflow.json"
)
TIP_GO_TO_SAFE_NODE_ID = "tip_go_to_safe_position"
TIP_PICK_FROM_S09_NODE_ID = "tip_pick_from_s09"
TIP_PLACE_TO_S02_NODE_ID = "tip_place_to_s02"
TIP_PICK_FROM_S02_NODE_ID = "tip_pick_from_s02"
TIP_PLACE_TO_S09_NODE_ID = "tip_place_to_s09"
TIP_BOX_CHANGE_NODE_IDS = (
    TIP_GO_TO_SAFE_NODE_ID,
    TIP_PICK_FROM_S09_NODE_ID,
    TIP_PLACE_TO_S02_NODE_ID,
    TIP_PICK_FROM_S02_NODE_ID,
    TIP_PLACE_TO_S09_NODE_ID,
)
_TIP_QUERY_KEYS = (
    "liquid_station_index",
    "solvent_batch_id",
    "volume",
    "volume_unit",
    "reuse_tip",
    "liquid_count",
    "liquid_additions",
)


@lru_cache(maxsize=1)
def tip_box_change_nodes() -> tuple[WorkflowNode, ...]:
    """换架工作流的五步 action，顺序与普通任务的 node_ids 一致。"""
    from scripts.task_execution_coordinator import workflow_nodes_from_payload

    document = json.loads(TIP_BOX_CHANGE_WORKFLOW_FILE.read_text(encoding="utf-8"))
    return tuple(workflow_nodes_from_payload(document))


def merge_tip_box_change_nodes(nodes: list[WorkflowNode]) -> list[WorkflowNode]:
    """把换架节点并进本轮派发节点表，样品工作流文件保持不变。"""
    present = {node.uuid for node in nodes}
    merged = list(nodes)
    for node in tip_box_change_nodes():
        if node.uuid not in present:
            merged.append(node)
    return merged


def tip_box_change_template_payload() -> dict[str, Any]:
    """换架模板不依赖样品顺序，触发时直接以 running 插入。"""
    return {
        "id": TIP_BOX_CHANGE_TEMPLATE_ID,
        "name": TIP_BOX_CHANGE_TEMPLATE_NAME,
        "node_ids": list(TIP_BOX_CHANGE_NODE_IDS),
        "resources": [],
        "input_triggers": [],
        "output_triggers": [],
        "dependencies": [],
    }


def tip_box_change_node_parameters(
    *,
    s02_place_position: int,
    s02_pick_position: int,
    s09_tip_position: int,
) -> dict[str, dict[str, Any]]:
    """触发时把扫描到的位号写进换架实例，不写死 1/2 对调。"""
    return {
        TIP_GO_TO_SAFE_NODE_ID: {"home_position": 1, "require_allow": True},
        TIP_PICK_FROM_S09_NODE_ID: {
            "product_type": 1,
            "position": int(s09_tip_position),
        },
        TIP_PLACE_TO_S02_NODE_ID: {"position": int(s02_place_position)},
        TIP_PICK_FROM_S02_NODE_ID: {"position": int(s02_pick_position)},
        TIP_PLACE_TO_S09_NODE_ID: {
            "product_type": 1,
            "position": int(s09_tip_position),
        },
    }


def reusable_tip_query_kwargs(node: Any) -> dict[str, Any]:
    params = getattr(node, "param", None) or {}
    return {key: params[key] for key in _TIP_QUERY_KEYS if key in params}


def execute_tip_box_change(
    station: Any,
    robot: Any,
    *,
    place_position: int,
    pick_position: int,
    s09_tip_position: int = 1,
) -> dict[str, Any]:
    """先让 S09 回到安全位，再按给定 S02 位号完成四步换盒。"""
    steps: list[dict[str, Any]] = []
    safe = station.go_to_safe_position(home_position=1, require_allow=True)
    steps.append({"action": "go_to_safe_position", "result": safe})
    if not safe.get("success"):
        return {
            "success": False,
            "message": safe.get("message") or "S09 未能回到安全位",
            "steps": steps,
            "place_position": place_position,
            "pick_position": pick_position,
        }

    spec = build_tip_box_change_workflow(
        s02_place_position=place_position,
        s02_pick_position=pick_position,
        s09_tip_position=s09_tip_position,
    )
    full_box_position: int | None = None
    for node in spec.nodes:
        method = getattr(robot, node.action_name)
        result = method(**node.param)
        steps.append({"action": node.action_name, "param": dict(node.param), "result": result})
        if node.action_name == "submit_place_to_s09" and result.get("success"):
            full_box_position = int(node.param["position"])
        if not result.get("success"):
            return {
                "success": False,
                "message": result.get("message") or f"{node.action_name} 失败",
                "steps": steps,
                "place_position": place_position,
                "pick_position": pick_position,
                "full_box_position": full_box_position,
            }
    if full_box_position is None:
        return {
            "success": False,
            "message": "上料流程没有记录满料架放到了哪个 S09 位",
            "steps": steps,
            "place_position": place_position,
            "pick_position": pick_position,
            "full_box_position": None,
        }
    return {
        "success": True,
        "message": "S09 TIP 盒已更换",
        "steps": steps,
        "place_position": place_position,
        "pick_position": pick_position,
        "full_box_position": full_box_position,
    }
