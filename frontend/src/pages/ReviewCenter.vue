<template>
  <section class="review-page">
    <PageHeader title="冲突审批" description="对照新旧来源，决定如何保存不一致的观点。">
      <button class="ui-button" type="button" @click="() => loadApprovals(true)">刷新</button>
    </PageHeader>

    <div class="review-grid">
      <aside class="queue-panel">
        <div class="panel-head"><strong>待处理冲突</strong><small>{{ approvals.length }}</small></div>
        <button
          v-for="item in approvals"
          :key="item.id"
          type="button"
          class="queue-row"
          :class="{ active: selected?.id === item.id }"
          @click="selectApproval(item.id)"
        >
          <span>{{ item.title || item.page_id }}</span>
          <small>{{ stateLabel(item.run?.current_state) }} · {{ shortId(item.id) }}</small>
        </button>
        <p v-if="!approvals.length && !loading" class="empty">没有需要处理的跨来源冲突。普通新增和补充会自动入库。</p>
      </aside>

      <main class="proposal-panel">
        <template v-if="selected">
          <div class="proposal-head">
            <div>
              <span>{{ selected.revision?.verification?.page_type || 'WikiPage' }}</span>
              <h2>{{ selected.title || selected.revision?.verification?.title }}</h2>
            </div>
            <div class="decision-actions">
              <template v-if="selected.status === 'commit_failed'">
                <button type="button" class="approve" :disabled="busy" @click="retrySelected">重试提交</button>
              </template>
              <template v-else>
                <button type="button" class="reject" :disabled="busy" @click="rejectSelected">{{ rejectLabel }}</button>
                <button type="button" :disabled="busy" @click="openEditor">编辑处理</button>
                <button type="button" class="approve" :disabled="busy" @click="approveSelected">{{ approveLabel }}</button>
              </template>
            </div>
          </div>

          <p v-if="selected.commit_error" class="commit-error">
            提交失败：{{ selected.commit_error }}
          </p>

          <section class="state-strip">
            <div v-for="state in visibleStates" :key="state.name" :class="['state-node', state.kind]">
              <span>{{ state.icon }}</span>
              <small>{{ state.label }}</small>
            </div>
          </section>

          <section class="decision-brief">
            <div class="decision-brief-copy">
              <span>冲突类型</span>
              <h3>{{ decisionTitle }}</h3>
              <p>{{ decisionExplanation }}</p>
            </div>
            <dl class="impact-grid">
              <div>
                <dt>冲突关系</dt>
                <dd>{{ conflictPairs.length }} 组</dd>
              </div>
              <div>
                <dt>新来源</dt>
                <dd>{{ incomingSourceCount }} 篇</dd>
              </div>
              <div>
                <dt>已有来源</dt>
                <dd>{{ existingSourceCount }} 篇</dd>
              </div>
            </dl>
            <p class="decision-hint">两边的结论都已经分别通过各自来源的证据核验。这里判断的是知识应如何共存，不要求你阅读 PDF。</p>
          </section>

          <section :class="['recommendation', recommendation.tone]">
            <div>
              <span>系统建议</span>
              <strong>{{ recommendation.title }}</strong>
              <p>{{ recommendation.text }}</p>
            </div>
            <small>Verifier 已自动核验 {{ verifiedClaimCount }}/{{ displayClaims.length }} 条新结论</small>
          </section>

          <section v-if="conflictPairs.length" class="conflict-section">
            <div class="section-head"><strong>冲突来源与对象</strong><small>{{ conflictPairs.length }} 组</small></div>
            <article v-for="pair in conflictPairs" :key="pair.id" class="conflict-pair">
              <header class="conflict-object">
                <span>冲突对象</span>
                <strong>{{ conflictObjectLabel(pair) }}</strong>
              </header>
              <div class="conflict-side incoming">
                <span>新来源</span>
                <strong>{{ sourceTitle(pair.incoming.sources, '新上传论文') }}</strong>
                <blockquote>{{ pair.incoming.statement }}</blockquote>
              </div>
              <div class="conflict-relation">
                <b>{{ pair.relation === 'supersedes' ? '替代' : '冲突' }}</b>
                <span>VS</span>
              </div>
              <div class="conflict-side existing">
                <span>已有来源</span>
                <strong>{{ sourceTitle(pair.existing.sources, '已有 Wiki 来源') }}</strong>
                <blockquote>{{ pair.existing.statement }}</blockquote>
              </div>
              <footer>
                <span>系统判断</span>
                <p>{{ conflictReason(pair.reason) }}</p>
              </footer>
            </article>
          </section>

          <section v-if="selected.decision_context?.summary_changed" class="summary-compare">
            <div class="section-head"><strong>摘要变化</strong><small>旧版与候选版</small></div>
            <div class="summary-columns">
              <article><span>当前 Wiki</span><p>{{ selected.decision_context.before_summary }}</p></article>
              <article><span>候选更新</span><p>{{ selected.decision_context.after_summary }}</p></article>
            </div>
          </section>

          <section v-if="!conflictPairs.length" class="claims-section">
            <div class="section-head"><strong>系统准备怎样修改知识</strong><small>{{ displayClaims.length }} 条</small></div>
            <article
              v-for="claim in displayClaims"
              :key="claim.id"
              class="claim-row"
            >
              <span>{{ claim.statement }}</span>
              <small>
                {{ claimActionLabel(claim.action) }} ·
                {{ isVerifiedClaim(claim) ? 'Verifier 已通过' : verificationLabel(claimVerification(claim)) }}
              </small>
            </article>
          </section>

          <details class="technical-details">
            <summary>
              <span>查看技术 Diff（可选）</span>
              <small>+{{ selected.decision_context?.line_changes?.added || 0 }} / −{{ selected.decision_context?.line_changes?.removed || 0 }} 行</small>
            </summary>
            <pre class="diff-view"><code><span
              v-for="(line, index) in diffLines"
              :key="index"
              :class="diffClass(line)"
            >{{ line }}
