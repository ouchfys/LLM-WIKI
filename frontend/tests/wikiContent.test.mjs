import assert from 'node:assert/strict'
import test from 'node:test'
import { isReaderContentField, hasReaderContent } from '../src/lib/wikiContent.ts'

test('QServe reader keeps knowledge but hides internal audit fields', () => {
  const stored = {
    problem: 'INT4 dequantization overhead', key_idea: 'Algorithm and system co-design',
    method: 'W4A8KV4', results: 'Measured performance', limitations: 'GPU-dependent',
    markdown_status: 'reviewed', sources: [{ uri: 'file://private.pdf' }],
    source_packet_ids: ['packet-id'], claims: [{ id: 'clm-1' }],
    affected_claims: [{ action: 'add_claim' }], compiler: { name: 'compiler' },
    review_status_text: 'reviewer: unreviewed', evidence_updates: [], merge_history: []
  }
  assert.deepEqual(Object.keys(stored).filter(isReaderContentField), ['problem', 'key_idea', 'method', 'results', 'limitations'])
  assert.equal(stored.claims.length, 1, 'source metadata must stay intact')
})

test('legacy heading forms cannot bypass the reader filter', () => {
  for (const key of ['Markdown Status', 'source packet ids', 'affected-claims', ' compiler ', '_private']) {
    assert.equal(isReaderContentField(key), false, key)
  }
})

test('empty notes disappear without dropping meaningful values or modifying stored content', () => {
  for (const value of ['', ' ', '-', '—', '***', ['-', ' '], { note: '-' }, null]) {
    assert.equal(hasReaderContent(value), false)
  }
  for (const value of [0, false, 'No benefit observed', '- 4-bit weights', { note: '人工补充的阅读心得' }]) {
    assert.equal(hasReaderContent(value), true)
  }
})
