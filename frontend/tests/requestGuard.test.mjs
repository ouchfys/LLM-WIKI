import assert from 'node:assert/strict'
import test from 'node:test'
import { createRequestGuard } from '../src/lib/requestGuard.ts'

test('deletion invalidates every older list request, including one started during deletion', () => {
  const guard = createRequestGuard()
  const old = guard.begin()
  const duringDelete = guard.begin()
  guard.invalidate()
  assert.equal(guard.isCurrent(old), false)
  assert.equal(guard.isCurrent(duringDelete), false)
  const refreshed = guard.begin()
  assert.equal(guard.isCurrent(refreshed), true)
})

test('late history responses cannot overwrite newer navigation or an empty draft', () => {
  const guard = createRequestGuard()
  const historyA = guard.begin()
  const historyB = guard.begin()
  assert.equal(guard.isCurrent(historyA), false)
  assert.equal(guard.isCurrent(historyB), true)
  const draft = guard.begin()
  assert.equal(guard.isCurrent(historyB), false)
  assert.equal(guard.isCurrent(draft), true)
})
