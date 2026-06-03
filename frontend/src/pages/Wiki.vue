<template>
  <section class="chat-scene">
    <div class="chat-window">
      <header class="chat-window-head">
        <div class="window-dots">
          <span></span>
          <span></span>
          <span></span>
        </div>
        <div class="chat-head-copy">
          <strong>{{ displaySessionTitle(currentSessionTitle) }}</strong>
          <span>{{ messages.length > 1 ? `${messages.length} 条消息` : '私人知识库对话' }}</span>
        </div>
        <div class="chat-head-side">
          <span v-if="activeCardTitle" class="active-context">{{ activeCardTitle }}</span>
        </div>
      </header>

      <div class="prompt-strip">
        <button
          v-for="prompt in quickPrompts"
          :key="prompt"
          type="button"
          class="prompt-chip"
          @click="draft = prompt"
        >
          {{ prompt }}
        </button>
      </div>

      <div ref="threadRef" class="chat-thread">
        <article
          v-for="message in messages"
          :key="message.id"
          class="chat-message"
          :class="message.role"
        >
          <div class="message-avatar">{{ message.role === 'assistant' ? 'J' : '你' }}</div>

          <div class="message-stack">
            <div class="message-role">{{ message.role === 'assistant' ? 'Jarvis' : '你' }}</div>
            <div class="bubble">
              <div v-if="message.toolEvents?.length" class="tool-trace">
                <div
                  v-for="event in message.toolEvents"
                  :key="event.tool"
                  class="tool-step"
                  :class="event.status"
                >
                  <span class="tool-dot"></span>
                  <div>
                    <strong>{{ event.label }}</strong>
                    <small>{{ event.detail || (event.status === 'running' ? '运行中' : '完成') }}</small>
                  </div>
                </div>
              </div>

              <div v-if="message.toolPlan || message.trace" class="agent-trace-panel">
                <div class="trace-head">
                  <strong>Agent Trace</strong>
                  <span>{{ traceSummary(message) }}</span>
                </div>

                <div v-if="message.toolPlan?.tools?.length" class="trace-tools">
                  <div
                    v-for="tool in message.toolPlan.tools"
                    :key="`${message.id}-${tool.name}-${tool.query}`"
                    class="trace-tool-chip"
                  >
                    <strong>{{ tool.name }}</strong>
                    <span>{{ tool.reason || tool.query }}</span>
                  </div>
                </div>

                <div v-if="message.trace?.retrieved_cards?.length" class="trace-card-grid">
                  <button
                    v-for="card in message.trace.retrieved_cards"
                    :key="card.card_id"
                    type="button"
                    class="trace-card"
                    @click="openTraceCard(card)"
                  >
                    <span>{{ card.page_type }}</span>
                    <strong>{{ card.title }}</strong>
                    <small>{{ card.matched_chunks?.[0] || card.summary }}</small>
                  </button>
                </div>

                <div v-if="message.trace?.web_results?.length || message.trace?.resources?.length" class="trace-extra">
                  <span v-if="message.trace?.web_results?.length">Web {{ message.trace.web_results.length }}</span>
                  <span v-if="message.trace?.resources?.length">Resources {{ message.trace.resources.length }}</span>
                </div>
              </div>

              <div class="message-text" v-html="renderMarkdown(message.content)"></div>

              <div v-if="message.citations?.length" class="citation-list">
                <button
                  v-for="citation in message.citations"
                  :key="citation.card_id"
                  type="button"
                  @click="openCitation(citation)"
                >
                  {{ citation.title }}
                </button>
              </div>

              <div v-if="message.resources?.length" class="resource-list">
                <a
                  v-for="resource in message.resources"
                  :key="resource.url"
                  :href="resource.url"
                  target="_blank"
                  rel="noreferrer"
                >
                  <span>{{ resourceLabel(resource.category) }}</span>
                  <strong>{{ resource.title }}</strong>
                </a>
              </div>

              <div v-if="message.profileUpdates?.length" class="profile-update-list">
                <span v-for="item in message.profileUpdates" :key="`${item.signal_type}-${item.value}`">
                  画像更新：{{ profileLabel(item.signal_type) }} / {{ item.value }}
                </span>
              </div>
            </div>
          </div>
        </article>
      </div>

      <div v-if="activeCardTitle" class="context-banner">
        <span>当前引用</span>
        <strong>{{ activeCardTitle }}</strong>
      </div>

      <form class="chat-composer" @submit.prevent="send">
        <n-input
          ref="composeInput"
          v-model:value="draft"
          type="textarea"
          class="composer-input"
          placeholder="问你的知识库，例如：帮我把最近保存的面经整理成一段面试表达。"
          :autosize="{ minRows: 2, maxRows: 6 }"
          @keydown="handleComposeKeydown"
        />
        <div class="composer-actions">
          <button type="button" class="composer-ghost" @click="draft = ''">清空</button>
          <n-button type="primary" attr-type="submit" :loading="sending" :disabled="!draft.trim()">
            发送
          </n-button>
        </div>
      </form>
    </div>

    <n-modal
      v-model:show="rawModalVisible"
      preset="card"
      style="width: 920px; max-width: 95vw;"
      :bordered="false"
    >
      <template #header>
        <div class="modal-head">
          <span>{{ rawModalTitle }}</span>
          <n-tag size="small">Markdown Source</n-tag>
        </div>
      </template>
      <pre class="raw-markdown-viewer"><code>{{ rawMarkdown }}</code></pre>
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NInput, NModal, NTag } from 'naive-ui'
import { api } from '../api'

