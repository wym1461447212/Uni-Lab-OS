#!/usr/bin/env python3
"""给 Task 排程服务续期 OPC 快照，避免 UI 重连后序号回退导致条件过期。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from opcua import Client


TASK = os.environ.get(
    "TASK_ORCHESTRATION_API_URL", "http://127.0.0.1:8091/api/v1"
).rstrip("/")
WF_PATH = "szlab_robot_action_workflow.json"
ENDPOINT = os.environ.get(
    "SZLAB_VIRTUAL_PLC_URL", "opc.tcp://127.0.0.1:4840/"
)
PLC_ID = "szlab_poly_plc"
VARIABLE = "传感器状态_上位机[3].NO[0]"
PREFIX = "ns=4;s=上位机通讯|"


def call(method: str, path: str, payload: dict | None = None, timeout: float = 10):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        TASK + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body[:400]}") from exc


def main() -> None:
    client = Client(ENDPOINT)
    client.connect()
    node = client.get_node(PREFIX + VARIABLE)
    print("keepalive connected", flush=True)
    try:
        while True:
            workspace = call(
                "GET", f"/workspaces?workflow_path={WF_PATH}"
            )
            snaps = (workspace.get("workspace") or {}).get("opc_snapshots") or []
            sequence = 0
            for snap in snaps:
                if snap.get("plc_device_id") == PLC_ID:
                    sequence = max(sequence, int(snap.get("sequence") or 0))
            value = bool(node.get_value())
            try:
                result = call(
                    "POST",
                    "/opc/snapshots",
                    {
                        "workflow_path": WF_PATH,
                        "expected_version": workspace["version"],
                        "plc_device_id": PLC_ID,
                        "sequence": sequence + 1,
                        "values": {VARIABLE: value},
                    },
                )
                print(
                    "seq",
                    sequence + 1,
                    "value",
                    value,
                    "accepted",
                    result.get("accepted"),
                    "ver",
                    result.get("version"),
                    flush=True,
                )
            except RuntimeError as exc:
                if "409" not in str(exc) and "version" not in str(exc).lower():
                    print("keepalive error", exc, flush=True)
            time.sleep(2.0)
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
