import assert from 'node:assert/strict'
import test from 'node:test'
import { selectContinuation } from '../src/lib/chatContinuation.ts'

const task = (run_id, changes = {}) => ({ run_id, message: 'Read eleven papers', completed_tools: 4, can_resume: true, unknown_writes: [], ...changes })

test('ordinary input continues the latest interrupted task; commands and queues do not', () => {
  const tasks = [task('latest'), task('older')]
  assert.deepEqual(selectContinuation(tasks), { runId: 'latest', error: '' })
  assert.equal(selectContinuation(tasks, '', false).runId, '')
  assert.equal(selectContinuation(tasks, 'older', false).runId, 'older')
  assert.equal(selectContinuation([]).runId, '')
})

test('uncertain writes, stopping workers and stale selections cannot silently start a new run', () => {
  for (const changes of [{ can_resume: false }, { unknown_writes: [{ id: 'c', tool: 'local_shell', arguments: {} }] }]) {
    const selection = selectContinuation([task('r', changes)])
    assert.equal(selection.runId, 'r')
    assert.ok(selection.error)
  }
  assert.ok(selectContinuation([], 'deleted').error)
})
