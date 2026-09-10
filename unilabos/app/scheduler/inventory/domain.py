"""调度测试所需的最小库存领域对象。"""

from __future__ import annotations

from dataclasses import dataclass


class InventoryError(RuntimeError):
    pass


class InsufficientStock(InventoryError):
    """当前可用库存不足。"""


@dataclass(frozen=True)
class MaterialRequirement:
    lot_id: str
    quantity: float = 1.0
    unit: str = ""
    template_id: str = ""
    instance_uuid: str = ""
    barcode: str = ""

    def is_instance_requirement(self) -> bool:
        return bool(self.instance_uuid or self.barcode)

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialRequirement":
        return cls(
            lot_id=str(data.get("lot_id") or data.get("lotId") or ""),
            quantity=float(data.get("quantity", 1.0)),
            unit=str(data.get("unit") or ""),
        )
