export type RecoverableTask = {
  run_id: string
  message: string
  completed_tools: number
  can_resume: boolean
  task_outcome?: { status: string; stop_reason?: string }
  unknown_writes: { id: string; tool: string; arguments: Record<string, unknown> }[]
}

// Ordinary input after an interruption continues the latest unfinished run.
// Commands and explicitly queued new turns remain separate operations.
export function selectContinuation(tasks: RecoverableTask[], requestedRunId = '', automatic = true) {
  const task = requestedRunId ? tasks.find(item => item.run_id === requestedRunId) : automatic ? tasks[0] : undefined
  if (requestedRunId && !task) return { runId: '', error: '任务状态已变化，请刷新后重试。' }
  if (!task) return { runId: '', error: '' }
  if (!task.can_resume) return { runId: task.run_id, error: '上一轮正在停止，请稍后发送。' }
  if (task.unknown_writes.length) return { runId: task.run_id, error: '请先核对上方结果不明的写操作，再继续任务。' }
  return { runId: task.run_id, error: '' }
}
