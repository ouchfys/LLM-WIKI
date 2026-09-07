<template>
  <section class="chat-scene">
    <div class="chat-window">
      <header class="chat-window-head">
        <div class="chat-head-copy">
          <strong>{{ displaySessionTitle(currentSessionTitle) }}</strong>
        <span>{{ messages.length > 1 ? `${messages.length} 条消息` : '基于知识库回答，附原文来源' }}</span>
        </div>
        <div class="chat-head-side">
          <span v-if="activeCardTitle" class="active-context">{{ activeCardTitle }}</span>
        </div>
      </header>

      <ChatPrompts :expanded="!messages.some(message => message.role === 'user')" @select="choosePrompt" />

      <div ref="threadRef" class="chat-thread">
        <article
          v-for="message in messages"
          :key="message.id"
          class="chat-message"
          :class="message.role"
        >
          <div class="message-stack">
            <section class="session-entry">
              <header class="session-entry-meta">
                <span>{{ message.role === 'assistant' ? 'Wiki 回答' : '用户提问' }}</span>
                <small>{{ message.role === 'assistant' ? 'Wiki Agent' : '问题记录' }}</small>
              </header>

              <section
                v-if="showProcess(message)"
                class="agent-process"
                :class="{ live: isMessageProcessing(message) }"
                aria-label="检索过程"
              >
                <header class="process-head">
                  <div>
                    <span class="process-live-dot"></span>
                    <strong>{{ isMessageProcessing(message) ? runPhaseLabel : '检索过程' }}</strong>
                  </div>
                  <span>{{ processSummary(message) }}</span>
                </header>

                <ol class="process-timeline">
                  <li
                    v-for="(step, index) in processSteps(message)"
                    :key="step.eventId || `${message.id}-${step.tool}-${index}`"
                    class="process-step"
                    :class="step.status"
                  >
                    <div class="process-marker">
                      <span>{{ step.status === 'done' ? '✓' : index + 1 }}</span>
                    </div>
                    <div class="process-body">
                      <header>
                        <strong>{{ processStepTitle(step.tool) }}</strong>
                        <span>{{ processStatusLabel(step.status) }}</span>
                      </header>
                      <p>{{ processStepDetail(step) }}</p>

                      <div v-if="step.items?.length" class="process-results">
                        <template v-for="(item, itemIndex) in step.items" :key="item.card_id || item.cell_id || item.url || `${step.eventId}-${itemIndex}`">
                          <button
                            v-if="item.card_id"
                            type="button"
                            class="process-card-result"
                            @click="openProcessCard(item)"
                          >
                            <span>{{ item.page_type || 'WikiPage' }}</span>
                            <strong>{{ item.title }}</strong>
                            <small v-if="item.score !== undefined">匹配分 {{ formatScore(item.score) }} · {{ formatMatchReason(item.match_reason) }}</small>
                            <small v-else>{{ item.summary || '已读取页面正文与关联关系' }}</small>
                            <small v-if="item.matched_sections?.length">
                              命中小节：{{ item.matched_sections.map(section => section.section).filter(Boolean).join(' / ') }}
                            </small>
                          </button>

                          <div v-else-if="step.tool === 'table_query'" class="process-cell-result">
                            <span>{{ item.row_label || item.source_title }} × {{ item.column_label || '匹配列' }}</span>
                            <strong>{{ normalizeCellValue(item.value) }}</strong>
                            <small>{{ item.page ? `第 ${item.page} 页` : item.table_id }}</small>
                          </div>

                          <div v-else class="process-generic-result">
                            <strong>{{ item.title || item.url || '工具结果' }}</strong>
                            <small>{{ item.snippet || item.summary || item.text || item.section || '' }}</small>
                          </div>
                        </template>
                      </div>
                    </div>
                  </li>
                </ol>
              </section>

              <details v-if="message.trace?.context_budget?.input_tokens_estimate" class="context-usage">
                <summary>会话上下文 · 约 {{ Math.round((message.trace.context_budget.input_tokens_estimate || 0) / 1000) }}K / {{ Math.round((message.trace.context_budget.window || 0) / 1000) }}K Token</summary>
                <p>显示本轮最后一次模型请求的估算输入用量，已另外预留回答空间。整理上下文会保存摘要，原始聊天仍可查找；Wiki 知识库独立保存。</p>
                <p v-if="message.trace.context_budget.compactions?.some(item => item.status === 'compacted')">本轮已整理较早对话。</p>
              </details>

              <div class="message-text" v-html="renderMarkdown(message.content)"></div>

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

      <div v-if="sending || controlNotice" class="run-status" role="status" aria-live="polite">
        <span v-if="sending">{{ runPhaseLabel }} · {{ elapsedSeconds }} 秒</span>
        <span v-if="sending && activeRunId">Enter 补充当前要求 · Alt + Enter 排队追问 · Shift + Enter 换行</span>
        <span v-if="controlNotice">{{ controlNotice }}</span>
      </div>
      <div v-if="visibleQueue.length" class="input-queue" aria-label="待处理消息">
        <div v-for="item in visibleQueue" :key="item.id" class="queued-input">
          <span>{{ queueLabel(item) }}：{{ item.content }}</span>
          <button v-if="item.status === 'ready' && !sending" type="button" @click="drainQueue(true)">执行</button>
          <button v-if="['blocked', 'failed'].includes(item.status)" type="button" @click="restoreQueuedInput(item)">重新填写</button>
        </div>
      </div>
      <form class="chat-composer" @submit.prevent="send()">
        <n-input
          ref="composeInput"
          v-model:value="draft"
          type="textarea"
          class="composer-input"
          :placeholder="sending ? '可以继续输入补充要求，按 Enter 插话；/wiki、/compact 将排队。' : '问你的知识库，或输入 /wiki、/compact。'"
          aria-label="向知识库提问"
          :autosize="{ minRows: 2, maxRows: 6 }"
          @keydown="handleComposeKeydown"
        />
        <div class="composer-actions">
          <button type="button" class="composer-ghost" @click="draft = ''">清空输入</button>
          <n-button v-if="sending" type="primary" attr-type="button"
            :disabled="!activeRunId || stopRequested || runPhase === 'saving'" @click="stopCurrentRun">
            {{ stopRequested ? '正在停止' : '停止' }}
          </n-button>
          <n-button v-else type="primary" attr-type="submit" :disabled="!draft.trim()">
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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NInput, NModal, NTag, type InputInst } from 'naive-ui'
import { api, apiErrorMessage } from '../api'
import ChatPrompts from '../components/ChatPrompts.vue'
import { consumeEventStream } from '../lib/chatStream'
import type { Citation, ToolEventItem, ToolEvent, TraceCard, ChatMessage, SlashCommand, RunInput, SseChunk, ChatSession, ChatHistory, StoredChatMessage, RunInputList, AgentTrace } from '../types/chat'

