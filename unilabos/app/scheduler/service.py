"""本地 Edge Scheduler 最小实现。

覆盖换 TIP 盒所需的编排规则：设备/物料锁优先、缺料挂起与补料 workflow、
以及补料前先让 S09 回到安全位置。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable

from .dispatch import build_job_start_payload
from .inventory.domain import InsufficientStock
from .models import (
    DispatchedJob,
    ReadyTask,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    WorkflowState,
    node_from_dict,
    priority_weight,
)
from .ordering import OrderingContext, StableLocalOrderer


# 换 TIP 盒的机器人搬运链。S09 复位节点由 EdgeScheduler 注入到链首，
# 因此这里只描述换盒本身的 4 个动作。
TIP_BOX_CHANGE_ACTIONS: tuple[str, ...] = (
    "submit_pick_from_s09",
    "submit_place_to_s02",
    "submit_pick_from_s02",
    "submit_place_to_s09",
)


def build_tip_box_change_workflow(
    *,
    workflow_id: str = "tip-box-change",
    robot_device_id: str = "szlab_mixer_robot",
    priority: Any = "urgent",
) -> WorkflowSpec:
    """构造"S09 复位 + 4 个换盒动作"的完整换 TIP 盒 workflow。

    复位（go_to_safe_position）由 EdgeScheduler 在补料排程时注入为首节点，
    这里给出机器人搬运链本身及其串行依赖。
    """
    nodes = [
        WorkflowNode(
            id=action,
            device_id=robot_device_id,
            action_name=action,
        )
        for action in TIP_BOX_CHANGE_ACTIONS
    ]
    edges = [
        WorkflowEdge(
            uuid=f"{source}->{target}",
            source_node_id=source,
            target_node_id=target,
        )
        for source, target in zip(TIP_BOX_CHANGE_ACTIONS, TIP_BOX_CHANGE_ACTIONS[1:])
    ]
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=nodes,
        edges=edges,
        priority=priority,
    )


def _default_tip_box_change_factory(
    spec: Any, requirements: Any = None
) -> WorkflowSpec:
    """未注入工厂时使用的默认补料工厂。"""
    del spec, requirements
    return build_tip_box_change_workflow()


@dataclass
class _Run:
    spec: WorkflowSpec
    state: WorkflowState = WorkflowState.RUNNING
    completed: set[str] = field(default_factory=set)
    dispatched: set[str] = field(default_factory=set)
    waiting_node_id: str | None = None
    failed_node_id: str | None = None

    def ready_nodes(self) -> list[WorkflowNode]:
        incoming: dict[str, set[str]] = {node.id: set() for node in self.spec.nodes}
        for edge in self.spec.edges:
            incoming.setdefault(edge.target_node_id, set()).add(edge.source_node_id)
        return [
            node
            for node in self.spec.nodes
            if not node.disabled
            and node.id not in self.completed
            and node.id not in self.dispatched
            and incoming.get(node.id, set()) <= self.completed
        ]

    def is_terminal(self) -> bool:
        active_ids = {node.id for node in self.spec.nodes if not node.disabled}
        return self.state in {
            WorkflowState.SUCCESS,
            WorkflowState.FAILED,
            WorkflowState.CANCELED,
        } or active_ids <= self.completed


class EdgeScheduler:
    """可注入库存、派发器和补料 workflow 工厂的本地调度器。"""

    TIP_ACTIONS = {
        "replace_tip_box",
        "replace_tip",
        "replace_reusable_tip",
        "submit_pick_from_s09",
        "submit_place_to_s02",
        "submit_pick_from_s02",
        "submit_place_to_s09",
    }

    def __init__(
        self,
        *,
        orderer: StableLocalOrderer | None = None,
        dispatcher: Any | None = None,
        external_busy_keys: set[str] | None = None,
        inventory: Any | None = None,
        material_replenishment_factory: Callable[..., WorkflowSpec | dict[str, Any]]
        | None = None,
        material_replenishment_priority: Any = "urgent",
        material_replenishment_s09_device_id: str = "szlab_mixer_pipetting_station",
        material_replenishment_s09_home_position: int | str = 1,
        material_replenishment_max_attempts: int = 3,
    ) -> None:
        self.orderer = orderer or StableLocalOrderer()
        self.dispatcher = dispatcher
        self.inventory = inventory
        self.material_replenishment_factory = (
            material_replenishment_factory or _default_tip_box_change_factory
        )
        self.material_replenishment_max_attempts = int(
            material_replenishment_max_attempts
        )
        self.material_replenishment_priority = material_replenishment_priority
        self.material_replenishment_s09_device_id = material_replenishment_s09_device_id
        self.material_replenishment_s09_home_position = (
            material_replenishment_s09_home_position
        )
        self._workflows: dict[str, _Run] = {}
        self._inflight: dict[str, DispatchedJob] = {}
        self._job_resource_locks: dict[str, set[str]] = {}
        self._external_busy_keys = (
            external_busy_keys if external_busy_keys is not None else set()
        )
        self._material_workflows: set[str] = set()
        self._deferred_material_workflows: set[str] = set()
        self._material_waiting_nodes: dict[str, str] = {}
        self._material_replenishment_by_run: dict[str, str] = {}
        self._material_replenished_run_by_workflow: dict[str, str] = {}
        self._material_replenishment_attempts: dict[str, int] = {}
        self._reschedule_count = 0
        self._lock = RLock()

    @staticmethod
    def _coerce_spec(spec: WorkflowSpec | dict[str, Any]) -> WorkflowSpec:
        if isinstance(spec, WorkflowSpec):
            return spec
        nodes = [
            node if isinstance(node, WorkflowNode) else node_from_dict(node)
            for node in spec.get("nodes", [])
        ]
        edges: list[WorkflowEdge] = []
        for index, edge in enumerate(spec.get("edges", [])):
            if isinstance(edge, WorkflowEdge):
                edges.append(edge)
                continue
            edges.append(
                WorkflowEdge(
                    uuid=str(edge.get("uuid") or edge.get("id") or f"edge-{index}"),
                    source_node_id=str(
                        edge.get("source_node_id")
                        or edge.get("source")
                        or edge.get("sourceNodeId")
                    ),
                    target_node_id=str(
                        edge.get("target_node_id")
                        or edge.get("target")
                        or edge.get("targetNodeId")
                    ),
                )
            )
        return WorkflowSpec(
            workflow_id=str(spec.get("workflow_id") or spec.get("id") or uuid.uuid4()),
            nodes=nodes,
            edges=edges,
            priority=spec.get("priority", "normal"),
            submitted_at=float(spec.get("submitted_at") or spec.get("submittedAt") or 0)
            or WorkflowSpec.__dataclass_fields__["submitted_at"].default_factory(),
            run_id=str(spec.get("run_id") or spec.get("runId") or ""),
        )

    @property
    def external_busy_keys(self) -> set[str]:
        return self._external_busy_keys

    @property
    def inflight(self) -> dict[str, DispatchedJob]:
        return dict(self._inflight)

    def _busy_device_keys(self) -> set[str]:
        busy = set(self._external_busy_keys)
        for job in self._inflight.values():
            busy.add(job.device_action_key)
            busy.add(f"/devices/{job.device_id}")
        return busy

    def _held_resource_locks(self) -> set[str]:
        held: set[str] = set()
        for locks in self._job_resource_locks.values():
            held.update(locks)
        held.update(
            key for key in self._external_busy_keys if not str(key).startswith("/devices/")
        )
        return held

    @staticmethod
    def _requirements(run: _Run) -> dict[str, list[Any]]:
        return run.spec.material_requirements_by_node()

    def _reserve_workflow(self, run: _Run) -> bool:
        requirements = self._requirements(run)
        if self.inventory is None or not requirements:
            return True
        try:
            self.inventory.reserve_workflow(run.spec.run_id, requirements)
        except InsufficientStock:
            return False
        return True

    def _reserve_node(self, run: _Run, node: WorkflowNode) -> bool:
        if self.inventory is None or not node.material_requirements:
            return True
        try:
            self.inventory.reserve_node(
                run.spec.run_id, node.id, list(node.material_requirements)
            )
        except InsufficientStock:
            return False
        return True

    def _consume_node(self, run: _Run, node: WorkflowNode) -> None:
        if self.inventory is None or not node.material_requirements:
            return
        consume = getattr(self.inventory, "consume_reservation", None)
        if consume is None:
            return
        try:
            consume(run.spec.run_id, node.id)
        except (KeyError, TypeError):
            return

    def _release_inventory(self, run: _Run) -> None:
        if self.inventory is None:
            return
        release = getattr(self.inventory, "release_workflow", None)
        if release is not None:
            release(run.spec.run_id, reason=f"workflow {run.state.value}")

    @classmethod
    def _is_tip_action(cls, node: WorkflowNode) -> bool:
        action = node.action_name.lower()
        return action in cls.TIP_ACTIONS or (
            "tip" in action
            and ("replace" in action or "pick" in action or "place" in action)
        )

    def _ensure_s09_restore_before_tip_replacement(
        self, spec: WorkflowSpec
    ) -> bool:
        active_nodes = [node for node in spec.nodes if not node.disabled]
        if not any(self._is_tip_action(node) for node in active_nodes):
            return False
        if any(node.action_name == "go_to_safe_position" for node in active_nodes):
            return False
        restore_id = f"{spec.workflow_id}:s09-restore"
        restore = WorkflowNode(
            id=restore_id,
            device_id=self.material_replenishment_s09_device_id,
            action_name="go_to_safe_position",
            action_type="goal",
            param={
                "home_position": self.material_replenishment_s09_home_position,
                "require_allow": True,
            },
        )
        old_nodes = list(spec.nodes)
        spec.nodes.insert(0, restore)
        for node in old_nodes:
            if node.disabled:
                continue
            spec.edges.append(
                WorkflowEdge(
                    uuid=f"{restore_id}->{node.id}",
                    source_node_id=restore_id,
                    target_node_id=node.id,
                )
            )
        return True

    def _call_replenishment_factory(
        self, run: _Run
    ) -> WorkflowSpec | dict[str, Any] | None:
        if self.material_replenishment_factory is None:
            return None
        requirements = self._requirements(run)
        try:
            return self.material_replenishment_factory(run.spec, requirements)
        except TypeError as first_error:
            try:
                return self.material_replenishment_factory(run, requirements)
            except TypeError:
                try:
                    return self.material_replenishment_factory(run.spec)
                except TypeError:
                    raise first_error

    def _ensure_material_replenishment(self, run: _Run) -> bool:
        if run.spec.workflow_id in self._material_replenishment_by_run:
            return False
        result = self._call_replenishment_factory(run)
        if result is None:
            return False
        spec = self._coerce_spec(result)
        # 工厂可能返回稳定的 workflow_id。若该 id 已被占用，直接覆盖会丢掉
        # 进行中的 _Run 进度，导致 S09 还原在换盒执行中重复派发，因此改为改名。
        if (
            not spec.workflow_id
            or spec.workflow_id == run.spec.workflow_id
            or spec.workflow_id in self._workflows
        ):
            base_id = spec.workflow_id or "tip-replenishment"
            spec.workflow_id = f"{base_id}-{uuid.uuid4().hex[:12]}"
            spec.run_id = spec.workflow_id
        spec.priority = self.material_replenishment_priority
        self._ensure_s09_restore_before_tip_replacement(spec)
        synthetic = _Run(spec=spec)
        self._workflows[spec.workflow_id] = synthetic
        self._material_replenishment_by_run[run.spec.workflow_id] = spec.workflow_id
        self._material_replenished_run_by_workflow[spec.workflow_id] = (
            run.spec.workflow_id
        )
        if not any(not node.disabled for node in spec.nodes):
            synthetic.state = WorkflowState.SUCCESS
        return True

    def _replenishment_settled(self, workflow_id: str) -> str | None:
        """返回该 run 最近一次补料的状态："running" / "success" / "failed"。"""
        replenishment_id = self._material_replenishment_by_run.get(workflow_id)
        if not replenishment_id:
            return None
        replenishment = self._workflows.get(replenishment_id)
        if replenishment is None:
            return None
        return (
            "success"
            if replenishment.state is WorkflowState.SUCCESS
            else "failed"
            if replenishment.state is WorkflowState.FAILED
            else "running"
        )

    def _rearm_settled_material_workflows(self) -> bool:
        """入库后仍缺料：说明补进来的量不够，再排一次补料（次数有上限）。

        只在入库事件上触发，避免换盒刚完成、TIP 尚未回补时就重复排队。
        """
        changed = False
        for workflow_id, run in list(self._workflows.items()):
            if run.state is not WorkflowState.WAITING_MATERIAL:
                continue
            if self._replenishment_settled(workflow_id) not in {"success", "failed"}:
                continue
            attempts = self._material_replenishment_attempts.get(workflow_id, 0)
            if attempts >= self.material_replenishment_max_attempts:
                continue
            self._material_replenishment_by_run.pop(workflow_id, None)
            self._material_replenishment_attempts[workflow_id] = attempts + 1
            changed |= self._ensure_material_replenishment(run)
        return changed

    def _try_resume_waiting(self) -> bool:
        changed = False
        for workflow_id, run in list(self._workflows.items()):
            if run.state is not WorkflowState.WAITING_MATERIAL:
                continue
            node_id = self._material_waiting_nodes.get(workflow_id)
            if node_id:
                node = next(
                    (item for item in run.spec.nodes if item.id == node_id), None
                )
                if node is not None and self._reserve_node(run, node):
                    run.state = WorkflowState.RUNNING
                    run.waiting_node_id = None
                    self._material_waiting_nodes.pop(workflow_id, None)
                    changed = True
                    continue
            elif self._reserve_workflow(run):
                run.state = WorkflowState.RUNNING
                run.waiting_node_id = None
                self._material_waiting_nodes.pop(workflow_id, None)
                changed = True
                continue
        return changed

    def submit_workflow(self, spec: WorkflowSpec | dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            workflow = self._coerce_spec(spec)
            if workflow.workflow_id in self._workflows:
                raise ValueError(f"workflow already exists: {workflow.workflow_id}")
            run = _Run(spec=workflow)
            requirements = self._requirements(run)
            created_replenishment = False
            if requirements and self.inventory is not None:
                self._material_workflows.add(workflow.workflow_id)
                if not self._reserve_workflow(run):
                    if len(requirements) == 1:
                        run.state = WorkflowState.WAITING_MATERIAL
                        run.waiting_node_id = next(iter(requirements))
                        self._material_waiting_nodes[workflow.workflow_id] = (
                            run.waiting_node_id
                        )
                        created_replenishment = self._ensure_material_replenishment(run)
                    else:
                        self._deferred_material_workflows.add(workflow.workflow_id)
            self._workflows[workflow.workflow_id] = run
            dispatched = self._reschedule_locked()
            if created_replenishment:
                # 同一轮 _reschedule_locked 只会恢复已存在的等待项，新生成的
                # 补料 workflow 需要立即再排一次，做到缺料即时触发。
                dispatched.extend(self._reschedule_locked())
            return {
                "workflow_id": workflow.workflow_id,
                "state": run.state.value,
                "dispatched": dispatched,
            }

    def _ready_tasks(self) -> list[ReadyTask]:
        ready: list[ReadyTask] = []
        for workflow_id, run in self._workflows.items():
            if run.state is not WorkflowState.RUNNING:
                continue
            unblocking = workflow_id in self._material_replenished_run_by_workflow
            for node in run.ready_nodes():
                ready.append(
                    ReadyTask(
                        workflow_id=workflow_id,
                        node=node,
                        priority_weight=priority_weight(run.spec.priority),
                        submitted_at=run.spec.submitted_at,
                        is_resource_unblocking=unblocking,
                        resource_lock_keys=node.resource_lock_keys,
                    )
                )
        return ready

    def _dispatch_task(self, task: ReadyTask) -> str:
        run = self._workflows[task.workflow_id]
        node = task.node
        self._consume_node(run, node)
        run.dispatched.add(node.id)
        job_id = str(uuid.uuid4())
        job = DispatchedJob(
            job_id=job_id,
            workflow_id=task.workflow_id,
            node_id=node.id,
            device_action_key=node.device_action_key,
            device_id=node.device_id,
            action_name=node.action_name,
            resource_lock_keys=tuple(node.resource_lock_keys),
        )
        self._inflight[job_id] = job
        self._job_resource_locks[job_id] = set(node.resource_lock_keys)
        if self.dispatcher is not None:
            payload = build_job_start_payload(
                job_id=job_id, workflow_id=task.workflow_id, node=node
            )
            dispatch = getattr(self.dispatcher, "dispatch", None)
            if dispatch is not None:
                dispatch(payload)
            elif callable(self.dispatcher):
                self.dispatcher(payload)
        return job_id

    def _reschedule_locked(self) -> list[str]:
        self._reschedule_count += 1
        self._try_resume_waiting()
        dispatched: list[str] = []
        created_replenishment = False
        ready = self.orderer.order(
            self._ready_tasks(),
            OrderingContext(
                busy_device_action_keys=self._busy_device_keys(),
                busy_resource_lock_keys=self._held_resource_locks(),
            ),
        )
        busy_devices = self._busy_device_keys()
        held_resources = self._held_resource_locks()
        for task in ready:
            node = task.node
            if (
                node.device_action_key in busy_devices
                or node.device_lock_key in busy_devices
                or set(node.resource_lock_keys) & held_resources
            ):
                continue
            run = self._workflows[task.workflow_id]
            if (
                task.workflow_id in self._deferred_material_workflows
                and node.material_requirements
                and not self._reserve_node(run, node)
            ):
                run.state = WorkflowState.WAITING_MATERIAL
                run.waiting_node_id = node.id
                self._material_waiting_nodes[task.workflow_id] = node.id
                created_replenishment |= self._ensure_material_replenishment(run)
                continue
            job_id = self._dispatch_task(task)
            dispatched.append(job_id)
            busy_devices.add(node.device_action_key)
            busy_devices.add(node.device_lock_key)
            held_resources.update(node.resource_lock_keys)
        if created_replenishment:
            dispatched.extend(self._reschedule_locked())
        return dispatched

    def reschedule(self) -> list[str]:
        with self._lock:
            return self._reschedule_locked()

    def on_job_finished(
        self,
        job_id: str,
        *,
        success: bool = True,
        ret_value: Any = None,
    ) -> dict[str, Any]:
        del ret_value
        with self._lock:
            job = self._inflight.pop(job_id)
            self._job_resource_locks.pop(job_id, None)
            run = self._workflows[job.workflow_id]
            run.dispatched.discard(job.node_id)
            if success:
                run.completed.add(job.node_id)
                if run.is_terminal():
                    run.state = WorkflowState.SUCCESS
            else:
                run.failed_node_id = job.node_id
                run.state = WorkflowState.FAILED
                self._release_inventory(run)
            dispatched = self._reschedule_locked()
            return {
                "workflow_id": run.spec.workflow_id,
                "workflow_state": run.state.value,
                "dispatched": dispatched,
            }

    def on_inventory_inbound(
        self, template_id: str, quantity: float, *, lot_id: str
    ) -> list[str]:
        with self._lock:
            if self.inventory is None:
                return []
            inbound = getattr(self.inventory, "inbound_lot", None)
            if inbound is None:
                return []
            inbound(template_id, quantity, lot_id=lot_id)
            # 先让到货满足已挂起的 run；仍缺料的 run 才需要再排一次补料。
            dispatched = self._reschedule_locked()
            if self._rearm_settled_material_workflows():
                dispatched.extend(self._reschedule_locked())
            return dispatched

    def cancel_workflow(self, workflow_id: str) -> bool:
        with self._lock:
            run = self._workflows.get(workflow_id)
            if run is None or run.is_terminal():
                return False
            run.state = WorkflowState.CANCELED
            self._release_inventory(run)
            return True

    def workflow_snapshot(self, workflow_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._workflows[workflow_id]
            return {
                "workflow_id": workflow_id,
                "state": run.state.value,
                "priority": run.spec.priority,
                "waiting_node_id": run.waiting_node_id,
                "nodes": [
                    {
                        "id": node.id,
                        "device_id": node.device_id,
                        "action": node.action_name,
                        "param": dict(node.param),
                        "completed": node.id in run.completed,
                        "dispatched": node.id in run.dispatched,
                    }
                    for node in run.spec.nodes
                ],
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "reschedule_count": self._reschedule_count,
                "workflows": [
                    self.workflow_snapshot(workflow_id)
                    for workflow_id in self._workflows
                ],
                "material_replenishments": dict(self._material_replenishment_by_run),
            }

    def get_workflow_state(self, workflow_id: str) -> WorkflowState:
        with self._lock:
            return self._workflows[workflow_id].state
