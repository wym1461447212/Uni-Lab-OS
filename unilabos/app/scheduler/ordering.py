"""设备锁、物料锁和 workflow priority 的稳定排序。"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ReadyTask


@dataclass(frozen=True)
class OrderingContext:
    busy_device_action_keys: set[str]
    busy_resource_lock_keys: set[str] | None = None

    def __post_init__(self) -> None:
        if self.busy_resource_lock_keys is None:
            object.__setattr__(self, "busy_resource_lock_keys", set())

    def device_is_busy(self, task: ReadyTask) -> bool:
        return (
            task.node.device_action_key in self.busy_device_action_keys
            or task.node.device_lock_key in self.busy_device_action_keys
        )

    def resource_is_busy(self, task: ReadyTask) -> bool:
        return bool(
            set(task.resource_lock_keys or ())
            & set(self.busy_resource_lock_keys or ())
        )


class StableLocalOrderer:
    """硬约束先于优先级，补料动作只在锁准入后优先于用户任务。"""

    def order(
        self,
        ready: list[ReadyTask],
        ctx: OrderingContext,
    ) -> list[ReadyTask]:
        return sorted(
            ready,
            key=lambda task: (
                ctx.device_is_busy(task),
                ctx.resource_is_busy(task),
                not task.is_resource_unblocking,
                -task.priority_weight,
                task.submitted_at,
                task.workflow_id,
                task.node.id,
            ),
        )
