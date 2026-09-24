"""通过真实 TCP OPC UA 虚拟 PLC 跑完整 SZLab 主流程。

这个测试不伪造 PLC 读写：模拟器和所有工位设备共享同一个
``SZLabPolyPLCDevice`` 网关，调度器只在设备动作返回成功后推进 DAG。
"""

from __future__ import annotations

import json
import logging
import os
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
from unilabos.app.scheduler import EdgeScheduler, WorkflowState
from unilabos.app.scheduler.models import spec_from_dict
from unilabos.devices.workstation.szlab_poly_studio.plc import SZLabPolyPLCDevice
from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.magnetic_stirring import (
    SzlabMixerMagneticStirrerDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.photoshotting import (
    SzlabMixerPhotoShottingDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s06_pump.pump import SzlabMixerPumpDevice
from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07 import (
    SZLabS07SolidAdditionDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    SZLabS08CapStationDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.pipetting_station import (
    SzlabMixerPipettingStationDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import SzlabMixerRobotDevice


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / "szlab_robot_action_workflow.scheduler.json"
PROFILE_PATH = REPO_ROOT / "scripts/config/szlab_robot_action_workflow-opc-simulator.json"
PLC_CSV = REPO_ROOT / "unilabos/devices/workstation/szlab_poly_studio/szlab_plc_0721.csv"
ENDPOINT = "opc.tcp://127.0.0.1:50170/"


class LiveOpcDispatcher:
    def __init__(self, devices: dict[str, object]) -> None:
        self.devices = devices
        self.pending: list[dict] = []
        self.executed: list[dict] = []

    def dispatch(self, payload: dict) -> None:
        self.pending.append(dict(payload))

    def drain(self, scheduler: EdgeScheduler) -> None:
        while self.pending:
            payload = self.pending.pop(0)
            device = self.devices[payload["device_id"]]
            result = getattr(device, payload["action"])(**payload["params"])
            self.executed.append({**payload, "result": result})
            assert result.get("success") is True, (
                payload["node_id"],
                result,
            )
            scheduler.on_job_finished(payload["job_id"], success=True)
            # 给 PLC 模拟器一个扫描周期观察 PC->PLC 的写入完成复位，
            # 下一动作才能形成独立的 rising edge。
            time.sleep(0.1)


def _load_test_spec():
    raw = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    # 真实 PLC 联调不需要等待 30 秒，也不依赖外部溶解检测 HTTP 服务。
    for node in raw["nodes"]:
        if node["id"] == "w04_run_stirring_s04":
            node["param"]["duration"] = 0.1
        if node["id"] == "w06_take_photo_s05":
            node["param"]["trigger_dissolution_detection"] = False
    return spec_from_dict(raw)


@pytest.fixture
def live_szlab_opcua(tmp_path):
    logging.getLogger("opcua").setLevel(logging.WARNING)
    logging.getLogger("unilabos.utils.log.post_process").setLevel(logging.CRITICAL)
    logging.getLogger("unilabos.utils.log.plc").setLevel(logging.WARNING)
    profile = load_simulator_profile(PROFILE_PATH)
    server = CsvOpcUaServer(
        endpoint=ENDPOINT,
        csv_path=PLC_CSV,
        object_name="上位机通讯",
        namespace_uri="http://unilabos.com/opcua/szlab-full-workflow-e2e",
        server_name="UniLabOS SZLab full workflow virtual PLC",
        name_column="变量名",
        data_type_column="数据类型",
        initial_value_column="初始值",
        node_id_column="node_id",
        initial_values={},
    )
    server.start()
    stop_event = threading.Event()
    simulator_errors: list[BaseException] = []
    simulator_thread = threading.Thread(
        target=lambda: _run_simulator(profile, stop_event, simulator_errors),
        name="szlab-full-opc-simulator",
        daemon=True,
    )
    simulator_thread.start()
    time.sleep(0.15)

    old_skip = os.environ.get("SKIP_SENSOR_PRECHECK")
    os.environ["SKIP_SENSOR_PRECHECK"] = "1"
    plc = SZLabPolyPLCDevice(
        url=ENDPOINT,
        csv_path=str(PLC_CSV),
        auto_connect=True,
        opcua_timeout=2.0,
    )
    # 本测试使用真实变量 CSV；CSV 中没有完整的 PLC 报警表。避免每次轮询
    # 动作完成信号时重新浏览缺失报警节点，保持测试关注 OPC 握手本身。
    plc._mixing_wait_should_abort = lambda: False
    devices = {
        "szlab_mixer_robot": SzlabMixerRobotDevice(
            poll_interval=0.05,
            write_done_hold_seconds=0.0,
            enable_gripper_check=False,
        ),
        "szlab_s04_magnetic_stirring": SzlabMixerMagneticStirrerDevice(
            poll_interval=0.05,
            auto_connect=False,
            use_plc_gateway=True,
        ),
        "szlab_s05_photoshotting": SzlabMixerPhotoShottingDevice(
            auto_connect=False,
            use_plc_gateway=True,
        ),
        "szlab_s06_pump": SzlabMixerPumpDevice(
            use_plc_gateway=True,
        ),
        "szlab_s07_solid_addition": SZLabS07SolidAdditionDevice(
            poll_interval=0.05,
            balance_poll_interval=0.05,
            balance_record_interval=0.05,
            enable_balance_history=False,
            use_plc_gateway=True,
        ),
        "szlab_s08_cap_station": SZLabS08CapStationDevice(
            poll_interval=0.05,
            require_station_ready=False,
            require_station_status=False,
            validate_cap_constraints=False,
            use_plc_gateway=True,
        ),
        "szlab_mixer_pipetting_station": SzlabMixerPipettingStationDevice(
            auto_connect=False,
            use_plc_gateway=True,
            tip_reuse_state_path=str(tmp_path / "tip-state.json"),
        ),
    }
    for device in devices.values():
        device.set_plc_gateway(plc)
    tip_init = devices["szlab_mixer_pipetting_station"].initialize_reusable_tip_inventory(
        reset=True,
        used_tip_count=0,
    )
    assert tip_init.get("success") is True, tip_init
    liquid_init = devices["szlab_mixer_pipetting_station"].initialize_liquid_bottle_remaining_volumes(
        remaining_volume=100.0,
    )
    assert liquid_init.get("success") is True, liquid_init
    dispatcher = LiveOpcDispatcher(devices)
    try:
        yield plc, server, dispatcher, simulator_errors
    finally:
        if old_skip is None:
            os.environ.pop("SKIP_SENSOR_PRECHECK", None)
        else:
            os.environ["SKIP_SENSOR_PRECHECK"] = old_skip
        stop_event.set()
        simulator_thread.join(timeout=5.0)
        for device in devices.values():
            disconnect = getattr(device, "disconnect", None)
            if callable(disconnect):
                disconnect()
        plc.disconnect()
        server.stop()
    assert not simulator_thread.is_alive()


def _run_simulator(profile, stop_event, errors) -> None:
    try:
        code = run_simulator(
            SimulatorConfig(
                profile=profile,
                url=ENDPOINT,
                poll_interval=0.05,
                io_timeout=2.0,
                allow_unsafe_url=True,
            ),
            stop_requested=stop_event.is_set,
            interruptible_wait=stop_event.wait,
            logger=logging.getLogger("szlab-full-opc-simulator"),
        )
        if code:
            errors.append(RuntimeError(f"simulator exit code={code}"))
    except BaseException as exc:  # surface simulator failure in the test assertion
        errors.append(exc)


def test_full_szlab_scheduler_workflow_runs_through_virtual_plc(live_szlab_opcua):
    _plc, server, dispatcher, simulator_errors = live_szlab_opcua
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    spec = _load_test_spec()

    result = scheduler.submit_workflow(spec)
    dispatcher.drain(scheduler)

    assert result["workflow_id"] == spec.workflow_id
    assert scheduler.get_workflow_state(spec.workflow_id) is WorkflowState.SUCCESS
    assert len(dispatcher.executed) == len(spec.nodes) == 28
    assert not simulator_errors
    assert server.read("Robot_任务完成") == 0
    assert server.read("S041加工完成") is True
    assert server.read("S08工艺完成") == 0
    assert [item["node_id"] for item in dispatcher.executed] == [
        node.id for node in spec.nodes
    ]
