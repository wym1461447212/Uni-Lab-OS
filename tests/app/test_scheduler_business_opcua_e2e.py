from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from scripts.szlab_task_opc_simulator import SimulatorConfig, load_simulator_profile, run_simulator
from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer
from unilabos.app.scheduler import (
    EdgeScheduler,
    MaterialRequirement,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
)
from unilabos.app.scheduler.inventory import InventoryService, InventoryStore
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
    SzlabMixerPipettingStationDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import SzlabMixerRobotDevice


REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = REPO_ROOT / "scripts/config/szlab_business_opc_e2e.json"
ENDPOINT = "opc.tcp://127.0.0.1:50130/"


def _write_profile_csv(path: Path, profile) -> None:
    lines = ["变量名\t数据类型\t初始值\tnode_id"]
    type_names = {"bool": "BOOL", "int": "DINT", "float": "REAL", "string": "STRING"}
    for variable in profile.variables:
        initial = variable.initial_value if variable.has_initial_value else 0
        lines.append(
            f"{variable.name}\t{type_names[variable.data_type]}\t{initial}\t"
            f"ns=4;s=上位机通讯|{variable.name}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class LiveOpcDispatcher:
    """把 EdgeScheduler 的 Job 真正映射为 OPC-connected device calls."""

    def __init__(self, robot, pipetting_station):
        self.robot = robot
        self.pipetting_station = pipetting_station
        self.pending: list[dict] = []
        self.executed: list[dict] = []

    def dispatch(self, payload: dict) -> None:
        self.pending.append(dict(payload))

    def drain(self, scheduler: EdgeScheduler) -> None:
        while self.pending:
            payload = self.pending.pop(0)
            if payload["device_id"] == "szlab_mixer_robot":
                device = self.robot
            elif payload["device_id"] == "szlab_mixer_pipetting_station":
                device = self.pipetting_station
            else:
                raise AssertionError(f"未接入 OPC 的设备动作: {payload}")
            action = getattr(device, payload["action"])
            result = action(**payload["params"])
            assert result.get("success") is True, (payload, result)
            self.executed.append({**payload, "result": result})
            scheduler.on_job_finished(payload["job_id"], success=True)


@pytest.fixture
def live_business_opcua(tmp_path):
    logging.getLogger("opcua").setLevel(logging.WARNING)
    profile = load_simulator_profile(PROFILE_PATH)
    csv_path = tmp_path / "business_nodes.tsv"
    _write_profile_csv(csv_path, profile)
    node_ids = {
        variable.name: f"ns=4;s=上位机通讯|{variable.name}"
        for variable in profile.variables
    }
    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=csv_path,
        object_name="上位机通讯",
        namespace_uri="http://unilabos.com/opcua/szlab-business-e2e",
        server_name="UniLabOS SZLab business PLC simulator",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="node_id",
        initial_values={
            "Robot_Home": True,
            "Robot_任务允许写入": True,
            "Robot_任务完成": 0,
            "S09原点信号_1": True,
            "S09允许加工": True,
            "传感器状态_上位机[0].NO[0]": False,
            "传感器状态_上位机[0].NO[1]": True,
            "传感器状态_上位机[4].NO[5]": True,
        },
    )
    server.start()
    stop_event = threading.Event()
    simulator_errors: list[BaseException] = []
    config = SimulatorConfig(
        profile=profile,
        url=ENDPOINT,
        poll_interval=0.05,
        io_timeout=2.0,
        allow_unsafe_url=True,
    )

    def run_sim() -> None:
        try:
            code = run_simulator(
                config,
                stop_requested=stop_event.is_set,
                interruptible_wait=stop_event.wait,
                logger=logging.getLogger("business-opc-simulator"),
            )
            if code:
                simulator_errors.append(RuntimeError(f"simulator exit code={code}"))
        except BaseException as exc:
            simulator_errors.append(exc)

    simulator_thread = threading.Thread(target=run_sim, daemon=True)
    simulator_thread.start()
    plc = SZLabPolyPLCDevice(
        url=ENDPOINT,
        csv_path=False,
        node_id_map=node_ids,
        opcua_object_name="上位机通讯",
        auto_connect=True,
        opcua_timeout=2.0,
    )
    robot = SzlabMixerRobotDevice(poll_interval=0.05, enable_gripper_check=False)
    robot.set_plc_gateway(plc)
    pipetting = SzlabMixerPipettingStationDevice(
        url=ENDPOINT,
        csv_path=False,
        auto_connect=False,
        use_plc_gateway=True,
    )
    pipetting.set_plc_gateway(plc)
    dispatcher = LiveOpcDispatcher(robot, pipetting)
    try:
        yield server, dispatcher, simulator_errors
    finally:
        stop_event.set()
        simulator_thread.join(timeout=5.0)
        plc.disconnect()
        server.stop()
    assert not simulator_thread.is_alive()


