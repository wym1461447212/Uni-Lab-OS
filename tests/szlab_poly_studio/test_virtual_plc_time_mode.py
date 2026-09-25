"""虚拟 PLC 的真实/调试时间模式。调试模式缩短计时，完成沿顺序保持不变。"""

from __future__ import annotations

import pytest

from scripts.szlab_virtual_plc_controller import (
    TIME_MODE_FAST,
    TIME_MODE_REAL,
    process_completion_delay,
    resolve_time_mode,
    step_new_cycle_done,
)


def test_resolve_time_mode_defaults_to_fast():
    assert resolve_time_mode(None) == TIME_MODE_FAST
    assert resolve_time_mode(" REAL ") == TIME_MODE_REAL


def test_resolve_time_mode_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_time_mode("turbo")


def test_real_mode_uses_recipe_milliseconds():
    assert process_completion_delay(TIME_MODE_REAL, 5000) == 5.0
    assert process_completion_delay(TIME_MODE_REAL, 0) == 0.2


def test_fast_mode_ignores_recipe_duration():
    assert process_completion_delay(TIME_MODE_FAST, 30000, fast_seconds=0.2) == 0.2


def test_fast_cycle_keeps_falling_edge_then_rises_quickly():
    write_done, state, due = step_new_cycle_done(
        written=True,
        done=True,
        state="idle",
        due_at=None,
        now=10.0,
        delay=0.2,
    )
    assert write_done is False
    assert state == "armed"
    assert due == 10.2

    write_done, state, due = step_new_cycle_done(
        written=True,
        done=False,
        state="armed",
        due_at=due,
        now=10.1,
        delay=0.0,
    )
    assert write_done is None
    assert state == "armed"

    write_done, state, due = step_new_cycle_done(
        written=True,
        done=False,
        state="armed",
        due_at=due,
        now=10.2,
        delay=0.0,
    )
    assert write_done is True
    assert state == "pulsed"


def test_real_cycle_does_not_finish_before_recipe_time():
    _, state, due = step_new_cycle_done(
        written=True,
        done=False,
        state="idle",
        due_at=None,
        now=0.0,
        delay=5.0,
    )
    write_done, state, due = step_new_cycle_done(
        written=True,
        done=False,
        state=state,
        due_at=due,
        now=4.9,
        delay=0.0,
    )
    assert write_done is None
    assert state == "armed"

    write_done, state, _due = step_new_cycle_done(
        written=True,
        done=False,
        state=state,
        due_at=due,
        now=5.0,
        delay=0.0,
    )
    assert write_done is True
    assert state == "pulsed"


def test_clearing_write_resets_done_for_the_next_cycle():
    write_done, state, due = step_new_cycle_done(
        written=False,
        done=True,
        state="pulsed",
        due_at=3.0,
        now=3.1,
        delay=0.0,
    )
    assert write_done is False
    assert state == "idle"
    assert due is None