type Citation = {
  card_id: string
  title: string
  page_type: string
  summary: string
  markdown_path: string
}

type LearningResource = {
  category: string
  title: string
  url: string
  snippet?: string
}

type ToolEvent = {
  tool: string
  label: string
  status: 'running' | 'done' | 'error'
  detail?: string
}

type ToolPlan = {
  intent?: string
  answer_mode?: string
  use_wiki?: boolean
  use_web?: boolean
  use_resources?: boolean
  open_cards?: boolean
  tools?: Array<{ name: string; query: string; reason?: string }>
}

type TraceCard = {
  card_id: string
  title: string
  page_type: string
  summary: string
  markdown_path: string
  matched_chunks?: string[]
}

type AgentTrace = {
  tool_plan?: ToolPlan
  retrieved_cards?: TraceCard[]
  web_results?: Array<{ title: string; url: string; snippet?: string }>
  resources?: LearningResource[]
  diagnostics?: {
    wiki_card_count?: number
    web_result_count?: number
    resource_count?: number
  }
}

type ChatMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  resources?: LearningResource[]
  toolEvents?: ToolEvent[]
  toolPlan?: ToolPlan
  trace?: AgentTrace
  profileUpdates?: Array<{ signal_type: string; value: string }>
}

type SseChunk =
  | { type: 'card_list'; citations?: Citation[] }
  | { type: 'resource_list'; resources?: LearningResource[] }
  | { type: 'tool_plan'; plan?: ToolPlan }
  | { type: 'agent_trace'; trace?: AgentTrace }
  | { type: 'tool_status'; tool: string; label: string; status: 'running' | 'done' | 'error'; detail?: string }
  | { type: 'token'; text?: string }
  | { type: 'profile'; updates?: Array<{ signal_type: string; value: string }> }
  | { type: 'error'; message?: string }
  | { type: 'done' }

const route = useRoute()
const router = useRouter()
const composeInput = ref<any>(null)
const threadRef = ref<HTMLElement | null>(null)
const historyIndex = ref(-1)
const SESSION_STORAGE_KEY = 'wiki_chat_session_id'

const draft = ref('')
const sending = ref(false)
const currentSessionId = ref('')
const currentSessionTitle = ref('新对话')
const activeCitation = ref<Citation | null>(null)
const rawModalVisible = ref(false)
const rawModalTitle = ref('')
const rawMarkdown = ref('')

