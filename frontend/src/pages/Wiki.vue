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

      <details class="prompt-drawer">
        <summary>参考提问</summary>
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
      </details>

      <div ref="threadRef" class="chat-thread">
        <article
          v-for="message in messages"
          :key="message.id"
          class="chat-message"
          :class="message.role"
          @mouseup="captureMessageSelection(message, $event)"
        >
          <div class="message-stack">
            <section class="session-entry">
              <header class="session-entry-meta">
                <span>{{ message.role === 'assistant' ? 'Jarvis 回答' : '用户提问' }}</span>
                <small>{{ message.role === 'assistant' ? '研究整理' : '问题记录' }}</small>
              </header>

              <div
                v-if="message.toolEvents?.length || message.toolPlan || message.trace"
                class="evidence-stack"
              >
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

                <section v-if="message.toolPlan || message.trace" class="agent-trace-panel">
                  <div class="trace-head">
                    <strong>证据链路</strong>
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
                    <span v-if="message.trace?.web_results?.length">网页 {{ message.trace.web_results.length }}</span>
                    <span v-if="message.trace?.resources?.length">资源 {{ message.trace.resources.length }}</span>
                  </div>
                </section>
              </div>

              <div class="message-text" v-html="renderMarkdown(message.content)"></div>

              <div
                v-if="message.role === 'assistant' && message.content.trim()"
                class="selection-capture"
                :class="{ active: selectedInsight?.messageId === message.id }"
              >
                <div>
                  <span>{{ selectedInsight?.messageId === message.id ? '已选片段' : '知识回流' }}</span>
                  <strong>{{ selectedInsight?.messageId === message.id ? selectedInsight.preview : insightPreview(message) }}</strong>
                </div>
                <button
                  type="button"
                  :disabled="capturingInsight"
                  @click="captureSelectedInsight(message)"
                >
                  {{ captureButtonText(message) }}
                </button>
              </div>

              <div
                v-if="message.citations?.length || message.resources?.length || message.profileUpdates?.length"
                class="evidence-rail"
              >
                <section v-if="message.citations?.length" class="citation-list" aria-label="引用卡片">
                  <header>引用卡片</header>
                  <button
                    v-for="citation in message.citations"
                    :key="citation.card_id"
                    type="button"
                    @click="openCitation(citation)"
                  >
                    {{ citation.title }}
                  </button>
                </section>

                <section v-if="message.resources?.length" class="resource-list" aria-label="延伸资源">
                  <header>延伸资源</header>
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
                </section>

                <section v-if="message.profileUpdates?.length" class="profile-update-list" aria-label="画像更新">
                  <header>画像更新</header>
                  <span v-for="item in message.profileUpdates" :key="`${item.signal_type}-${item.value}`">
                    {{ profileLabel(item.signal_type) }} / {{ item.value }}
                  </span>
                </section>
              </div>
            </section>
          </div>
        </article>
      </div>

      <div v-if="activeCardTitle" class="context-banner">
        <span>当前引用</span>
        <strong>{{ activeCardTitle }}</strong>
      </div>

      <div v-if="insightStatus" class="insight-status" :class="insightStatus.type">
        <span>{{ insightStatus.text }}</span>
        <button
          v-if="insightStatus.cardId"
          type="button"
          @click="openCapturedCard(insightStatus.cardId)"
        >
          查看卡片
        </button>
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

type SelectedInsight = {
  messageId: number
  text: string
  preview: string
}

