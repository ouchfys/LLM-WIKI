<template>
  <details v-if="entries.length || loading" class="agent-activity" :open="expanded" @toggle="onToggle">
    <summary class="activity-heading" title="执行进度与实际工具记录">
      <span>{{ loading ? '执行中' : duration !== undefined ? '用时' : '执行记录' }}<template v-if="duration !== undefined"> {{ formatElapsed(duration) }}</template></span>
      <ChevronDown class="activity-chevron" aria-hidden="true" />
      <span class="activity-stats">{{ summary }}</span>
    </summary>

    <details v-if="research && research.status !== 'not_required'" class="tool-entry">
      <summary class="tool-line">{{ researchStatus }} · {{ research.questions.filter(q => q.status === 'supported').length }}/{{ research.questions.length }} 项有据可答</summary>
      <ul class="tool-items">
        <li v-for="question in research.questions" :key="question.id">
          {{ question.status === 'supported' ? '已有依据' : question.status === 'conflict' ? '证据冲突' : question.status === 'partial' ? '部分有据' : '尚缺依据' }}：{{ question.question }}
        </li>
      </ul>
    </details>

    <button v-if="overview.length < allEntries.length" type="button" class="trace-toggle" @click="showAll = !showAll">
      {{ showAll ? '只看关键流程' : `已合并 ${allEntries.length - overview.length} 条重复进度查询 · 查看完整记录` }}
    </button>
    <ol class="activity-timeline" aria-label="执行记录">
      <li v-for="(entry, index) in entries" :key="entry.event_id || `${entry.type}-${index}`" :class="entry.type">
        <p v-if="entry.type === 'progress'" class="activity-commentary">{{ entry.text }}</p>
        <details v-else class="tool-entry" :class="entry.status">
          <summary class="tool-line">
            <component :is="entry.status === 'error' ? AlertCircle : toolIcon(entry.tool)" class="tool-icon" aria-hidden="true" />
            <span class="tool-action">{{ toolAction(entry) }}</span>
            <span v-if="toolSubject(entry)" class="tool-subject" :class="{ command: entry.tool === 'local_shell' }" :title="toolSubject(entry)">{{ toolSubject(entry) }}</span>
            <span v-if="entry.status === 'running'" class="tool-state">{{ loading ? '进行中' : '未确认完成' }}</span>
            <span v-else-if="entry.status === 'error'" class="tool-state">失败</span>
            <span v-if="entry.duration_ms !== undefined" class="tool-duration">{{ formatElapsed(entry.duration_ms) }}</span>
            <ChevronDown class="tool-chevron" aria-hidden="true" />
          </summary>

          <div class="tool-detail">
            <div class="tool-detail-meta">
              <code>{{ entry.tool }}</code>
              <span>{{ statusLabel(entry.status) }}</span>
              <span v-if="entry.result_id !== undefined">记录 #{{ entry.result_id }}</span>
            </div>
            <div v-if="entry.arguments && Object.keys(entry.arguments).length" class="tool-section">
              <span class="tool-section-label">参数</span>
              <pre>{{ formatValue(entry.arguments) }}</pre>
            </div>
            <p v-else-if="entry.query" class="tool-query">{{ entry.query }}</p>
            <div v-if="entry.output_preview !== undefined || entry.detail" class="tool-section">
              <span class="tool-section-label">输出摘要</span>
              <pre>{{ formatValue(entry.output_preview ?? entry.detail) }}</pre>
            </div>
            <ul v-if="entry.items?.length" class="tool-results">
              <li v-for="(item, itemIndex) in entry.items" :key="item.card_id || item.url || itemIndex">
                <button v-if="item.card_id && item.title" type="button" @click="emit('open-card', item)">{{ item.title }}</button>
                <a v-else-if="safeWebUrl(item.url)" :href="safeWebUrl(item.url)" target="_blank" rel="noreferrer">{{ item.title || item.url }}</a>
                <strong v-else>{{ item.title || item.source_title || item.section || '结果' }}</strong>
                <span v-if="item.page"> · 第 {{ item.page }} 页</span>
                <p v-if="item.snippet || item.summary || item.text">{{ item.snippet || item.summary || item.text }}</p>
              </li>
            </ul>
            <p v-if="entry.status === 'running' && !loading" class="tool-empty">此记录尚未包含工具的完成结果。</p>
            <p v-else-if="!entry.detail && entry.output_preview === undefined && !entry.items?.length" class="tool-empty">{{ entry.status === 'running' ? '等待工具返回结果。' : '此记录未保存输出摘要。' }}</p>
          </div>
        </details>
      </li>
    </ol>
    <p v-if="loading" class="activity-wait" role="status">{{ phaseLabel || '正在处理' }}…</p>
  </details>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { AlertCircle, Book2, ChevronDown, Database, FileImport, FileText, ListCheck, Search, Terminal2, World } from '@vicons/tabler'