const quickPrompts = [
  '总结我最近记录的面经要点。',
  '把我的 RAG 知识整理成项目表达。',
  '我最近保存了哪些 Agent 相关内容？'
]

const initialMessages: ChatMessage[] = [
  {
    id: 1,
    role: 'assistant',
    content:
      '我已经连到你的个人知识库。直接问我论文、面经、博客或截图里的内容，我会优先从你已经保存的资料中组织答案。'
  }
]

const messages = ref<ChatMessage[]>([...initialMessages])

const activeCardTitle = computed(() => activeCitation.value?.title || '')

function scrollThreadToBottom() {
  nextTick(() => {
    const el = threadRef.value
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  })
}

async function createSession(syncRoute = true) {
  const { data } = await api.post('/wiki/sessions', { title: '新对话' })
  currentSessionId.value = data.id
  currentSessionTitle.value = data.title || '新对话'
  localStorage.setItem(SESSION_STORAGE_KEY, data.id)
  messages.value = [...initialMessages]
  activeCitation.value = null
  scrollThreadToBottom()
  if (syncRoute) {
    await router.replace({
      path: '/',
      query: { session: data.id }
    })
  }
}

async function loadSessionHistory(sessionId: string) {
  const { data } = await api.get(`/wiki/sessions/${sessionId}/messages`)
  currentSessionId.value = sessionId
  currentSessionTitle.value = data.session?.title || '对话'
  messages.value = data.items?.length ? normalizeMessages(data.items) : [...initialMessages]
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
  scrollThreadToBottom()
}

function normalizeMessages(items: any[]): ChatMessage[] {
  return items.map((item) => ({
    id: item.id,
    role: item.role,
    content: item.content,
    citations: item.citations || [],
    resources: item.resources || [],
    toolEvents: [],
    toolPlan: item.tool_plan || undefined,
    trace: item.trace || undefined,
    profileUpdates: item.profile_updates || []
  }))
}

async function ensureSession() {
  const routeSession = typeof route.query.session === 'string' ? route.query.session : ''
  const saved = routeSession || localStorage.getItem(SESSION_STORAGE_KEY) || ''

  if (!saved) {
    await createSession(true)
    return
  }

  try {
    await loadSessionHistory(saved)
    if (routeSession !== saved) {
      await router.replace({ path: '/', query: { session: saved } })
    }
  } catch {
    await createSession(true)
  }
}

function openCitation(citation: Citation) {
  activeCitation.value = citation
}

function openTraceCard(card: TraceCard) {
  activeCitation.value = {
    card_id: card.card_id,
    title: card.title,
    page_type: card.page_type,
    summary: card.summary,
    markdown_path: card.markdown_path
  }
}

function traceSummary(message: ChatMessage) {
  const trace = message.trace
  const plan = message.toolPlan || trace?.tool_plan
  const parts = []
  if (plan?.use_wiki) parts.push('Wiki')
  if (plan?.use_web) parts.push('Web')
  if (plan?.use_resources) parts.push('Resources')
  const cardCount = trace?.diagnostics?.wiki_card_count ?? trace?.retrieved_cards?.length ?? 0
  if (cardCount) parts.push(`${cardCount} cards`)
  return parts.join(' / ') || 'No trace'
}

function handleComposeKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
    return
  }

  if (event.key === 'ArrowUp' && !draft.value.trim()) {
    event.preventDefault()
    const userMessages = messages.value.filter((message) => message.role === 'user')
    if (!userMessages.length) return
    historyIndex.value = Math.min(historyIndex.value + 1, userMessages.length - 1)
    draft.value = userMessages[userMessages.length - 1 - historyIndex.value].content
    return
  }

  if (event.key === 'ArrowDown' && historyIndex.value >= 0) {
    event.preventDefault()
    const userMessages = messages.value.filter((message) => message.role === 'user')
    historyIndex.value = Math.max(historyIndex.value - 1, -1)
    draft.value = historyIndex.value === -1 ? '' : userMessages[userMessages.length - 1 - historyIndex.value].content
    return
  }

  historyIndex.value = -1
}

