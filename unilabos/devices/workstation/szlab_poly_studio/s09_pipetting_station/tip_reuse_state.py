from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


TIP_STATUS_UNUSED = "unused"
TIP_STATUS_BOUND = "bound"
TIP_STATUS_EXHAUSTED = "exhausted"
TIP_STATUS_UNKNOWN = "unknown"
TIP_STATUSES = {
    TIP_STATUS_UNUSED,
    TIP_STATUS_BOUND,
    TIP_STATUS_EXHAUSTED,
    TIP_STATUS_UNKNOWN,
}


def _rack_pair(source_box: int, waste_box: int) -> tuple[int, int]:
    """有 TIP 料架和废料架必须占满 S09 的 1、2 号位，且不能是同一个位。"""
    source_box = int(source_box)
    waste_box = int(waste_box)
    if sorted((source_box, waste_box)) != [1, 2]:
        raise ValueError("S09 有 TIP 料架和废料架必须分别占用 1 号和 2 号位")
    return source_box, waste_box


class ReusableTipStateStore:
    """持久化维护 S09 溶剂与可复用 TIP 的绑定状态。"""

    STATE_VERSION = 1

    def __init__(
        self,
        state_path: str | Path,
        *,
        tip_count: int = 24,
        max_use_count: int = 20,
    ) -> None:
        self.state_path = Path(state_path)
        self.tip_count = int(tip_count)
        self.max_use_count = int(max_use_count)
        if self.tip_count <= 0:
            raise ValueError("S09 TIP 数量必须大于 0")
        if self.max_use_count <= 0:
            raise ValueError("S09 TIP 最大使用次数必须大于 0")

        self._lock = threading.RLock()
        self._state = self._load_or_empty()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "initialized": False,
            "tip_count": self.tip_count,
            "max_use_count": self.max_use_count,
            "last_operation": None,
            "tip_source_box": 1,
            "tip_waste_box": 2,
            "solvents": {},
            "tips": {},
        }

    def _initialized_state(
        self,
        *,
        used_tip_count: int = 0,
        known_bindings: dict[str, int] | None = None,
        source_box: int = 1,
        waste_box: int = 2,
    ) -> dict[str, Any]:
        used_tip_count = int(used_tip_count)
        source_box, waste_box = _rack_pair(source_box, waste_box)
        if not 0 <= used_tip_count <= self.tip_count:
            raise ValueError(f"S09 已使用 TIP 数量必须在 0-{self.tip_count} 范围内")
        state = self._empty_state()
        state["initialized"] = True
        state["tip_source_box"] = source_box
        state["tip_waste_box"] = waste_box
        state["tips"] = {
            str(index): {
                "status": TIP_STATUS_UNUSED,
                "solvent_key": None,
                "current_box": source_box,
                "use_count": 0,
            }
            for index in range(1, self.tip_count + 1)
        }
        for index in range(1, used_tip_count + 1):
            state["tips"][str(index)].update(
                {"status": TIP_STATUS_EXHAUSTED, "current_box": waste_box}
            )
        for raw_key, raw_index in (known_bindings or {}).items():
            key = self._normalize_solvent_key(raw_key)
            index = int(raw_index)
            if not 1 <= index <= self.tip_count:
                raise ValueError(f"S09 已知绑定 TIP 编号必须在 1-{self.tip_count} 范围内")
            tip = state["tips"][str(index)]
            if tip["solvent_key"] is not None:
                raise ValueError(f"S09 TIP {index} 被重复绑定")
            tip.update(
                {
                    "status": TIP_STATUS_BOUND,
                    "solvent_key": key,
                    "current_box": waste_box,
                    "use_count": max(1, int(tip["use_count"])),
                }
            )
            state["solvents"][key] = {
                "active_tip_index": index,
                "tip_history": [index],
                "remaining_volume_ml": None,
                "active_s09_slot": None,
                "status": "ready",
            }
        return state

    def _load_or_empty(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        with self.state_path.open("r", encoding="utf-8") as file:
            state = json.load(file)
        self._validate_loaded_state(state)
        return state

    def _validate_loaded_state(self, state: dict[str, Any]) -> None:
        if state.get("version") != self.STATE_VERSION:
            raise ValueError(f"不支持的 S09 TIP 状态版本: {state.get('version')}")
        if int(state.get("tip_count", 0)) != self.tip_count:
            raise ValueError("S09 TIP 状态中的 TIP 数量与当前配置不一致")
        if int(state.get("max_use_count", 0)) != self.max_use_count:
            raise ValueError("S09 TIP 状态中的最大使用次数与当前配置不一致")
        if not isinstance(state.get("solvents"), dict) or not isinstance(state.get("tips"), dict):
            raise ValueError("S09 TIP 状态文件格式错误")
        for tip in state["tips"].values():
            if tip.get("status") not in TIP_STATUSES:
                raise ValueError(f"未知的 S09 TIP 状态: {tip.get('status')}")

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                json.dump(self._state, file, ensure_ascii=False, indent=2, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
                temporary_path = Path(file.name)
            os.replace(temporary_path, self.state_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def _require_initialized_locked(self) -> None:
        if not self._state["initialized"]:
            raise RuntimeError("S09 TIP 库存尚未初始化")

    @staticmethod
    def _normalize_solvent_key(solvent_key: str | int) -> str:
        key = str(solvent_key).strip()
        if not key:
            raise ValueError("S09 溶剂标识不能为空")
        return key

    def initialize(
        self,
        *,
        reset: bool = False,
        used_tip_count: int = 0,
        known_bindings: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """确认盒1满、盒2空后初始化软件库存。"""
        with self._lock:
            if self._state["initialized"] and not reset:
                raise RuntimeError("S09 TIP 库存已经初始化；如需重置必须显式传入 reset=True")
            self._state = self._initialized_state(
                used_tip_count=used_tip_count,
                known_bindings=known_bindings,
            )
            self._save_locked()
            return copy.deepcopy(self._state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def rack_positions(self) -> dict[str, int]:
        """当前有 TIP 料架和废料架的 S09 位号。初始为 1 号有 TIP、2 号废料。"""
        with self._lock:
            source_box, waste_box = _rack_pair(
                self._state.get("tip_source_box", 1),
                self._state.get("tip_waste_box", 2),
            )
            return {"source": source_box, "waste": waste_box}

    def finish_tip_box_change(self, *, full_box_position: int) -> dict[str, Any]:
        """按上料流程实际放下满盒的位号，登记有 TIP 料架，另一位作为废料架。

        full_box_position 来自换料流程里 submit_place_to_s09 的 position，
        不按 1/2 对调推断。
        """
        full_box = int(full_box_position)
        if full_box not in (1, 2):
            raise ValueError("满料架位必须是 1 或 2")
        waste_box = 2 if full_box == 1 else 1
        with self._lock:
            previous_source, previous_waste = _rack_pair(
                self._state.get("tip_source_box", 1),
                self._state.get("tip_waste_box", 2),
            )
            self._state = self._initialized_state(
                used_tip_count=0,
                source_box=full_box,
                waste_box=waste_box,
            )
            self._save_locked()
            return {
                "tip_source_box": full_box,
                "tip_waste_box": waste_box,
                "full_box_position": full_box,
                "previous_tip_source_box": previous_source,
                "previous_tip_waste_box": previous_waste,
                "tip_count": self.tip_count,
            }

    def _source_box_locked(self) -> int:
        return _rack_pair(
            self._state.get("tip_source_box", 1),
            self._state.get("tip_waste_box", 2),
        )[0]

    def _waste_box_locked(self) -> int:
        return _rack_pair(
            self._state.get("tip_source_box", 1),
            self._state.get("tip_waste_box", 2),
        )[1]

    def _unused_tip_count_locked(self) -> int:
        return sum(
            1
            for tip in self._state["tips"].values()
            if tip.get("status") == TIP_STATUS_UNUSED
        )

    def can_allocate_operations(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """只读判断这些加液还能不能分到 TIP，不写库存。"""
        with self._lock:
            unused_tip_count = self._unused_tip_count_locked()
            if not self._state["initialized"]:
                return {
                    "can_allocate": False,
                    "needs_box_change": False,
                    "reason": "not_initialized",
                    "unused_tip_count": unused_tip_count,
                }
            tips = copy.deepcopy(self._state["tips"])
            solvents = copy.deepcopy(self._state["solvents"])
            for operation in operations:
                key = self._normalize_solvent_key(operation["solvent_key"])
                cycles = int(operation["required_cycles"])
                reuse = bool(operation.get("reuse", True))
                if cycles <= 0:
                    return {
                        "can_allocate": False,
                        "needs_box_change": False,
                        "reason": "invalid_cycles",
                        "unused_tip_count": unused_tip_count,
                    }
                if cycles > self.max_use_count:
                    return {
                        "can_allocate": False,
                        "needs_box_change": False,
                        "reason": "cycles_exceed_limit",
                        "unused_tip_count": unused_tip_count,
                    }
                if reuse:
                    solvent = solvents.setdefault(
                        key,
                        {
                            "active_tip_index": None,
                            "tip_history": [],
                            "remaining_volume_ml": None,
                            "active_s09_slot": None,
                            "status": "ready",
                        },
                    )
                    if solvent["status"] == TIP_STATUS_UNKNOWN:
                        return {
                            "can_allocate": False,
                            "needs_box_change": False,
                            "reason": "unknown",
                            "unused_tip_count": unused_tip_count,
                        }
                    active_tip_index = solvent["active_tip_index"]
                    if active_tip_index is not None:
                        active_tip = tips[str(active_tip_index)]
                        if (
                            active_tip["status"] != TIP_STATUS_BOUND
                            or active_tip["solvent_key"] != key
                        ):
                            return {
                                "can_allocate": False,
                                "needs_box_change": False,
                                "reason": "binding_inconsistent",
                                "unused_tip_count": unused_tip_count,
                            }
                        if int(active_tip["use_count"]) + cycles <= self.max_use_count:
                            continue
                        active_tip["status"] = TIP_STATUS_EXHAUSTED
                        solvent["active_tip_index"] = None
                available_tip_index = next(
                    (
                        index
                        for index in range(1, self.tip_count + 1)
                        if tips[str(index)]["status"] == TIP_STATUS_UNUSED
                    ),
                    None,
                )
                if available_tip_index is None:
                    return {
                        "can_allocate": False,
                        "needs_box_change": True,
                        "reason": "box_empty",
                        "unused_tip_count": unused_tip_count,
                    }
                tip = tips[str(available_tip_index)]
                tip.update(
                    {
                        "status": TIP_STATUS_BOUND,
                        "solvent_key": key if reuse else None,
                        "current_box": self._source_box_locked(),
                        "use_count": 0,
                    }
                )
                if reuse:
                    solvent["active_tip_index"] = available_tip_index
                    solvent["status"] = "ready"
            return {
                "can_allocate": True,
                "needs_box_change": False,
                "reason": "ok",
                "unused_tip_count": unused_tip_count,
            }

    def record_last_operation(
        self,
        *,
        solvent_batch_id: str,
        liquid_station_index: int,
        liquid_tip_index: int,
        density_tip_index: int,
    ) -> dict[str, Any]:
        """记录最近一次实际启动的 S09 加液绑定，供下一位操作人员确认。"""
        with self._lock:
            self._require_initialized_locked()
            operation = {
                "solvent_batch_id": str(solvent_batch_id),
                "liquid_station_index": int(liquid_station_index),
                "liquid_tip_index": int(liquid_tip_index),
                "density_tip_index": int(density_tip_index),
            }
            self._state["last_operation"] = operation
            self._save_locked()
            return copy.deepcopy(operation)

    def get_solvent_binding(self, solvent_key: str | int) -> dict[str, Any] | None:
        key = self._normalize_solvent_key(solvent_key)
        with self._lock:
            solvent = self._state["solvents"].get(key)
            return copy.deepcopy(solvent) if solvent is not None else None

    def prepare_tip(
        self,
        solvent_key: str | int,
        *,
        required_cycles: int = 1,
        liquid_station_index: int | None = None,
    ) -> dict[str, Any]:
        """返回容量足够的当前 TIP；不足时废弃旧 TIP 并分配新 TIP。"""
        key = self._normalize_solvent_key(solvent_key)
        required_cycles = int(required_cycles)
        if required_cycles <= 0:
            raise ValueError("S09 TIP 预计使用次数必须大于 0")
        if required_cycles > self.max_use_count:
            raise ValueError(
                f"S09 单次操作需要使用 TIP {required_cycles} 次，超过上限 {self.max_use_count}"
            )
        with self._lock:
            self._require_initialized_locked()
            solvent = self._state["solvents"].setdefault(
                key,
                {
                    "active_tip_index": None,
                    "tip_history": [],
                    "remaining_volume_ml": None,
                    "active_s09_slot": None,
                    "status": "ready",
                },
            )
            if liquid_station_index is not None:
                solvent["active_s09_slot"] = int(liquid_station_index)
            if solvent["status"] == TIP_STATUS_UNKNOWN:
                raise RuntimeError(f"S09 溶剂 {key} 的 TIP 状态不确定，必须人工确认")

            active_tip_index = solvent["active_tip_index"]
            if active_tip_index is not None:
                active_tip = self._state["tips"][str(active_tip_index)]
                if (
                    active_tip["status"] != TIP_STATUS_BOUND
                    or active_tip["solvent_key"] != key
                ):
                    raise RuntimeError(f"S09 溶剂 {key} 的 TIP 绑定状态不一致")
                if int(active_tip["use_count"]) + required_cycles <= self.max_use_count:
                    return copy.deepcopy(active_tip | {"tip_index": active_tip_index})

                active_tip["status"] = TIP_STATUS_EXHAUSTED
                solvent["active_tip_index"] = None

            available_tip_index = next(
                (
                    index
                    for index in range(1, self.tip_count + 1)
                    if self._state["tips"][str(index)]["status"] == TIP_STATUS_UNUSED
                ),
                None,
            )
            if available_tip_index is None:
                self._save_locked()
                raise RuntimeError(
                    f"S09 {self._source_box_locked()} 号有 TIP 料架上没有可分配的新 TIP"
                )

            tip = self._state["tips"][str(available_tip_index)]
            tip.update(
                {
                    "status": TIP_STATUS_BOUND,
                    "solvent_key": key,
                    "current_box": self._source_box_locked(),
                    "use_count": 0,
                }
            )
            solvent["active_tip_index"] = available_tip_index
            solvent["tip_history"].append(available_tip_index)
            solvent["status"] = "ready"
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": available_tip_index})

    def record_tip_use(self, solvent_key: str | int, *, cycles: int = 1) -> dict[str, Any]:
        """在完整吸排液成功后记录 TIP 使用次数并将位置更新为盒2。"""
        key = self._normalize_solvent_key(solvent_key)
        cycles = int(cycles)
        if cycles <= 0:
            raise ValueError("S09 TIP 使用次数增量必须大于 0")

        with self._lock:
            self._require_initialized_locked()
            solvent = self._state["solvents"].get(key)
            if solvent is None or solvent["active_tip_index"] is None:
                raise RuntimeError(f"S09 溶剂 {key} 尚未绑定 TIP")
            tip_index = int(solvent["active_tip_index"])
            tip = self._state["tips"][str(tip_index)]
            if tip["status"] != TIP_STATUS_BOUND or tip["solvent_key"] != key:
                raise RuntimeError(f"S09 溶剂 {key} 的 TIP 绑定状态不一致")

            new_use_count = int(tip["use_count"]) + cycles
            if new_use_count > self.max_use_count:
                raise ValueError(
                    f"S09 TIP {tip_index} 使用次数将超过上限 {self.max_use_count}"
                )
            tip["use_count"] = new_use_count
            tip["current_box"] = self._waste_box_locked()
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})

    def replace_tip(
        self,
        solvent_key: str | int,
        *,
        liquid_station_index: int | None = None,
    ) -> dict[str, Any]:
        """手动报废当前绑定 TIP，并为同一溶剂批次绑定一支新 TIP。"""
        key = self._normalize_solvent_key(solvent_key)
        with self._lock:
            self._require_initialized_locked()
            solvent = self._state["solvents"].get(key)
            if solvent is None or solvent["active_tip_index"] is None:
                raise RuntimeError(f"S09 溶剂 {key} 尚未绑定 TIP")
            if solvent["status"] == TIP_STATUS_UNKNOWN:
                raise RuntimeError(f"S09 溶剂 {key} 的 TIP 状态不确定，必须人工确认")

            old_tip_index = int(solvent["active_tip_index"])
            old_tip = self._state["tips"].get(str(old_tip_index))
            if (
                old_tip is None
                or old_tip["status"] != TIP_STATUS_BOUND
                or old_tip["solvent_key"] != key
            ):
                raise RuntimeError(f"S09 溶剂 {key} 的 TIP 绑定状态不一致")

            new_tip_index = next(
                (
                    index
                    for index in range(1, self.tip_count + 1)
                    if self._state["tips"][str(index)]["status"] == TIP_STATUS_UNUSED
                ),
                None,
            )
            if new_tip_index is None:
                raise RuntimeError(
                    f"S09 {self._source_box_locked()} 号有 TIP 料架上没有可分配的新 TIP"
                )

            old_tip_snapshot = copy.deepcopy(old_tip | {"tip_index": old_tip_index})
            old_tip.update(
                {"status": TIP_STATUS_EXHAUSTED, "current_box": self._waste_box_locked()}
            )
            new_tip = self._state["tips"][str(new_tip_index)]
            new_tip.update(
                {
                    "status": TIP_STATUS_BOUND,
                    "solvent_key": key,
                    "current_box": self._source_box_locked(),
                    "use_count": 0,
                }
            )
            solvent["active_tip_index"] = new_tip_index
            solvent["tip_history"].append(new_tip_index)
            solvent["status"] = "ready"
            if liquid_station_index is not None:
                solvent["active_s09_slot"] = int(liquid_station_index)
            self._save_locked()
            return {
                "solvent_key": key,
                "old_tip": old_tip_snapshot,
                "new_tip": copy.deepcopy(new_tip | {"tip_index": new_tip_index}),
                "old_tip_index": old_tip_index,
                "new_tip_index": new_tip_index,
            }

    def prepare_single_use_tip(self) -> dict[str, Any]:
        """分配一支不与任何溶剂绑定的一次性 TIP。"""
        with self._lock:
            self._require_initialized_locked()
            tip_index = next(
                (
                    index
                    for index in range(1, self.tip_count + 1)
                    if self._state["tips"][str(index)]["status"] == TIP_STATUS_UNUSED
                ),
                None,
            )
            if tip_index is None:
                raise RuntimeError(
                    f"S09 {self._source_box_locked()} 号有 TIP 料架上没有可用的一次性新 TIP"
                )
            tip = self._state["tips"][str(tip_index)]
            tip.update(
                {
                    "status": TIP_STATUS_BOUND,
                    "solvent_key": None,
                    "current_box": self._source_box_locked(),
                    "use_count": 0,
                }
            )
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})

    def consume_single_use_tip(self, tip_index: int) -> dict[str, Any]:
        """使用完成后将一次性 TIP 标记为已耗尽并记录在盒2。"""
        tip_index = int(tip_index)
        with self._lock:
            self._require_initialized_locked()
            tip = self._state["tips"].get(str(tip_index))
            if tip is None or tip["status"] != TIP_STATUS_BOUND or tip["solvent_key"] is not None:
                raise RuntimeError(f"S09 一次性 TIP {tip_index} 状态不一致")
            tip.update(
                {
                    "status": TIP_STATUS_EXHAUSTED,
                    "current_box": self._waste_box_locked(),
                    "use_count": 1,
                }
            )
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})

    def release_single_use_tip_reservation(self, tip_index: int) -> dict[str, Any]:
        """一次性 TIP 尚未实际取出时，释放软件预留并恢复为未使用。"""
        tip_index = int(tip_index)
        with self._lock:
            self._require_initialized_locked()
            tip = self._state["tips"].get(str(tip_index))
            if tip is None or tip["status"] != TIP_STATUS_BOUND or tip["solvent_key"] is not None:
                raise RuntimeError(f"S09 一次性 TIP {tip_index} 预留状态不一致")
            tip.update(
                {
                    "status": TIP_STATUS_UNUSED,
                    "solvent_key": None,
                    "current_box": self._source_box_locked(),
                    "use_count": 0,
                }
            )
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})

    def mark_single_use_tip_unknown(self, tip_index: int) -> dict[str, Any]:
        """一次性 TIP 取放结果不确定时将其隔离。"""
        tip_index = int(tip_index)
        with self._lock:
            self._require_initialized_locked()
            tip = self._state["tips"].get(str(tip_index))
            if tip is None:
                raise RuntimeError(f"S09 TIP {tip_index} 不存在")
            tip["status"] = TIP_STATUS_UNKNOWN
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})

    def mark_active_tip_unknown(self, solvent_key: str | int) -> dict[str, Any]:
        """取放结果不确定时隔离当前 TIP，禁止后续自动复用。"""
        key = self._normalize_solvent_key(solvent_key)
        with self._lock:
            self._require_initialized_locked()
            solvent = self._state["solvents"].get(key)
            if solvent is None or solvent["active_tip_index"] is None:
                raise RuntimeError(f"S09 溶剂 {key} 尚未绑定 TIP")
            tip_index = int(solvent["active_tip_index"])
            tip = self._state["tips"][str(tip_index)]
            tip["status"] = TIP_STATUS_UNKNOWN
            solvent["status"] = TIP_STATUS_UNKNOWN
            self._save_locked()
            return copy.deepcopy(tip | {"tip_index": tip_index})
