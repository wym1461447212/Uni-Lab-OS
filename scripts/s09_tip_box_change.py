"""S09 用 TIP 动作前的换 TIP 盒动作序列。"""

from __future__ import annotations

from typing import Any

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
_TIP_QUERY_KEYS = (
    "liquid_station_index",
    "solvent_batch_id",
    "volume",
    "volume_unit",
    "reuse_tip",
    "liquid_count",
    "liquid_additions",
)


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