function handleStreamChunk(chunk: SseChunk, assistantMessage: ChatMessage) {
  if (chunk.type === 'tool_plan') {
    assistantMessage.toolPlan = chunk.plan
    return
  }

  if (chunk.type === 'card_list') {
    assistantMessage.citations = chunk.citations || []
    if (chunk.citations?.[0]) {
      activeCitation.value = chunk.citations[0]
    } else {
      activeCitation.value = null
    }
    return
  }

  if (chunk.type === 'agent_trace') {
    assistantMessage.trace = chunk.trace
    if (!assistantMessage.toolPlan && chunk.trace?.tool_plan) {
      assistantMessage.toolPlan = chunk.trace.tool_plan
    }
    return
  }

  if (chunk.type === 'tool_status') {
    const events = assistantMessage.toolEvents || []
    const index = events.findIndex((item) => item.tool === chunk.tool)
    const nextEvent: ToolEvent = {
      tool: chunk.tool,
      label: chunk.label,
      status: chunk.status,
      detail: chunk.detail
    }
    if (index >= 0) {
      events[index] = nextEvent
    } else {
      events.push(nextEvent)
    }
    assistantMessage.toolEvents = [...events]
    scrollThreadToBottom()
    return
  }

  if (chunk.type === 'resource_list') {
    assistantMessage.resources = chunk.resources || []
    return
  }

  if (chunk.type === 'token') {
    assistantMessage.content += chunk.text || ''
    scrollThreadToBottom()
    return
  }

  if (chunk.type === 'profile') {
    assistantMessage.profileUpdates = chunk.updates || []
    return
  }

  if (chunk.type === 'error' && chunk.message) {
    assistantMessage.content += `\n${chunk.message}`
  }
}

async function consumeSseResponse(response: Response, assistantMessage: ChatMessage) {
  const reader = response.body?.getReader()
  if (!reader) {
    throw new Error('No response body')
  }

  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split(/\r?\n\r?\n/)
    buffer = events.pop() || ''

    for (const eventBlock of events) {
      const lines = eventBlock
        .split(/\r?\n/)
        .map((line) => line.trimEnd())
        .filter(Boolean)

      const dataLines = lines
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())

      if (!dataLines.length) continue

      const payload = dataLines.join('\n')
      if (!payload || payload === '[DONE]') continue

      let chunk: SseChunk
      try {
        chunk = JSON.parse(payload)
      } catch {
        continue
      }

      if (chunk.type === 'done') {
        return
      }

      handleStreamChunk(chunk, assistantMessage)
    }
  }
}

async function send() {
  historyIndex.value = -1
  const text = draft.value.trim()
  if (!text || sending.value) return

  messages.value.push({ id: Date.now(), role: 'user', content: text })
  draft.value = ''
  sending.value = true
  activeCitation.value = null
  scrollThreadToBottom()

  const assistantId = Date.now() + 1
  const assistantMessageDraft: ChatMessage = {
    id: assistantId,
    role: 'assistant',
    content: '',
    citations: [],
    resources: [],
    toolEvents: [],
    toolPlan: undefined,
    trace: undefined,
    profileUpdates: []
  }
  messages.value.push(assistantMessageDraft)
  const assistantMessage = messages.value[messages.value.length - 1]
  scrollThreadToBottom()

  try {
    const response = await fetch('/api/wiki/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream'
      },
      body: JSON.stringify({
        message: text,
        session_id: currentSessionId.value,
        stream: true
      })
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    await consumeSseResponse(response, assistantMessage)

    if (!assistantMessage.content.trim()) {
      assistantMessage.content = '这次没有拿到有效回答，请重试。'
    }
  } catch (error) {
    console.error('[WikiChat] stream failed:', error)
    assistantMessage.content = '请求失败，请稍后重试。'
  } finally {
    sending.value = false
    scrollThreadToBottom()
  }
}

