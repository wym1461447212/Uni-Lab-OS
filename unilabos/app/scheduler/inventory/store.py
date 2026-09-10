"""向后兼容导出：旧测试/适配器从 ``inventory.store`` 导入存储。"""

from .service import InventoryStore

__all__ = ["InventoryStore"]
