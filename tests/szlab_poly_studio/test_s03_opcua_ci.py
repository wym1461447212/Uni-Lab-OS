from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest

from scripts.szlab_task_opc_simulator import (
    SimulatorConfig,
    load_simulator_profile,
    run_simulator,
)
from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import (
    SzlabMixerRobotDevice,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
S03_PROFILE = REPO_ROOT / "scripts/config/szlab_s03_opc_simulator.json"
ENDPOINT = "opc.tcp://127.0.0.1:50125/"


def _write_profile_csv(path: Path, profile) -> None:
    lines = ["变量名\t数据类型\t初始值\tnode_id"]
    for variable in profile.variables:
        data_type = {
            "bool": "BOOL",
            "int": "DINT",
            "float": "REAL",
            "string": "STRING",
        }[variable.data_type]
        initial = variable.initial_value if variable.has_initial_value else 0
        lines.append(
            f"{variable.name}\t{data_type}\t{initial}\t"
            f"ns=4;s=上位机通讯|{variable.name}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def live_s03_opcua(tmp_path):
    """Real TCP OPC UA server plus the declarative S03 PLC state machine."""
    logging.getLogger("opcua").setLevel(logging.WARNING)
    profile = load_simulator_profile(S03_PROFILE)
    csv_path = tmp_path / "s03_nodes.tsv"
    _write_profile_csv(csv_path, profile)
    node_ids = {
        variable.name: f"ns=4;s=上位机通讯|{variable.name}"
        for variable in profile.variables
    }
    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=csv_path,
        object_name="上位机通讯",
        namespace_uri="http://unilabos.com/opcua/szlab-plc-sim",
        server_name="UniLabOS SZLab S03 PLC Simulator",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="node_id",
        initial_values={
            "Robot_Home": True,
            "Robot_任务允许写入": True,
            "Robot_任务完成": 0,
            "工站状态[2]": 2,
            "传感器状态_上位机[0].NO[6]": False,
            "传感器状态_上位机[1].NO[7]": False,
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

    def run() -> None:
        try:
            code = run_simulator(
                config,
                stop_requested=stop_event.is_set,
                interruptible_wait=stop_event.wait,
                logger=logging.getLogger("s03-opc-simulator"),
            )
            if code:
                simulator_errors.append(RuntimeError(f"S03 simulator exit code={code}"))
        except BaseException as exc:  # surface simulator failures in the test thread
            simulator_errors.append(exc)

    simulator_thread = threading.Thread(target=run, daemon=True)
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
    try:
        yield server, robot, simulator_errors
    finally:
        stop_event.set()
        simulator_thread.join(timeout=5.0)
        plc.disconnect()
        server.stop()
    assert not simulator_thread.is_alive()


def test_s03_place_pick_multiple_stack_positions_over_real_opcua(live_s03_opcua):
    server, robot, simulator_errors = live_s03_opcua

    first_place = robot.submit_place_to_s03(product_type=1, position="1-1")
    first_pick = robot.submit_pick_from_s03(product_type=1, position="1-1")
    second_place = robot.submit_place_to_s03(product_type=1, position="3-6")
    second_pick = robot.submit_pick_from_s03(product_type=1, position="3-6")

    assert not simulator_errors
    assert [result["success"] for result in (first_place, first_pick, second_place, second_pick)] == [
        True,
        True,
        True,
        True,
    ]
    assert [result["task_number"] for result in (first_place, first_pick, second_place, second_pick)] == [
        5,
        6,
        5,
        6,
    ]
    assert server.read("传感器状态_上位机[0].NO[6]") is False
    assert server.read("传感器状态_上位机[1].NO[7]") is False
    assert server.read("工站状态[2]") == 2
    deadline = time.monotonic() + 1.0
    while not server.read("Robot_任务允许写入") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.read("Robot_任务允许写入") is True
