"""本地调度器的轻量库存实现。

它只为缺料准入、补料后重试和本地单元测试提供数量事实；生产环境可以通过
同名方法注入已有库存服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import InsufficientStock


@dataclass
class _Lot:
    quantity_total: float
    quantity_available: float
    quantity_reserved: float = 0.0


class InventoryStore:
    """兼容 ``InventoryStore(":memory:")`` 的内存存储。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.lots: dict[str, _Lot] = {}
        self.reservations: dict[tuple[str, str, int], dict[str, Any]] = {}

    def get_lot(self, lot_id: str) -> dict[str, float]:
        lot = self.lots[lot_id]
        return {
            "quantity_total": lot.quantity_total,
            "quantity_available": lot.quantity_available,
            "quantity_reserved": lot.quantity_reserved,
        }

    def get_reservation(self, workflow_id: str, node_id: str, attempt: int = 1):
        return self.reservations[(workflow_id, node_id, attempt)]


class InventoryService:
    def __init__(self, store: InventoryStore) -> None:
        self.store = store

    def inbound_lot(
        self,
        template_id: str,
        quantity: float,
        *,
        lot_id: str,
    ) -> None:
        del template_id
        amount = float(quantity)
        lot = self.store.lots.get(lot_id)
        if lot is None:
            self.store.lots[lot_id] = _Lot(amount, amount)
            return
        lot.quantity_total += amount
        lot.quantity_available += amount

    @staticmethod
    def _amounts(requirements: dict[str, list[Any]]) -> dict[str, float]:
        amounts: dict[str, float] = {}
        for values in requirements.values():
            for requirement in values:
                lot_id = str(getattr(requirement, "lot_id", "") or "")
                quantity = float(getattr(requirement, "quantity", 0.0))
                if lot_id and quantity > 0:
                    amounts[lot_id] = amounts.get(lot_id, 0.0) + quantity
        return amounts

    def _reserve(
        self,
        workflow_id: str,
        requirements: dict[str, list[Any]],
        *,
        attempt: int = 1,
    ) -> None:
        amounts = self._amounts(requirements)
        for lot_id, quantity in amounts.items():
            lot = self.store.lots.get(lot_id)
            if lot is None or lot.quantity_available + 1e-9 < quantity:
                raise InsufficientStock(
                    f"lot {lot_id} available stock is insufficient"
                )
        for lot_id, quantity in amounts.items():
            lot = self.store.lots[lot_id]
            lot.quantity_available -= quantity
            lot.quantity_reserved += quantity
        for node_id, values in requirements.items():
            node_amounts = self._amounts({node_id: values})
            self.store.reservations[(workflow_id, node_id, attempt)] = {
                "status": "active",
                "amounts": node_amounts,
            }

    def reserve_workflow(
        self,
        workflow_id: str,
        requirements: dict[str, list[Any]],
        *,
        attempt: int = 1,
    ) -> None:
        self._reserve(workflow_id, requirements, attempt=attempt)

    def reserve_node(
        self,
        workflow_id: str,
        node_id: str,
        requirements: list[Any],
        *,
        attempt: int = 1,
    ) -> None:
        key = (workflow_id, node_id, attempt)
        if key in self.store.reservations:
            return
        self._reserve(workflow_id, {node_id: requirements}, attempt=attempt)

    def consume_reservation(
        self,
        workflow_id: str,
        node_id: str,
        *,
        attempt: int = 1,
    ) -> None:
        reservation = self.store.reservations[(workflow_id, node_id, attempt)]
        if reservation["status"] != "active":
            return
        for lot_id, quantity in reservation["amounts"].items():
            lot = self.store.lots[lot_id]
            lot.quantity_reserved -= quantity
            lot.quantity_total -= quantity
        reservation["status"] = "consumed"

    def release_workflow(self, workflow_id: str, *, reason: str = "") -> None:
        del reason
        for (reserved_workflow, _node_id, _attempt), reservation in list(
            self.store.reservations.items()
        ):
            if reserved_workflow != workflow_id or reservation["status"] != "active":
                continue
            for lot_id, quantity in reservation["amounts"].items():
                lot = self.store.lots[lot_id]
                lot.quantity_reserved -= quantity
                lot.quantity_available += quantity
            reservation["status"] = "released"
