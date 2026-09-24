from unilabos.app.scheduler import (
    EdgeScheduler,
    MaterialRequirement,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
    build_liquid_bottle_switch_workflow,
)


def test_liquid_bottle_switch_is_s09_s10_serial_robot_workflow():
    spec = build_liquid_bottle_switch_workflow(
        current_container_id="s09-liquid-station-1",
        replacement_container_id="s10-liquid-position-3",
    )
    assert [node.action_name for node in spec.nodes] == [
        "submit_pick_from_s09",
        "submit_place_to_s10",
        "submit_pick_from_s10",
        "submit_place_to_s09",
    ]
    assert spec.nodes[0].param == {"product_type": 2, "position": 1}
    assert spec.nodes[2].param == {"position": 3}
    assert len(spec.edges) == 3
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory import InventoryService, InventoryStore


def test_current_container_shortage_switches_when_total_stock_is_sufficient():
    store = InventoryStore(":memory:")
    inventory = InventoryService(store)
    inventory.inbound_lot("powder", 1, lot_id="powder-lot", container_id="s07-pos-1")
    inventory.inbound_lot("powder", 9, lot_id="powder-lot", container_id="s07-pos-2")

    def switch_factory(spec, requirements, shortage):
        assert shortage.container_id == "s07-pos-1"
        return WorkflowSpec(
            workflow_id="switch-powder-barrel",
            priority="low",
            nodes=[WorkflowNode("switch", "s07", "rotate_powder_cartridge_to_feed", param={"position": 2})],
        )

    scheduler = EdgeScheduler(
        dispatcher=RecordingDispatcher(),
        inventory=inventory,
        material_switch_factory=switch_factory,
    )
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="dose-1",
            nodes=[
                WorkflowNode(
                    "dose",
                    "s07",
                    "dose_powder",
                    material_requirements=[
                        MaterialRequirement("powder-lot", 2, "g", "s07-pos-1")
                    ],
                )
            ],
        )
    )
    assert result["state"] == WorkflowState.WAITING_MATERIAL.value
    switch_job = next(iter(scheduler.inflight.values()))
    assert switch_job.action_name == "rotate_powder_cartridge_to_feed"
    scheduler.on_job_finished(switch_job.job_id)
    dose_job = next(iter(scheduler.inflight.values()))
    assert dose_job.action_name == "dose_powder"
    assert inventory.material_total("powder") == 8
    assert inventory.container_binding("powder-lot", "s07-pos-1")["quantity_available"] == 7


def test_total_shortage_remains_waiting_without_switch_workflow():
    store = InventoryStore(":memory:")
    inventory = InventoryService(store)
    inventory.inbound_lot("powder", 1, lot_id="powder-lot", container_id="s07-pos-1")
    scheduler = EdgeScheduler(inventory=inventory, material_switch_factory=lambda *args: None)
    result = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="dose-2",
            nodes=[
                WorkflowNode(
                    "dose",
                    "s07",
                    "dose_powder",
                    material_requirements=[
                        MaterialRequirement("powder-lot", 2, "g", "s07-pos-1")
                    ],
                )
            ],
        )
    )
    assert result["state"] == WorkflowState.WAITING_MATERIAL.value
    assert scheduler.inflight == {}
