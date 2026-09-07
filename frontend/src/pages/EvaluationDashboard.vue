<template>
  <section class="evaluation-page">
    <PageHeader title="质量评测" description="查看回答质量、引用依据与检索表现，定位需要改进的样例。">
      <div class="run-picker">
        <span>评测记录</span>
        <n-select
          v-model:value="selectedRunId"
          :options="runOptions"
          :loading="loadingRuns"
          filterable
          @update:value="selectRun"
        />
      </div>
    </PageHeader>

    <n-spin :show="loadingRun">
      <div class="eval-layout">
        <aside class="run-list">
          <div class="panel-head">
            <span>历史记录</span>
            <button type="button" @click="loadRuns">刷新</button>
          </div>
          <button
            v-for="run in runs"
            :key="run.id"
            type="button"
            class="run-row"
            :class="{ active: run.id === selectedRunId }"
            @click="selectRun(run.id)"
          >
            <strong>{{ run.id }}</strong>
            <span>{{ run.case_count }} 个样例 · {{ scoreText(run.overall_final_score) }}</span>
          </button>
        </aside>

        <div class="eval-main">
          <section class="metric-grid">
            <article v-for="metric in metrics" :key="metric.key" class="metric-card">
              <span>{{ metric.label }}</span>
              <strong>{{ metric.value }}</strong>
            </article>
          </section>

          <section class="split-panel">
            <div class="panel-head">
              <span>各来源表现</span>
              <small>{{ splitMetrics.length }}</small>
            </div>
            <div class="split-table-wrap">
              <table class="eval-table">
                <thead>
                  <tr>
                    <th>来源</th>
                    <th>样例数</th>
                    <th>综合得分</th>
                    <th>置信度</th>
                    <th>引用依据</th>
                    <th>首位命中</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in splitMetrics" :key="item.source">
                    <td>{{ item.source }}</td>
                    <td>{{ item.count }}</td>
                    <td>{{ scoreText(item.final_score) }}</td>
                    <td>{{ scoreText(item.answer_confidence) }}</td>
                    <td>{{ scoreText(item.citation_grounding) }}</td>
                    <td>{{ scoreText(item.top1_hit_rate) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>

          <section class="case-panel">
            <div class="panel-head">
              <span>评测样例</span>
              <div class="case-actions">
                <button type="button" :aria-pressed="lowOnly" :class="{ active: lowOnly }" @click="toggleLowOnly">
                  低分优先
                </button>
                <button type="button" @click="loadCases">刷新</button>
              </div>
            </div>
            <div class="case-table-wrap">
              <table class="eval-table cases-table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>问题</th>
                    <th>来源</th>
                    <th>综合得分</th>
                    <th>置信度</th>
                    <th>首位命中</th>
                    <th>联网</th>
                    <th>问题类型</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="item in cases"
                    :key="item.id"
                    :class="{ weak: item.final_score < 0.85 }"
                    @click="openCase(item)"
                  >
                    <td><button class="case-open" type="button" @click.stop="openCase(item)">{{ item.id }}</button></td>
                    <td>{{ item.query }}</td>
                    <td>{{ item.expected_source }}</td>
                    <td>{{ scoreText(item.final_score) }}</td>
                    <td>{{ scoreText(item.answer_confidence) }}</td>
                    <td>
                      <n-tag size="small" :color="tagColor(item.top1_hit)">
                        {{ item.top1_hit ? '命中' : '未命中' }}
                      </n-tag>
                    </td>
                    <td>{{ item.web_used ? '是' : '否' }}</td>
                    <td>{{ failureLabel(item.failure_bucket) }}</td>
                  </tr>
                </tbody>
              </table>
              <n-empty v-if="!cases.length && !loadingRun" description="暂无评测样例" />
            </div>
          </section>
        </div>
      </div>
    </n-spin>

    <n-drawer v-model:show="caseDrawerVisible" width="min(760px, 100vw)" placement="right">
      <n-drawer-content v-if="selectedCase" :title="selectedCase.id" closable>
        <div class="case-detail">
          <section>
            <span class="detail-label">问题</span>
            <p>{{ selectedCase.query }}</p>
          </section>
          <section>
            <span class="detail-label">回答</span>
            <pre>{{ selectedCase.answer || '暂无回答记录' }}</pre>
          </section>
          <section>
            <span class="detail-label">引用来源</span>
            <div v-if="citations.length" class="citation-list">
              <article v-for="citation in citations" :key="citation.card_id || citation.title">
                <strong>{{ citation.title || citation.card_id }}</strong>
                <span>{{ citation.page_type || 'WikiCard' }}</span>
                <p>{{ citation.summary }}</p>
              </article>
            </div>
            <p v-else class="muted">暂无引用记录</p>
          </section>
          <section>
            <span class="detail-label">工具调用</span>
            <div v-if="toolPlan?.tools?.length" class="tool-list">
              <article v-for="tool in toolPlan.tools" :key="tool.name + tool.query">
                <strong>{{ tool.name }}</strong>
                <span>{{ tool.query }}</span>
                <p>{{ tool.reason }}</p>
              </article>
            </div>
            <pre v-else>{{ selectedCase.tool_plan || '暂无工具调用记录' }}</pre>
          </section>
          <section>
            <span class="detail-label">评审说明</span>
            <p>{{ selectedCase.reviewer_reason || '-' }}</p>
          </section>
        </div>
      </n-drawer-content>
    </n-drawer>
  </section>
</template>

<script setup lang="ts">
import PageHeader from '../components/PageHeader.vue'
import { computed, onMounted, ref } from 'vue'
import { NDrawer, NDrawerContent, NEmpty, NSelect, NSpin, NTag } from 'naive-ui'
import { api } from '../api'

type EvaluationRun = {
  id: string
  updated_at: string
  has_summary: boolean
  case_count: number
  overall_final_score: number
  overall_answer_confidence: number
  overall_citation_grounding: number
  retrieval_hit_rate: number
  top1_hit_rate: number
  pass_rate: number
}

type SplitMetric = {
  source: string
  count: number
  final_score: number
  answer_confidence: number
  citation_grounding: number
  retrieval_hit_rate: number
  top1_hit_rate: number
}

type Citation = {
  card_id?: string
  title?: string
  page_type?: string
  summary?: string
  markdown_path?: string
}

type ToolStep = {
  name: string
  query: string
  reason: string
}

type ToolPlan = {
  intent?: string
  answer_mode?: string
  tools?: ToolStep[]
}

type EvaluationCase = {
  id: string
  query: string
  expected_source: string
  final_score: number
  answer_confidence: number
  citation_grounding: number
  retrieval_hit: boolean
  top1_hit: boolean
  web_used: boolean
  latency_seconds: number
  failure_bucket: string
  reviewer_reason: string
  answer?: string
  citations?: Citation[] | string
  resources?: unknown
  tool_plan?: ToolPlan | string
}

const runs = ref<EvaluationRun[]>([])
const selectedRunId = ref('')
const summary = ref<Record<string, number>>({})
const splitMetrics = ref<SplitMetric[]>([])
const cases = ref<EvaluationCase[]>([])
const selectedCase = ref<EvaluationCase | null>(null)
const loadingRuns = ref(false)
const loadingRun = ref(false)
const caseDrawerVisible = ref(false)
const lowOnly = ref(false)

const runOptions = computed(() => runs.value.map((run) => ({ label: run.id, value: run.id })))

const metrics = computed(() => [
  { key: 'final', label: '综合得分', value: scoreText(summary.value.overall_final_score) },
  { key: 'confidence', label: '回答置信度', value: scoreText(summary.value.overall_answer_confidence) },
  { key: 'grounding', label: '引用依据', value: scoreText(summary.value.overall_citation_grounding) },
  { key: 'retrieval', label: '检索命中率', value: scoreText(summary.value.retrieval_hit_rate) },
  { key: 'top1', label: '首位命中率', value: scoreText(summary.value.top1_hit_rate) },
  { key: 'latency', label: '平均引用数', value: scoreText(summary.value.mean_citations_per_answer) }
])

const citations = computed<Citation[]>(() => {
  const raw = selectedCase.value?.citations
  return Array.isArray(raw) ? raw : []
})

const toolPlan = computed<ToolPlan | null>(() => {
  const raw = selectedCase.value?.tool_plan
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) return raw as ToolPlan
  return null
})

function scoreText(value: number | undefined) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '-'
  return value.toFixed(value >= 10 ? 1 : 3)
}