function profileLabel(value: string) {
  return {
    interest: '兴趣',
    weak_point: '薄弱点',
    preference: '偏好',
    goal: '目标'
  }[value] || value
}

function resourceLabel(value: string) {
  return {
    paper: '论文',
    video: '视频',
    interview_post: '面经'
  }[value] || '资料'
}

function escapeHtml(value: string) {
  return (value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderInlineMarkdown(value: string) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
}

function flushList(html: string[], listItems: string[], ordered: boolean) {
  if (!listItems.length) return
  const tag = ordered ? 'ol' : 'ul'
  html.push(`<${tag}>${listItems.map((item) => `<li>${item}</li>`).join('')}</${tag}>`)
  listItems.length = 0
}

function renderMarkdown(markdown: string) {
  const lines = (markdown || '').replace(/\r\n/g, '\n').split('\n')
  const html: string[] = []
  const listItems: string[] = []
  const tableRows: string[][] = []
  const codeLines: string[] = []
  let ordered = false
  let inCodeBlock = false

  for (const rawLine of lines) {
    const line = rawLine.trim()

    if (line.startsWith('```')) {
      if (inCodeBlock) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
        codeLines.length = 0
        inCodeBlock = false
      } else {
        flushList(html, listItems, ordered)
        flushTable(html, tableRows)
        inCodeBlock = true
      }
      continue
    }

    if (inCodeBlock) {
      codeLines.push(rawLine)
      continue
    }

    if (!line) {
      flushList(html, listItems, ordered)
      flushTable(html, tableRows)
      continue
    }
    if (isMarkdownTableSeparator(line)) {
      continue
    }
    if (isMarkdownTableRow(line)) {
      flushList(html, listItems, ordered)
      tableRows.push(parseMarkdownTableRow(line))
      continue
    }
    if (/^---+$/.test(line)) {
      flushList(html, listItems, ordered)
      flushTable(html, tableRows)
      html.push('<hr />')
      continue
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/)
    if (heading) {
      flushList(html, listItems, ordered)
      flushTable(html, tableRows)
      const level = Math.min(heading[1].length + 2, 5)
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`)
      continue
    }
    const unordered = line.match(/^[-*]\s+(.+)$/)
    if (unordered) {
      if (ordered) flushList(html, listItems, ordered)
      ordered = false
      listItems.push(renderInlineMarkdown(unordered[1]))
      continue
    }
    const orderedMatch = line.match(/^\d+\.\s+(.+)$/)
    if (orderedMatch) {
      if (!ordered) flushList(html, listItems, ordered)
      ordered = true
      listItems.push(renderInlineMarkdown(orderedMatch[1]))
      continue
    }
    if (line.startsWith('>')) {
      flushList(html, listItems, ordered)
      flushTable(html, tableRows)
      html.push(`<blockquote>${renderInlineMarkdown(line.replace(/^>\s*/, ''))}</blockquote>`)
      continue
    }
    flushList(html, listItems, ordered)
    flushTable(html, tableRows)
    html.push(`<p>${renderInlineMarkdown(line)}</p>`)
  }

  if (inCodeBlock) {
    html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`)
  }
  flushList(html, listItems, ordered)
  flushTable(html, tableRows)
  return html.join('')
}

function isMarkdownTableRow(line: string) {
  return /^\|.*\|$/.test(line) && line.split('|').length >= 4
}

function isMarkdownTableSeparator(line: string) {
  return /^\|\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|$/.test(line)
}

function parseMarkdownTableRow(line: string) {
  return line
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => renderInlineMarkdown(cell.trim()))
}

