import assert from 'node:assert/strict'
import test from 'node:test'
import { repositoryReviewLabel } from '../src/lib/repositoryReview.ts'

test('author self-check policy adds no review or self-check guarantee', () => {
  const current = {
    policy: 'author_self_check', status: 'not_requested', model_calls: 0,
    independent_status: 'not_run', current_version_reviewed: false,
  }
  assert.equal(repositoryReviewLabel(current), '')
  assert.equal(repositoryReviewLabel({ ...current, status: 'content_changed' }), '')
  // Stale review fields must not imply a new independent approval.
  assert.equal(repositoryReviewLabel({
    ...current, status: 'complete', independent_status: 'passed', current_version_reviewed: true,
  }), '')
})

test('independent approval applies only to the reviewed current version', () => {
  const approved = { status: 'complete', independent_status: 'passed', current_version_reviewed: true }
  assert.equal(repositoryReviewLabel(approved), '独立复核通过')
  for (const change of [
    { current_version_reviewed: false },
    { current_version_reviewed: undefined },
    { independent_status: 'incomplete' },
    { independent_status: 'unknown' },
    { status: 'not_started' },
  ]) {
    assert.equal(repositoryReviewLabel({ ...approved, ...change }), '复核状态未确认')
  }
})

test('author corrections and self-checks stay distinct from independent approval', () => {
  assert.equal(repositoryReviewLabel({
    status: 'author_revised', independent_status: 'changes_requested', current_version_reviewed: false,
  }), '已按复核意见修正')
  assert.equal(repositoryReviewLabel({
    status: 'author_checked', independent_status: 'incomplete', current_version_reviewed: false,
  }), '主模型自查（独立复核未完成）')
  assert.equal(repositoryReviewLabel({
    status: 'incomplete', independent_status: 'incomplete', current_version_reviewed: false,
  }), '独立复核未完成')
  assert.equal(repositoryReviewLabel({
    status: 'author_revised', independent_status: 'passed', current_version_reviewed: false,
  }), '主模型修订（当前版本未独立复核）')
})

test('legacy, missing and contradictory records cannot become approval', () => {
  for (const review of [undefined, null, {}, { status: 'passed' }, {
    status: 'author_revised', independent_status: 'unknown', current_version_reviewed: true,
  }]) {
    assert.equal(repositoryReviewLabel(review), '复核状态未确认')
  }
})

test('changed content never inherits approval from an older frozen revision', () => {
  assert.equal(repositoryReviewLabel({
    status: 'content_changed', independent_status: 'passed', current_version_reviewed: false,
  }), '正文已变更，当前版本未复核')
})
