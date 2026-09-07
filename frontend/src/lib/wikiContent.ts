// Internal bookkeeping stays available to the runtime, outside the reading view.
const internalFields = new Set([

    'schema_version',
    'compile_status',
    'compile_error',
    'source_packet_id',
    'raw_source_path',
    'pdf_storage_uri',
    'compiler_model',
    'parser_used',
    'pipeline',
    'extractor_agent',
    'distiller_agent',
    'reviewer_agent',
    'merge_agent',
    'review_status',
    'review_confidence',
    'review_hints',
    'distill_review',
    'source_kind',
    'source_type',
    'source_query_id',
    'source_session_id',
    'source_message_ids',
    'conversation_instruction',
    'related_sources',
    'artifact_uri',
    'maintenance_candidate_id',
    'maintenance_candidate_type',
    'evidence',
    'question',
    'title',
    'import_impact',
    'linked_knowledge',
    'downloaded_images',
    'attachments',
    'image_urls',
    '_ocr_text',
    '_ocr_notes',
    '_ocr_status',
    'source_packet_ids', 'sources', 'aliases', 'links',
    'claims', 'affected_claims', 'compiler', 'markdown_status',
    'review_status_text', 'evidence_updates', 'merge_history',
])

export function isReaderContentField(key: string): boolean {
  const normalized = key.trim().toLowerCase().replace(/[\s-]+/g, '_')
  return !normalized.startsWith('_') && !internalFields.has(normalized)
}

export function hasReaderContent(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (Array.isArray(value)) return value.some(hasReaderContent)
  if (typeof value === 'object') return Object.values(value).some(hasReaderContent)
  const text = String(value).trim()
  return Boolean(text) && !/^[-–—_＊*·.\s]+$/.test(text)
}
