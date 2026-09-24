"""调度测试所需的最小库存领域对象。"""

from __future__ import annotations

from dataclasses import dataclass


class InventoryError(RuntimeError):
    pass


class InsufficientStock(InventoryError):
    """当前可用库存不足。"""


class MaterialSwitchRequired(InventoryError):
    """当前料桶不足，但同一物料的总可用库存仍足够，需要先换桶。"""

    def __init__(self, message: str, *, lot_id: str = "", container_id: str = "", quantity: float = 0.0, replacement_container_id: str = ""):
        super().__init__(message)
        self.lot_id = lot_id
        self.container_id = container_id
        self.quantity = float(quantity)
        self.replacement_container_id = replacement_container_id


@dataclass(frozen=True)
class MaterialRequirement:
    lot_id: str
    quantity: float = 1.0
    unit: str = ""
    template_id: str = ""
    instance_uuid: str = ""
    barcode: str = ""
    container_id: str = ""
    material_id: str = ""

    def is_instance_requirement(self) -> bool:
        return bool(self.instance_uuid or self.barcode)

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialRequirement":
        return cls(
            lot_id=str(data.get("lot_id") or data.get("lotId") or ""),
            material_id=str(data.get("material_id") or data.get("materialId") or ""),
            quantity=float(data.get("quantity", 1.0)),
            unit=str(data.get("unit") or ""),
            container_id=str(data.get("container_id") or data.get("containerId") or ""),
        )
