"""将 SZLab ``rules/actions`` 工作流转换为 EdgeScheduler 的 DAG。

SZLab 的动作工作流主要服务于本地执行器和 OPC 模拟器，而 EdgeScheduler
需要显式的 ``WorkflowNode``/``WorkflowEdge``。本模块只负责格式转换：
它不会替设备推断库存，也不会把 ``index`` 当成跨规则依赖。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import MaterialRequirement, WorkflowEdge, WorkflowNode, WorkflowSpec


def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} 必须是对象")
    return value


def _text(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} 必须是非空字符串")
    return value.strip()


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _material_requirements(action: Mapping[str, Any], path: str) -> list[MaterialRequirement]:
    raw = _first(action, "material_requirements", "materialRequirements")
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{path}.material_requirements 必须是数组")
    result: list[MaterialRequirement] = []
    for index, item in enumerate(raw):
        item = _as_mapping(item, f"{path}.material_requirements[{index}]")
        lot_id = _text(
            _first(item, "lot_id", "lotId"),
            path=f"{path}.material_requirements[{index}].lot_id",
        )
        try:
            quantity = float(item.get("quantity", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{path}.material_requirements[{index}].quantity 必须是数字"
            ) from exc
        if quantity <= 0:
            raise ValueError(
                f"{path}.material_requirements[{index}].quantity 必须大于 0"
            )
        result.append(
            MaterialRequirement(
                lot_id=lot_id,
                quantity=quantity,
                unit=str(item.get("unit") or ""),
            )
        )
    return result


def _resource_lock_keys(action: Mapping[str, Any], path: str) -> tuple[str, ...]:
    raw = _first(action, "resource_lock_keys", "resourceLockKeys")
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{path}.resource_lock_keys 必须是数组")
    keys: list[str] = []
    for index, item in enumerate(raw):
        key = _text(item, path=f"{path}.resource_lock_keys[{index}]")
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _dependencies(action: Mapping[str, Any], path: str) -> list[str] | None:
    raw = _first(action, "depends_on", "dependsOn")
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{path}.depends_on 必须是数组")
    result: list[str] = []
    for index, item in enumerate(raw):
        dependency = _text(item, path=f"{path}.depends_on[{index}]")
        if dependency not in result:
            result.append(dependency)
    return result


def workflow_spec_from_szlab_json(
    workflow: Mapping[str, Any],
    *,
    workflow_id: str | None = None,
    priority: Any | None = None,
) -> WorkflowSpec:
    """Convert a ``rules/actions`` SZLab workflow into ``WorkflowSpec``.

    By default, actions within one rule are connected sequentially because the
    legacy file has an ordered action list but no dependency graph. An action
    may provide ``depends_on``/``dependsOn`` to override that rule-local
    predecessor list. Actions from different rules are not connected implicitly.
    """
    root = _as_mapping(workflow.get("data", workflow), "workflow")
    rules = root.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("workflow.rules 必须是非空数组")

    nodes: list[WorkflowNode] = []
    edges: list[WorkflowEdge] = []
    seen_ids: set[str] = set()
    rule_action_ids: list[list[str]] = []

    for rule_index, raw_rule in enumerate(rules):
        rule = _as_mapping(raw_rule, f"workflow.rules[{rule_index}]")
        actions = rule.get("actions")
        if not isinstance(actions, list):
            raise ValueError(f"workflow.rules[{rule_index}].actions 必须是数组")
        ids: list[str] = []
        for action_index, wrapped in enumerate(actions):
            wrapped = _as_mapping(
                wrapped,
                f"workflow.rules[{rule_index}].actions[{action_index}]",
            )
            action = _as_mapping(
                wrapped.get("action"),
                f"workflow.rules[{rule_index}].actions[{action_index}].action",
            )
            path = f"workflow.rules[{rule_index}].actions[{action_index}].action"
            node_id = _text(
                _first(action, "workflow_node_id", "uuid", "id"),
                path=f"{path}.workflow_node_id",
            )
            if node_id in seen_ids:
                raise ValueError(f"重复的 workflow_node_id: {node_id}")
            seen_ids.add(node_id)
            device_id = _text(
                action.get("device_id"), path=f"{path}.device_id"
            )
            method = _text(
                _first(action, "method", "action_name"),
                path=f"{path}.method",
            )
            params = _first(action, "params", "param")
            if params is None:
                params = {}
            if not isinstance(params, Mapping):
                raise ValueError(f"{path}.params 必须是对象")
            disabled = action.get("disabled", False)
            if not isinstance(disabled, bool):
                raise ValueError(f"{path}.disabled 必须是 bool")
            nodes.append(
                WorkflowNode(
                    id=node_id,
                    device_id=device_id,
                    action_name=method,
                    param=dict(params),
                    material_requirements=_material_requirements(action, path),
                    resource_lock_keys=_resource_lock_keys(action, path),
                    disabled=disabled,
                )
            )
            ids.append(node_id)
        rule_action_ids.append(ids)

        # Legacy rules are ordered action lists. Only connect actions in the
        # same rule; cross-rule dependencies must be declared explicitly.
        for source, target in zip(ids, ids[1:]):
            edges.append(
                WorkflowEdge(
                    uuid=f"{source}->{target}",
                    source_node_id=source,
                    target_node_id=target,
                )
            )

    node_by_id = {node.id: node for node in nodes}
    for rule_index, raw_rule in enumerate(rules):
        rule = _as_mapping(raw_rule, f"workflow.rules[{rule_index}]")
        actions = rule.get("actions") or []
        ids = rule_action_ids[rule_index]
        for action_index, wrapped in enumerate(actions):
            action = _as_mapping(
                _as_mapping(wrapped, "action wrapper").get("action"),
                f"workflow.rules[{rule_index}].actions[{action_index}].action",
            )
            dependencies = _dependencies(
                action,
                f"workflow.rules[{rule_index}].actions[{action_index}].action",
            )
            if dependencies is None:
                continue
            target = ids[action_index]
            # Remove the implicit sequential edge for this target when an
            # explicit dependency list is supplied.
            edges = [
                edge
                for edge in edges
                if edge.target_node_id != target
                or edge.source_node_id not in set(ids)
            ]
            for source in dependencies:
                if source not in node_by_id:
                    raise ValueError(
                        f"节点 {target} depends_on 未找到节点: {source}"
                    )
                edges.append(
                    WorkflowEdge(
                        uuid=f"{source}->{target}",
                        source_node_id=source,
                        target_node_id=target,
                    )
                )

    root_name = root.get("name") or "szlab-workflow"
    resolved_id = workflow_id or str(root.get("workflow_id") or root_name)
    resolved_priority = (
        priority if priority is not None else root.get("priority", "normal")
    )
    return WorkflowSpec(
        workflow_id=_text(resolved_id, path="workflow_id"),
        nodes=nodes,
        edges=edges,
        priority=resolved_priority,
    )


def load_szlab_workflow_spec(
    path: str | Path,
    *,
    workflow_id: str | None = None,
    priority: Any | None = None,
) -> WorkflowSpec:
    """Load and convert a SZLab JSON file."""
    source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    return workflow_spec_from_szlab_json(
        _as_mapping(raw, str(source)),
        workflow_id=workflow_id,
        priority=priority,
    )