</span></code></pre>
          </details>

        </template>
        <div v-else class="empty large">选择一条冲突查看来源和结论。</div>
      </main>
    </div>

    <n-modal v-model:show="editorOpen" preset="card" style="width: min(1040px, 94vw)" title="编辑冻结的 Markdown Proposal">
      <p class="editor-note">保存后会创建新的 proposal，并重新运行 evidence Verifier；不会绕过门禁。</p>
      <n-input v-model:value="editedMarkdown" type="textarea" :autosize="{ minRows: 20, maxRows: 30 }" />
      <template #footer>
        <div class="modal-actions">
          <n-button @click="editorOpen = false">取消</n-button>
          <n-button type="primary" :loading="busy" @click="submitEdit">重新验证并提交审批</n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import PageHeader from '../components/PageHeader.vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NInput, NModal, useMessage } from 'naive-ui'
import { api } from '../api'

type ClaimRow = { id: string; statement: string; action?: string; verifier_result?: string; verification?: Record<string, any> }
type ConflictSource = { source_packet_id: string; title: string; source_type: string; url?: string; parser?: string }
type ConflictSide = { claim_id: string; statement: string; sources: ConflictSource[] }
type ConflictObject = { subject?: string; aspect?: string; scope?: Record<string, string> }
type ConflictPair = { id: string; relation: 'contradicts' | 'supersedes'; reason: string; confidence: number; object?: ConflictObject; incoming: ConflictSide; existing: ConflictSide }
type DecisionContext = {
  kind: 'update_existing' | 'create_new'
  risk_reasons?: string[]
  line_changes?: { added: number; removed: number }
  claim_actions?: Record<string, number>
  new_source_urls?: string[]
  before_summary?: string
  after_summary?: string
  summary_changed?: boolean
  conflict_count?: number
}
type Approval = {
  id: string; run_id: string; revision_id: string; page_id: string; title: string; status: string
  commit_error?: string
  run?: Record<string, any>; revision?: Record<string, any>; claims?: ClaimRow[]; affected_claims?: ClaimRow[]
  conflict_pairs?: ConflictPair[]
  decision_context?: DecisionContext
}
const message = useMessage()
const approvals = ref<Approval[]>([])
const selected = ref<Approval | null>(null)
const loading = ref(false)
const busy = ref(false)
const editorOpen = ref(false)
const editedMarkdown = ref('')
let pollTimer = 0

