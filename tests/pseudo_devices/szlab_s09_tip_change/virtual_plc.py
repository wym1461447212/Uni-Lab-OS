"""虚拟 PLC：完成 S09 工艺和机器人换 TIP 料架握手，并改写在位传感器。"""

from __future__ import annotations

import threading
import time
from typing import Any

from opcua import ua


S09_TIP_SENSORS = {
    1: "传感器状态_上位机[4].NO[5]",
    2: "传感器状态_上位机[4].NO[6]",
}
S02_TIP_SENSORS = {
    position: f"传感器状态_上位机[0].NO[{position - 1}]" for position in range(1, 7)
}
_PICK_FROM_S09 = 20
_PLACE_TO_S09 = 19
_PLACE_TO_S02 = 3
_PICK_FROM_S02 = 4


class VirtualTipRackPlc:
    def __init__(self, url: str, object_name: str, poll_interval: float = 0.02) -> None:
        self.url = url
        self.object_name = object_name
        self.poll_interval = poll_interval
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="virtual-tip-rack-plc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        from tests.pseudo_devices.common.opcua_flow_daemon import connect_with_retry

        client, nodes = connect_with_retry(
            url=self.url,
            object_name=self.object_name,
            timeout=20.0,
            interval=0.2,
        )
        try:
            while not self._stop.is_set():
                try:
                    self._tick(nodes)
                except Exception as exc:
                    self.errors.append(str(exc))
                time.sleep(self.poll_interval)
        finally:
            client.disconnect()

    def _tick(self, nodes: dict[str, Any]) -> None:
        self._complete_robot_task(nodes)
        self._complete_s09_process(nodes)

    def _complete_robot_task(self, nodes: dict[str, Any]) -> None:
        write_done = bool(nodes["Robot_任务写入完成"].get_value())
        task_number = int(nodes["任务号"].get_value() or 0)
        completed = int(nodes["Robot_任务完成"].get_value() or 0)
        if write_done and task_number and completed != task_number:
            self._move_tip_box(nodes, task_number)
            _write_node(nodes["Robot_任务完成"], task_number)
            return
        if not write_done and completed != 0:
            _write_node(nodes["Robot_任务完成"], 0)

    def _move_tip_box(self, nodes: dict[str, Any], task_number: int) -> None:
        if task_number == _PICK_FROM_S09:
            position = int(nodes["S09取放料编号"].get_value() or 0)
            _write_node(nodes[S09_TIP_SENSORS[position]], False)
            return
        if task_number == _PLACE_TO_S09:
            position = int(nodes["S09取放料编号"].get_value() or 0)
            _write_node(nodes[S09_TIP_SENSORS[position]], True)
            return
        if task_number == _PLACE_TO_S02:
            position = int(nodes["S02取放料编号"].get_value() or 0)
            _write_node(nodes[S02_TIP_SENSORS[position]], True)
            return
        if task_number == _PICK_FROM_S02:
            position = int(nodes["S02取放料编号"].get_value() or 0)
            _write_node(nodes[S02_TIP_SENSORS[position]], False)

    def _complete_s09_process(self, nodes: dict[str, Any]) -> None:
        params_written = bool(nodes["S09参数写入完成"].get_value())
        process = int(nodes["S09工艺选择"].get_value() or 0)
        done = int(nodes["S09工艺完成"].get_value() or 0)
        if params_written and process and done != process:
            # 设备会先读到旧完成值，再等待本次工艺号。稍等再置位，避免一上来就相等。
            time.sleep(0.05)
            if not bool(nodes["S09参数写入完成"].get_value()):
                return
            process = int(nodes["S09工艺选择"].get_value() or 0)
            if process in {1, 2, 3, 4}:
                _write_node(nodes[f"S09原点信号_{process}"], True)
            if process:
                _write_node(nodes["S09工艺完成"], process)
            return
        if not params_written and done != 0:
            _write_node(nodes["S09工艺完成"], 0)


def _write_node(node: Any, value: Any) -> None:
    variant_type = node.get_data_type_as_variant_type()
    if variant_type == ua.VariantType.Boolean:
        typed: Any = bool(value)
    elif variant_type in {ua.VariantType.Int16, ua.VariantType.Int32, ua.VariantType.UInt16}:
        typed = int(value)
    elif variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
        typed = float(value)
    else:
        typed = value
    node.set_value(ua.Variant(typed, variant_type))