type InsightStatus = {
  type: 'pending' | 'success' | 'error'
  text: string
  cardId?: string
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
const selectedInsight = ref<SelectedInsight | null>(null)
const insightStatus = ref<InsightStatus | null>(null)
const capturingInsight = ref(false)

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

function captureMessageSelection(message: ChatMessage, event: MouseEvent) {
  if (message.role !== 'assistant') return
  const selection = window.getSelection()
  const text = (selection?.toString() || '').trim()
  if (!text || text.length < 20) return

  const target = event.currentTarget as HTMLElement | null
  const anchor = selection?.anchorNode
  const focus = selection?.focusNode
  if (!target || !anchor || !focus || !target.contains(anchor) || !target.contains(focus)) {
    return
  }

  selectedInsight.value = {
    messageId: message.id,
    text,
    preview: text.length > 120 ? `${text.slice(0, 120)}...` : text
  }
  insightStatus.value = null
}

function previousUserQuestion(messageId: number) {
  const index = messages.value.findIndex((message) => message.id === messageId)
  if (index <= 0) return ''
  for (let i = index - 1; i >= 0; i -= 1) {
    const item = messages.value[i]
    if (item.role === 'user') return item.content
  }
  return ''
}

async function captureSelectedInsight(message: ChatMessage) {
  if (message.role !== 'assistant' || capturingInsight.value) return
  const selected = selectedInsight.value?.messageId === message.id ? selectedInsight.value : null
  const selectedText = selected?.text || message.content.trim()
  if (!selectedText) return

  capturingInsight.value = true
  insightStatus.value = { type: 'pending', text: '正在蒸馏、审查并合并到 Wiki...' }
  try {
    const { data } = await api.post(
      '/wiki/maintenance/query-insights/capture-selection',
      {
        session_id: currentSessionId.value,
        message_id: String(message.id),
        selected_text: selectedText,
        question: previousUserQuestion(message.id),
        answer: message.content,
        citations: message.citations || [],
        resources: message.resources || [],
        tool_plan: message.toolPlan || {},
        trace: message.trace || {},
        auto_merge: true,
        use_llm: true
      },
      { timeout: 180000 }
    )
    const merged = data?.candidate?.status === 'merged'
    const title = data?.distill?.title || '已选内容'
    const resultCardId = data?.result_card_id || data?.candidate?.merge?.result_card_id || ''
    insightStatus.value = {
      type: 'success',
      text: merged
        ? `已加入知识库：${title}`
        : `已进入知识候选区：${title}`,
      cardId: resultCardId || undefined
    }
    selectedInsight.value = null
    window.getSelection()?.removeAllRanges()
  } catch (error) {
    console.error('[WikiChat] capture selection failed:', error)
    insightStatus.value = { type: 'error', text: '加入知识库失败，请稍后重试。' }
  } finally {
    capturingInsight.value = false
  }
}

function openCapturedCard(cardId: string) {
  if (!cardId) return
  router.push({ path: '/vault', query: { card: cardId } })
}

function insightPreview(message: ChatMessage) {
  const text = message.content.replace(/\s+/g, ' ').trim()
  if (!text) return '将这条回答整理成一条可维护的 Wiki 知识。'
  return text.length > 120 ? `${text.slice(0, 120)}...` : text
}

function captureButtonText(message: ChatMessage) {
  if (capturingInsight.value) return '保存中...'
  return selectedInsight.value?.messageId === message.id ? '加入选中内容' : '加入整段'
}

function traceSummary(message: ChatMessage) {
  const trace = message.trace
  const plan = message.toolPlan || trace?.tool_plan
  const parts = []
  if (plan?.use_wiki) parts.push('Wiki')
  if (plan?.use_web) parts.push('网页')
  if (plan?.use_resources) parts.push('资源')
  const cardCount = trace?.diagnostics?.wiki_card_count ?? trace?.retrieved_cards?.length ?? 0
  if (cardCount) parts.push(`${cardCount} 张卡片`)
  return parts.join(' / ') || '暂无轨迹'
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
  --ink-bg-deep: #080706;
  --ink-bg: #0b0908;
  --ink-raised: #11100d;
  --ink-panel: #15130f;
  --ink-panel-soft: #1d1913;
  --ink-text: #f8fafc;
  --ink-text-soft: #cbd5e1;
  --ink-text-muted: #94a3b8;
  --desk-accent: #9bb8ad;
  --desk-accent-bright: #d4e3d8;
  --desk-signal: #8fa99e;
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr) auto auto;
  gap: 16px;
  height: calc(100vh - 84px);
  padding: 16px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 20px;
  background: linear-gradient(180deg, rgba(21, 19, 15, 0.96), rgba(8, 7, 6, 0.98));
  box-shadow: 0 18px 48px rgba(8, 7, 6, 0.32);
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
  color: var(--ink-text-muted);
  font-size: 12px;
}

.chat-head-side {
  display: flex;
  justify-content: flex-end;
}

