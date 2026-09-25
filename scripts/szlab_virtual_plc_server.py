#!/usr/bin/env python3
"""本机虚拟 PLC 的 OPC UA 进程。

这里只暴露 ns=4 变量和上电初值，不实现机器人或工站握手。
业务完成逻辑在 scripts.szlab_virtual_plc_controller，它作为 OPC UA 客户端连到本进程。
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import signal
import time
from pathlib import Path
from typing import Any

from opcua import Server, ua

from unilabos.devices.workstation.szlab_poly_studio.error_codes import PLC_ALARM_MAP
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import s02_sensor, s04_sensor
from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    S03Sensors,
    S05Sensors,
    S06Sensors,
    S08Sensors,
    S09Sensors,
    S11Sensors,
    read_plc_csv_text,
)


LOGGER = logging.getLogger("szlab-virtual-plc")
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = (
    REPO_ROOT
    / "unilabos"
    / "devices"
    / "workstation"
    / "szlab_poly_studio"
    / "szlab_plc_0810.csv"
)
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840/"
NAMESPACE_URI = "http://unilabos.com/opcua/szlab/virtual-plc"
_DATA_TYPES = {
    "BOOL": ua.VariantType.Boolean,
    "BOOLEAN": ua.VariantType.Boolean,
    "INT": ua.VariantType.Int16,
    "INT16": ua.VariantType.Int16,
    "DINT": ua.VariantType.Int32,
    "INT32": ua.VariantType.Int32,
    "REAL": ua.VariantType.Float,
    "FLOAT": ua.VariantType.Float,
}


class VirtualPlcServer:
    def __init__(self, endpoint: str, csv_path: Path) -> None:
        self.endpoint = endpoint
        self.csv_path = csv_path
        self.nodes: dict[str, Any] = {}
        self.server = Server()
        self.server.set_endpoint(endpoint)
        self.server.set_server_name("SZLab Virtual PLC")
        # 设备图固定使用 ns=4;s=上位机通讯|变量名。默认命名空间占 0 和 1。
        self.server.register_namespace("urn:szlab:virtual:pad2")
        self.server.register_namespace("urn:szlab:virtual:pad3")
        self.namespace_index = self.server.register_namespace(NAMESPACE_URI)
        if self.namespace_index != 4:
            raise RuntimeError(f"虚拟 PLC 命名空间索引必须是 4，实际是 {self.namespace_index}")

    def start(self) -> None:
        self._create_nodes()
        self._apply_initial_state()
        self.server.start()
        LOGGER.info("虚拟 PLC OPC UA 已启动: %s 变量=%d", self.endpoint, len(self.nodes))

    def stop(self) -> None:
        self.server.stop()
        LOGGER.info("虚拟 PLC OPC UA 已停止")

    def _create_nodes(self) -> None:
        objects = self.server.get_objects_node()
        folder = objects.add_object(self.namespace_index, "VirtualPLC")
        for name, variant_type, initial in _load_csv_variables(self.csv_path):
            self._add_variable(folder, name, initial, variant_type)
        for name, variant_type, initial in _alarm_variables():
            if name not in self.nodes:
                self._add_variable(folder, name, initial, variant_type)

    def _add_variable(self, folder: Any, name: str, initial: Any, variant_type: ua.VariantType) -> None:
        node = folder.add_variable(
            ua.NodeId(f"上位机通讯|{name}", self.namespace_index),
            name,
            initial,
            variant_type,
        )
        node.set_writable()
        self.nodes[name] = node

    def _apply_initial_state(self) -> None:
        for name, value in _initial_values().items():
            node = self.nodes.get(name)
            if node is None:
                LOGGER.warning("上电初值跳过未注册变量: %s", name)
                continue
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


def _load_csv_variables(csv_path: Path) -> list[tuple[str, ua.VariantType, Any]]:
    text = read_plc_csv_text(str(csv_path))
    variables: list[tuple[str, ua.VariantType, Any]] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("变量名") or "").strip()
        data_type = (row.get("数据类型") or "").strip().upper()
        variant_type = _DATA_TYPES.get(data_type)
        if not name or variant_type is None or name in seen:
            continue
        seen.add(name)
        variables.append((name, variant_type, _coerce(row.get("初始值"), variant_type)))
    if not variables:
        raise ValueError(f"CSV 中没有可用 PLC 变量: {csv_path}")
    return variables


def _coerce(value: Any, variant_type: ua.VariantType) -> Any:
    text = "" if value is None else str(value).strip()
    if variant_type == ua.VariantType.Boolean:
        return text.upper() in {"ON", "TRUE", "1", "YES"}
    if variant_type in {ua.VariantType.Int16, ua.VariantType.Int32}:
        return int(float(text or 0))
    if variant_type == ua.VariantType.Float:
        return float(text or 0)
    return text


def _alarm_variables() -> list[tuple[str, ua.VariantType, Any]]:
    variables: list[tuple[str, ua.VariantType, Any]] = []
    seen: set[str] = set()
    for alarm in PLC_ALARM_MAP.values():
        parent = alarm.variable_name.split(".NO[", 1)[0]
        root = parent.split("[", 1)[0]
        register = alarm.address.split(".", 1)[0]
        for name, variant_type, initial in (
            (alarm.variable_name, ua.VariantType.Boolean, False),
            (parent, ua.VariantType.Int16, 0),
            (root, ua.VariantType.Int16, 0),
            (register, ua.VariantType.Int16, 0),
        ):
            if name in seen:
                continue
            seen.add(name)
            variables.append((name, variant_type, initial))
    return variables


def _initial_values() -> dict[str, Any]:
    """上电初值。在位和夹爪只表示开机状态，后续改写由控制进程完成。"""
    values: dict[str, Any] = {
        "Robot_Home": True,
        "Robot_任务允许写入": True,
        "Robot_任务写入完成": False,
        "Robot_任务完成": 0,
        "任务号": 0,
        "Robot_夹爪.原点位": True,
        "Robot_夹爪.夹爪状态": 1,
        S05Sensors.MATERIAL: False,
        S06Sensors.MATERIAL: False,
        "S05准备信号": True,
        "S05加工完成": False,
        "S05拍照结果": 0,
        "S06准备信号": True,
        "S06允许加工": True,
        "S07原点信号": True,
        "S07允许加工": True,
        "S08原点信号": True,
        "S08允许加工": True,
        "S09允许加工": True,
        "S09天平读数稳定": True,
        "S09天平读数": 12.34,
        "工站状态[6]": 2,
        "工站状态[7]": 2,
        "工站状态[8]": 2,
    }
    for position in range(1, 5):
        values[f"S04{position}准备信号"] = True
        values[f"S04{position}允许加工"] = True
        # 磁搅驱动会无限等待该变量 == 1，必须写入上电初值。
        values[f"S04{position}磁搅状态"] = 1
        values[f"S04{position}加工完成"] = False
        values[s04_sensor(position)] = False
    for home in range(1, 5):
        values[f"S09原点信号_{home}"] = True
    for bottle in range(1, 6):
        values[f"S09液体瓶{bottle}剩余液量"] = 100.0
        values[S09Sensors.STATION[bottle]] = True
    for tip_box in (1, 2):
        values[S09Sensors.TIP_BOX[tip_box]] = True
    for slot, sensor in S08Sensors.CAP_STORAGE_SLOT.items():
        values[sensor] = False
    for sensor in S08Sensors.CAP_STATION.values():
        values[sensor] = False
    # 废 TIP 位 1-3 先空，供放料；新 TIP 位 4-6 先有料，供取料。
    for position in range(1, 4):
        values[s02_sensor(position)] = False
    for position in range(4, 7):
        values[s02_sensor(position)] = True
    unused_beaker_slots = list(S03Sensors.UNUSED_BEAKER)[:15]
    unused_vial_slots = list(S03Sensors.UNUSED_SAMPLE_VIAL)[:15]
    for slot in unused_beaker_slots:
        values[S03Sensors.UNUSED_BEAKER[slot]] = True
    for slot in unused_vial_slots:
        values[S03Sensors.UNUSED_SAMPLE_VIAL[slot]] = True
    for sensor in S11Sensors.USED_BEAKER.values():
        values[sensor] = False
    for sensor in S11Sensors.USED_SAMPLE_VIAL.values():
        values[sensor] = False
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SZLab 虚拟 PLC 的 OPC UA 进程")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("opcua").setLevel(logging.WARNING)
    server = VirtualPlcServer(args.endpoint, Path(args.csv))
    server.start()

    def _stop(_signum: int, _frame: Any) -> None:
        server.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
