import schedulerWorkflow from '../../szlab_robot_action_workflow.scheduler.json';

/** Convert the scheduler's canonical serial DAG into the local canvas format. */
export function createSzlabPresetFlow() {
  const source = schedulerWorkflow as {
    workflow_id?: string;
    priority?: string;
    nodes?: Array<{
      id: string;
      device_id?: string;
      action_name: string;
      param?: Record<string, unknown>;
    }>;
  };
  return {
    name: source.workflow_id || 'szlab_robot_action_workflow',
    priority: source.priority || 'normal',
    rules: [{
      trigger: { type: 'manual' },
      actions: (source.nodes || []).map((node, index) => ({
        action: {
          index: index + 1,
          workflow_node_id: node.id,
          device_id: node.device_id || '',
          method: node.action_name,
          params: node.param || {},
        },
      })),
    }],
  };
}