def _beaker_node(position: str, lot_id: str = "beaker-lot") -> WorkflowNode:
    return WorkflowNode(
        id=f"place-{position}",
        device_id="szlab_mixer_robot",
        action_name="submit_place_to_s03",
        param={"product_type": 1, "position": position},
        material_requirements=[MaterialRequirement(lot_id, 1, "beaker")],
    )


def test_business_scenario_1_register_beaker_and_run_single_workflow(live_business_opcua):
    server, dispatcher, simulator_errors = live_business_opcua
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot("beaker", 1, lot_id="beaker-lot", container_id="beaker-stock")
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)

    result = scheduler.submit_workflow(WorkflowSpec("single-beaker", [_beaker_node("1-1")]))
    dispatcher.drain(scheduler)

    assert result["state"] == WorkflowState.RUNNING.value
    assert scheduler.get_workflow_state("single-beaker") is WorkflowState.SUCCESS
    assert inventory.material_total("beaker") == 0
    assert server.read("传感器状态_上位机[0].NO[6]") is True
    assert not simulator_errors
    assert [item["action"] for item in dispatcher.executed] == ["submit_place_to_s03"]


def test_business_scenario_2_schedule_five_parameterized_workflows(live_business_opcua):
    server, dispatcher, simulator_errors = live_business_opcua
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot("beaker", 5, lot_id="beaker-lot", container_id="beaker-stock")
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)

    positions = ["1-1", "1-2", "1-3", "1-4", "1-5"]
    for index, position in enumerate(positions, start=1):
        scheduler.submit_workflow(
            WorkflowSpec(f"batch-{index}", [_beaker_node(position)])
        )
    dispatcher.drain(scheduler)

    assert all(
        scheduler.get_workflow_state(f"batch-{index}") is WorkflowState.SUCCESS
        for index in range(1, 6)
    )
    assert inventory.material_total("beaker") == 0
    assert [item["params"]["position"] for item in dispatcher.executed] == positions
    assert [server.read(f"传感器状态_上位机[0].NO[{index + 6}]") for index in range(5)] == [True] * 5
    assert not simulator_errors


def test_business_scenario_3_tip_shortage_runs_real_change_box_then_resumes(live_business_opcua):
    server, dispatcher, simulator_errors = live_business_opcua
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot("tip", 0, lot_id="tip-lot", container_id="s09-tip-1")
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    workflow = WorkflowSpec(
        "tip-shortage",
        [
            WorkflowNode(
                "take-tip",
                "szlab_mixer_robot",
                "submit_pick_from_s09",
                param={"product_type": 1, "position": 1},
                material_requirements=[MaterialRequirement("tip-lot", 1, "tip")],
            )
        ],
    )

    result = scheduler.submit_workflow(workflow)
    assert result["state"] == WorkflowState.WAITING_MATERIAL.value
    dispatcher.drain(scheduler)
    assert [item["action"] for item in dispatcher.executed] == [
        "go_to_safe_position",
        "submit_pick_from_s09",
        "submit_place_to_s02",
        "submit_pick_from_s02",
        "submit_place_to_s09",
    ]
    assert scheduler.get_workflow_state("tip-shortage") is WorkflowState.WAITING_MATERIAL

    scheduler.on_inventory_inbound("tip", 1, lot_id="tip-lot")
    dispatcher.drain(scheduler)
    assert scheduler.get_workflow_state("tip-shortage") is WorkflowState.SUCCESS
    assert inventory.material_total("tip") == 0
    # The resumed user action consumes the newly installed TIP, so S09 is
    # empty again after the complete business scenario.
    assert server.read("传感器状态_上位机[4].NO[5]") is False
    assert not simulator_errors


def test_business_scenario_4_every_dispatched_job_uses_live_opc_device(live_business_opcua):
    server, dispatcher, simulator_errors = live_business_opcua
    inventory = InventoryService(InventoryStore(":memory:"))
    inventory.inbound_lot("beaker", 1, lot_id="beaker-lot", container_id="beaker-stock")
    scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=inventory)
    scheduler.submit_workflow(WorkflowSpec("opc-only", [_beaker_node("3-6")]))
    dispatcher.drain(scheduler)

    assert dispatcher.executed
    assert all(item["device_id"] in {"szlab_mixer_robot", "szlab_mixer_pipetting_station"} for item in dispatcher.executed)
    assert all(item["result"]["success"] is True for item in dispatcher.executed)
    assert server.read("传感器状态_上位机[1].NO[7]") is True
    assert not simulator_errors
