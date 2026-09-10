from __future__ import annotations

from unilabos.app.scheduler import (
    EdgeScheduler,
    MaterialRequirement,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory import InventoryService, InventoryStore


def _req(quantity: float = 1) -> MaterialRequirement:
    return MaterialRequirement(lot_id="tip-lot", quantity=quantity, unit="tip")


def _node(
    node_id: str,
    action: str,
    *,
    device: str = "robot",
    requirements=(),
    locks=(),
) -> WorkflowNode:
    return WorkflowNode(
        id=node_id,
        device_id=device,
        action_name=action,
        material_requirements=list(requirements),
        resource_lock_keys=tuple(locks),
    )


def _inventory(quantity: float = 1) -> InventoryService:
    store = InventoryStore(":memory:")
    service = InventoryService(store)
    if quantity:
        service.inbound_lot("tip", quantity, lot_id="tip-lot")
    return service


def _last_job(scheduler: EdgeScheduler):
    assert scheduler.inflight
    return next(iter(scheduler.inflight.values()))


def test_shortage_waits_original_and_runs_s09_restore_before_replace():
    dispatcher = RecordingDispatcher()
    inventory = _inventory(1)
    factory_calls = []

    def factory(spec, requirements):
        factory_calls.append((spec.workflow_id, requirements))
        return WorkflowSpec(
            workflow_id="tip-box-change",
            nodes=[_node("replace", "replace_tip_box")],
        )

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=inventory,
        material_replenishment_factory=factory,
    )
    original = WorkflowSpec(
        workflow_id="user-run",
        nodes=[
            _node("first", "pipette", requirements=[_req()]),
            _node("second", "pipette", requirements=[_req()]),
        ],
        edges=[WorkflowEdge("e1", "first", "second")],
    )

    result = scheduler.submit_workflow(original)
    assert result["state"] == WorkflowState.RUNNING.value
    first = _last_job(scheduler)
    assert first.action_name == "pipette"
    scheduler.on_job_finished(first.job_id)

    restore = _last_job(scheduler)
    assert restore.action_name == "go_to_safe_position"
    assert restore.device_id == "szlab_mixer_pipetting_station"
    assert restore.workflow_id == "tip-box-change"
    assert scheduler.get_workflow_state("user-run") is WorkflowState.WAITING_MATERIAL
    assert len(factory_calls) == 1

    scheduler.on_job_finished(restore.job_id)
    replace = _last_job(scheduler)
    assert replace.action_name == "replace_tip_box"
    scheduler.on_job_finished(replace.job_id)
    assert scheduler.get_workflow_state("user-run") is WorkflowState.WAITING_MATERIAL

    dispatched = scheduler.on_inventory_inbound("tip", 1, lot_id="tip-lot")
    assert dispatched
    second = _last_job(scheduler)
    assert second.workflow_id == "user-run"
    assert second.action_name == "pipette"
    scheduler.on_job_finished(second.job_id)
    assert scheduler.get_workflow_state("user-run") is WorkflowState.SUCCESS


def test_single_node_shortage_creates_urgent_replenishment_once():
    dispatcher = RecordingDispatcher()
    factory_calls = []

    def factory(spec, requirements):
        factory_calls.append(spec.workflow_id)
        return WorkflowSpec(
            workflow_id="replace-box",
            priority="low",
            nodes=[_node("replace", "replace_tip_box")],
        )

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=_inventory(1),
        material_replenishment_factory=factory,
    )
    original = WorkflowSpec(
        workflow_id="need-two",
        nodes=[_node("pipette", "pipette", requirements=[_req(2)])],
    )

    result = scheduler.submit_workflow(original)
    assert result["state"] == WorkflowState.WAITING_MATERIAL.value
    restore = _last_job(scheduler)
    assert restore.action_name == "go_to_safe_position"
    assert restore.workflow_id == "replace-box"
    snapshot = scheduler.workflow_snapshot("replace-box")
    assert snapshot["priority"] == "urgent"
    assert snapshot["nodes"][0]["param"] == {
        "home_position": 1,
        "require_allow": True,
    }
    scheduler.reschedule()
    assert factory_calls == ["need-two"]


def test_s09_restore_can_run_while_robot_is_busy_but_replace_waits():
    dispatcher = RecordingDispatcher()
    external_busy = {"/devices/robot"}

    def factory(spec, requirements):
        return WorkflowSpec(
            workflow_id="replace-box",
            nodes=[_node("replace", "replace_tip_box", device="robot")],
        )

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        external_busy_keys=external_busy,
        inventory=_inventory(0),
        material_replenishment_factory=factory,
    )
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="need-tip",
            nodes=[_node("pipette", "pipette", requirements=[_req()])],
            priority="normal",
        )
    )
    restore = _last_job(scheduler)
    assert restore.action_name == "go_to_safe_position"
    scheduler.on_job_finished(restore.job_id)
    assert scheduler.inflight == {}

    external_busy.clear()
    scheduler.reschedule()
    replacement = _last_job(scheduler)
    assert replacement.workflow_id == "replace-box"
    assert replacement.action_name == "replace_tip_box"