import type { ChatMessage, ToolEventItem, ToolStatusEvent } from '../types/chat'
import { compactTimeline, formatElapsed, messageTimeline, toolSubject } from '../lib/traceTimeline'

const props = defineProps<{ message: ChatMessage; loading: boolean; elapsedSeconds: number; phaseLabel?: string }>()
const emit = defineEmits<{ 'open-card': [item: ToolEventItem] }>()
const expanded = ref(true)
const showAll = ref(false)
const research = computed(() => props.message.trace?.research_state)
const researchStatus = computed(() => research.value?.reason === 'assessment_unavailable' ? '证据评估失败，结果未完整核验' : ({
  researching: '正在核对证据', complete: '证据已覆盖问题', insufficient: '证据尚不完整',
  conflicted: '证据存在冲突', budget_exhausted: '检索达到上限', not_required: '无需检索'
}[research.value?.status || 'not_required']))
const allEntries = computed(() => messageTimeline(props.message))
const overview = computed(() => compactTimeline(allEntries.value))
const entries = computed(() => showAll.value ? allEntries.value : overview.value)
const duration = computed(() => props.loading ? props.elapsedSeconds * 1000 : props.message.trace?.runtime?.wall_time_ms)
const summary = computed(() => {
  const tools = allEntries.value.filter(entry => entry.type === 'tool_status')
  const runtime = props.message.trace?.runtime
  const parts = tools.length ? [`${tools.length} 次工具调用`] : []
  const effort = props.message.trace?.thinking_effort
  if (effort) parts.push(`思考：${{ none: '关闭', low: '低', high: '高', max: '最高' }[effort]}`)
  const tokens = runtime?.token_usage?.total_tokens
  if (tokens) parts.push(`${runtime?.token_usage?.contains_estimates ? '约 ' : ''}${tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : tokens} tokens`)
  if (runtime?.retry_count) parts.push(`${runtime.retry_count} 次重试`)
  return parts.join(' · ')
})

function onToggle(event: Event) {
  expanded.value = (event.target as HTMLDetailsElement).open
}

const labels: Record<string, string> = {
  repository: '读取代码仓库',
  local_shell: '运行命令', wiki_open: '阅读知识页', wiki_card: '阅读知识页', evidence_lookup: '回查原文证据',
  task_plan_read: '读取任务计划', task_plan_write: '更新任务计划', project_memory_update: '更新项目记忆',
  read_tool_result: '读取工具记录', arxiv_import_paper: '提交论文入库', arxiv_ingestion_status: '检查入库进度',
  wiki_search: '搜索知识库', arxiv_search: '搜索 arXiv', arxiv_lookup: '查询 arXiv',
  web_search: '搜索网页', web_fetch: '读取网页', resource_recommend: '查找相关资料',
  corpus_manifest: '查看论文目录', workspace_list: '查看文件目录', workspace_search: '搜索本地文件', workspace_read: '读取本地文件',
  context: '读取对话上下文', conversation_context: '读取相关对话', context_compact: '整理较早对话',
  wiki_write: '保存 Wiki 知识', wiki_validate: '校验 Wiki 结构', project_purpose: '更新项目目标',
  search_session_history: '搜索会话记录', read_session_messages: '读取会话记录',
  search_project_history: '搜索项目历史', read_project_messages: '读取项目历史',
  research_plan: '制定研究计划', research_task_status: '查看研究进度', research_next_batch: '读取下一批论文',
  submit_paper_reading: '保存阅读记录', research_reopen_paper: '重新核对论文', capture_research_source: '保存研究资料'
}

function toolAction(entry: ToolStatusEvent): string {
  if (entry.tool === 'arxiv') {
    const actions: Record<string, string> = {
      search: '搜索 arXiv', lookup: '查询论文信息', import: '提交论文入库', status: '检查入库进度'
    }
    const action = actions[String(entry.arguments?.action)] || '访问 arXiv'
    return `${entry.status === 'done' ? '已' : entry.status === 'running' && props.loading ? '正在' : ''}${action}`
  }
  const repositoryActions: Record<string, string> = {
    discover: '查找代码仓库', open: '打开代码仓库', list: '查看仓库目录', search: '搜索仓库代码',
    read: '阅读源码', checkout: '获取仓库快照', cache_status: '查看缓存占用'
  }
  const action = (entry.tool === 'repository' && repositoryActions[String(entry.arguments?.operation)]) || labels[entry.tool] || entry.label || entry.tool
  return `${entry.status === 'done' ? '已' : entry.status === 'running' && props.loading ? '正在' : ''}${action}`
}