function tagColor(ok: boolean) {
  return ok
    ? { color: 'rgba(16,185,129,0.16)', textColor: '#bbf7d0', borderColor: 'rgba(16,185,129,0.34)' }
    : { color: 'rgba(244,63,94,0.16)', textColor: '#fecdd3', borderColor: 'rgba(244,63,94,0.34)' }
}

async function loadRuns() {
  loadingRuns.value = true
  try {
    const { data } = await api.get('/wiki/evaluations', { params: { limit: 80 } })
    runs.value = data.items || []
    const preferred = runs.value.find((run) => run.id === 'current30_full_answer_check')
    const target = selectedRunId.value || preferred?.id || data.latest_run_id || runs.value[0]?.id || ''
    if (target) await selectRun(target)
  } finally {
    loadingRuns.value = false
  }
}

async function selectRun(runId: string) {
  if (!runId) return
  selectedRunId.value = runId
  loadingRun.value = true
  try {
    const [{ data: detail }] = await Promise.all([
      api.get(`/wiki/evaluations/${encodeURIComponent(runId)}`)
    ])
    summary.value = detail.summary || {}
    splitMetrics.value = detail.split_metrics || []
    await loadCases()
  } finally {
    loadingRun.value = false
  }
}

async function loadCases() {
  if (!selectedRunId.value) return
  const { data } = await api.get(`/wiki/evaluations/${encodeURIComponent(selectedRunId.value)}/cases`, {
    params: {
      limit: 200,
      low_only: lowOnly.value
    }
  })
  cases.value = data.items || []
  if (!selectedCase.value && cases.value.length) {
    selectedCase.value = cases.value[0]
  }
}

