"""本地调度器的轻量库存实现。

它只为缺料准入、补料后重试和本地单元测试提供数量事实；生产环境可以通过
同名方法注入已有库存服务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import InsufficientStock, MaterialSwitchRequired


@dataclass
class _Lot:
    quantity_total: float
    quantity_available: float
    quantity_reserved: float = 0.0
    material_id: str = ""


class InventoryStore:
    """兼容 ``InventoryStore(":memory:")`` 的内存存储。"""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self.lots: dict[str, _Lot] = {}
        # (lot_id, container_id) -> 当前料桶可用量。无 container_id 的旧数据仍按 lot 总量处理。
        self.container_available: dict[tuple[str, str], float] = {}
        self.material_totals: dict[str, float] = {}
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
        container_id: str = "",
        material_id: str | None = None,
    ) -> None:
        amount = float(quantity)
        resolved_material_id = str(material_id or template_id)
        lot = self.store.lots.get(lot_id)
        if lot is None:
            self.store.lots[lot_id] = _Lot(amount, amount, material_id=resolved_material_id)
            lot = self.store.lots[lot_id]
        else:
            lot.quantity_total += amount
            lot.quantity_available += amount
        resolved_material_id = lot.material_id or resolved_material_id
        lot.material_id = resolved_material_id
        self.store.material_totals[resolved_material_id] = self.store.material_totals.get(resolved_material_id, 0.0) + amount
        if container_id:
            key = (lot_id, str(container_id))
            self.store.container_available[key] = (
                self.store.container_available.get(key, 0.0) + amount
            )

    def container_stock(self, lot_id: str, container_id: str) -> float:
        return float(self.store.container_available.get((lot_id, container_id), 0.0))

    def container_binding(self, lot_id: str, container_id: str) -> dict[str, Any]:
        lot = self.store.lots[lot_id]
        return {
            "lot_id": lot_id,
            "material_id": lot.material_id or lot_id,
            "container_id": container_id,
            "quantity_available": self.container_stock(lot_id, container_id),
        }

    def suggest_container(self, lot_id: str, target_container_id: str) -> str | None:
        candidates = [
            (container_id, amount)
            for (candidate_lot, container_id), amount in self.store.container_available.items()
            if candidate_lot == lot_id and container_id != target_container_id and amount > 0
        ]
        return max(candidates, key=lambda item: item[1])[0] if candidates else None

    def material_total(self, material_id: str) -> float:
        return float(self.store.material_totals.get(material_id, 0.0))

    def switch_container(self, lot_id: str, target_container_id: str, quantity: float = 0.0) -> str | None:
        """将有库存的备用料桶切换到目标进料位，返回备用料桶 id。"""
        target = (lot_id, target_container_id)
        if self.container_stock(lot_id, target_container_id) + 1e-9 >= float(quantity):
            return target_container_id
        candidates = [
            (container_id, amount)
            for (candidate_lot, container_id), amount in self.store.container_available.items()
            if candidate_lot == lot_id and container_id != target_container_id and amount > 0
        ]
        if not candidates:
            return None
        source_id, amount = max(candidates, key=lambda item: item[1])
        old_target_amount = self.container_stock(lot_id, target_container_id)
        self.store.container_available[target] = amount
        # 物理换桶是位置交换，不是销毁旧桶；旧桶余额随新位置状态一起保留。
        self.store.container_available[(lot_id, source_id)] = old_target_amount
        return source_id

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
        material_amounts: dict[str, float] = {}
        for values in requirements.values():
            for requirement in values:
                material_id = str(getattr(requirement, "material_id", "") or "")
                quantity = float(getattr(requirement, "quantity", 0.0))
                if material_id and quantity > 0:
                    material_amounts[material_id] = material_amounts.get(material_id, 0.0) + quantity
        for material_id, quantity in material_amounts.items():
            if self.material_total(material_id) + 1e-9 < quantity:
                raise InsufficientStock(
                    f"material {material_id} total available stock is insufficient"
                )
        for values in requirements.values():
            for requirement in values:
                lot_id = str(getattr(requirement, "lot_id", "") or "")
                container_id = str(getattr(requirement, "container_id", "") or "")
                quantity = float(getattr(requirement, "quantity", 0.0))
                if not lot_id or not container_id or quantity <= 0:
                    continue
                # 只有登记过分桶余额时才启用“当前桶不足、总量足够”的分支，
                # 兼容旧的 lot-only 库存。
                if (lot_id, container_id) in self.store.container_available and self.container_stock(lot_id, container_id) + 1e-9 < quantity:
                    raise MaterialSwitchRequired(
                        f"container {container_id} for lot {lot_id} is insufficient; switch required",
                        lot_id=lot_id,
                        container_id=container_id,
                        quantity=quantity,
                        replacement_container_id=self.suggest_container(lot_id, container_id) or "",
                    )
        for lot_id, quantity in amounts.items():
            lot = self.store.lots[lot_id]
            lot.quantity_available -= quantity
            lot.quantity_reserved += quantity
        for node_id, values in requirements.items():
            node_amounts = self._amounts({node_id: values})
            container_amounts = [
                {
                    "lot_id": str(getattr(req, "lot_id", "") or ""),
                    "quantity": float(getattr(req, "quantity", 0.0)),
                    "container_id": str(getattr(req, "container_id", "") or ""),
                }
                for req in values
                if getattr(req, "container_id", "")
            ]
            for item in container_amounts:
                key = (item["lot_id"], item["container_id"])
                self.store.container_available[key] -= item["quantity"]
            self.store.reservations[(workflow_id, node_id, attempt)] = {
                "status": "active",
                "amounts": node_amounts,
                "container_amounts": container_amounts,
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
            material_id = lot.material_id or lot_id
            self.store.material_totals[material_id] = max(
                0.0, self.store.material_totals.get(material_id, 0.0) - quantity
            )
        for item in reservation.get("container_amounts", []):
            # 已在 reserve 时从当前桶扣除；消费只改变 lot 总量。
            del item
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
            for item in reservation.get("container_amounts", []):
                key = (item["lot_id"], item["container_id"])
                self.store.container_available[key] = self.store.container_available.get(key, 0.0) + item["quantity"]
            reservation["status"] = "released"
