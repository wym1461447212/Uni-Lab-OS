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
PLC_CSV = REPO_ROOT / "unilabos/devices/workstation/szlab_poly_studio/szlab_plc_0721.csv"
TIP_PROFILE = REPO_ROOT / "scripts/config/szlab_tip_box_opc_simulator.json"
ENDPOINT = "opc.tcp://127.0.0.1:50120/"


TIP_NODE_NAMES = (
    "Robot_Home",
    "Robot_任务允许写入",
    "Robot_任务写入完成",
    "任务号",
    "Robot_任务完成",
    "S02取放料编号",
    "S09取放料产品",
    "S09取放料编号",
    "S09原点信号_1",
    "传感器状态_上位机[0].NO[0]",
    "传感器状态_上位机[0].NO[1]",
    "传感器状态_上位机[0].NO[2]",
    "传感器状态_上位机[0].NO[3]",
    "传感器状态_上位机[0].NO[4]",
    "传感器状态_上位机[0].NO[5]",
    "传感器状态_上位机[4].NO[5]",
    "传感器状态_上位机[4].NO[6]",
)


def _write_tip_node_csv(path: Path) -> None:
    """Write only the real PLC variables used by the four TIP actions.

    The server still uses the production CSV schema and explicit NodeIds; the
    reduced table only avoids making the client browse unrelated PLC tags.
    """
    lines = ["变量名\t数据类型\t初始值\tnode_id"]
    for name in TIP_NODE_NAMES:
        data_type = "BOOL" if "传感器" in name or name in {"Robot_Home", "Robot_任务允许写入", "Robot_任务写入完成", "S09原点信号_1"} else "INT"
        lines.append(f"{name}\t{data_type}\t0\tns=4;s=上位机通讯|{name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def live_tip_box_opcua(tmp_path):
    """Real TCP OPC UA server + task handshake simulator for S02/S09 TIP exchange."""
    from tests.pseudo_devices.common.opcua_csv_server import CsvOpcUaServer

    logging.getLogger("opcua").setLevel(logging.WARNING)
    logging.getLogger("unilabos.utils.log.post_process").setLevel(logging.WARNING)

    tip_csv = tmp_path / "tip_box_nodes.tsv"
    _write_tip_node_csv(tip_csv)
    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=tip_csv,
        object_name="上位机通讯",
        namespace_uri="http://unilabos.com/opcua/szlab-plc-sim",
        server_name="UniLabOS SZLab TIP Box PLC Simulator",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="node_id",
        initial_values={
            "Robot_Home": True,
            "Robot_任务允许写入": True,
            "Robot_任务完成": 0,
            "传感器状态_上位机[0].NO[0]": False,
            "传感器状态_上位机[0].NO[1]": True,
            "传感器状态_上位机[4].NO[5]": True,
            "S09原点信号_1": True,
        },
    )
    server.start()
    stop_event = threading.Event()
    simulator_error: list[BaseException] = []
    profile = load_simulator_profile(TIP_PROFILE)
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
                logger=logging.getLogger("tip-box-opc-simulator"),
            )
            if code:
                simulator_error.append(RuntimeError(f"TIP OPC simulator exit code={code}"))
        except BaseException as exc:  # surface simulator failures in the test thread
            simulator_error.append(exc)

    simulator_thread = threading.Thread(target=run, name="tip-box-opc-simulator", daemon=True)
    simulator_thread.start()
    plc = SZLabPolyPLCDevice(
        url=ENDPOINT,
        csv_path=False,
        node_id_map={
            name: f"ns=4;s=上位机通讯|{name}" for name in TIP_NODE_NAMES
        },
        opcua_object_name="上位机通讯",
        auto_connect=True,
        opcua_timeout=2.0,
    )
    robot = SzlabMixerRobotDevice(poll_interval=0.02)
    robot.set_plc_gateway(plc)
    try:
        yield server, robot, plc, simulator_error
    finally:
        stop_event.set()
        simulator_thread.join(timeout=5.0)
        try:
            plc.disconnect()
        finally:
            server.stop()
    assert not simulator_thread.is_alive()


def test_four_tip_box_actions_complete_over_real_opcua(live_tip_box_opcua):
    server, robot, _plc, simulator_error = live_tip_box_opcua

    results = [
        robot.submit_pick_from_s09(product_type=1, position=1),
        robot.submit_place_to_s02(position=1),
        robot.submit_pick_from_s02(position=2),
        robot.submit_place_to_s09(product_type=1, position=1),
    ]

    assert not simulator_error
    assert [result["success"] for result in results] == [True, True, True, True]
    assert [result["task_number"] for result in results] == [20, 3, 4, 19]
    assert server.read("传感器状态_上位机[4].NO[5]") is True
    assert server.read("传感器状态_上位机[0].NO[0]") is True
    assert server.read("传感器状态_上位机[0].NO[1]") is False
    # after_reset is an asynchronous simulator transition; allow one polling
    # interval for the PLC-side write-enable edge to be reflected.
    deadline = time.monotonic() + 1.0
    while not server.read("Robot_任务允许写入") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.read("Robot_任务允许写入") is True