def test_concurrent_shortage_gets_own_box_change_without_resetting_inflight():
    dispatcher = RecordingDispatcher()

    def factory(spec, requirements):
        return WorkflowSpec(
            workflow_id="tip-box-change",
            nodes=[_node("replace", "replace_tip_box")],
        )

    scheduler = EdgeScheduler(
        dispatcher=dispatcher,
        inventory=_inventory(0),
        material_replenishment_factory=factory,
    )
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="run-a",
            nodes=[_node("a", "pipette", requirements=[_req()])],
        )
    )
    restore = _last_job(scheduler)
    assert restore.action_name == "go_to_safe_position"
    scheduler.on_job_finished(restore.job_id)
    assert [job.action_name for job in scheduler.inflight.values()] == [
        "replace_tip_box"
    ]

    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="run-b",
            nodes=[_node("b", "pipette", requirements=[_req()])],
        )
    )

    # 第二个缺料 run 需要自己的换盒 workflow；若复用同一个 id 会覆盖进行中的
    # _Run，使 S09 还原在换盒执行途中被重复派发。
    replenishments = scheduler.snapshot()["material_replenishments"]
    assert replenishments["run-a"] != replenishments["run-b"]
    first_restore = scheduler.workflow_snapshot(replenishments["run-a"])["nodes"][0]
    assert first_restore["action"] == "go_to_safe_position"
    assert first_restore["completed"] is True
    assert first_restore["dispatched"] is False
    assert (
        sum(1 for item in dispatcher.dispatched if item["action"] == "replace_tip_box")
        == 1
    )


def test_default_factory_emits_restore_plus_four_robot_actions():
    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(), inventory=_inventory(0)
    )
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="need-tip",
            nodes=[_node("p", "pipette", requirements=[_req()])],
        )
    )

    sequence = []
    while scheduler.inflight:
        job = _last_job(scheduler)
        sequence.append((job.device_id, job.action_name))
        scheduler.on_job_finished(job.job_id)
        if len(sequence) > 8:
            break

    # 换 TIP 盒是一个完整 workflow：先复位 S09，再走机器人的 4 个搬运动作。
    assert sequence == [
        ("szlab_mixer_pipetting_station", "go_to_safe_position"),
        ("szlab_mixer_robot", "submit_pick_from_s09"),
        ("szlab_mixer_robot", "submit_place_to_s02"),
        ("szlab_mixer_robot", "submit_pick_from_s02"),
        ("szlab_mixer_robot", "submit_place_to_s09"),
    ]


def test_shortage_rearms_box_change_up_to_attempt_limit():
    def run(max_attempts: int) -> int:
        store = InventoryStore(":memory:")
        inventory = InventoryService(store)
        inventory.inbound_lot("tip", 0, lot_id="tip-lot")
        scheduler = EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            inventory=inventory,
            material_replenishment_max_attempts=max_attempts,
        )
        scheduler.submit_workflow(
            WorkflowSpec(
                workflow_id="two-tips",
                nodes=[_node("p", "pipette", requirements=[_req(2)])],
            )
        )

        def drain() -> None:
            while scheduler.inflight:
                job = _last_job(scheduler)
                scheduler.on_job_finished(job.job_id)

        drain()
        for _ in range(3):
            scheduler.on_inventory_inbound("tip", 1, lot_id="tip-lot")
            drain()
        return sum(
            1
            for item in scheduler.snapshot()["workflows"]
            if item["workflow_id"].startswith("tip-box-change")
        )

    # 每次只补 1 支、需求 2 支：允许重排时多换一次盒，达到上限后不再重排。
    assert run(3) == 2
    assert run(1) == 2


def test_transfer_chain_gets_restore_as_first_node():
    def factory(spec, requirements):
        return WorkflowSpec(
            workflow_id="transfer-box",
            nodes=[
                _node("pick-old", "submit_pick_from_s09"),
                _node("place-s02", "submit_place_to_s02"),
                _node("pick-s02", "submit_pick_from_s02"),
                _node("place-s09", "submit_place_to_s09"),
            ],
            edges=[
                WorkflowEdge("e1", "pick-old", "place-s02"),
                WorkflowEdge("e2", "place-s02", "pick-s02"),
                WorkflowEdge("e3", "pick-s02", "place-s09"),
            ],
        )

    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=_inventory(0),
        material_replenishment_factory=factory,
    )
    scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="need-tip",
            nodes=[_node("pipette", "pipette", requirements=[_req()])],
        )
    )

    actions = []
    while scheduler.inflight:
        job = _last_job(scheduler)
        actions.append(job.action_name)
        scheduler.on_job_finished(job.job_id)
        if len(actions) == 5:
            break
    assert actions == [
        "go_to_safe_position",
        "submit_pick_from_s09",
        "submit_place_to_s02",
        "submit_pick_from_s02",
        "submit_place_to_s09",
    ]
