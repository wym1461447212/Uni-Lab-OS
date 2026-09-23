from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.app.scheduler import (
    WorkflowSpec,
    load_szlab_workflow_spec,
    workflow_spec_from_szlab_json,
)


ROOT = Path(__file__).resolve().parents[2]


def test_load_existing_szlab_robot_action_workflow_as_scheduler_spec():
    spec = load_szlab_workflow_spec(ROOT / "szlab_robot_action_workflow.json")

    assert isinstance(spec, WorkflowSpec)
    assert spec.workflow_id == "szlab_robot_action_workflow"
    assert len(spec.nodes) == 27
    assert len(spec.edges) == 26
    assert spec.nodes[0].id == "w01_pick_beaker_s03"
    assert spec.nodes[0].device_id == "szlab_mixer_robot"
    assert spec.nodes[0].action_name == "submit_pick_from_s03"
    assert spec.nodes[0].param == {"product_type": 1, "position": "1-1"}
    assert spec.edges[0].source_node_id == "w01_pick_beaker_s03"
    assert spec.edges[0].target_node_id == "w01_place_beaker_s072"


def test_explicit_dependencies_replace_legacy_rule_order():
    spec = workflow_spec_from_szlab_json(
        {
            "name": "custom",
            "priority": "high",
            "rules": [
                {
                    "actions": [
                        {
                            "action": {
                                "workflow_node_id": "a",
                                "device_id": "robot",
                                "method": "pick",
                                "params": {},
                            }
                        },
                        {
                            "action": {
                                "workflow_node_id": "b",
                                "device_id": "pump",
                                "method": "dose",
                                "params": {"volume": 10},
                                "depends_on": [],
                                "resource_lock_keys": ["station:S06"],
                                "material_requirements": [
                                    {
                                        "lot_id": "ethanol-lot",
                                        "quantity": 10,
                                        "unit": "mL",
                                    }
                                ],
                            }
                        },
                    ]
                }
            ],
        }
    )

    assert spec.priority == "high"
    assert spec.edges == []
    assert spec.nodes[1].resource_lock_keys == ("station:S06",)
    assert spec.nodes[1].material_requirements[0].lot_id == "ethanol-lot"


def test_adapter_rejects_duplicate_node_ids():
    action = {
        "action": {
            "workflow_node_id": "same",
            "device_id": "robot",
            "method": "pick",
            "params": {},
        }
    }
    with pytest.raises(ValueError, match="重复"):
        workflow_spec_from_szlab_json(
            {"rules": [{"actions": [action, action]}]}
        )