const diffLines = computed(() => String(selected.value?.revision?.patch || '').split('\n'))
const displayClaims = computed(() => selected.value?.affected_claims?.length ? selected.value.affected_claims : selected.value?.claims || [])
const conflictPairs = computed(() => selected.value?.conflict_pairs || [])
const incomingSourceCount = computed(() => uniqueSourceCount(conflictPairs.value.flatMap(pair => pair.incoming.sources || [])))
const existingSourceCount = computed(() => uniqueSourceCount(conflictPairs.value.flatMap(pair => pair.existing.sources || [])))
const verifiedClaimCount = computed(() => displayClaims.value.filter(isVerifiedClaim).length)
const allClaimsVerified = computed(() => displayClaims.value.length > 0 && verifiedClaimCount.value === displayClaims.value.length)
const summaryLooksRedundant = computed(() => {
  const before = String(selected.value?.decision_context?.before_summary || '').trim()
  const after = String(selected.value?.decision_context?.after_summary || '').trim()
  return Boolean(before && after && after.includes(before) && after.length > before.length * 1.15)
})
const recommendation = computed(() => {
  const reasons = selected.value?.decision_context?.risk_reasons || []
  if (!allClaimsVerified.value || reasons.includes('verifier_not_clean')) {
    return { tone: 'reject', title: '建议忽略', text: '至少一条知识变更没有通过自动证据核验，不建议写入正式 Wiki。' }
  }
  if (!conflictPairs.value.length && summaryLooksRedundant.value) {
    return { tone: 'reject', title: '建议忽略', text: '候选摘要主要重复当前内容，新增信息不足以支撑一次 Wiki 更新。' }
  }
  if (conflictPairs.value.some(pair => pair.relation === 'supersedes')) {
    return { tone: 'caution', title: '需要选择知识版本', text: '新论文准备替代已有结论。接受后旧结论会保留在历史中，新结论成为当前版本。' }
  }
  if (conflictPairs.value.length) {
    return { tone: 'caution', title: '建议保留两个观点', text: '两条结论分别有来源支持，但彼此不一致。接受后 Wiki 会同时记录双方，不会静默覆盖旧结论。' }
  }
  if (selected.value?.decision_context?.kind === 'create_new') {
    return { tone: 'accept', title: '建议接受', text: '这是已通过自动核验的新知识页面，接受后会写入 Wiki 并建立索引。' }
  }
  return { tone: 'accept', title: '建议接受', text: '候选内容已通过自动核验，并为现有 Wiki 增加了新的结论或信息。' }
})
const decisionTitle = computed(() => conflictPairs.value.length ? '新论文与已有 Wiki 结论不一致' : '这条历史 proposal 缺少冲突对象')
const decisionExplanation = computed(() => {
  const reasons = selected.value?.decision_context?.risk_reasons || []
  if (conflictPairs.value.length) return `系统找到了 ${conflictPairs.value.length} 组明确的冲突关系，因此暂停自动写入。下面直接展示冲突双方。`
  if (reasons.includes('verifier_not_clean')) return '至少一条变更没有通过证据验证，因此不能自动写入。'
  return '这是旧版本生成的待审批项，尚未保存可定位的冲突双方；建议忽略后用新流程重新处理。'
})
const approveLabel = computed(() => conflictPairs.value.some(pair => pair.relation === 'supersedes') ? '采用新结论' : '保留两个观点')
const rejectLabel = computed(() => '保留原结论')
const flowStates = ['EXTRACTING', 'DISTILLING', 'VERIFYING', 'COMPILING_PROPOSAL', 'AWAITING_APPROVAL', 'COMMITTING', 'COMMIT_FAILED', 'REINDEXING', 'COMPLETED']
const labels: Record<string, string> = {
  EXTRACTING: '解析', DISTILLING: '提炼', VERIFYING: '核验', COMPILING_PROPOSAL: '生成更新',
  AWAITING_APPROVAL: '审批', COMMITTING: '保存', COMMIT_FAILED: '重试', REINDEXING: '索引', COMPLETED: '完成'
}
const visibleStates = computed(() => {
  const current = String(selected.value?.run?.current_state || 'AWAITING_APPROVAL')
  const currentIndex = flowStates.indexOf(current)
  return flowStates.map((name, index) => ({
    name, label: labels[name], kind: index < currentIndex ? 'done' : index === currentIndex ? 'current' : 'pending',
    icon: index < currentIndex ? '✓' : index === currentIndex ? '●' : '○'
  }))
})