const route = useRoute()
const router = useRouter()
const composeInput = ref<InputInst | null>(null)
const threadRef = ref<HTMLElement | null>(null)
const historyIndex = ref(-1)
const SESSION_STORAGE_KEY = 'wiki_chat_session_id'

const draft = ref('')
const sending = ref(false)
const activeRunId = ref('')
const activeAssistantId = ref<number | null>(null)
const stopRequested = ref(false)
const runPhase = ref('')
const controlNotice = ref('')
const inboxItems = ref<RunInput[]>([])
const queuePosting = ref(false)
const elapsedSeconds = ref(0)
let startedAt = 0
let timer: ReturnType<typeof setInterval> | undefined
let streamController: AbortController | null = null
let turnEpoch = 0
let draining = false
const consumedInputIds = new Set<string>()
const automaticQueueIds = new Set<string>()
let turnOutcome: 'completed' | 'cancelled' | 'failed' = 'completed'
const visibleQueue = computed(() => inboxItems.value.filter(item => ['pending', 'ready', 'blocked', 'running', 'failed'].includes(item.status)))
const runPhaseLabel = computed(() => stopRequested.value ? '正在请求停止' : ({
  starting: '正在连接', searching: '正在检索资料', answering: '正在生成回答',
  saving: '正在保存结果（此步骤不可中断）', command: '正在执行知识/上下文命令（此步骤不可中断）'
} as Record<string, string>)[runPhase.value] || '正在处理')
const currentSessionId = ref('')
const currentSessionTitle = ref('新对话')
const activeCitation = ref<Citation | null>(null)
const rawModalVisible = ref(false)
const rawModalTitle = ref('')
const rawMarkdown = ref('')

function choosePrompt(prompt: string) {
  draft.value = prompt
  nextTick(() => composeInput.value?.focus())
}

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
  await abandonActiveStream()
  const { data } = await api.post<ChatSession>('/wiki/sessions', { title: '新对话' })
  currentSessionId.value = data.id
  currentSessionTitle.value = data.title || '新对话'
  localStorage.setItem(SESSION_STORAGE_KEY, data.id)
  messages.value = [...initialMessages]
  activeCitation.value = null
  inboxItems.value = []
  scrollThreadToBottom()
  if (syncRoute) {
    await router.replace({
      path: '/',
      query: { session: data.id }
    })
  }
}

