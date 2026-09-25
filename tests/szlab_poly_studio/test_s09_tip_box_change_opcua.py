"""在 CSV OPC UA 虚拟 PLC 上跑通：空 TIP 阻塞加液、换料架、按上料落点改参数、加液继续。"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path

from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
    SzlabMixerPipettingStationDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import SzlabMixerRobotDevice
from tests.pseudo_devices.szlab_s09_tip_change.virtual_plc import VirtualTipRackPlc
from tests.szlab_poly_studio.test_task_execution_coordinator import (
    ATOMIC_START_NODES,
    DENSITY_NODE,
    WORKFLOW_PATH,
    FakeTaskClient,
    _coordinator,
    _density_workspace,
    _liquid_workspace,
    _pump_tip_change,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CSV_PATH = REPO_ROOT / "tests" / "pseudo_devices" / "szlab_s09_tip_change" / "nodes.csv"
ENDPOINT = "opc.tcp://127.0.0.1:50109/"
OBJECT_NAME = "VirtualS09TipChange"


def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def test_virtual_plc_changes_tip_rack_then_add_liquid_continues(tmp_path):
    logging.getLogger("opcua").setLevel(logging.WARNING)
    from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer

    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=CSV_PATH,
        object_name=OBJECT_NAME,
        namespace_uri="http://unilabos.com/opcua/test/pseudo-device",
        server_name="UniLabOS Test OPC UA Server",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="",
        initial_values={},
    )
    virtual_plc = VirtualTipRackPlc(ENDPOINT, OBJECT_NAME)
    plc = None
    coordinator = None
    try:
        server.start()
        virtual_plc.start()
        time.sleep(0.4)

        plc = SZLabPolyPLCDevice(
            url=ENDPOINT,
            csv_path=False,
            opcua_object_name=OBJECT_NAME,
            mixing_alarm_poll_interval=30,
        )
        robot = SzlabMixerRobotDevice(poll_interval=0.05, enable_gripper_check=False)
        robot.set_plc_gateway(plc)
        station = SzlabMixerPipettingStationDevice(
            use_plc_gateway=True,
            tip_reuse_state_path=str(tmp_path / "tip_state.json"),
        )
        station.set_plc_gateway(plc)
        initialized = station.initialize_reusable_tip_inventory(reset=True, used_tip_count=24)
        assert initialized["success"] is True

        client = FakeTaskClient(_liquid_workspace())
        results: list[dict] = []

        def runner(node, _devices, action_callable):
            result = action_callable(**node.param)
            if node.method == "add_liquid_with_reusable_tip":
                results.append(result)
            return result

        coordinator = _coordinator(
            client,
            runner,
            {
                "szlab_mixer_pipetting_station": station,
                "szlab_mixer_robot": robot,
            },
        )
        liquid = next(item for item in ATOMIC_START_NODES if item.uuid == "w03_add_liquid_s09")
        liquid = replace(
            liquid,
            param={
                "liquid_station_index": 1,
                "solvent_batch_id": "solvent-batch-001",
                "volume": 100,
                "volume_unit": "raw",
                "reuse_tip": False,
            },
        )

        first = coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=[liquid])
        assert first["claimed"] == 0, first["diagnostics"]
        assert coordinator._tip_waits, first["diagnostics"]
        second = _pump_tip_change(
            coordinator,
            [liquid],
            timeout=90,
        )
        assert client.parameter_updates, second["diagnostics"]
        assert client.parameter_updates[0]["parameters"] == {
            "take_tip_box_index": 2,
            "release_tip_box_index": 1,
        }
        assert second["diagnostics"][0]["detail"]["full_box_position"] == 2
        assert second["claimed"] == 1, second["diagnostics"]
        tips = [
            item for item in client.response["workspace"]["task_instances"]
            if item["template_id"] == "tip_box_change"
        ]
        assert len(tips) == 1
        assert tips[0]["status"] == "completed"
        assert tips[0]["payload"]["priority"] == "urgent"

        _wait_until(
            lambda: bool(results) or any(
                item.get("instance_id") == "instance-1" for item in client.failed
            ),
            60,
            f"加液没有返回: diagnostics={second['diagnostics']} plc={virtual_plc.errors}",
        )
        assert client.failed == [], client.failed
        assert results and results[0]["success"] is True, results[0].get("message") if results else results
        tip_reuse = results[0]["data"]["tip_reuse"]
        assert tip_reuse["take_tip_box_index"] == 2
        assert tip_reuse["release_tip_box_index"] == 1
        assert plc.read_variable("传感器状态_上位机[4].NO[6]", use_cache=False) is True
        assert plc.read_variable("传感器状态_上位机[4].NO[5]", use_cache=False) is True
        assert plc.read_variable("传感器状态_上位机[0].NO[1]", use_cache=False) is True
        assert plc.read_variable("传感器状态_上位机[0].NO[4]", use_cache=False) is False
    finally:
        if coordinator is not None:
            coordinator.shutdown()
        if plc is not None:
            plc.disconnect()
        virtual_plc.stop()
        server.stop()


def test_virtual_plc_changes_tip_rack_then_measure_density_continues(tmp_path):
    logging.getLogger("opcua").setLevel(logging.WARNING)
    from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer

    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=CSV_PATH,
        object_name=OBJECT_NAME,
        namespace_uri="http://unilabos.com/opcua/test/pseudo-device",
        server_name="UniLabOS Test OPC UA Server",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="",
        initial_values={},
    )
    virtual_plc = VirtualTipRackPlc(ENDPOINT, OBJECT_NAME)
    plc = None
    coordinator = None
    try:
        server.start()
        virtual_plc.start()
        time.sleep(0.4)

        plc = SZLabPolyPLCDevice(
            url=ENDPOINT,
            csv_path=False,
            opcua_object_name=OBJECT_NAME,
            mixing_alarm_poll_interval=30,
        )
        robot = SzlabMixerRobotDevice(poll_interval=0.05, enable_gripper_check=False)
        robot.set_plc_gateway(plc)
        station = SzlabMixerPipettingStationDevice(
            use_plc_gateway=True,
            tip_reuse_state_path=str(tmp_path / "tip_state.json"),
        )
        station.set_plc_gateway(plc)
        initialized = station.initialize_reusable_tip_inventory(reset=True, used_tip_count=24)
        assert initialized["success"] is True

        client = FakeTaskClient(_density_workspace())
        results: list[dict] = []

        def runner(node, _devices, action_callable):
            result = action_callable(**node.param)
            if node.method == "measure_density":
                results.append(result)
            return result

        coordinator = _coordinator(
            client,
            runner,
            {
                "szlab_mixer_pipetting_station": station,
                "szlab_mixer_robot": robot,
            },
        )
        density = replace(
            DENSITY_NODE,
            param={
                "density_volume": 5000,
                "density_measurement_count": 1,
                "volume_unit": "raw",
            },
        )

        first = coordinator.cycle(workflow_path=WORKFLOW_PATH, workflow_nodes=[density])
        assert first["claimed"] == 0, first["diagnostics"]
        assert coordinator._tip_waits, first["diagnostics"]
        second = _pump_tip_change(coordinator, [density], timeout=90)

        assert client.parameter_updates == []
        assert second["diagnostics"][0]["detail"]["full_box_position"] == 2
        assert second["claimed"] == 1, second["diagnostics"]
        _wait_until(
            lambda: bool(results or client.failed),
            60,
            f"测密度没有返回: diagnostics={second['diagnostics']} plc={virtual_plc.errors}",
        )
        assert client.failed == [], client.failed
        assert results and results[0]["success"] is True, results[0].get("message") if results else results
        density_tip = results[0]["data"]["density_tip"]
        assert density_tip["take_tip_box_index"] == 2
        assert density_tip["release_tip_box_index"] == 1
    finally:
        if coordinator is not None:
            coordinator.shutdown()
        if plc is not None:
            plc.disconnect()
        virtual_plc.stop()
        server.stop()
