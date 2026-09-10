"""Scheduler 的最小派发适配器。"""

from __future__ import annotations

from typing import Any


class RecordingDispatcher:
    """本地测试与 dry-run 使用的派发器。"""

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def dispatch(self, payload: dict[str, Any]) -> None:
        self.dispatched.append(dict(payload))


def build_job_start_payload(
    *,
    job_id: str,
    workflow_id: str,
    node: Any,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "workflow_id": workflow_id,
        "node_id": node.id,
        "device_id": node.device_id,
        "action": node.action_name,
        "action_type": node.action_type,
        "params": dict(node.param),
    }