async function loadApprovals(preserve = true) {
  loading.value = true
  try {
    const { data } = await api.get('/agent-runs/approvals', { params: { status: 'action_required', limit: 100 } })
    approvals.value = data.items || []
    const target = preserve && selected.value ? approvals.value.find(item => item.id === selected.value?.id) : approvals.value[0]
    if (target) await selectApproval(target.id)
    else clearSelection()
  } catch (error) {
    console.error('[ReviewCenter] load approvals failed', error)
  } finally { loading.value = false }
}

async function selectApproval(id: string) {
  const { data } = await api.get(`/agent-runs/approvals/${id}`)
  selected.value = data
}

async function approveSelected() {
  if (!selected.value || !window.confirm(`${approveLabel.value}：「${selected.value.title}」？`)) return
  busy.value = true
  try {
    await api.post(`/agent-runs/approvals/${selected.value.id}/approve`, { reason: 'approved in review center' })
    message.success('知识更新已写入 Wiki 并重新索引。')
    await loadApprovals(false)
  } catch (error: any) { message.error(error?.response?.data?.detail || '审批失败') }
  finally { busy.value = false }
}

async function retrySelected() {
  if (!selected.value || selected.value.status !== 'commit_failed') return
  busy.value = true
  try {
    await api.post(`/agent-runs/approvals/${selected.value.id}/retry`, { reason: 'retried in review center' })
    message.success('提交已完成，Wiki 与索引状态一致。')
    await loadApprovals(false)
  } catch (error: any) { message.error(error?.response?.data?.detail || '重试提交失败') }
  finally { busy.value = false }
}

async function rejectSelected() {
  if (!selected.value || !window.confirm(`保留「${selected.value.title}」的原结论并忽略新冲突结论？`)) return
  busy.value = true
  try {
    await api.post(`/agent-runs/approvals/${selected.value.id}/reject`, { reason: 'rejected in review center' })
    message.success('已忽略这次更新，Wiki 未修改。')
    await loadApprovals(false)
  } catch (error: any) { message.error(error?.response?.data?.detail || '拒绝失败') }
  finally { busy.value = false }
}

function openEditor() {
  editedMarkdown.value = String(selected.value?.revision?.full_markdown || '')
  editorOpen.value = true
}

async function submitEdit() {
  if (!selected.value) return
  busy.value = true
  try {
    const { data } = await api.post(`/agent-runs/approvals/${selected.value.id}/edit`, {
      full_markdown: editedMarkdown.value, reason: 'edited in review center'
    })
    editorOpen.value = false
    message.success('已重新验证，并生成新的待审批 proposal。')
    await loadApprovals(false)
    if (data.approval?.id) await selectApproval(data.approval.id)
  } catch (error: any) { message.error(typeof error?.response?.data?.detail === 'string' ? error.response.data.detail : '编辑后的内容没有通过验证') }
  finally { busy.value = false }
}

function clearSelection() { selected.value = null }
function uniqueSourceCount(sources: ConflictSource[]) {
  return new Set(sources.map(source => source.source_packet_id || source.title).filter(Boolean)).size
}
function sourceTitle(sources: ConflictSource[], fallback: string) {
  if (!sources?.length) return fallback
  return sources.map(source => source.title || fallback).filter(Boolean).join('、')
}

