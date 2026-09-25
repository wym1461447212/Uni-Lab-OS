#!/usr/bin/env python3
"""在隔离虚拟 OPC 上生成并派发 15 样品，不改工作流定义。"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WF_PATH = "szlab_robot_action_workflow.json"
WF_JSON = json.loads((REPO / WF_PATH).read_text(encoding="utf-8"))


def workflow_for_tick() -> dict:
    """把规则工作流展开成 Task 预检需要的 nodes，不改源文件。"""
    nodes = []
    for rule in WF_JSON.get("rules") or []:
        for item in rule.get("actions") or []:
            action = item.get("action") or {}
            nodes.append(
                {
                    "workflow_node_id": action["workflow_node_id"],
                    "device_id": action["device_id"],
                    "method": action["method"],
                    "params": dict(action.get("params") or {}),
                }
            )
    return {"nodes": nodes}
TASK = os.environ.get(
    "TASK_ORCHESTRATION_API_URL", "http://127.0.0.1:8091/api/v1"
).rstrip("/")
UI = os.environ.get("SZLAB_WORKFLOW_UI_URL", "http://127.0.0.1:8014/api").rstrip("/")
SAMPLES = [f"Sample {chr(ord('A') + i)}" for i in range(15)]
SLOTS = [f"{row}-{col}" for row in range(1, 4) for col in range(1, 7)][:15]
PROCESS_TEMPLATES = [
    "task_w_ae3c6440a875496887ddc9e1e3f988de",
    "task_x_b8833e9573c14ef987962563f83491d7",
    "task_y_0ec9afcdcc1246e1a9dbc4caeb10865e",
    "task_z_9a599902d061465b8507f47145e13681",
    "task_10_252e5ac2de1e4070bf1a4ab4560361ad",
    "task_11_c23cf203e41b45aa9ef1e21946315663",
    "task_12_ddf3487bcf334cd2883f57dd539e232f",
    "task_13_3913b0c1c62a401faeacea2f1431eeac",
    "task_14_17dc3803b96548348184ab74e5df2aa7",
    "task_15_14ec48e0e0a243b3ae3a2f3fb5d5e9cf",
    "task_16_5059b9d9471d46b79a37e38e49fc2068",
    "task_17_888c089cd83b4b0d9cda634e6657d296",
    "task_18_cb50a0e35a694e48a13305ef829e14fd",
    "task_19_0480d4e88835496c9118fe8e14c3fdf4",
    "task_1a_9245b2850e6844e2b8135ca645c220e5",
    "task_1b_59510a4a4ba0414790d14cdb36293157",
    "task_1c_6b0c60b7fa3a4a39a6dda2f08df56ac2",
]


def call(base: str, method: str, path: str, payload: dict | None = None, timeout: float = 120):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:800]}") from exc


def workspace():
    return call(TASK, "GET", f"/workspaces?workflow_path={WF_PATH}")


def summarize(ws: dict) -> dict:
    items = ws["workspace"]["task_instances"]
    return {
        "version": ws["version"],
        "paused": ws["workspace"]["scheduler_paused"],
        "pause_reason": ws["workspace"].get("pause_reason"),
        "count": len(items),
        "status": dict(Counter(item["status"] for item in items)),
    }


def sample_parameters() -> dict:
    params: dict[str, dict] = {}
    for index, sample_id in enumerate(SAMPLES):
        slot = SLOTS[index]
        stir = (index % 4) + 1
        cap_slot = (index % 5) + 1
        sample_code = 101 + index
        params[sample_id] = {
            "task_w_ae3c6440a875496887ddc9e1e3f988de": {
                "w01_pick_beaker_s03": {"product_type": 1, "position": slot},
            },
            "task_x_b8833e9573c14ef987962563f83491d7": {
                "w01_dose_powder_s07": {
                    "coarse_position": 1,
                    "fine_position": 1,
                    "target_weight": 1.0,
                    "recipe_name": "default",
                }
            },
            "task_11_c23cf203e41b45aa9ef1e21946315663": {
                "w03_add_liquid_s09": {
                    "liquid_count": 1,
                    "liquid_additions": [
                        {
                            "liquid_station_index": 1,
                            "solvent_batch_id": f"solvent-{sample_id.replace(' ', '-')}",
                            "volume": 5000,
                            "reuse_tip": False,
                        }
                    ],
                    "volume_unit": "raw",
                    "skip_level_check": False,
                    "initialize_tip_inventory": False,
                }
            },
            "task_12_ddf3487bcf334cd2883f57dd539e232f": {
                "w04_place_beaker_s04": {"position": stir, "sample_id": sample_id},
            },
            "task_13_3913b0c1c62a401faeacea2f1431eeac": {
                "w04_run_stirring_s04": {"position": stir, "duration": 5.0},
            },
            "task_14_17dc3803b96548348184ab74e5df2aa7": {
                "w06_pick_beaker_s04": {"position": stir},
            },
            "task_18_cb50a0e35a694e48a13305ef829e14fd": {
                "w05_pick_sample_vial_s03": {"product_type": 2, "position": slot},
            },
            "task_19_0480d4e88835496c9118fe8e14c3fdf4": {
                "w05_open_sample_vial_s08": {
                    "工艺选择": 3,
                    "样品ID": [sample_code],
                    "瓶盖暂存位": cap_slot,
                }
            },
            "task_1a_9245b2850e6844e2b8135ca645c220e5": {
                "w07_place_beaker_s11": {"product_type": 1, "position": slot},
            },
            "task_1b_59510a4a4ba0414790d14cdb36293157": {
                "w07_close_sample_vial_s08": {
                    "工艺选择": 4,
                    "样品ID": [sample_code],
                    "瓶盖暂存位": cap_slot,
                }
            },
            "task_1c_6b0c60b7fa3a4a39a6dda2f08df56ac2": {
                "w07_place_sample_vial_s11": {"product_type": 2, "position": slot},
            },
        }
    return params


def main() -> None:
    parser = argparse.ArgumentParser(description="在隔离虚拟 OPC 上派发 15 样品")
    parser.add_argument("--resume", action="store_true", help="不清理实例，只继续 tick")
    args = parser.parse_args()
    current = workspace()
    print("initial", summarize(current), flush=True)
    if not args.resume:
        if not current["workspace"]["scheduler_paused"]:
            current = call(
                TASK,
                "POST",
                "/schedule:plan",
                {
                    "workflow_path": WF_PATH,
                    "expected_version": current["version"],
                    "paused": True,
                },
            )
        cleared = call(
            TASK,
            "POST",
            "/instances:clear",
            {"workflow_path": WF_PATH, "expected_version": current["version"]},
        )
        generated = call(
            TASK,
            "POST",
            "/instances:generate",
            {
                "workflow_path": WF_PATH,
                "expected_version": cleared["version"],
                "template_ids": PROCESS_TEMPLATES,
                "sample_ids": SAMPLES,
                "sample_start_interval_seconds": 1,
                "sample_template_node_parameters": sample_parameters(),
            },
        )
        print("generated", summarize(generated), flush=True)

    connected = call(
        UI,
        "POST",
        "/task-opc/connect",
        {"url": "opc.tcp://127.0.0.1:48620/", "task_workspace_path": WF_PATH},
        timeout=180,
    )
    print(
        "opc_connect",
        connected.get("success"),
        connected.get("message"),
        (connected.get("plc") or {}).get("connected"),
        len((connected.get("plc") or {}).get("registered_variables") or []),
        flush=True,
    )
    if not connected.get("success"):
        raise SystemExit("OPC 连接失败")

    try:
        polled = call(UI, "POST", "/task-opc/poll", {"task_workspace_path": WF_PATH})
    except RuntimeError as exc:
        polled = {"success": False, "message": str(exc)}
    print("opc_poll", {k: polled.get(k) for k in ("success", "active", "variable_count", "message")}, flush=True)

    planned = None
    for _ in range(8):
        current = workspace()
        try:
            planned = call(
                TASK,
                "POST",
                "/schedule:plan",
                {
                    "workflow_path": WF_PATH,
                    "expected_version": current["version"],
                    "paused": False,
                },
            )
            break
        except RuntimeError as exc:
            if "409" not in str(exc) and "version conflict" not in str(exc):
                raise
            time.sleep(0.3)
    if planned is None:
        raise RuntimeError("解除暂停时持续遇到版本冲突")
    print("dispatch", summarize(planned), flush=True)

    started = time.monotonic()
    last = ""
    while True:
        current = workspace()
        try:
            call(
                TASK,
                "POST",
                "/schedule:advance",
                {
                    "workflow_path": WF_PATH,
                    "expected_version": current["version"],
                    "completed_instance_ids": [],
                },
            )
        except RuntimeError as exc:
            if "409" not in str(exc) and "version conflict" not in str(exc):
                raise
        try:
            call(UI, "POST", "/task-opc/poll", {"task_workspace_path": WF_PATH})
        except RuntimeError as exc:
            if "409" not in str(exc) and "version conflict" not in str(exc):
                raise
        tick = call(
            UI,
            "POST",
            "/task-execution/tick",
            {"task_workspace_path": WF_PATH, "workflow": workflow_for_tick()},
            timeout=900,
        )
        current = workspace()
        summary = summarize(current)
        status = summary["status"]
        line = (
            f"t={time.monotonic() - started:7.1f}s paused={summary['paused']} "
            f"reason={summary['pause_reason']} {status} "
            f"tick_ok={tick.get('success')} claimed={tick.get('claimed')} "
            f"completed={tick.get('completed')} failed={tick.get('failed')} "
            f"msg={tick.get('message')}"
        )
        if line != last:
            print(line, flush=True)
            last = line
        failed = status.get("failed", 0)
        completed = status.get("completed", 0)
        total = summary["count"]
        if failed:
            print("RUN_FAILED", summary, flush=True)
            raise SystemExit(2)
        if summary["paused"] and summary["pause_reason"]:
            print("RUN_PAUSED", summary, flush=True)
            raise SystemExit(3)
        if completed == total and total == 15 * 17:
            print("RUN_OK", f"{completed}/{total}", summary, flush=True)
            return
        if time.monotonic() - started > 14400:
            print("RUN_TIMEOUT", summary, flush=True)
            raise SystemExit(4)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
