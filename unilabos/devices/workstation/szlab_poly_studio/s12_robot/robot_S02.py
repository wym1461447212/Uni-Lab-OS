from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, s02_sensor


# 传感器只表示有盒/无盒。1-3 约定为废盒区，4-6 约定为新盒区。
S02_TIP_POSITIONS = (1, 2, 3, 4, 5, 6)
S02_WASTE_POSITIONS = (1, 2, 3)
S02_FRESH_POSITIONS = (4, 5, 6)


class SzlabRobotS02Mixin:
    def scan_s02_tip_slots(self) -> dict[int, bool]:
        """读取 S02 六个 TIP 盒位。True 表示该位有盒。"""
        if self._plc_alarm_active():
            raise RuntimeError("PLC 报警已中止 S02 TIP 盒位扫描")
        occupied: dict[int, bool] = {}
        read_errors: list[str] = []
        for position in S02_TIP_POSITIONS:
            sensor = s02_sensor(position)
            try:
                occupied[position] = bool(self._read_variable(sensor, use_cache=False))
            except Exception as exc:
                read_errors.append(f"{position}: {exc}")
        if read_errors:
            raise RuntimeError(f"无法读取 S02 TIP 盒位: {'; '.join(read_errors)}")
        return occupied

    def choose_s02_place_position(
        self, occupied: dict[int, bool] | None = None
    ) -> int | None:
        """废盒区第一个空位，用于放下从 S09 取出的旧盒。"""
        slots = self.scan_s02_tip_slots() if occupied is None else occupied
        for position in S02_WASTE_POSITIONS:
            if not bool(slots.get(position)):
                return position
        return None

    def choose_s02_pick_position(
        self, occupied: dict[int, bool] | None = None
    ) -> int | None:
        """新盒区第一个有盒位，用于取回放到 S09 的新盒。"""
        slots = self.scan_s02_tip_slots() if occupied is None else occupied
        for position in S02_FRESH_POSITIONS:
            if bool(slots.get(position)):
                return position
        return None

    def _resolve_s02_position(self, position: int | str, *, mode: str) -> int:
        if str(position).strip().lower() == "auto":
            chosen = (
                self.choose_s02_place_position()
                if mode == "place"
                else self.choose_s02_pick_position()
            )
            if chosen is None:
                if mode == "place":
                    raise RuntimeError("S02 废盒区 1-3 没有空位")
                raise RuntimeError("S02 新盒区 4-6 没有可取的 TIP 盒")
            return chosen
        resolved = int(position)
        if resolved not in S02_TIP_POSITIONS:
            raise ValueError("S02 TIP 位置必须在 1-6 范围内")
        return resolved

    def _run_s02_place(self, position: int) -> dict[str, Any]:
        sensor = s02_sensor(position)
        return self._submit_robot_task(
            task="place",
            station="S02",
            task_number=3,
            variables=build_variables("place_to_s02", S02取放料编号=position),
            reset_variables={"S02取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S02 放料目标位必须为空"),
            position=int(position),
            target_sensor_variable=sensor,
        )

    def _run_s02_pick(self, position: int) -> dict[str, Any]:
        sensor = s02_sensor(position)
        return self._submit_robot_task(
            task="pick",
            station="S02",
            task_number=4,
            variables=build_variables("pick_from_s02", S02取放料编号=position),
            reset_variables={"S02取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S02 取料源位必须有 TIP"),
            position=int(position),
            source_sensor_variable=sensor,
        )