function conflictObjectLabel(pair: ConflictPair) {
  const subject = String(pair.object?.subject || selected.value?.title || '当前知识页面').trim()
  const aspect = String(pair.object?.aspect || '同一知识问题').trim()
  const scope = Object.entries(pair.object?.scope || {})
    .filter(([, value]) => String(value || '').trim())
    .map(([key, value]) => `${key}=${value}`)
    .join(' · ')
  return [subject, aspect, scope].filter(Boolean).join(' / ')
}
function conflictReason(reason: string) {
  if (reason.includes('opposite polarity')) return '两条结论讨论同一对象，但肯定与否定关系相反。'
  if (reason.includes('explicitly supersedes')) return '新来源明确提出了用于替代已有结论的新结果。'
  if (reason.includes('explicitly challenges')) return '新来源明确挑战了已有结论。'
  return reason || '新结论与已有结论无法同时作为同一事实成立。'
}
function shortId(value: string) { return value?.slice(0, 8) || '' }
function stateLabel(value: unknown) { return labels[String(value || '')] || String(value || 'pending') }
function claimActionLabel(action?: string) {
  return ({ add_claim: '新增结论', strengthen_claim: '加强旧结论', challenge_claim: '质疑旧结论', supersede_claim: '替代旧结论' } as Record<string, string>)[String(action || '')] || '核对结论'
}
function claimVerification(claim: ClaimRow) {
  return claim.verification?.semantic_result || claim.verifier_result || claim.verification?.result
}
function isVerifiedClaim(claim: ClaimRow) {
  return ['entailed', 'supported'].includes(String(claimVerification(claim) || '').toLowerCase())
}
function verificationLabel(value: unknown) {
  return ({ entailed: '原文支持', supported: '原文支持', legacy_unverified: '历史未验证', contradicted: '原文冲突', insufficient: '证据不足' } as Record<string, string>)[String(value || '')] || '已定位原文'
}
function diffClass(line: string) { return line.startsWith('+') && !line.startsWith('+++') ? 'added' : line.startsWith('-') && !line.startsWith('---') ? 'removed' : line.startsWith('@@') ? 'hunk' : '' }

onMounted(() => { loadApprovals(false); pollTimer = window.setInterval(() => loadApprovals(true), 8000) })
onBeforeUnmount(() => window.clearInterval(pollTimer))
</script>

