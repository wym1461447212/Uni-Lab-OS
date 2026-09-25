#!/usr/bin/env python3
"""虚拟 PLC 的控制进程。

通过 OPC UA 客户端读写变量，不持有服务器对象。机器人任务完成前，
先按设备驱动的后置传感器和夹爪条件改写在位，再回写 Robot_任务完成。
工位加工完成也按各驱动的握手（新周期下降沿 + 上升沿，或工艺号回显）回写。

时间模式：
- real：磁搅等写了加工时间的工位，按 PLC 中的时长保持完成位为假，到点再拉高。
- fast：调试默认。同样先拉低再拉高，但计时等待压到很短，只保留信号变化。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from typing import Any, Callable

from opcua import Client, ua

from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    CAP_STORAGE_SLOT_SENSORS,
    OPEN_PROCESS_IDS,
    S08ProcessType,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot import (
    GRIPPER_ORIGIN_VARIABLE,
    GRIPPER_POSITION_VARIABLES,
    GRIPPER_STATUS_VARIABLE,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    ROBOT_ACTION_SPECS,
    ROBOT_HOME_VARIABLE,
    ROBOT_TASK_COMPLETE_VARIABLE,
    ROBOT_TASK_NUMBER_VARIABLE,
    ROBOT_WRITE_ALLOWED_VARIABLE,
    ROBOT_WRITE_DONE_VARIABLE,
    RobotActionSpec,
    powder_container_sensor,
    product_slot_sensor,
    s02_sensor,
    s04_sensor,
    s09_sensor,
    s10_sensor,
)
from unilabos.devices.workstation.szlab_poly_studio.sensor import S05Sensors, S06Sensors, S08Sensors


LOGGER = logging.getLogger("szlab-virtual-plc-controller")
TIME_MODE_FAST = "fast"
TIME_MODE_REAL = "real"
TIME_MODES = (TIME_MODE_FAST, TIME_MODE_REAL)
# 调试模式仍留出下降沿到上升沿的间隔，避免完成位在同一拍里翻转后被驱动漏看。
FAST_COMPLETION_SECONDS = 0.2
# 驱动写入的是毫秒，见 s04 磁搅 duration_ms。
_S04_DURATION_MS_BY_DONE = {
    "S041加工完成": "磁搅时间设置_上位机[0]",
    "S042加工完成": "磁搅时间设置_上位机[1]",
    "S043加工完成": "磁搅时间设置_上位机[2]",
    "S044加工完成": "磁搅时间设置_上位机[3]",
}


def _arm_socket_timeout(client: Client, timeout: float) -> None:
    try:
        sock = client.uaclient._uasocket._socket.socket
        sock.settimeout(timeout)
    except Exception:
        LOGGER.warning("设置虚拟 PLC 套接字读超时失败", exc_info=True)


def _connection_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    markers = (
        "Timeout",
        "timed out",
        "BadSession",
        "BadSecureChannel",
        "BadConnection",
        "BadCommunication",
        "Connection reset",
        "Connection aborted",
        "Broken pipe",
        "EOFError",
        "Socket is closed",
        "Bad file descriptor",
    )
    return any(marker in text for marker in markers)
DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840/"
_NODE_PREFIX = "ns=4;s=上位机通讯|"
_SPEC_BY_NUMBER = {spec.task_number: spec for spec in ROBOT_ACTION_SPECS.values()}
_GRIPPER_POSITIONS = tuple(GRIPPER_POSITION_VARIABLES.values()) + (GRIPPER_ORIGIN_VARIABLE,)
_INT_ECHOES = (
    ("S07参数写入完成", "S07工艺选择", "S07工艺完成"),
    ("S08参数写入完成", "S08工艺选择", "S08工艺完成"),
    ("S09参数写入完成", "S09工艺选择", "S09工艺完成"),
)
_BOOL_CYCLES = (
    ("S041参数写入完成", "S041加工完成"),
    ("S042参数写入完成", "S042加工完成"),
    ("S043参数写入完成", "S043加工完成"),
    ("S044参数写入完成", "S044加工完成"),
    ("S06参数写入完成", "S06加工完成"),
)
_FIXED_GRIPPER = {
    "S02": GRIPPER_POSITION_VARIABLES["tip_box"],
    "S04": GRIPPER_POSITION_VARIABLES["beaker"],
    "S05": GRIPPER_POSITION_VARIABLES["beaker"],
    "S06": GRIPPER_POSITION_VARIABLES["beaker"],
    "S071": GRIPPER_POSITION_VARIABLES["solid_powder"],
    "S10": GRIPPER_POSITION_VARIABLES["liquid_reagent_100ml"],
}
_PRODUCT_GRIPPER = {
    "S01": {
        1: GRIPPER_POSITION_VARIABLES["tip_box"],
        2: GRIPPER_POSITION_VARIABLES["beaker"],
        3: GRIPPER_POSITION_VARIABLES["sample_vial_250ml"],
        4: GRIPPER_POSITION_VARIABLES["sample_vial_500ml"],
        5: GRIPPER_POSITION_VARIABLES["liquid_reagent_100ml"],
        6: GRIPPER_POSITION_VARIABLES["solid_powder"],
    },
    "S03": {
        1: GRIPPER_POSITION_VARIABLES["beaker"],
        2: GRIPPER_POSITION_VARIABLES["sample_vial_250ml"],
        3: GRIPPER_POSITION_VARIABLES["sample_vial_500ml"],
    },
    "S11": {
        1: GRIPPER_POSITION_VARIABLES["beaker"],
        2: GRIPPER_POSITION_VARIABLES["sample_vial_250ml"],
        3: GRIPPER_POSITION_VARIABLES["sample_vial_500ml"],
    },
    "S072": {
        1: GRIPPER_POSITION_VARIABLES["solid_powder"],
        2: GRIPPER_POSITION_VARIABLES["beaker"],
    },
    "S08": {
        1: GRIPPER_POSITION_VARIABLES["sample_vial_250ml"],
        2: GRIPPER_POSITION_VARIABLES["sample_vial_500ml"],
        3: GRIPPER_POSITION_VARIABLES["liquid_reagent_100ml"],
    },
    "S09": {
        1: GRIPPER_POSITION_VARIABLES["tip_box"],
        2: GRIPPER_POSITION_VARIABLES["liquid_reagent_100ml"],
        3: GRIPPER_POSITION_VARIABLES["beaker"],
        4: GRIPPER_POSITION_VARIABLES["beaker"],
    },
}


def resolve_time_mode(value: str | None) -> str:
    mode = (value or TIME_MODE_FAST).strip().lower()
    if mode not in TIME_MODES:
        allowed = "、".join(TIME_MODES)
        raise ValueError(f"虚拟时间模式必须是 {allowed}，收到 {value!r}")
    return mode


def process_completion_delay(
    time_mode: str,
    duration_ms: int | None,
    *,
    fast_seconds: float = FAST_COMPLETION_SECONDS,
) -> float:
    """真实模式使用配方时长；调试模式忽略配方，只保留一段很短的完成沿间隔。"""
    mode = resolve_time_mode(time_mode)
    dwell = max(0.0, float(fast_seconds))
    if mode == TIME_MODE_REAL:
        millis = int(duration_ms or 0)
        if millis > 0:
            return millis / 1000.0
    return dwell


def step_new_cycle_done(
    *,
    written: bool,
    done: bool,
    state: str,
    due_at: float | None,
    now: float,
    delay: float,
) -> tuple[bool | None, str, float | None]:
    """推进新周期完成握手。返回 (要写入的完成位, 新状态, 到期时间)。

    写入期间先保持完成位为假，到期后再拉高。未写入时把完成位拉回假，供下一轮使用。
    """
    if written:
        if state == "idle":
            deadline = now + max(0.0, delay)
            if done:
                return False, "armed", deadline
            return None, "armed", deadline
        if state == "armed":
            deadline = now if due_at is None else due_at
            if now < deadline:
                if done:
                    return False, "armed", deadline
                return None, "armed", deadline
            return True, "pulsed", deadline
        return None, state, due_at
    if done:
        return False, "idle", None
    return None, "idle", None


class VirtualPlcController:
    def __init__(
        self,
        endpoint: str,
        poll_interval: float = 0.4,
        time_mode: str = TIME_MODE_FAST,
        fast_completion_seconds: float = FAST_COMPLETION_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self.poll_interval = poll_interval
        self.time_mode = resolve_time_mode(time_mode)
        self.fast_completion_seconds = max(0.0, float(fast_completion_seconds))
        self._client: Client | None = None
        self._nodes: dict[str, Any] = {}
        self._echo_ready_at: dict[str, float] = {}
        self._bool_cycle: dict[str, str] = {}
        self._cycle_due_at: dict[str, float] = {}
        self._stop = False

    def serve(self) -> None:
        # python-opcua 连接后会把套接字超时清掉。响应丢失时接收线程永久阻塞，
        # 节拍不再完成机器人/工位握手。读超时后丢弃会话并重连。
        client: Client | None = None
        try:
            while not self._stop:
                if client is None:
                    try:
                        client = self._connect_client()
                    except Exception:
                        LOGGER.exception("虚拟 PLC 控制进程连接失败")
                        time.sleep(1.0)
                        continue
                try:
                    self._tick()
                except Exception:
                    LOGGER.exception("虚拟 PLC 控制节拍失败，准备重连")
                    self._drop_client(client)
                    client = None
                    time.sleep(0.5)
                    continue
                time.sleep(self.poll_interval)
        finally:
            if client is not None:
                self._drop_client(client)
            LOGGER.info("控制进程已断开")

    def _connect_client(self) -> Client:
        client = Client(self.endpoint, timeout=8)
        client.connect()
        _arm_socket_timeout(client, 8)
        self._client = client
        self._nodes.clear()
        self._echo_ready_at.clear()
        self._bool_cycle.clear()
        self._cycle_due_at.clear()
        if self.time_mode == TIME_MODE_REAL:
            LOGGER.info("控制进程已连接虚拟 PLC: %s，时间模式=真实", self.endpoint)
        else:
            LOGGER.info(
                "控制进程已连接虚拟 PLC: %s，时间模式=调试，计时等待 %.2f 秒内完成",
                self.endpoint,
                self.fast_completion_seconds,
            )
        return client

    def _drop_client(self, client: Client) -> None:
        self._client = None
        self._nodes.clear()
        try:
            client.disconnect()
        except Exception:
            LOGGER.debug("断开虚拟 PLC 控制连接失败", exc_info=True)

    def stop(self) -> None:
        self._stop = True

    def _tick(self) -> None:
        # 不在每个节拍强制改写 Robot_Home / 允许写入。驱动会在下发任务前自己清这些位，
        # 每 0.1 秒回写会把前置握手冲掉，并淹没单线程 OPC 服务。
        self._complete_robot_task()
        for written_name, select_name, done_name in _INT_ECHOES:
            self._echo_process(written_name, select_name, done_name)
        for written_name, done_name in _BOOL_CYCLES:
            self._pulse_new_cycle_done(written_name, done_name)

    def _complete_robot_task(self) -> None:
        write_done = self._read_bool(ROBOT_WRITE_DONE_VARIABLE)
        task_number = self._read_int(ROBOT_TASK_NUMBER_VARIABLE)
        completed = self._read_int(ROBOT_TASK_COMPLETE_VARIABLE)
        if write_done and task_number and completed != task_number:
            # 产品参数此时仍非 0，必须先改传感器/夹爪再写完成号。
            self._apply_robot_effects(task_number)
            self._write(ROBOT_TASK_COMPLETE_VARIABLE, task_number)
            LOGGER.info("机器人任务 %s 已完成", task_number)
            return
        if not write_done and completed:
            self._write(ROBOT_TASK_COMPLETE_VARIABLE, 0)

    def _apply_robot_effects(self, task_number: int) -> None:
        spec = _SPEC_BY_NUMBER.get(task_number)
        if spec is None:
            LOGGER.warning("未登记的机器人任务号: %s", task_number)
            return
        sensor = sensor_for_spec(spec, self._read_int)
        if sensor is not None:
            self._write(sensor, spec.task == "place")
        if spec.station == "S05" and spec.task == "place":
            self._write("S05加工完成", True)
            self._write("S05拍照结果", 1)
        elif spec.station == "S05" and spec.task == "pick":
            self._write("S05加工完成", False)
            self._write("S05拍照结果", 0)
        for name, value in gripper_values(spec, self._read_int).items():
            self._write(name, value)

    def _echo_process(self, written_name: str, select_name: str, done_name: str) -> None:
        written = self._read_bool(written_name)
        process = self._read_int(select_name)
        done = self._read_int(done_name)
        if written and process and done != process:
            ready_at = self._echo_ready_at.get(done_name)
            if ready_at is None:
                self._echo_ready_at[done_name] = time.monotonic() + 0.05
                return
            if time.monotonic() < ready_at:
                return
            process = self._read_int(select_name)
            if select_name == "S09工艺选择" and process in {1, 2, 3, 4}:
                self._write(f"S09原点信号_{process}", True)
            if select_name == "S08工艺选择" and process:
                self._apply_s08_process_sensors(process)
            if process:
                self._write(done_name, process)
            self._echo_ready_at.pop(done_name, None)
            return
        self._echo_ready_at.pop(done_name, None)
        if not written and done:
            self._write(done_name, 0)

    def _apply_s08_process_sensors(self, process: int) -> None:
        try:
            process_type = S08ProcessType(int(process))
        except ValueError:
            return
        slot = self._read_int("S082瓶盖暂存位")
        sensor = CAP_STORAGE_SLOT_SENSORS.get(slot)
        if sensor is None:
            return
        self._write(sensor, process_type in OPEN_PROCESS_IDS)

    def _process_delay(self, done_name: str) -> float:
        duration_ms = 0
        if self.time_mode == TIME_MODE_REAL:
            variable = _S04_DURATION_MS_BY_DONE.get(done_name)
            if variable:
                duration_ms = self._read_int(variable)
        return process_completion_delay(
            self.time_mode,
            duration_ms,
            fast_seconds=self.fast_completion_seconds,
        )

    def _pulse_new_cycle_done(self, written_name: str, done_name: str) -> None:
        """匹配 wait_new_cycle_done：完成位先为假，到期后再拉高。"""
        written = self._read_bool(written_name)
        done = self._read_bool(done_name)
        state = self._bool_cycle.get(done_name, "idle")
        due_at = self._cycle_due_at.get(done_name)
        delay = self._process_delay(done_name) if written and state == "idle" else 0.0
        write_done, new_state, new_due = step_new_cycle_done(
            written=written,
            done=done,
            state=state,
            due_at=due_at,
            now=time.monotonic(),
            delay=delay,
        )
        if write_done is not None and write_done != done:
            self._write(done_name, write_done)
        self._bool_cycle[done_name] = new_state
        if new_due is None:
            self._cycle_due_at.pop(done_name, None)
        else:
            self._cycle_due_at[done_name] = new_due
        if (
            written
            and state == "idle"
            and self.time_mode == TIME_MODE_REAL
            and delay > self.fast_completion_seconds
        ):
            LOGGER.info("%s 按真实时间等待 %.2f 秒后置完成", done_name, delay)

    def _node(self, name: str) -> Any:
        cached = self._nodes.get(name)
        if cached is None:
            if self._client is None:
                raise RuntimeError("控制进程尚未连接 OPC UA")
            cached = self._client.get_node(f"{_NODE_PREFIX}{name}")
            self._nodes[name] = cached
        return cached

    def _write(self, name: str, value: Any) -> None:
        try:
            node = self._node(name)
            variant_type = node.get_data_type_as_variant_type()
        except Exception as exc:
            if _connection_error(exc):
                raise
            LOGGER.warning("虚拟 PLC 没有变量 %s，跳过写入", name)
            return
        if variant_type == ua.VariantType.Boolean:
            typed: Any = bool(value)
        elif variant_type in {ua.VariantType.Int16, ua.VariantType.Int32, ua.VariantType.UInt16}:
            typed = int(value)
        elif variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
            typed = float(value)
        else:
            typed = value
        try:
            node.set_value(ua.Variant(typed, variant_type))
        except Exception as exc:
            if _connection_error(exc):
                raise
            LOGGER.warning("虚拟 PLC 写入 %s 失败: %s", name, exc)

    def _read_bool(self, name: str) -> bool:
        try:
            return bool(self._node(name).get_value())
        except Exception as exc:
            if _connection_error(exc):
                raise
            return False

    def _read_int(self, name: str) -> int:
        try:
            return int(self._node(name).get_value() or 0)
        except Exception as exc:
            if _connection_error(exc):
                raise
            return 0


def sensor_for_spec(spec: RobotActionSpec, read_int: Callable[[str], int]) -> str | None:
    """复用设备驱动选传感器的规则。放料后该位为真，取料后为假。"""
    if spec.station in {"S01", "S072"} or spec.task == "pour":
        return None
    try:
        if spec.station == "S02":
            position = read_int("S02取放料编号")
            return s02_sensor(position) if position else None
        if spec.station == "S03":
            product_type = read_int("S03取放料产品")
            slot = read_int("S03取放料编号")
            if not product_type or not slot:
                return None
            return product_slot_sensor(product_type, slot_key(slot), used=False)
        if spec.station == "S04":
            position = read_int("S04取放料编号")
            return s04_sensor(position) if position else None
        if spec.station == "S05":
            return S05Sensors.MATERIAL
        if spec.station == "S06":
            return S06Sensors.MATERIAL
        if spec.station == "S071":
            slot = read_int("S071取放料编号")
            return powder_container_sensor(slot_key(slot)) if slot else None
        if spec.station == "S08":
            position = read_int("S08取放料编号")
            return S08Sensors.CAP_STATION.get(position)
        if spec.station == "S09":
            product_type = read_int("S09取放料产品")
            if product_type in {3, 4}:
                return None
            position = read_int("S09取放料编号")
            return s09_sensor(product_type, position) if product_type and position else None
        if spec.station == "S10":
            position = read_int("S10取放料编号")
            return s10_sensor(position) if position else None
        if spec.station == "S11":
            product_type = read_int("S11取放料产品")
            slot = read_int("S11取放料编号")
            if not product_type or not slot:
                return None
            return product_slot_sensor(product_type, slot_key(slot), used=True)
    except (KeyError, TypeError, ValueError) as exc:
        LOGGER.warning("任务 %s 无法解析传感器: %s", spec.task_number, exc)
        return None
    return None


def gripper_values(spec: RobotActionSpec, read_int: Callable[[str], int]) -> dict[str, Any]:
    """与机器人驱动的夹爪前后检查一致：取料后夹持，放料后空爪回原点，倒料保持夹烧杯。"""
    if spec.task == "pour":
        holding = GRIPPER_POSITION_VARIABLES["beaker"]
    elif spec.task == "place":
        holding = None
    else:
        holding = _holding_gripper(spec, read_int)
    values = {name: name == holding for name in _GRIPPER_POSITIONS}
    if holding is None:
        values[GRIPPER_ORIGIN_VARIABLE] = True
    values[GRIPPER_STATUS_VARIABLE] = 1
    return values


def _holding_gripper(spec: RobotActionSpec, read_int: Callable[[str], int]) -> str:
    fixed = _FIXED_GRIPPER.get(spec.station)
    if fixed is not None:
        return fixed
    product_type = 0
    for name in spec.variables:
        if "产品" in name:
            product_type = read_int(name)
            break
    mapping = _PRODUCT_GRIPPER.get(spec.station, {})
    position = mapping.get(product_type)
    if position is None:
        raise ValueError(f"{spec.station} 任务 {spec.task_number} 无法根据产品 {product_type} 确定夹爪位")
    return position


def slot_key(slot_number: int) -> str:
    slot_number = int(slot_number)
    row = (slot_number - 1) // 6 + 1
    column = (slot_number - 1) % 6 + 1
    return f"{row}-{column}"


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SZLab 虚拟 PLC 控制进程")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument(
        "--time-mode",
        choices=TIME_MODES,
        default=os.environ.get("UNILABOS_VIRTUAL_TIME_MODE", TIME_MODE_FAST),
        help="real=按磁搅等配方时间等待；fast=调试，短时间内完成并保留完成沿",
    )
    parser.add_argument(
        "--fast-completion-seconds",
        type=float,
        default=FAST_COMPLETION_SECONDS,
        help="调试模式下，完成位拉低到拉高之间的间隔（秒）",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("opcua").setLevel(logging.WARNING)
    controller = VirtualPlcController(
        args.endpoint,
        time_mode=args.time_mode,
        fast_completion_seconds=args.fast_completion_seconds,
    )

    def _stop(_signum: int, _frame: Any) -> None:
        controller.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    controller.serve()


if __name__ == "__main__":
    main()
