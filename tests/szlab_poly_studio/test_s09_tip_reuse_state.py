from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.tip_reuse_state import (
    TIP_STATUS_BOUND,
    TIP_STATUS_EXHAUSTED,
    TIP_STATUS_UNKNOWN,
    TIP_STATUS_UNUSED,
    ReusableTipStateStore,
)


def test_can_allocate_is_read_only_and_requests_box_change_when_box_is_empty(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    operation = {
        "solvent_key": "S09-STATION-1:BATCH:solvent-batch-001",
        "required_cycles": 1,
        "reuse": True,
    }

    uninitialized = store.can_allocate_operations([operation])
    assert uninitialized["can_allocate"] is False
    assert uninitialized["needs_box_change"] is False
    assert uninitialized["reason"] == "not_initialized"

    store.initialize(used_tip_count=24)
    before = store.snapshot()
    empty = store.can_allocate_operations([operation])

    assert empty == {
        "can_allocate": False,
        "needs_box_change": True,
        "reason": "box_empty",
        "unused_tip_count": 0,
    }
    assert store.snapshot() == before


def test_can_allocate_reuses_bound_tip_without_taking_a_new_one(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json", tip_count=1)
    store.initialize()
    store.prepare_tip("S09-STATION-1:BATCH:solvent-batch-001")

    result = store.can_allocate_operations(
        [
            {
                "solvent_key": "S09-STATION-1:BATCH:solvent-batch-001",
                "required_cycles": 1,
                "reuse": True,
            }
        ]
    )

    assert result["can_allocate"] is True
    assert result["needs_box_change"] is False
    assert result["unused_tip_count"] == 0


def test_finish_tip_box_change_uses_loading_workflow_full_rack(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json", tip_count=2)
    store.initialize()

    placed_at_two = store.finish_tip_box_change(full_box_position=2)

    assert placed_at_two["previous_tip_source_box"] == 1
    assert placed_at_two["tip_source_box"] == 2
    assert placed_at_two["tip_waste_box"] == 1
    assert store.snapshot()["tips"]["1"]["current_box"] == 2

    placed_at_one = store.finish_tip_box_change(full_box_position=1)
    assert placed_at_one["tip_source_box"] == 1
    assert placed_at_one["tip_waste_box"] == 2
    assert store.rack_positions() == {"source": 1, "waste": 2}


def test_tip_inventory_requires_explicit_initialization(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")

    with pytest.raises(RuntimeError, match="尚未初始化"):
        store.prepare_tip("S10-1")

    state = store.initialize()

    assert state["initialized"] is True
    assert len(state["tips"]) == 24
    assert state["tips"]["1"] == {
        "status": TIP_STATUS_UNUSED,
        "solvent_key": None,
        "current_box": 1,
        "use_count": 0,
    }


def test_same_solvent_reuses_bound_tip_and_moves_it_to_box_two(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    store.initialize()

    first = store.prepare_tip("S10-7")
    used = store.record_tip_use("S10-7")
    reused = store.prepare_tip("S10-7")

    assert first["tip_index"] == 1
    assert first["current_box"] == 1
    assert used["current_box"] == 2
    assert used["use_count"] == 1
    assert reused == used
    assert store.get_solvent_binding("S10-7")["active_tip_index"] == 1


def test_initialization_can_isolate_used_tips_and_restore_known_bindings(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")

    state = store.initialize(
        used_tip_count=3,
        known_bindings={"batch-a": 2},
    )
    allocated = store.prepare_tip(
        "batch-new",
        liquid_station_index=1,
    )

    assert state["tips"]["1"]["status"] == TIP_STATUS_EXHAUSTED
    assert state["tips"]["2"]["status"] == TIP_STATUS_BOUND
    assert state["solvents"]["batch-a"]["active_tip_index"] == 2
    assert allocated["tip_index"] == 4
    assert store.get_solvent_binding("batch-new")["active_s09_slot"] == 1


def test_new_solvent_batch_on_same_liquid_station_gets_next_tip(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    store.initialize()

    first_batch = store.prepare_tip("batch-a", liquid_station_index=1)
    store.record_tip_use("batch-a")
    second_batch = store.prepare_tip("batch-b", liquid_station_index=1)

    assert first_batch["tip_index"] == 1
    assert second_batch["tip_index"] == 2
    assert store.get_solvent_binding("batch-a")["active_s09_slot"] == 1
    assert store.get_solvent_binding("batch-b")["active_s09_slot"] == 1


def test_tip_at_use_limit_is_retired_and_replaced(tmp_path):
    store = ReusableTipStateStore(
        tmp_path / "tip_state.json",
        max_use_count=2,
    )
    store.initialize()
    store.prepare_tip("S10-1")
    store.record_tip_use("S10-1", cycles=2)

    replacement = store.prepare_tip("S10-1")
    state = store.snapshot()

    assert replacement["tip_index"] == 2
    assert replacement["current_box"] == 1
    assert state["tips"]["1"]["status"] == TIP_STATUS_EXHAUSTED
    assert state["tips"]["1"]["current_box"] == 2
    assert state["tips"]["2"]["status"] == TIP_STATUS_BOUND
    assert state["solvents"]["S10-1"]["tip_history"] == [1, 2]


def test_tip_is_replaced_early_when_operation_needs_more_remaining_cycles(tmp_path):
    store = ReusableTipStateStore(
        tmp_path / "tip_state.json",
        max_use_count=3,
    )
    store.initialize()
    store.prepare_tip("S10-1")
    store.record_tip_use("S10-1", cycles=2)

    replacement = store.prepare_tip("S10-1", required_cycles=2)
    state = store.snapshot()

    assert replacement["tip_index"] == 2
    assert state["tips"]["1"]["status"] == TIP_STATUS_EXHAUSTED
    assert state["tips"]["1"]["use_count"] == 2


def test_manual_tip_replacement_retires_old_binding_and_preserves_history(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    store.initialize()
    store.prepare_tip("S10-1", liquid_station_index=3)
    store.record_tip_use("S10-1", cycles=2)

    replacement = store.replace_tip("S10-1", liquid_station_index=4)
    state = store.snapshot()

    assert replacement["old_tip_index"] == 1
    assert replacement["new_tip_index"] == 2
    assert replacement["old_tip"]["use_count"] == 2
    assert replacement["new_tip"]["current_box"] == 1
    assert state["tips"]["1"]["status"] == TIP_STATUS_EXHAUSTED
    assert state["tips"]["2"]["status"] == TIP_STATUS_BOUND
    assert state["solvents"]["S10-1"]["active_tip_index"] == 2
    assert state["solvents"]["S10-1"]["active_s09_slot"] == 4
    assert state["solvents"]["S10-1"]["tip_history"] == [1, 2]


def test_tip_preparation_rejects_operation_larger_than_tip_limit(tmp_path):
    store = ReusableTipStateStore(
        tmp_path / "tip_state.json",
        max_use_count=2,
    )
    store.initialize()

    with pytest.raises(ValueError, match="单次操作"):
        store.prepare_tip("S10-1", required_cycles=3)

    assert store.snapshot()["solvents"] == {}


def test_tip_bindings_survive_store_restart(tmp_path):
    state_path = tmp_path / "tip_state.json"
    first_store = ReusableTipStateStore(state_path)
    first_store.initialize()
    first_store.prepare_tip("S10-9")
    first_store.record_tip_use("S10-9", cycles=3)

    restored_store = ReusableTipStateStore(state_path)
    restored = restored_store.prepare_tip("S10-9")

    assert restored["tip_index"] == 1
    assert restored["current_box"] == 2
    assert restored["use_count"] == 3


def test_concurrent_solvents_receive_different_tips(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    store.initialize()

    with ThreadPoolExecutor(max_workers=10) as executor:
        allocated = list(
            executor.map(
                lambda index: store.prepare_tip(f"S10-{index}")["tip_index"],
                range(1, 21),
            )
        )

    assert len(set(allocated)) == 20
    assert set(allocated) == set(range(1, 21))


def test_unknown_tip_is_quarantined_from_reuse(tmp_path):
    store = ReusableTipStateStore(tmp_path / "tip_state.json")
    store.initialize()
    store.prepare_tip("S10-3")

    unknown = store.mark_active_tip_unknown("S10-3")

    assert unknown["status"] == TIP_STATUS_UNKNOWN
    with pytest.raises(RuntimeError, match="必须人工确认"):
        store.prepare_tip("S10-3")


def test_record_tip_use_rejects_count_over_limit(tmp_path):
    store = ReusableTipStateStore(
        tmp_path / "tip_state.json",
        max_use_count=2,
    )
    store.initialize()
    store.prepare_tip("S10-1")

    with pytest.raises(ValueError, match="超过上限"):
        store.record_tip_use("S10-1", cycles=3)

    assert store.prepare_tip("S10-1")["use_count"] == 0