async function loadSessionHistory(sessionId: string) {
  await abandonActiveStream()
  const { data } = await api.get<ChatHistory>(`/wiki/sessions/${sessionId}/messages`)
  currentSessionId.value = sessionId
  currentSessionTitle.value = data.session?.title || '对话'
  messages.value = data.items?.length ? normalizeMessages(data.items) : [...initialMessages]
  localStorage.setItem(SESSION_STORAGE_KEY, sessionId)
  const queued = await api.get<RunInputList>('/agent-runs/inbox', { params: { session_id: sessionId } })
  inboxItems.value = queued.data.items || []
  scrollThreadToBottom()
}

function normalizeMessages(items: StoredChatMessage[]): ChatMessage[] {
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

function isMessageProcessing(message: ChatMessage) {
  return Boolean(
    sending.value
    && message.role === 'assistant'
    && activeAssistantId.value === message.id
  )
}

function processSteps(message: ChatMessage): ToolEvent[] {
  if (message.toolEvents?.length) return message.toolEvents

  const observations = message.trace?.tool_observations || []
  if (observations.length) {
    return observations.map((item, index) => ({
      eventId: `${item.tool}:${item.query || index}`,
      tool: item.tool,
      label: item.tool,
      status: item.status || 'done',
      detail: item.summary,
      query: item.query,
      items: (item.items || []).slice(0, 4)
    }))
  }

  if (isMessageProcessing(message)) {
    return [{
      eventId: `planning:${message.id}`,
      tool: 'planning',
      label: 'Planning',
      status: 'running',
      detail: '正在分析问题并选择可审计的检索工具。',
      items: []
    }]
  }

  return (message.toolPlan?.tools || []).map((tool, index) => ({
    eventId: `${tool.name}:${tool.query || index}`,
    tool: tool.name,
    label: tool.name,
    status: 'done',
    detail: tool.reason,
    query: tool.query,
    items: []
  }))
}

function showProcess(message: ChatMessage) {
  return message.role === 'assistant' && (isMessageProcessing(message) || processSteps(message).length > 0)
}

function processSummary(message: ChatMessage) {
  const steps = processSteps(message)
  if (isMessageProcessing(message)) {
    const active = steps.find((step) => step.status === 'running')
    if (active) return processStepTitle(active.tool)
    return message.content.trim() ? '正在生成回答' : '正在规划下一步'
  }
  const cardCount = message.trace?.diagnostics?.wiki_page_count ?? message.trace?.diagnostics?.wiki_card_count ?? message.trace?.retrieved_cards?.length ?? 0
  const runtime = message.trace?.runtime
  const parts = [`${steps.length} 步`]
  if (cardCount) parts.push(`${cardCount} 个 Wiki 页面`)
  if (runtime?.wall_time_ms) parts.push(formatDuration(runtime.wall_time_ms))
  if (runtime?.token_usage?.total_tokens) {
    const prefix = runtime.token_usage.contains_estimates ? '约 ' : ''
    parts.push(`${prefix}${formatTokenCount(runtime.token_usage.total_tokens)} tokens`)
  }
  if (runtime?.retry_count) parts.push(`${runtime.retry_count} 次重试`)
  if ((runtime?.model_failures || 0) + (runtime?.tool_failures || 0) > 0) {
    parts.push(`${(runtime?.model_failures || 0) + (runtime?.tool_failures || 0)} 次失败`)
  }
  return parts.join(' · ')
}

function formatDuration(value: number) {
  if (value < 1000) return `${Math.round(value)}ms`
  return `${(value / 1000).toFixed(1)}s`
}

function formatTokenCount(value: number) {
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(1)}k`
}

function processStepTitle(tool: string) {
  const labels: Record<string, string> = {
    planning: '规划检索路径',
    context: '解析对话上下文',
    wiki_search: '搜索 Wiki',
    wiki_open: '阅读 Wiki 页面',
    wiki_card: '阅读 Wiki 页面',
    table_query: '核验表格证据',
    evidence_lookup: '核验原文依据',
    web_search: '搜索外部资料',
    web_fetch: '读取网页原文',
    resource_recommend: '查找延伸资源',
    conversation_context: '选择相关对话',
    wiki_write: '沉淀 Wiki 知识',
    wiki_validate: '校验 Wiki 结构',
    context_compact: '整理较早对话',
    search_session_history: '搜索会话原文',
    read_session_messages: '读取会话原文',
    read_tool_result: '读取工具记录'
  }
  return labels[tool] || tool
}

function processStatusLabel(status: ToolEvent['status']) {
  return ({ running: '进行中', done: '已完成', error: '失败' } as Record<string, string>)[status] || status
}

function extractCount(detail: string | undefined) {
  const match = String(detail || '').match(/\d+/)
  return match ? Number(match[0]) : 0
}

function processStepDetail(step: ToolEvent) {
  if (step.status === 'running') {
    const query = step.query ? `“${step.query}”` : '当前问题'
    const runningCopy: Record<string, string> = {
      planning: '正在分析问题并选择可审计的检索工具。',
      context: '正在结合最近对话补全当前问题。',
      wiki_search: `正在用 ${query} 匹配标题、别名与 Wiki 索引。`,
      wiki_open: '正在打开高相关页面，只读取面向用户的 Markdown 正文。',
      wiki_card: '正在打开高相关页面，读取 Markdown 正文与页面关系。',
      table_query: '正在定位表格、行列和原始单元格证据。',
      evidence_lookup: '正在按需回查来源段落，核验当前关键结论。',
      web_search: `正在搜索 ${query} 的外部信息。`,
      web_fetch: '正在读取候选网页的正文段落。',
      resource_recommend: '正在筛选相关学习资料。'
    }
    return runningCopy[step.tool] || step.detail || '正在执行工具。'
  }

  const count = extractCount(step.detail)
  if (step.tool === 'wiki_search') return `找到 ${count || step.items?.length || 0} 个候选页面，并保留匹配分与命中原因。`
  if (step.tool === 'wiki_open') return `已读取 ${count || step.items?.length || 0} 个高相关 Wiki 页面。`
  if (step.tool === 'wiki_card') return `已读取 ${count || step.items?.length || 0} 张高相关 Wiki 页面。`
  if (step.tool === 'table_query') return step.detail || '已定位表格单元格及页码。'
  if (step.tool === 'evidence_lookup') return step.detail || '已按需回查原文段落。'
  if (step.tool === 'context') return '已结合最近对话解析追问指代。'
  if (step.tool === 'conversation_context') return step.detail || '已按用户要求选择相关对话。'
  if (step.tool === 'wiki_write') return step.detail || '已生成对话洞见并写入 Wiki。'
  if (step.tool === 'wiki_validate') return step.detail || '已校验 Wiki 结构并更新索引。'
  if (step.tool === 'context_compact') return step.detail || '已保存上下文摘要，原始对话仍然保留。'
  return step.detail || (step.status === 'error' ? '工具执行失败。' : '工具执行完成。')
}

function formatScore(score: number | undefined) {
  if (score === undefined || Number.isNaN(Number(score))) return '—'
  return Number(score).toFixed(1)
}

function formatMatchReason(reason: string | undefined) {
  if (!reason) return '语义匹配'
  return reason
    .replace('page_fts', '页面全文命中')
    .replace(/term_overlap:(\d+)\/(\d+)/, '关键词重合 $1/$2')
}

function normalizeCellValue(value: string | undefined) {
  return String(value || '—').replace(/\s*\.\s*/g, '.').replace(/\s+%/g, '%')
}

function openProcessCard(item: ToolEventItem) {
  if (!item.card_id || !item.title) return
  openTraceCard({
    card_id: item.card_id,
    title: item.title,
    page_type: item.page_type || 'WikiPage',
    summary: item.summary || '',
    markdown_path: item.markdown_path || ''
  })
}

function handleComposeKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send(undefined, event.altKey)
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
  if (chunk.type === 'run_started') {
    activeRunId.value = chunk.run_id
    return
  }
  if (chunk.type === 'heartbeat') return
  if (chunk.type === 'phase') {
    runPhase.value = chunk.phase
    return
  }
  if (chunk.type === 'queue_update') {
    for (const item of chunk.items) {
      upsertInput(item)
      if (item.status === 'ready') automaticQueueIds.add(item.id)
    }
    return
  }
  if (chunk.type === 'answer_reset') {
    assistantMessage.content = ''
    assistantMessage.citations = []
    assistantMessage.resources = []
    assistantMessage.toolEvents = (assistantMessage.toolEvents || []).filter(item => item.status === 'done')
    activeCitation.value = null
    for (const id of chunk.input_ids || []) consumedInputIds.add(id)
    for (const item of inboxItems.value) {
      if (chunk.input_ids?.includes(item.id)) item.status = 'consumed'
    }
    controlNotice.value = chunk.detail || '正在重新生成回答。'
    return
  }
  if (chunk.type === 'cancelled') {
    turnOutcome = 'cancelled'
    assistantMessage.content = chunk.message
    assistantMessage.citations = []
    assistantMessage.resources = []
    assistantMessage.toolEvents = (assistantMessage.toolEvents || []).filter(item => item.status !== 'running')
    activeCitation.value = null
    controlNotice.value = '已停止；待执行命令不会自动保存未完成的回答。'
    return
  }
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
    const eventId = chunk.event_id || `${chunk.tool}:${chunk.query || ''}`
    const index = events.findIndex((item) => item.eventId === eventId)
    const nextEvent: ToolEvent = {
      eventId,
      tool: chunk.tool,
      label: chunk.label,
      status: chunk.status,
      detail: chunk.detail,
      query: chunk.query,
      reason: chunk.reason,
      items: chunk.items || []
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
    turnOutcome = 'failed'
    assistantMessage.content += `\n${chunk.message}`
  }
}

async function consumeSseResponse(response: Response, assistantMessage: ChatMessage, epoch: number) {
  await consumeEventStream<SseChunk>(
    response,
    chunk => handleStreamChunk(chunk, assistantMessage),
    () => epoch === turnEpoch
  )
}

function parseSlashCommand(text: string): SlashCommand | null {
  const match = text.trim().match(/^\/(wiki|compact)(?:\s+([\s\S]*))?$/i)
  if (!match) return null
  return {
    name: match[1].toLowerCase() as SlashCommand['name'],
    argument: (match[2] || '').trim()
  }
}

function upsertInput(item: RunInput) {
  const index = inboxItems.value.findIndex(value => value.id === item.id)
  const previous = inboxItems.value[index]
  const next = { ...item }
  if (next.status === 'pending' && previous && previous.status !== 'pending') next.status = previous.status
  if (consumedInputIds.has(item.id)) next.status = 'consumed'
  if (index < 0) inboxItems.value.push(next)
  else inboxItems.value[index] = next
}

function queueLabel(item: RunInput) {
  if (item.status === 'blocked') return '本轮已停止，未执行'
  if (item.status === 'failed') return '执行未确认，请检查后重试'
  if (item.status === 'running') return '执行中（若连接断开，请先检查结果）'
  return item.kind === 'interrupt' ? '补充要求待生效' : '当前回答结束后执行'
}

async function queueDuringRun(text: string, followup: boolean) {
  if (queuePosting.value) {
    controlNotice.value = '上一条消息正在确认，输入已保留，请稍后发送。'
    return
  }
  const runId = activeRunId.value
  const epoch = turnEpoch
  if (!runId || stopRequested.value || runPhase.value === 'saving') {
    controlNotice.value = '当前步骤暂不接受插话，输入已保留，请稍后发送。'
    return
  }
  const command = parseSlashCommand(text)
  if (command?.name === 'wiki' && !command.argument) {
    controlNotice.value = '请在 /wiki 后写明要沉淀的内容。'
    return
  }
  const kind = command ? 'command' : followup ? 'followup' : 'interrupt'
  const content = command ? (command.name === 'compact' ? '/compact' : `/wiki ${command.argument}`) : text
  let accepted = false
  queuePosting.value = true
  try {
    const { data } = await api.post<RunInput>(`/agent-runs/${runId}/inputs`, { id: crypto.randomUUID(), kind, content })
    accepted = true
    if (epoch !== turnEpoch) return
    upsertInput(data)
    if (kind === 'interrupt') {
      const index = messages.value.findIndex(item => item.id === activeAssistantId.value)
      messages.value.splice(index < 0 ? messages.value.length : index, 0, { id: Date.now(), role: 'user', content: text })
    }
    controlNotice.value = kind === 'interrupt'
      ? '补充要求已收到，将在当前模型/工具步骤结束后生效。'
      : '已排队，当前回答完成后执行；若本轮取消则不会自动执行。'
    const latest = await api.get<RunInputList>(`/agent-runs/${runId}/inputs`)
    if (epoch !== turnEpoch) return
    for (const item of latest.data.items || []) {
      upsertInput(item)
      if (item.status === 'ready') automaticQueueIds.add(item.id)
    }
  } catch (error) {
    if (epoch !== turnEpoch) return
    controlNotice.value = accepted ? '消息已接收，但队列刷新失败；请勿重复发送。'
      : apiErrorMessage(error, '未能加入队列，输入已保留。')
  } finally {
    if (accepted && epoch === turnEpoch && draft.value.trim() === text) draft.value = ''
    queuePosting.value = false
  }
  if (!sending.value) await drainQueue()
}

async function stopCurrentRun() {
  const runId = activeRunId.value
  const epoch = turnEpoch
  if (!runId || stopRequested.value) return
  stopRequested.value = true
  try {
    await api.post(`/agent-runs/${runId}/cancel`)
    // Keep reading SSE until the server acknowledges cancellation; a browser
    // abort alone cannot stop backend tools or prevent late writes.
  } catch (error) {
    if (epoch !== turnEpoch) return
    stopRequested.value = false
    controlNotice.value = apiErrorMessage(error, '停止请求未确认，请重试。')
  }
}

async function abandonActiveStream() {
  const runId = activeRunId.value
  const controller = streamController
  ++turnEpoch
  automaticQueueIds.clear()
  activeRunId.value = ''
  activeAssistantId.value = null
  sending.value = false
  controlNotice.value = ''
  if (runId) {
    // keepalive also covers page navigation. The original session owns this run.
    void fetch(`/api/agent-runs/${runId}/cancel`, { method: 'POST', keepalive: true }).catch(() => {})
  }
  controller?.abort()
  streamController = null
}

async function restoreQueuedInput(item: RunInput) {
  draft.value = item.content
  const { data } = await api.post<RunInput>(`/agent-runs/${item.run_id}/inputs/${item.id}/state`, { status: 'dismissed' })
  upsertInput(data)
}

async function drainQueue(manual = false) {
  if (draining || sending.value || queuePosting.value) return
  draining = true
  const sessionId = currentSessionId.value
  try {
    while (!sending.value && sessionId === currentSessionId.value) {
      const item = inboxItems.value.find(value => value.status === 'ready' && value.kind !== 'interrupt'
        && (manual || automaticQueueIds.has(value.id)))
      if (!item) break
      try {
        const { data } = await api.post<RunInput>(`/agent-runs/${item.run_id}/inputs/${item.id}/state`, { status: 'running' })
        upsertInput(data)
      } catch {
        controlNotice.value = '队列已由其他页面处理，请刷新后查看。'
        break
      }
      if (sessionId !== currentSessionId.value) {
        await api.post<RunInput>(`/agent-runs/${item.run_id}/inputs/${item.id}/state`, { status: 'failed' })
        break
      }
      const success = await send(item.content)
      const { data } = await api.post<RunInput>(`/agent-runs/${item.run_id}/inputs/${item.id}/state`, {
        status: success ? 'completed' : 'failed'
      })
      if (sessionId === currentSessionId.value) upsertInput(data)
      automaticQueueIds.delete(item.id)
      if (!success) {
        // Stopping one queued turn also stops its remaining queued commands.
        // Do not let a later, unrelated chat silently execute those writes.
        const remaining = inboxItems.value.filter(value => value.status === 'ready')
        automaticQueueIds.clear()
        if (sessionId === currentSessionId.value) {
          for (const pending of remaining) {
            const blocked = await api.post<RunInput>(`/agent-runs/${pending.run_id}/inputs/${pending.id}/state`, { status: 'blocked' })
            if (sessionId === currentSessionId.value) upsertInput(blocked.data)
          }
        }
        break
      }
    }
  } catch {
    controlNotice.value = '队列执行状态未确认，请先检查结果，不会自动重复执行。'
  } finally {
    draining = false
  }
}

async function runSlashCommand(command: SlashCommand, assistantMessage: ChatMessage, sessionId: string) {
  if (command.name === 'wiki') {
    if (!command.argument) {
      assistantMessage.content = '请在 `/wiki` 后说明想沉淀的内容，例如：`/wiki 把刚才关于 Agent 记忆的最终方案存进知识库`。'
      return
    }
    assistantMessage.toolEvents = [{
      eventId: `conversation_context:${assistantMessage.id}`,
      tool: 'conversation_context',
      label: 'Conversation Context',
      status: 'running',
      detail: '正在按你的要求定位本次会话中的相关内容。'
    }]
    const { data } = await api.post<{ answer?: string; card?: Citation; trace?: AgentTrace }>(
      '/wiki/maintenance/query-insights/capture-conversation',
      {
        session_id: sessionId,
        instruction: command.argument,
        auto_merge: true,
        use_llm: true
      },
      { timeout: 180000 }
    )
    assistantMessage.content = data.answer || '对话洞见已处理。'
    assistantMessage.citations = data.card ? [data.card] : []
    assistantMessage.trace = data.trace || undefined
    assistantMessage.toolEvents = []
    if (data.card && sessionId === currentSessionId.value) activeCitation.value = data.card
    return
  }

  assistantMessage.toolEvents = [{
    eventId: `context_compact:${assistantMessage.id}`,
    tool: 'context_compact',
    label: 'Context Compact',
    status: 'running',
    detail: '正在把较早对话整理成可继续使用的上下文摘要。'
  }]
  const { data } = await api.post<{ answer?: string; trace?: AgentTrace }>(
    `/wiki/sessions/${sessionId}/compact`,
    { keep_recent_turns: 2, use_llm: true },
    { timeout: 180000 }
  )
  assistantMessage.content = data.answer || '已整理较早对话。'
  assistantMessage.trace = data.trace || undefined
  assistantMessage.toolEvents = []
}

async function send(queuedText?: string, followup = false): Promise<boolean> {
  historyIndex.value = -1
  const text = (queuedText ?? draft.value).trim()
  if (!text) return false
  if (text.toLowerCase() === '/stop') {
    if (sending.value) await stopCurrentRun()
    else controlNotice.value = '当前没有运行中的任务。'
    draft.value = ''
    return false
  }
  if (sending.value) {
    await queueDuringRun(text, followup)
    return false
  }
  const command = parseSlashCommand(text)
  const sessionId = currentSessionId.value
  const epoch = ++turnEpoch
  turnOutcome = 'completed'
  stopRequested.value = false
  controlNotice.value = ''
  activeRunId.value = ''
  runPhase.value = command ? 'command' : 'starting'
  startedAt = Date.now()
  elapsedSeconds.value = 0

  messages.value.push({ id: Date.now(), role: 'user', content: text })
  if (queuedText === undefined) draft.value = ''
  sending.value = true
  activeCitation.value = null
  scrollThreadToBottom()

  const assistantId = Date.now() + 1
  activeAssistantId.value = assistantId
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
    if (command) {
      await runSlashCommand(command, assistantMessage, sessionId)
    } else {
    streamController = new AbortController()
    const response = await fetch('/api/wiki/chat', {
      method: 'POST',
      signal: streamController.signal,
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream'
      },
      body: JSON.stringify({
        message: text,
        session_id: sessionId,
        stream: true
      })
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    await consumeSseResponse(response, assistantMessage, epoch)
    }

    if (!assistantMessage.content.trim()) {
      assistantMessage.content = '这次没有拿到有效回答，请重试。'
    }
  } catch (error) {
    if (epoch !== turnEpoch) return false
    turnOutcome = 'failed'
    console.error('[WikiChat] stream failed:', error)
    assistantMessage.toolEvents = []
    assistantMessage.content = command
      ? `/${command.name} 执行未确认，请检查结果后再重试。`
      : '请求失败，请稍后重试。'
    if (activeRunId.value) {
      void api.post(`/agent-runs/${activeRunId.value}/cancel`).catch(() => {})
    }
  } finally {
    if (epoch === turnEpoch) {
    sending.value = false
    activeRunId.value = ''
    activeAssistantId.value = null
    streamController = null
    scrollThreadToBottom()
    }
  }
  if (epoch !== turnEpoch) return false
  const success = turnOutcome === 'completed'
  if (success && !draining) await drainQueue()
  return success
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
  timer = setInterval(() => {
    if (sending.value) elapsedSeconds.value = Math.floor((Date.now() - startedAt) / 1000)
  }, 1000)
  await ensureSession()
  applyRoutePrompt()
  scrollThreadToBottom()
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  void abandonActiveStream()
})
</script>

<style scoped>
.context-usage { margin: 0.5rem 0; color: var(--text-muted, #aaa); font-size: 0.78rem; }
.context-usage summary { cursor: pointer; }
.context-usage p { max-width: 64ch; margin: 0.5rem 0; line-height: 1.65; }

.run-status, .input-queue { padding: 8px 20px; color: var(--muted, #9bb8ad); font-size: 13px; }
.run-status { display: flex; flex-wrap: wrap; gap: 8px 18px; }
.queued-input { display: flex; align-items: center; gap: 12px; padding: 7px 0; }
.queued-input span { flex: 1; overflow-wrap: anywhere; white-space: pre-wrap; }
.queued-input button { background: transparent; color: inherit; border: 1px solid currentColor; border-radius: 6px; padding: 4px 8px; cursor: pointer; }
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
  --ink-text-muted: #aab5bd;
  --desk-accent: #9bb8ad;
  --desk-accent-bright: #d4e3d8;
  --desk-signal: #8fa99e;
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: calc(100dvh - 68px);
  padding: 16px;
  border: 1px solid rgba(195, 214, 202, 0.14);
  border-radius: var(--radius-panel);
  background: var(--panel);
}

.chat-window-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  align-items: center;
  padding: 8px 10px 10px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.08);
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

.chat-thread {
  flex: 1;
  min-height: 0;
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
  background: transparent;
}

.chat-message.user .session-entry {
  background: var(--panel-soft);
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

.agent-process {
  display: grid;
  gap: 13px;
  padding: 14px 15px 15px;
  border: 1px solid rgba(195, 214, 202, 0.16);
  border-radius: 13px;
  background:
    radial-gradient(circle at 0 0, rgba(155, 184, 173, 0.1), transparent 32%),
    rgba(17, 16, 13, 0.74);
}

.process-head,
.process-head > div,
.process-body > header {
  display: flex;
  align-items: center;
}

.process-head {
  justify-content: space-between;
  gap: 14px;
}

.process-head > div {
  gap: 9px;
}

.process-head strong {
  color: var(--desk-accent-bright);
  font-size: 12px;
  font-weight: 700;
}

.process-head > span {
  color: var(--desk-signal);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 10px;
}

.process-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #8fa99e;
  box-shadow: 0 0 0 4px rgba(155, 184, 173, 0.11);
}

.agent-process.live .process-live-dot {
  animation: process-pulse 1.35s ease-out infinite;
}

@keyframes process-pulse {
  0% { box-shadow: 0 0 0 0 rgba(155, 184, 173, 0.34); }
  72%, 100% { box-shadow: 0 0 0 8px rgba(155, 184, 173, 0); }
}

.process-timeline {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.process-step {
  position: relative;
  display: grid;
  grid-template-columns: 26px minmax(0, 1fr);
  gap: 11px;
  padding: 0 0 15px;
}

.process-step:last-child {
  padding-bottom: 0;
}

.process-step:not(:last-child)::before {
  content: "";
  position: absolute;
  left: 12px;
  top: 25px;
  bottom: 2px;
  width: 1px;
  background: rgba(195, 214, 202, 0.14);
}

.process-marker {
  position: relative;
  z-index: 1;
  width: 25px;
  height: 25px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(195, 214, 202, 0.18);
  border-radius: 7px;
  background: #15130f;
  color: var(--ink-text-muted);
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 9px;
  font-weight: 750;
}

.process-step.running .process-marker {
  border-color: rgba(155, 184, 173, 0.38);
  color: #d4e3d8;
}

.process-step.done .process-marker {
  border-color: rgba(74, 222, 128, 0.2);
  background: rgba(34, 197, 94, 0.08);
  color: #acd4b7;
}

.process-step.error .process-marker {
  border-color: rgba(244, 63, 94, 0.22);
  background: rgba(244, 63, 94, 0.08);
  color: #fda4af;
}

.process-body {
  min-width: 0;
  padding-top: 2px;
}

.process-body > header {
  justify-content: space-between;
  gap: 12px;
}

.process-body > header strong {
  color: var(--ink-text);
  font-size: 12px;
  font-weight: 650;
}

.process-body > header span {
  color: var(--ink-text-muted);
  font-size: 10px;
}

.process-step.running .process-body > header span {
  color: #c2d6ca;
}

.process-body > p {
  margin: 4px 0 0;
  color: var(--ink-text-muted);
  font-size: 11px;
  line-height: 1.55;
  text-wrap: pretty;
}

.process-results {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 7px;
  margin-top: 9px;
}

.process-card-result,
.process-cell-result,
.process-generic-result {
  min-width: 0;
  display: grid;
  gap: 4px;
  padding: 9px 10px;
  border: 1px solid rgba(195, 214, 202, 0.11);
  border-radius: 8px;
  background: rgba(29, 25, 19, 0.58);
  color: var(--ink-text);
  text-align: left;
}

.process-card-result {
  cursor: pointer;
  transition: border-color 180ms ease, background 180ms ease, transform 180ms ease;
}

.process-card-result:hover {
  border-color: rgba(195, 214, 202, 0.32);
  background: rgba(155, 184, 173, 0.1);
  transform: translateY(-1px);
}

.process-card-result:focus-visible {
  outline: 2px solid #c2d6ca;
  outline-offset: 2px;
}

.process-card-result > span,
.process-cell-result > span {
  color: #a8bdb3;
  font-size: 9px;
  font-weight: 750;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.process-card-result > strong,
.process-generic-result > strong {
  overflow: hidden;
  font-size: 11px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.process-card-result > small,
.process-cell-result > small,
.process-generic-result > small {
  overflow: hidden;
  color: var(--ink-text-muted);
  font-size: 9px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.process-cell-result {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}

.process-cell-result > span,
.process-cell-result > small {
  grid-column: 1;
}

.process-cell-result > strong {
  grid-column: 2;
  grid-row: 1 / span 2;
  color: #dce9e3;
  font-family: "JetBrains Mono", Consolas, monospace;
  font-size: 16px;
  font-variant-numeric: tabular-nums;
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
.chat-message.assistant .session-entry { border: 0; }
.chat-head-copy strong { font-size: 18px; font-weight: 600; }
.session-entry-meta small { font-family: var(--font-sans); }
</style>
