/** Consume a JSON SSE stream, releasing the reader on completion or session change. */
export async function consumeEventStream<T extends { type: string }>(
  response: Response,
  onEvent: (event: T) => void,
  isCurrent: () => boolean = () => true
): Promise<void> {
  const reader = response.body?.getReader()
  if (!reader) throw new Error('No response body')
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (!isCurrent()) return
      if (done) throw new Error('连接在任务结束前断开')
      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() || ''
      for (const block of blocks) {
        const payload = block.split(/\r?\n/)
          .filter(line => line.startsWith('data:'))
          .map(line => line.slice(5).trimStart()).join('\n')
        if (!payload || payload === '[DONE]') continue
        let event: T
        try {
          const parsed: unknown = JSON.parse(payload)
          if (!parsed || typeof parsed !== 'object' || !('type' in parsed) || typeof parsed.type !== 'string') continue
          event = parsed as T
        } catch {
          continue
        }
        if (event.type === 'done') return
        onEvent(event)
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
