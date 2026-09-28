import assert from 'node:assert/strict'
import test from 'node:test'
import { compactTimeline, formatElapsed, mergeTimelineEvent, messageTimeline, toolSubject } from '../src/lib/traceTimeline.ts'

const call = (event_id, status, extra = {}) => ({
  type: 'tool_status', event_id, tool: 'local_shell', label: '运行命令', status, ...extra
})

test('unified arxiv status compacts by job while preserving searches, imports and errors', () => {
  const event = (id, action, status = 'done') => call(id, status, {
    tool: 'arxiv', arguments: { action, job_id: 'j', query: 'RSIAgent' }
  })
  const events = [event('search', 'search'), event('import', 'import'), event('old', 'status'),
    event('error', 'status', 'error'), event('new', 'status')]
  assert.deepEqual(compactTimeline(events).map(e => e.event_id), ['search', 'import', 'error', 'new'])
  assert.equal(toolSubject(call('lookup', 'done', {
    tool: 'arxiv', arguments: { action: 'lookup', arxiv_ids: ['1706.03762', '2501.12345'] }
  })), '1706.03762, 2501.12345')
})

test('parallel completions preserve call order and commentary position', () => {
  let events = []
  events = mergeTimelineEvent(events, { type: 'progress', event_id: 'p1', text: '先检查两个来源。' })
  events = mergeTimelineEvent(events, call('a', 'running', { arguments: { command: 'read a' } }))
  events = mergeTimelineEvent(events, call('b', 'running', { arguments: { command: 'read b' } }))
  events = mergeTimelineEvent(events, call('b', 'done', { duration_ms: 100 }))
  events = mergeTimelineEvent(events, { type: 'progress', event_id: 'p2', text: '第二个来源已返回。' })
  events = mergeTimelineEvent(events, call('a', 'done', { duration_ms: 300 }))
  assert.deepEqual(events.map(item => item.event_id), ['p1', 'a', 'b', 'p2'])
  assert.equal(events[1].arguments.command, 'read a')
  assert.equal(events[2].status, 'done')
})

test('duplicate replay is idempotent and late start cannot regress completion', () => {
  const completed = call('a', 'done', { detail: 'completed' })
  let events = mergeTimelineEvent([], completed)
  events = mergeTimelineEvent(events, completed)
  events = mergeTimelineEvent(events, call('a', 'running'))
  assert.equal(events.length, 1)
  assert.equal(events[0].status, 'done')
})

test('repeated calls with distinct IDs remain separate even for the same query', () => {
  const first = call('a', 'done', { query: 'same' })
  const second = call('b', 'done', { query: 'same' })
  assert.equal(mergeTimelineEvent([first], second).length, 2)
})

test('history replays persisted timeline without inventing completed plan calls', () => {
  const timeline = [{ type: 'progress', text: '已完成来源核对。' }, call('a', 'done')]
  assert.deepEqual(messageTimeline({ trace: { timeline } }), timeline)
  assert.deepEqual(messageTimeline({ toolPlan: { tools: [{ name: 'wiki_open' }] } }), [])
  const legacy = messageTimeline({ trace: { tool_observations: [{ tool: 'wiki_open', status: 'done', summary: 'opened one', items: [] }] } })
  assert.equal(legacy[0].detail, 'opened one')
  assert.equal(legacy[0].type, 'tool_status')
})

test('append-only persisted start and completion events replay as one tool row', () => {
  const timeline = [call('a', 'running'), { type: 'progress', event_id: 'p1', text: '开始核验来源。' }, call('a', 'done')]
  const events = messageTimeline({ trace: { timeline } })
  assert.equal(events.length, 2)
  assert.equal(events[0].status, 'done')
  assert.equal(events[1].text, '开始核验来源。')
})

test('command and explicit arguments take precedence over vague query labels', () => {
  assert.equal(toolSubject(call('a', 'done', { query: 'inspect', arguments: { command: 'Get-Content paper.md' } })), 'Get-Content paper.md')
  assert.equal(toolSubject(call('b', 'done', { arguments: { card_ids: ['p1', 'p2'] } })), 'p1, p2')
})

test('elapsed times include minutes and hours for long research tasks', () => {
  assert.equal(formatElapsed(123000), '2 分 3 秒')
  assert.equal(formatElapsed(3601000), '1 小时 0 分钟')
})

test('overview folds polls by job but retains errors and other tools', () => {
  const poll = (id, job, status = 'done') => call(id, status, {tool: 'arxiv_ingestion_status', arguments: {job_id: job}})
  const events = [poll('a', 'j1'), poll('b', 'j2'), poll('c', 'j1', 'error'), call('read', 'done'), poll('d', 'j1')]
  assert.deepEqual(compactTimeline(events).map(e => e.event_id), ['b', 'c', 'read', 'd'])
  assert.equal(events.length, 5)
})