.active-context {
  max-width: 320px;
  padding: 6px 10px;
  border: 1px solid rgba(195, 214, 202, 0.18);
  border-radius: 999px;
  background: rgba(155, 184, 173, 0.1);
  color: var(--desk-accent-bright);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.prompt-drawer {
  padding: 0 4px;
}

.prompt-drawer summary {
  width: fit-content;
  min-height: 28px;
  padding: 5px 9px;
  border: 1px solid rgba(195, 214, 202, 0.12);
  border-radius: 8px;
  background: rgba(29, 25, 19, 0.42);
  color: var(--ink-text-muted);
  font-size: 12px;
  cursor: pointer;
}

.prompt-drawer summary:hover,
.prompt-drawer summary:focus-visible {
  border-color: rgba(195, 214, 202, 0.34);
  color: var(--ink-text-soft);
}

.prompt-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 10px;
}

.prompt-chip {
  min-height: 34px;
  padding: 0 12px;
  border: 1px solid rgba(195, 214, 202, 0.12);
  border-radius: 8px;
  background: rgba(29, 25, 19, 0.46);
  color: var(--ink-text-soft);
  cursor: pointer;
}

.prompt-chip:hover {
  border-color: rgba(155, 184, 173, 0.34);
  background: rgba(155, 184, 173, 0.1);
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
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
}

.chat-message.user {
  justify-items: stretch;
}

.chat-message.user .message-stack {
  width: min(100%, 760px);
  max-width: min(100%, 760px);
  margin-left: auto;
}

.chat-message.user .message-stack {
  align-items: flex-end;
}

.message-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  width: min(100%, 920px);
  max-width: min(100%, 920px);
}

.session-entry {
  display: grid;
  gap: 14px;
  padding: 14px 16px 16px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(21, 19, 15, 0.86), rgba(17, 16, 13, 0.94));
}

.chat-message.user .session-entry {
  background: linear-gradient(180deg, rgba(29, 25, 19, 0.86), rgba(17, 16, 13, 0.94));
}

.session-entry-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(195, 214, 202, 0.1);
}

.session-entry-meta span {
  color: var(--ink-text);
  font-size: 12px;
  font-weight: 800;
}

.session-entry-meta small {
  color: var(--ink-text-muted);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 11px;
}

.message-text {
  color: var(--ink-text);
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
  color: var(--ink-text);
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
  color: var(--ink-text);
  font-weight: 700;
}

.message-text :deep(code) {
  padding: 1px 5px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 6px;
  background: rgba(8, 7, 6, 0.56);
  color: var(--desk-accent-bright);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 0.92em;
}

.message-text :deep(pre) {
  overflow: auto;
  margin: 10px 0 14px;
  padding: 12px 14px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 12px;
  background: rgba(8, 7, 6, 0.52);
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
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 12px;
}

.message-text :deep(table) {
  width: 100%;
  min-width: 520px;
  border-collapse: collapse;
  background: rgba(17, 16, 13, 0.58);
}

.message-text :deep(th),
.message-text :deep(td) {
  padding: 10px 12px;
  border-bottom: 1px solid rgba(195, 214, 202, 0.1);
  text-align: left;
  vertical-align: top;
}

.message-text :deep(th) {
  color: var(--desk-accent-bright);
  font-size: 13px;
  font-weight: 700;
  background: rgba(155, 184, 173, 0.1);
}

.message-text :deep(td) {
  color: var(--ink-text-soft);
}

.message-text :deep(tr:last-child td) {
  border-bottom: 0;
}

.message-text :deep(blockquote) {
  margin: 8px 0 12px;
  padding: 8px 12px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 10px;
  background: rgba(155, 184, 173, 0.08);
  color: var(--ink-text-soft);
}

.message-text :deep(hr) {
  height: 1px;
  margin: 16px 0;
  border: 0;
  background: rgba(195, 214, 202, 0.14);
}