<style scoped>
.review-page { min-height: 100%; padding: 0; max-width: 1240px; margin: 0 auto; color: #e8ece9; }
.review-hero { display: flex; justify-content: space-between; align-items: end; gap: 20px; margin-bottom: 20px; }
.ui-button, .decision-actions button { border: 1px solid rgba(195,214,202,.16); border-radius: 9px; background: #161813; color: #cbd5d1; padding: 8px 12px; cursor: pointer; transition: border-color .2s ease, background .2s ease, transform .2s ease; }
.ui-button:hover, .decision-actions button:hover { border-color: rgba(195,214,202,.34); background: #1b1e19; }
.ui-button:active, .decision-actions button:active { transform: translateY(1px); }
.ui-button:focus-visible, .decision-actions button:focus-visible, .queue-row:focus-visible { outline: 2px solid #a7c0b6; outline-offset: 2px; }
.review-grid { display: grid; grid-template-columns: 230px minmax(0, 1fr); min-height: calc(100dvh - 220px); border: 1px solid rgba(195,214,202,.12); border-radius: 16px; overflow: hidden; background: #10110e; }
.queue-panel { padding: 15px; background: #0d0e0c; overflow: auto; }
.queue-panel { border-right: 1px solid rgba(195,214,202,.1); }
.proposal-panel { padding: 18px; overflow: auto; max-height: calc(100dvh - 220px); }
.panel-head, .section-head, .proposal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.panel-head { margin-bottom: 12px; }
.panel-head small, .section-head small { color: #718079; }
.queue-row, .claim-row { width: 100%; display: grid; text-align: left; gap: 5px; padding: 11px; margin-bottom: 7px; border: 1px solid transparent; border-radius: 10px; background: transparent; color: #cbd5d1; }
.queue-row { cursor: pointer; }
.queue-row:hover, .queue-row.active { background: rgba(155,184,173,.08); border-color: rgba(155,184,173,.22); }
.queue-row small, .claim-row small { color: #75837d; }
.proposal-head > div:first-child { min-width: 0; }
.proposal-head h2 { margin: 3px 0 0; font-size: 22px; text-wrap: balance; }
.proposal-head span { color: #849a91; font-size: 11px; letter-spacing: .12em; }
.commit-error { padding: 10px 12px; border: 1px solid rgba(244,63,94,.25); border-radius: 9px; background: rgba(244,63,94,.08); color: #f3a7ad; font-size: 12px; }
.decision-actions { display: flex; flex: 0 0 auto; gap: 8px; }
.decision-actions button { white-space: nowrap; }
.decision-actions .approve { background: #a7c0b6; color: #0d110f; }
.decision-actions .reject { color: #fda4af; }
.state-strip { display: flex; align-items: flex-start; gap: 4px; margin: 20px 0; overflow-x: auto; }
.state-node { min-width: 68px; display: grid; place-items: center; gap: 5px; color: #4f5d57; position: relative; }
.state-node:not(:last-child)::after { content: ''; width: 28px; height: 1px; background: #303a35; position: absolute; right: -16px; top: 9px; }
.state-node.done { color: #8eb7a7; }.state-node.current { color: #f2b84b; }.state-node small { font-size: 10px; }
.decision-brief { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, .8fr); gap: 18px 28px; margin-top: 20px; padding: 18px; border: 1px solid rgba(155,184,173,.16); border-radius: 13px; background: #141711; }
.decision-brief-copy span, .recommendation span, .summary-columns span { color: #86a096; font-size: 10px; letter-spacing: .13em; text-transform: uppercase; }
.decision-brief-copy h3 { margin: 5px 0 7px; font-size: 18px; }
.decision-brief-copy p { max-width: 64ch; margin: 0; color: #9ca8a3; line-height: 1.65; }
.impact-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; margin: 0; overflow: hidden; border-radius: 9px; background: rgba(195,214,202,.08); }
.impact-grid div { padding: 12px; background: #0f110d; }
.impact-grid dt { color: #708078; font-size: 10px; }
.impact-grid dd { margin: 5px 0 0; color: #d9e1dd; font-size: 13px; font-variant-numeric: tabular-nums; }
.decision-hint { grid-column: 1 / -1; margin: 0; padding-top: 13px; border-top: 1px solid rgba(195,214,202,.09); color: #9a8e6f; font-size: 12px; }
.recommendation { display: flex; align-items: center; justify-content: space-between; gap: 22px; margin-top: 14px; padding: 15px 18px; border: 1px solid rgba(155,184,173,.2); border-radius: 12px; background: rgba(155,184,173,.07); }
.recommendation > div { display: grid; gap: 3px; }
.recommendation strong { font-size: 17px; }
.recommendation p { margin: 2px 0 0; color: #9da9a4; font-size: 12px; line-height: 1.55; }
.recommendation > small { flex: 0 0 auto; color: #86a096; }
.recommendation.accept { border-color: rgba(74,222,128,.22); background: rgba(34,197,94,.06); }
.recommendation.reject { border-color: rgba(251,113,133,.22); background: rgba(244,63,94,.06); }
.recommendation.reject strong { color: #f3a7ad; }
.recommendation.caution { border-color: rgba(245,158,11,.24); background: rgba(245,158,11,.06); }
.recommendation.caution strong { color: #f4c56a; }
.conflict-section, .summary-compare, .claims-section { margin-top: 20px; }
.conflict-pair { display: grid; grid-template-columns: minmax(0, 1fr) 64px minmax(0, 1fr); margin-top: 10px; overflow: hidden; border: 1px solid rgba(195,214,202,.13); border-radius: 13px; background: #0f110d; }
.conflict-object { grid-column: 1 / -1; padding: 12px 17px; border-bottom: 1px solid rgba(195,214,202,.08); background: #12150f; }
.conflict-object span { color: #81978e; font-size: 10px; font-weight: 650; letter-spacing: .13em; }
.conflict-object strong { display: block; margin-top: 5px; color: #d9e2de; font-size: 13px; font-weight: 600; }
.conflict-side { min-width: 0; padding: 17px; }
.conflict-side.incoming { background: rgba(244,63,94,.045); }
.conflict-side.existing { background: rgba(155,184,173,.045); }
.conflict-side > span, .conflict-pair footer > span { color: #81978e; font-size: 10px; font-weight: 650; letter-spacing: .13em; }
.conflict-side strong { display: block; margin-top: 5px; color: #e1e7e4; font-size: 13px; line-height: 1.45; text-wrap: balance; }
.conflict-side blockquote { margin: 16px 0 0; color: #c2cbc7; font-size: 14px; line-height: 1.7; }
.conflict-relation { display: grid; place-content: center; place-items: center; gap: 6px; border-right: 1px solid rgba(195,214,202,.08); border-left: 1px solid rgba(195,214,202,.08); color: #74827c; }
.conflict-relation b { color: #efb3ad; font-size: 11px; font-weight: 650; }
.conflict-relation span { font-family: Consolas, monospace; font-size: 10px; }
.conflict-pair footer { grid-column: 1 / -1; padding: 12px 17px 14px; border-top: 1px solid rgba(195,214,202,.08); background: #0c0e0b; }
.conflict-pair footer p { margin: 5px 0 0; color: #929f99; font-size: 12px; line-height: 1.55; }
.summary-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.summary-columns article { padding: 14px; border-radius: 10px; background: #11130f; }
.summary-columns article:last-child { background: #151811; }
.summary-columns p { margin: 7px 0 0; color: #aab5b0; font-size: 12px; line-height: 1.65; white-space: pre-wrap; }
.claim-row { background: #131510; border-color: rgba(195,214,202,.08); }
.technical-details { margin-top: 20px; border-top: 1px solid rgba(195,214,202,.09); }
.technical-details summary { display: flex; justify-content: space-between; gap: 12px; padding: 14px 0; color: #82918b; cursor: pointer; list-style: none; }
.technical-details summary::-webkit-details-marker { display: none; }
.technical-details summary::before { content: '＋'; margin-right: 8px; color: #89a198; }
.technical-details[open] summary::before { content: '−'; }
.technical-details summary span { flex: 1; }
.technical-details summary small { color: #65726c; }
.diff-view { max-height: 430px; overflow: auto; padding: 14px; border-radius: 12px; background: #090a08; color: #aeb9b4; font-size: 12px; line-height: 1.55; white-space: pre-wrap; }
.diff-view span { display: block; min-height: 1.5em; }.diff-view .added { color: #a7d8b8; background: rgba(34,197,94,.08); }.diff-view .removed { color: #f3a7ad; background: rgba(244,63,94,.08); }.diff-view .hunk { color: #9bb8d4; }
.empty { color: #69756f; font-size: 13px; }.empty.large { min-height: 400px; display: grid; place-items: center; }
.editor-note { color: #95a49e; }.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 1180px) { .review-grid { grid-template-columns: 210px 1fr; }.proposal-head { align-items: flex-start; flex-direction: column; }.decision-actions { width: 100%; flex-wrap: wrap; } }
@media (max-width: 860px) { .decision-brief { grid-template-columns: 1fr; }.decision-hint { grid-column: auto; }.summary-columns { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .review-grid { grid-template-columns: 1fr; }.queue-panel { border-right: 0; }.impact-grid { grid-template-columns: 1fr; }.recommendation { align-items: flex-start; flex-direction: column; }.conflict-pair { grid-template-columns: 1fr; }.conflict-relation { grid-auto-flow: column; border: 0; border-top: 1px solid rgba(195,214,202,.08); border-bottom: 1px solid rgba(195,214,202,.08); padding: 8px; }.conflict-pair footer { grid-column: auto; } }
@media (max-width: 1320px) { .decision-brief { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .proposal-panel { max-height: none; padding: 16px; } .queue-panel { border-bottom: 1px solid var(--line); } }
</style>