function toolIcon(tool: string) {
  if (tool === 'local_shell') return Terminal2
  if (tool.includes('search')) return Search
  if (tool.includes('web') || tool.includes('arxiv_lookup')) return World
  if (tool.includes('plan') || tool.includes('status')) return ListCheck
  if (tool.includes('import')) return FileImport
  if (tool.includes('memory')) return Database
  if (tool === 'wiki_open' || tool === 'wiki_card' || tool === 'evidence_lookup') return Book2
  return FileText
}

function statusLabel(status: ToolStatusEvent['status']) {
  if (status === 'running' && !props.loading) return '未确认完成'
  return { running: '进行中', done: '已完成', error: '失败' }[status]
}

function formatValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2) || ''
}

function safeWebUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined
  } catch { return undefined }
}
</script>

<style scoped>
.agent-activity { min-width: 0; color: var(--ink-text-muted, #929692); }
.activity-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 1px 0 12px; margin-bottom: 14px; border-bottom: 1px solid rgba(190, 205, 195, .12); cursor: pointer; list-style: none; font-size: 13px; }
.activity-heading::-webkit-details-marker, .tool-line::-webkit-details-marker { display: none; }
.activity-chevron { width: 14px; height: 14px; transition: transform 120ms ease; }
.agent-activity:not([open]) .activity-chevron { transform: rotate(-90deg); }
.activity-stats { margin-left: auto; font-size: 12px; color: var(--desk-signal, #929692); }
.activity-timeline { margin: 0; padding: 0; list-style: none; }
.activity-commentary { margin: 8px 0 13px; color: var(--ink-text-soft, #c9ceca); font-size: 16px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.activity-timeline > .progress:not(:first-child) { margin-top: 18px; }
.tool-entry { min-width: 0; }
.tool-line { display: flex; align-items: center; gap: 8px; min-height: 32px; padding: 3px 0; list-style: none; cursor: pointer; font-size: 14px; }
.tool-line:hover { color: var(--ink-text, #e2e6e3); }
.tool-icon { flex: 0 0 16px; width: 16px; height: 16px; }
.tool-action { flex: 0 0 auto; }
.tool-subject { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-subject.command { font-family: var(--font-mono, Consolas, monospace); font-size: 13px; }
.tool-state, .tool-duration { flex: 0 0 auto; font-size: 10px; }
.tool-state { color: var(--desk-accent-bright, #a9c7b7); }
.tool-chevron { flex: 0 0 12px; width: 12px; height: 12px; }
.tool-entry[open] .tool-chevron { transform: rotate(180deg); }
.tool-entry.error .tool-line, .tool-entry.error .tool-state { color: #e1a5a5; }
.tool-detail { display: grid; gap: 12px; margin: 3px 0 12px 24px; padding: 12px 14px; border-left: 1px solid rgba(190, 205, 195, .22); background: rgba(150, 165, 155, .045); font-size: 12px; min-width: 0; }
.tool-detail-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; font-size: 11px; }
.tool-detail-meta code { color: var(--ink-text-soft, #c9ceca); }
.tool-section { min-width: 0; }
.tool-section-label { display: block; margin-bottom: 5px; font-size: 11px; }
.tool-section pre { max-height: 320px; overflow: auto; margin: 0; color: var(--ink-text-soft, #c9ceca); font-family: var(--font-mono, Consolas, monospace); font-size: 11px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; }
.tool-query, .tool-empty { margin: 0; overflow-wrap: anywhere; }
.tool-results { display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }
.tool-results button, .tool-results a { padding: 0; border: 0; background: transparent; color: var(--desk-accent-bright, #bfd4c7); font: inherit; text-align: left; cursor: pointer; overflow-wrap: anywhere; }
.tool-results button:hover, .tool-results a:hover { text-decoration: underline; }
.tool-results strong { font-weight: 500; }
.tool-results p { margin: 3px 0 0; line-height: 1.6; overflow-wrap: anywhere; }
.activity-wait { margin: 10px 0 2px; font-size: 12px; }
.activity-heading:focus-visible, .tool-line:focus-visible, .tool-results button:focus-visible, .tool-results a:focus-visible { outline: 2px solid var(--desk-accent-bright, #bfd4c7); outline-offset: 3px; border-radius: 2px; }
@media (max-width: 600px) {
  .activity-stats { flex-basis: 100%; margin: 0; }
  .tool-line { gap: 6px; font-size: 12px; }
  .tool-action { max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tool-detail { margin-left: 10px; padding: 10px; }
}
@media (prefers-reduced-motion: reduce) { .activity-chevron { transition: none; } }
</style>

<style scoped>
.trace-toggle { border: 0; background: transparent; color: inherit; opacity: .75; cursor: pointer; padding: .5rem 0; font: inherit; font-size: .85rem; }
</style>