.message-text :deep(a) {
  color: var(--desk-accent-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.evidence-stack {
  display: grid;
  gap: 10px;
}

.tool-trace {
  display: grid;
  gap: 8px;
}

.tool-step {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr);
  gap: 9px;
  align-items: start;
  padding: 8px 10px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 10px;
  background: rgba(29, 25, 19, 0.56);
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
  color: var(--desk-accent-bright);
  font-size: 12px;
}

.tool-step small {
  margin-top: 2px;
  color: var(--ink-text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.agent-trace-panel {
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 12px;
  background:
    linear-gradient(135deg, rgba(155, 184, 173, 0.1), rgba(29, 25, 19, 0.62)),
    rgba(29, 25, 19, 0.72);
}

.trace-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.trace-head strong {
  color: var(--desk-accent-bright);
  font-size: 12px;
}

.trace-head span {
  color: var(--desk-signal);
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
  border: 1px solid rgba(195, 214, 202, 0.12);
  border-radius: 8px;
  background: rgba(17, 16, 13, 0.68);
}

.trace-tool-chip strong {
  color: var(--ink-text);
  font-size: 11px;
}

.trace-tool-chip span {
  overflow: hidden;
  color: var(--ink-text-muted);
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
  padding: 12px 14px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(155, 184, 173, 0.12), rgba(29, 25, 19, 0.62)),
    rgba(29, 25, 19, 0.72);
  color: #eef7f2;
  text-align: left;
  cursor: pointer;
}

.trace-card:hover {
  border-color: rgba(195, 214, 202, 0.42);
  background:
    linear-gradient(135deg, rgba(155, 184, 173, 0.22), rgba(29, 25, 19, 0.72)),
    rgba(29, 25, 19, 0.82);
}

.trace-card:focus-visible {
  outline: 2px solid #c2d6ca;
  outline-offset: 2px;
}

.trace-card span {
  color: #c2d6ca;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.trace-card strong {
  overflow: hidden;
  color: var(--ink-text);
  font-size: 14px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-card small {
  display: -webkit-box;
  overflow: hidden;
  color: var(--desk-signal);
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
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 7px;
  background: rgba(155, 184, 173, 0.1);
  color: var(--desk-accent-bright);
  font-size: 11px;
}

.evidence-rail {
  display: grid;
  gap: 10px;
  padding-top: 2px;
}

.citation-list,
.resource-list,
.profile-update-list {
  display: grid;
  gap: 8px;
  gap: 8px;
}

.citation-list header,
.resource-list header,
.profile-update-list header {
  color: var(--ink-text-muted);
  font-size: 11px;
  font-weight: 800;
}

.citation-list button {
  min-height: 30px;
  width: fit-content;
  max-width: 100%;
  padding: 0 10px;
  border: 1px solid rgba(195, 214, 202, 0.18);
  border-radius: 8px;
  background: rgba(155, 184, 173, 0.08);
  color: var(--desk-accent-bright);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.citation-list button:hover {
  border-color: rgba(195, 214, 202, 0.38);
  background: rgba(155, 184, 173, 0.14);
}

.resource-list {
  align-items: stretch;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.resource-list a {
  display: grid;
  gap: 4px;
  max-width: min(100%, 420px);
  padding: 9px 11px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 10px;
  background: rgba(29, 25, 19, 0.58);
  color: var(--ink-text-soft);
}

.resource-list a:hover {
  border-color: rgba(155, 184, 173, 0.38);
  background: rgba(155, 184, 173, 0.1);
}

.resource-list span {
  color: var(--desk-signal);
  font-size: 11px;
  font-weight: 700;
}

.resource-list strong {
  overflow: hidden;
  color: var(--ink-text);
  font-size: 13px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.profile-update-list span {
  width: fit-content;
  padding: 5px 10px;
  border: 1px solid rgba(34, 197, 94, 0.18);
  border-radius: 8px;
  background: rgba(16, 185, 129, 0.12);
  color: #bbf7d0;
  font-size: 12px;
}

.selection-capture {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 12px;
  background: rgba(29, 25, 19, 0.5);
}

.selection-capture.active {
  border-color: rgba(195, 214, 202, 0.28);
  background: rgba(155, 184, 173, 0.1);
}

.selection-capture span,
.selection-capture strong {
  display: block;
}

.selection-capture span {
  color: var(--ink-text-muted);
  font-size: 11px;
  font-weight: 800;
}

.selection-capture.active span {
  color: #c2d6ca;
}

.selection-capture strong {
  overflow: hidden;
  margin-top: 3px;
  color: var(--ink-text-soft);
  font-size: 12px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.selection-capture.active strong {
  color: var(--desk-accent-bright);
}

.selection-capture button {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid rgba(195, 214, 202, 0.18);
  border-radius: 8px;
  background: rgba(29, 25, 19, 0.72);
  color: var(--ink-text-soft);
  cursor: pointer;
}

.selection-capture.active button {
  border-color: rgba(195, 214, 202, 0.34);
  background: rgba(155, 184, 173, 0.14);
  color: var(--desk-accent-bright);
}

.selection-capture button:hover {
  border-color: rgba(195, 214, 202, 0.34);
  background: rgba(155, 184, 173, 0.12);
}

.selection-capture button:disabled {
  cursor: wait;
  opacity: 0.65;
}

.context-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 12px;
  background: rgba(155, 184, 173, 0.08);
}

.context-banner span {
  color: var(--ink-text-muted);
  font-size: 12px;
}

.context-banner strong {
  font-size: 13px;
}

.insight-status {
  padding: 9px 12px;
  border-radius: 14px;
  font-size: 12px;
}

.insight-status.pending {
  border: 1px solid rgba(245, 158, 11, 0.2);
  background: rgba(245, 158, 11, 0.1);
  color: #fde68a;
}

.insight-status.success {
  border: 1px solid rgba(16, 185, 129, 0.2);
  background: rgba(16, 185, 129, 0.1);
  color: #bbf7d0;
}

.insight-status.error {
  border: 1px solid rgba(239, 68, 68, 0.22);
  background: rgba(239, 68, 68, 0.1);
  color: #fecaca;
}

.chat-composer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 12px;
  align-items: end;
  padding: 12px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: 16px;
  background: rgba(17, 16, 13, 0.72);
}

.composer-input {
  min-width: 0;
  width: 100%;
}

.composer-input :deep(.n-input) {
  --n-border: 1px solid rgba(195, 214, 202, 0.16) !important;
  --n-border-hover: 1px solid rgba(195, 214, 202, 0.34) !important;
  --n-border-focus: 1px solid rgba(195, 214, 202, 0.62) !important;
  --n-box-shadow-focus: 0 0 0 3px rgba(155, 184, 173, 0.16) !important;
  --n-caret-color: #d4e3d8 !important;
  --n-color-focus: rgba(8, 7, 6, 0.72) !important;
  --n-color: rgba(8, 7, 6, 0.72) !important;
  --n-placeholder-color: #94a3b8 !important;
  width: 100%;
}

.composer-input :deep(.n-input-wrapper) {
  padding: 0;
  border-radius: 12px;
  background: rgba(8, 7, 6, 0.72);
}

.composer-input :deep(.n-input__textarea) {
  width: 100%;
}

.composer-input :deep(textarea) {
  width: 100%;
  min-height: 56px;
  padding: 14px 16px;
  color: var(--ink-text);
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

.composer-actions :deep(.n-button--primary-type) {
  --n-color: #9bb8ad !important;
  --n-color-hover: #d4e3d8 !important;
  --n-color-pressed: #8fa99e !important;
  --n-color-focus: #d4e3d8 !important;
  --n-color-disabled: rgba(155, 184, 173, 0.34) !important;
  --n-border: 1px solid rgba(195, 214, 202, 0.22) !important;
  --n-border-hover: 1px solid rgba(195, 214, 202, 0.46) !important;
  --n-border-pressed: 1px solid rgba(155, 184, 173, 0.5) !important;
  --n-border-focus: 1px solid rgba(195, 214, 202, 0.54) !important;
  --n-border-disabled: 1px solid rgba(195, 214, 202, 0.12) !important;
  --n-ripple-color: #9bb8ad !important;
  --n-text-color: #080706 !important;
  --n-text-color-hover: #080706 !important;
  --n-text-color-pressed: #080706 !important;
  --n-text-color-focus: #080706 !important;
  --n-text-color-disabled: rgba(8, 7, 6, 0.62) !important;
  --n-box-shadow-focus: 0 0 0 3px rgba(155, 184, 173, 0.18) !important;
}

.composer-ghost {
  min-height: 38px;
  padding: 0 12px;
  border: 0;
  background: transparent;
  color: var(--ink-text-muted);
  cursor: pointer;
}

.composer-ghost:hover {
  color: var(--ink-text);
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
