import type { ChatMessage, ToolStatusEvent, TraceTimelineEvent } from '../types/chat.ts'

/** Keep a call at its start position when its completion arrives out of order. */
export function mergeTimelineEvent(
  events: TraceTimelineEvent[],
  next: TraceTimelineEvent
): TraceTimelineEvent[] {
  const nextEvents = [...events]
  let index = next.event_id
    ? nextEvents.findIndex(item => item.type === next.type && item.event_id === next.event_id)
    : -1
  if (index < 0 && next.type === 'tool_status' && next.status !== 'running' && !next.event_id) {
    index = nextEvents.findIndex(item => item.type === 'tool_status'
      && item.status === 'running' && item.tool === next.tool && item.query === next.query)
  }
  if (index < 0) return [...nextEvents, next]
  const previous = nextEvents[index]
  if (previous.type === 'tool_status' && next.type === 'tool_status') {
    if (previous.status !== 'running' && next.status === 'running') return nextEvents
    nextEvents[index] = {
      ...previous, ...next,
      arguments: next.arguments ?? previous.arguments,
      query: next.query ?? previous.query,
      items: next.items ?? previous.items
    }
  } else {
    nextEvents[index] = next
  }
  return nextEvents
}

/** Old sessions have observations but no commentary; never invent executed plan steps. */
export function messageTimeline(message: ChatMessage): TraceTimelineEvent[] {
  const persisted = message.timeline?.length ? message.timeline : message.trace?.timeline
  if (persisted?.length) return persisted.reduce(mergeTimelineEvent, [])
  if (message.toolEvents?.length) {
    return message.toolEvents.map(({ eventId, ...event }) => ({
      ...event, type: 'tool_status', event_id: eventId
    }))
  }
  return (message.trace?.tool_observations || []).map((item, index) => ({
    ...item,
    type: 'tool_status',
    event_id: item.event_id || `legacy-${index}`,
    label: item.tool,
    status: item.status || 'done',
    detail: item.summary
  }))
}

export function toolSubject(event: ToolStatusEvent): string {
  const args = event.arguments || {}
  if (event.tool === 'repository') return [args.repository, args.path || args.query].filter(Boolean).map(String).join(' · ')
  if (event.tool === 'wiki_write') return String(args.title || args.repository || event.query || '')
  const value = args.command || args.arxiv_id || args.arxiv_ids || args.paper_id || args.job_id || args.query || args.url
    || args.card_ids || args.task_id || args.job_id || event.query
  if (Array.isArray(value)) return value.join(', ')
  return value == null ? '' : String(value)
}

export function formatElapsed(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))} 毫秒`
  const totalSeconds = Math.floor(milliseconds / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`
  return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`
}

/** The complete audit remains available; the overview keeps the latest poll per job.
 * Errors always remain visible, even if a later poll succeeds.
 */
export function compactTimeline(events: TraceTimelineEvent[]): TraceTimelineEvent[] {
  const isIngestionStatus = (event: ToolStatusEvent) => event.tool === 'arxiv_ingestion_status'
    || (event.tool === 'arxiv' && event.arguments?.action === 'status')
  const last = new Map<string, number>()
  events.forEach((event, index) => {
    if (event.type === 'tool_status' && isIngestionStatus(event) && event.status !== 'error') {
      const id = event.arguments?.job_id
      if (id) last.set(String(id), index)
    }
  })
  return events.filter((event, index) => {
    if (event.type !== 'tool_status' || !isIngestionStatus(event) || event.status === 'error') return true
    const id = event.arguments?.job_id
    return !id || last.get(String(id)) === index
  })
}