async function toggleLowOnly() {
  lowOnly.value = !lowOnly.value
  await loadCases()
}

function failureLabel(value: string) {
  const labels: Record<string, string> = { ok: '正常', weak_top1: '首位检索偏差', low_confidence: '置信度偏低', answer_quality: '回答质量' }
  return labels[value] || value
}

function openCase(item: EvaluationCase) {
  selectedCase.value = item
  caseDrawerVisible.value = true
}

onMounted(loadRuns)
</script>

<style scoped>
.evaluation-page {
  min-height: 100%;
  padding: 0;
  max-width: 1240px;
  margin: 0 auto;
  container-type: inline-size;
  container-name: evaluation;
  color: var(--text);
}

.run-picker {
  display: grid;
  gap: 7px;
  width: min(360px, 100%);
  min-width: 260px;
}

.run-picker span,
.panel-head small,
.run-row span,
.detail-label,
.muted {
  color: var(--text-muted);
}

.eval-layout {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr);
  gap: 16px;
}

.run-list,
.split-panel,
.case-panel {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--panel);
}

.run-list {
  display: grid;
  align-content: start;
  gap: 8px;
  padding: 14px;
  max-height: calc(100dvh - 184px);
  overflow: auto;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 2px 12px;
  font-weight: 600;
}

.panel-head button,
.case-actions button {
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--panel);
  color: var(--text-soft);
  cursor: pointer;
  padding: 7px 10px;
}

.case-actions {
  display: flex;
  gap: 8px;
}

.case-actions button.active,
.panel-head button:hover {
  border-color: var(--accent);
  color: var(--text);
}

.run-row {
  display: grid;
  gap: 5px;
  width: 100%;
  padding: 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}

.run-row.active,
.run-row:hover {
  border-color: var(--line-strong);
  background: var(--accent-soft);
}

.run-row strong {
  font-size: 13px;
  line-height: 1.3;
  word-break: break-word;
}

.run-row span {
  font-size: 12px;
}

.eval-main {
  display: grid;
  gap: 16px;
  min-width: 0;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.metric-card {
  min-height: 88px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--panel);
}

.metric-card span {
  display: block;
  min-height: 32px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.35;
}

.metric-card strong {
  display: block;
  margin-top: 8px;
  font-size: 25px;
  line-height: 1;
}

.split-panel,
.case-panel {
  padding: 14px;
  min-width: 0;
}

.split-table-wrap,
.case-table-wrap {
  overflow: auto;
}

.eval-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  min-width: 660px;
  font-variant-numeric: tabular-nums;
}

.eval-table th,
.eval-table td {
  padding: 10px;
  border-top: 1px solid rgba(148, 163, 184, 0.11);
  text-align: left;
  vertical-align: top;
}

.eval-table th {
  color: var(--text-muted);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.eval-table td {
  color: var(--text-soft);
  font-size: 13px;
  line-height: 1.45;
}

.eval-table td:first-child,
.eval-table th:first-child {
  width: 40%;
}

.cases-table { min-width: 1100px; }
.cases-table th:first-child, .cases-table td:first-child { width: 110px; }
.cases-table th:nth-child(2) { width: 300px; }
.cases-table th:nth-child(3) { width: 230px; }
.case-open { border: 0; padding: 0; background: transparent; color: var(--accent); text-align: left; cursor: pointer; }
.case-open:hover { text-decoration: underline; }

.cases-table tbody tr {
  cursor: pointer;
}

.cases-table tbody tr:hover {
  background: var(--accent-soft);
}

.cases-table tbody tr.weak {
  background: rgba(244, 63, 94, 0.06);
}

.case-detail {
  display: grid;
  gap: 18px;
}

.detail-label {
  display: block;
  margin-bottom: 8px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.case-detail p,
.case-detail pre {
  margin: 0;
  color: var(--text-soft);
  line-height: 1.65;
}

.case-detail pre {
  max-height: 360px;
  overflow: auto;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  white-space: pre-wrap;
}

.citation-list,
.tool-list {
  display: grid;
  gap: 10px;
}

.citation-list article,
.tool-list article {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
}

.citation-list strong,
.tool-list strong,
.citation-list span,
.tool-list span {
  display: block;
}

.citation-list span,
.tool-list span {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 12px;
}

.citation-list p,
.tool-list p {
  margin-top: 8px;
  font-size: 13px;
}

@container evaluation (max-width: 1100px) {
  .eval-layout {
    grid-template-columns: 1fr;
  }

  .run-list {
    max-height: none;
    grid-template-columns: minmax(0, 1fr);
  }

  .metric-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {


  .run-picker {
    width: 100%;
  }

  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.run-list { min-width: 0; }
.run-list .panel-head { margin-bottom: 0; }
.metric-card strong { font-variant-numeric: tabular-nums; }
.eval-table td { overflow-wrap: anywhere; }
</style>
