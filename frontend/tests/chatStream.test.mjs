import assert from 'node:assert/strict'
import test from 'node:test'
import { consumeEventStream } from '../src/lib/chatStream.ts'

function stream(text, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text)
  let cancelled = false
  const body = new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize))
      controller.close()
    },
    cancel() { cancelled = true }
  })
  return { response: new Response(body), body, cancelled: () => cancelled }
}

test('decodes split UTF-8, CRLF and multiline events; stops at done', async () => {
  const source = stream(': heartbeat\r\n\r\ndata: {"type":"token",\r\ndata: "text":"中文回答"}\r\n\r\ndata: {"type":"done"}\n\ndata: {"type":"token","text":"late"}\n\n')
  const events = []
  await consumeEventStream(source.response, event => events.push(event))
  assert.deepEqual(events, [{ type: 'token', text: '中文回答' }])
  assert.equal(source.body.locked, false)
  assert.equal(source.cancelled(), true)
})

test('reports premature disconnect rather than successful completion', async () => {
  const source = stream('data: {"type":"token","text":"partial"}\n\n')
  await assert.rejects(consumeEventStream(source.response, () => {}), /连接在任务结束前断开/)
  assert.equal(source.body.locked, false)
})

test('ignores obsolete sessions and releases their stream', async () => {
  const source = stream('data: {"type":"token","text":"old session"}\n\n')
  await consumeEventStream(source.response, () => assert.fail('obsolete event delivered'), () => false)
  assert.equal(source.body.locked, false)
  assert.equal(source.cancelled(), true)
})

test('ignores malformed and untyped payloads without hiding valid events', async () => {
  const source = stream('data: nope\n\ndata: null\n\ndata: {}\n\ndata: {"type":"phase","phase":"answering"}\n\ndata: {"type":"done"}\n\n', 8)
  const events = []
  await consumeEventStream(source.response, event => events.push(event))
  assert.deepEqual(events, [{ type: 'phase', phase: 'answering' }])
})