function flushTable(html: string[], rows: string[][]) {
  if (!rows.length) return
  const [head, ...body] = rows
  const bodyRows = body.length ? body : []
  html.push(
    `<div class="markdown-table-wrap"><table><thead><tr>${head
      .map((cell) => `<th>${cell}</th>`)
      .join('')}</tr></thead><tbody>${bodyRows
      .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join('')}</tr>`)
      .join('')}</tbody></table></div>`
  )
  rows.length = 0
}

function displaySessionTitle(value: string) {
  const text = (value || '').trim()
  if (!text) return '未命名会话'
  if (/^[?\uFFFD\s]+$/.test(text)) return '历史异常会话'
  return text
}

function applyRoutePrompt() {
  const ask = typeof route.query.ask === 'string' ? route.query.ask : ''
  if (ask) {
    draft.value = ask
  }
}

watch(
  () => route.query.new,
  async (value) => {
    if (!value) return
    await createSession(true)
  }
)

watch(
  () => route.query.session,
  async (value) => {
    const sessionId = typeof value === 'string' ? value : ''
    if (!sessionId || sessionId === currentSessionId.value) return
    await loadSessionHistory(sessionId)
  }
)

watch(() => route.query.ask, applyRoutePrompt)

onMounted(async () => {
  await ensureSession()
  applyRoutePrompt()
  scrollThreadToBottom()
})
</script>

<style scoped>
.chat-scene {
  height: 100%;
}

.chat-window {
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto auto;
  gap: 16px;
  height: calc(100vh - 84px);
  padding: 16px;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 24px;
  background:
    radial-gradient(circle at top right, rgba(59, 130, 246, 0.1), transparent 20%),
    linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(8, 12, 21, 0.98));
  box-shadow: 0 18px 48px rgba(2, 6, 23, 0.28);
}

.chat-window-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  padding: 8px 10px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
}

.window-dots {
  display: flex;
  gap: 6px;
}

.window-dots span {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.2);
}

.window-dots span:first-child {
  background: rgba(248, 113, 113, 0.7);
}

.window-dots span:nth-child(2) {
  background: rgba(245, 185, 66, 0.7);
}

.window-dots span:nth-child(3) {
  background: rgba(54, 211, 153, 0.7);
}

.chat-head-copy strong,
.chat-head-copy span {
  display: block;
}

.chat-head-copy strong {
  font-size: 14px;
}

.chat-head-copy span {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.chat-head-side {
  display: flex;
  justify-content: flex-end;
}

.active-context {
  max-width: 320px;
  padding: 6px 10px;
  border: 1px solid rgba(96, 165, 250, 0.18);
  border-radius: 999px;
  background: rgba(59, 130, 246, 0.08);
  color: #bfdbfe;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prompt-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 0 4px;
}

.prompt-chip {
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--text-soft);
  cursor: pointer;
}

.prompt-chip:hover {
  border-color: rgba(96, 165, 250, 0.22);
  background: rgba(59, 130, 246, 0.08);
}

.chat-thread {
  display: flex;
  flex-direction: column;
  gap: 18px;
  overflow: auto;
  padding: 8px 4px 8px 2px;
}

.chat-message {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.chat-message.user {
  grid-template-columns: minmax(0, 1fr) 42px;
  justify-items: stretch;
}

.chat-message.user .message-stack {
  grid-column: 1;
  width: fit-content;
  max-width: min(88%, 840px);
  margin-left: auto;
}

.chat-message.user .message-avatar {
  grid-column: 2;
  background: linear-gradient(180deg, rgba(168, 85, 247, 0.9), rgba(96, 65, 255, 0.9));
}

.chat-message.user .message-stack {
  align-items: flex-end;
}

.chat-message.user .message-role {
  align-self: flex-end;
  text-align: right;
}

.message-avatar {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border-radius: 999px;
  background: linear-gradient(180deg, rgba(59, 130, 246, 0.9), rgba(37, 99, 235, 0.9));
  color: #ffffff;
  font-size: 13px;
  font-weight: 700;
  box-shadow: 0 0 18px rgba(59, 130, 246, 0.24);
}

.message-stack {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  width: min(80%, 840px);
  max-width: min(80%, 840px);
}

.message-role {
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.bubble {
  padding: 14px 16px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(10px);
}

.chat-message.user .bubble {
  width: auto;
  max-width: 100%;
  background: linear-gradient(180deg, rgba(30, 41, 59, 0.96), rgba(15, 23, 42, 0.96));
}

.chat-message.assistant .bubble {
  box-shadow: 0 0 28px rgba(59, 130, 246, 0.08);
}

.message-text {
  color: var(--text);
  word-break: break-word;
  line-height: 1.75;
  font-size: 14px;
}

.message-text :deep(p) {
  margin: 0 0 10px;
}

.message-text :deep(p:last-child) {
  margin-bottom: 0;
}

.message-text :deep(h3),
.message-text :deep(h4),
.message-text :deep(h5) {
  margin: 12px 0 8px;
  color: #f8fafc;
  font-size: 15px;
  line-height: 1.45;
}

.message-text :deep(ul),
.message-text :deep(ol) {
  margin: 6px 0 12px;
  padding-left: 22px;
}

.message-text :deep(li) {
  margin: 4px 0;
}

.message-text :deep(strong) {
  color: #ffffff;
  font-weight: 700;
}

.message-text :deep(code) {
  padding: 1px 5px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 6px;
  background: rgba(15, 23, 42, 0.82);
  color: #bfdbfe;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 0.92em;
}

.message-text :deep(pre) {
  overflow: auto;
  margin: 10px 0 14px;
  padding: 12px 14px;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 12px;
  background: rgba(2, 6, 23, 0.5);
}

.message-text :deep(pre code) {
  display: block;
  padding: 0;
  border: 0;
  background: transparent;
  white-space: pre;
}

.message-text :deep(.markdown-table-wrap) {
  overflow: auto;
  margin: 10px 0 14px;
  border: 1px solid rgba(148, 163, 184, 0.16);
  border-radius: 12px;
}

.message-text :deep(table) {
  width: 100%;
  min-width: 520px;
  border-collapse: collapse;
  background: rgba(15, 23, 42, 0.42);
}

.message-text :deep(th),
.message-text :deep(td) {
  padding: 10px 12px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.12);
  text-align: left;
  vertical-align: top;
}

.message-text :deep(th) {
  color: #dbeafe;
  font-size: 13px;
  font-weight: 700;
  background: rgba(59, 130, 246, 0.08);
}

.message-text :deep(td) {
  color: var(--text-soft);
}

.message-text :deep(tr:last-child td) {
  border-bottom: 0;
}

.message-text :deep(blockquote) {
  margin: 8px 0 12px;
  padding: 8px 12px;
  border-left: 3px solid rgba(96, 165, 250, 0.55);
  background: rgba(59, 130, 246, 0.08);
  color: var(--text-soft);
}

.message-text :deep(hr) {
  height: 1px;
  margin: 16px 0;
  border: 0;
  background: rgba(148, 163, 184, 0.14);
}

.message-text :deep(a) {
  color: #93c5fd;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.tool-trace {
  display: grid;
  gap: 8px;
  margin-bottom: 12px;
}

.tool-step {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr);
  gap: 9px;
  align-items: start;
  padding: 8px 10px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.48);
}

.tool-dot {
  width: 8px;
  height: 8px;
  margin-top: 6px;
  border-radius: 999px;
  background: #f59e0b;
  box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.12);
}

.tool-step.done .tool-dot {
  background: #10b981;
  box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.12);
}

.tool-step.error .tool-dot {
  background: #ef4444;
  box-shadow: 0 0 0 4px rgba(239, 68, 68, 0.12);
}

.tool-step strong,
.tool-step small {
  display: block;
}

.tool-step strong {
  color: #dbeafe;
  font-size: 12px;
}

.tool-step small {
  margin-top: 2px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.agent-trace-panel {
  display: grid;
  gap: 10px;
  margin-bottom: 12px;
  padding: 10px;
  border: 1px solid rgba(96, 165, 250, 0.16);
  border-radius: 14px;
  background:
    linear-gradient(180deg, rgba(15, 23, 42, 0.78), rgba(2, 6, 23, 0.42));
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.trace-head strong {
  color: #dbeafe;
  font-size: 12px;
}

.trace-head span {
  color: #93c5fd;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
}

.trace-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.trace-tool-chip {
  display: grid;
  gap: 3px;
  max-width: 260px;
  padding: 7px 9px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 10px;
  background: rgba(15, 23, 42, 0.58);
}

.trace-tool-chip strong {
  color: #f8fafc;
  font-size: 11px;
}

.trace-tool-chip span {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
}

.trace-card {
  display: grid;
  gap: 5px;
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid rgba(96, 165, 250, 0.14);
  border-radius: 12px;
  background: rgba(59, 130, 246, 0.06);
  text-align: left;
  cursor: pointer;
}

.trace-card:hover {
  border-color: rgba(96, 165, 250, 0.28);
  background: rgba(59, 130, 246, 0.1);
}

.trace-card span {
  color: #93c5fd;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.trace-card strong {
  overflow: hidden;
  color: #f8fafc;
  font-size: 12px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-card small {
  display: -webkit-box;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.trace-extra {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.trace-extra span {
  padding: 4px 8px;
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.1);
  color: #bbf7d0;
  font-size: 11px;
}

.citation-list,
.resource-list,
.profile-update-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.citation-list button {
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid rgba(96, 165, 250, 0.18);
  border-radius: 999px;
  background: rgba(59, 130, 246, 0.08);
  color: #bfdbfe;
  cursor: pointer;
}

.citation-list button:hover {
  background: rgba(59, 130, 246, 0.14);
}

.resource-list {
  align-items: stretch;
}

.resource-list a {
  display: grid;
  gap: 4px;
  max-width: min(100%, 420px);
  padding: 9px 11px;
  border: 1px solid rgba(16, 185, 129, 0.18);
  border-radius: 14px;
  background: rgba(16, 185, 129, 0.08);
  color: #d1fae5;
}

.resource-list a:hover {
  border-color: rgba(16, 185, 129, 0.3);
  background: rgba(16, 185, 129, 0.12);
}

.resource-list span {
  color: #86efac;
  font-size: 11px;
  font-weight: 700;
}

.resource-list strong {
  overflow: hidden;
  color: #ecfdf5;
  font-size: 13px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-update-list span {
  padding: 5px 10px;
  border-radius: 999px;
  background: rgba(16, 185, 129, 0.12);
  color: #bbf7d0;
  font-size: 12px;
}

.context-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid rgba(96, 165, 250, 0.16);
  border-radius: 16px;
  background: rgba(59, 130, 246, 0.06);
}

.context-banner span {
  color: var(--text-muted);
  font-size: 12px;
}

.context-banner strong {
  font-size: 13px;
}

.chat-composer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: end;
  padding: 12px;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.03);
}

.composer-input {
  min-width: 0;
  width: 100%;
}

.composer-input :deep(.n-input) {
  width: 100%;
}

.composer-input :deep(.n-input-wrapper) {
  padding: 0;
  border-radius: 16px;
  background: rgba(11, 18, 32, 0.92);
}

.composer-input :deep(.n-input__textarea) {
  width: 100%;
}

.composer-input :deep(textarea) {
  width: 100%;
  min-height: 56px;
  padding: 14px 16px;
  color: var(--text);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  writing-mode: horizontal-tb;
  text-orientation: mixed;
  resize: none;
}

.composer-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.composer-ghost {
  min-height: 38px;
  padding: 0 12px;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.composer-ghost:hover {
  color: #ffffff;
}

@media (max-width: 860px) {
  .chat-window {
    height: auto;
    min-height: calc(100vh - 160px);
  }

  .chat-window-head,
  .chat-composer {
    grid-template-columns: 1fr;
  }

  .chat-head-side {
    justify-content: flex-start;
  }

  .message-stack,
  .chat-message.user .message-stack {
    width: 100%;
    max-width: 100%;
  }

  .composer-actions {
    justify-content: flex-end;
  }
}
</style>
