<template>
  <section class="capture-page">
    <header class="capture-header">
      <div class="capture-intro">
        <p class="eyebrow">KNOWLEDGE INGESTION</p>
        <h1>资料编译</h1>
        <p>上传原始资料，系统会保留证据坐标，并把可验证结论编译进 Markdown Wiki。</p>
      </div>
      <div class="capture-mode">
        <button type="button" :class="{ active: activeMode === 'paper' }" @click="activeMode = 'paper'">论文 PDF</button>
        <button type="button" :class="{ active: activeMode === 'xhs' }" @click="activeMode = 'xhs'">小红书笔记</button>
      </div>
    </header>

    <div class="capture-layout">
      <section v-show="activeMode === 'paper'" class="capture-panel paper-panel">
        <div class="panel-head">
          <div>
            <span class="capture-icon">PDF</span>
            <div>
              <h2>论文证据编译</h2>
              <p>只需选择文件，不需要维护项目目录。</p>
            </div>
          </div>
          <span class="system-badge">Evidence-first Wiki</span>
        </div>

        <ol class="compiler-flow" aria-label="论文编译流程">
          <li>
            <span>01</span>
            <div><strong>结构化证据</strong><small>Docling 保留段落、表格、页码与 bbox</small></div>
          </li>
          <li>
            <span>02</span>
            <div><strong>Wiki Patch</strong><small>Resolver 定位页面，Compiler 生成 claim-aware diff</small></div>
          </li>
          <li>
            <span>03</span>
            <div><strong>验证与提交</strong><small>Verifier 回读原文，高风险变更进入审批</small></div>
          </li>
        </ol>

        <div class="upload-card">
          <label class="field-label" for="paper-source-url">来源信息 <span>可选</span></label>
          <n-input
            id="paper-source-url"
            v-model:value="paperSourceUrl"
            placeholder="arXiv、DOI 或论文主页链接"
          />
          <div class="file-row">
            <label class="file-label" :class="{ selected: selectedFile }">
              <input type="file" accept="application/pdf" hidden @change="onFileChange" />
              <span class="file-mark">PDF</span>
              <span class="file-copy">
                <strong>{{ selectedFile ? selectedFile.name : '选择一篇 PDF' }}</strong>
                <small>{{ selectedFile ? formatSize(selectedFile.size) : '文件会复制到受管存储，并保留原始版本' }}</small>
              </span>
              <span class="file-action">{{ selectedFile ? '重新选择' : '浏览文件' }}</span>
            </label>
            <n-button type="primary" size="large" :loading="uploading" :disabled="!selectedFile || paperBusy" @click="uploadPaper">
              开始编译
            </n-button>
          </div>
        </div>

        <div class="policy-note">
          <span class="policy-dot"></span>
          <p><strong>默认自动编译。</strong>新建、补充和增加来源会自动提交；只有新论文与已有结论发生冲突时，任务才会停在审批中心。</p>
        </div>
        <n-alert v-if="paperBusy" type="info" class="import-tip" :bordered="false">
          正在后台编译。耗时取决于页数、表格数量和是否需要 OCR，你可以离开本页，任务不会中断。
        </n-alert>
        <n-alert v-if="paperNotice" :type="paperNoticeType" closable @close="paperNotice = ''">{{ paperNotice }}</n-alert>

        <section class="job-panel">
          <div class="paper-impact-head">
            <div>
              <strong>最近编译任务</strong>
              <span>{{ ingestionJobs.length ? `显示最近 ${Math.min(ingestionJobs.length, 6)} 条` : '上传后可在这里追踪状态' }}</span>
            </div>
            <button type="button" @click="loadIngestionJobs">刷新状态</button>
          </div>
          <div v-if="ingestionJobs.length" class="job-list">
            <article v-for="job in ingestionJobs.slice(0, 6)" :key="job.id" class="job-row" :class="{ current: job.id === currentJobId }">
              <div class="job-title-row">
                <div class="job-title">
                  <strong>{{ job.metadata?.filename || job.source_uri }}</strong>
                  <span>{{ formatStage(job.stage) }} · {{ progressPercent(job.progress) }}%</span>
                </div>
                <span class="status-chip" :data-tone="statusTone(job.status)">{{ formatJobStatus(job.status) }}</span>
              </div>
              <div class="job-progress" :aria-label="`${progressPercent(job.progress)}%`">
                <span :style="{ width: `${progressPercent(job.progress)}%` }"></span>
              </div>
              <router-link v-if="job.status === 'waiting'" class="job-review-link" to="/reviews">查看冲突来源与结论</router-link>
              <small v-if="job.error" class="job-error">{{ job.error }}</small>
            </article>
          </div>
          <div v-else class="job-empty">
            <span>00</span>
            <p><strong>还没有编译任务</strong><small>选择 PDF 后，解析、验证和提交进度会显示在这里。</small></p>
          </div>
        </section>

        <div v-if="paperImpact" class="paper-impact">
          <div class="paper-impact-head">
            <div>
              <strong>本次 Wiki 变更</strong>
              <span>已通过证据验证</span>
            </div>
            <span>{{ formatSeconds(paperImpact.timings) }}</span>
          </div>
          <div class="paper-impact-grid">
            <div><b>{{ paperImpact.created_cards?.length || 0 }}</b><span>新建页面</span></div>
            <div><b>{{ paperImpact.updated_cards?.length || 0 }}</b><span>更新页面</span></div>
            <div><b>{{ paperImpact.linked_cards?.length || 0 }}</b><span>新增关联</span></div>
            <div><b>{{ paperImpact.review_rejections?.length || 0 }}</b><span>拒绝变更</span></div>
          </div>
          <ul v-if="impactItems.length" class="paper-impact-list">
            <li v-for="item in impactItems" :key="item.key"><span>{{ formatImpactKind(item.kind) }}</span>{{ item.title }}</li>
          </ul>
        </div>
      </section>

      <section v-show="activeMode === 'xhs'" class="capture-panel xhs-panel">
        <div class="panel-head">
          <div>
            <span class="capture-icon">XHS</span>
            <div>
              <h2>小红书笔记</h2>
              <p>保存链接、分享文案与图片 OCR，形成可检索的资料页。</p>
            </div>
          </div>
          <span class="system-badge">Lightweight capture</span>
        </div>

        <n-input
          v-model:value="xhsText"
          type="textarea"
          placeholder="粘贴 xiaohongshu.com / xhslink.com 链接，或完整分享文案"
          :autosize="{ minRows: 7, maxRows: 12 }"
        />
        <n-input v-model:value="xhsTagsText" placeholder="标签，可选，例如：LLM, 面经, 秋招" />
        <n-button type="primary" :loading="importingXhs" :disabled="!xhsText.trim()" @click="importXhs">
          保存到知识库
        </n-button>
        <n-alert v-if="xhsNotice" type="success" closable @close="xhsNotice = ''">{{ xhsNotice }}</n-alert>

        <div class="xhs-route-note">
          <strong>与论文编译分流</strong>
          <span>小红书笔记作为直接来源页保存，不生成论文 claim，也不会伪造 PDF 级证据坐标。</span>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NAlert, NButton, NInput } from 'naive-ui'
import { api } from '../api'

const activeMode = ref<'paper' | 'xhs'>('paper')

type IngestionJob = {
  id: string
  source_uri: string
  status: 'queued' | 'running' | 'done' | 'failed' | string
  stage: string
  progress: number
  error: string
  paper_card_id?: string
  result?: any
  metadata?: {
    filename?: string
    source_url?: string
    pipeline?: string
    approval_mode?: string
  }
}

const selectedFile = ref<File | null>(null)
const paperSourceUrl = ref('')
const uploading = ref(false)
const paperNotice = ref('')
const paperNoticeType = ref<'success' | 'info' | 'warning' | 'error'>('success')
const paperImpact = ref<any | null>(null)
const ingestionJobs = ref<IngestionJob[]>([])
const currentJobId = ref('')
let jobPollTimer: number | null = null
const hasActivePaperJob = computed(() => ingestionJobs.value.some((job) => ['queued', 'running'].includes(job.status)))
const hasTrackablePaperJob = computed(() => ingestionJobs.value.some((job) => ['queued', 'running', 'waiting'].includes(job.status)))
const paperBusy = computed(() => uploading.value || hasActivePaperJob.value)

const xhsText = ref('')
const xhsTagsText = ref('')
const importingXhs = ref(false)
const xhsNotice = ref('')

const impactItems = computed(() => {
  if (!paperImpact.value) return []
  const created = (paperImpact.value.created_cards || []).map((item: any) => ({ key: `created:${item.id}`, kind: 'created', title: item.title }))
  const updated = (paperImpact.value.updated_cards || []).map((item: any) => ({ key: `updated:${item.id}`, kind: 'updated', title: item.title }))
  const linked = (paperImpact.value.linked_cards || []).map((item: any, index: number) => ({ key: `linked:${item.to || index}`, kind: 'linked', title: item.title }))
  return [...created, ...updated, ...linked].slice(0, 10)
})

async function loadIngestionJobs() {
  const { data } = await api.get('/wiki/ingest/jobs', { params: { limit: 20 } })
  ingestionJobs.value = data.items || []
  const current = ingestionJobs.value.find((job) => job.id === currentJobId.value)
  if (current?.status === 'done') {
    paperImpact.value = current.result || null
    paperNoticeType.value = 'success'
    paperNotice.value = `论文已入库：新建 ${current.result?.created_cards?.length || 0}，更新 ${current.result?.updated_cards?.length || 0}，关联 ${current.result?.linked_cards?.length || 0}。`
    currentJobId.value = ''
  } else if (current?.status === 'failed') {
    paperNoticeType.value = 'error'
    paperNotice.value = `论文入库失败：${current.error || 'unknown error'}`
    currentJobId.value = ''
  } else if (current?.status === 'rejected') {
    paperNoticeType.value = 'warning'
    paperNotice.value = '本次 Wiki proposal 已全部拒绝，正式 Wiki 未发生变化。'
    currentJobId.value = ''
  } else if (current?.status === 'waiting') {
    paperNoticeType.value = 'info'
    paperNotice.value = '论文已完成编译和验证，正在等待你审批 Markdown diff。'
  }
  if (hasTrackablePaperJob.value) {
    startJobPolling()
  } else {
    stopJobPolling()
  }
}

function startJobPolling() {
  if (jobPollTimer !== null) return
  jobPollTimer = window.setInterval(() => {
    loadIngestionJobs().catch((error) => console.error('[CaptureNote] failed to poll ingestion jobs:', error))
  }, 3000)
}

function stopJobPolling() {
  if (jobPollTimer === null) return
  window.clearInterval(jobPollTimer)
  jobPollTimer = null
}

function trackCreatedJob(job: IngestionJob) {
  currentJobId.value = job.id
  ingestionJobs.value = [job, ...ingestionJobs.value.filter((item) => item.id !== job.id)]
  paperNoticeType.value = 'info'
  paperNotice.value = `已创建后台入库任务：${job.metadata?.filename || job.source_uri}`
  startJobPolling()
}

function onFileChange(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0] || null
  if (file && file.type && file.type !== 'application/pdf') {
    selectedFile.value = null
    paperNoticeType.value = 'error'
    paperNotice.value = '请选择 PDF 文件。'
    return
  }
  selectedFile.value = file
  paperNotice.value = ''
}

async function uploadPaper() {
  if (!selectedFile.value) return
  uploading.value = true
  paperNotice.value = ''
  paperImpact.value = null
  try {
    const form = new FormData()
    form.append('file', selectedFile.value)
    form.append('source_url', paperSourceUrl.value)
    const { data } = await api.post('/wiki/ingest', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 20000
    })
    if (data.already_exists) {
      paperNoticeType.value = 'info'
      paperNotice.value = `《${data.title || selectedFile.value.name}》已经在知识库中，无需重复入库。`
      selectedFile.value = null
      paperSourceUrl.value = ''
      await loadIngestionJobs()
      return
    }
    trackCreatedJob(data.job)
    selectedFile.value = null
    paperSourceUrl.value = ''
  } catch (error: any) {
    paperNoticeType.value = 'error'
    paperNotice.value = `无法创建编译任务：${error?.response?.data?.detail || error?.message || '请检查后端服务。'}`
  } finally {
    uploading.value = false
  }
}

async function importXhs() {
  importingXhs.value = true
  xhsNotice.value = ''
  try {
    const { data } = await api.post('/wiki/import-xhs', {
      text_or_url: xhsText.value,
      tags: splitTags(xhsTagsText.value)
    }, { timeout: 120000 })
    xhsNotice.value = data.deduped
      ? `已更新：《${data.title}》`
      : `已保存：《${data.title}》，图片 ${data.images_downloaded || 0}，关联 ${data.linked_cards?.length || 0}，OCR: ${data.ocr_status}`
    xhsText.value = ''
    xhsTagsText.value = ''
  } finally {
    importingXhs.value = false
  }
}

function splitTags(raw: string) {
  return raw.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean)
}

function formatSize(size: number) {
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024).toFixed(1)} KB`
}

function progressPercent(progress: number) {
  return Math.max(0, Math.min(100, Math.round(Number(progress || 0) * 100)))
}

function formatJobStatus(status: string) {
  const labels: Record<string, string> = {
    queued: '等待执行',
    running: '编译中',
    waiting: '等待审批',
    done: '已完成',
    failed: '失败',
    rejected: '已拒绝'
  }
  return labels[status] || status
}

function statusTone(status: string) {
  if (status === 'done') return 'success'
  if (status === 'waiting') return 'warning'
  if (status === 'failed' || status === 'rejected') return 'danger'
  if (status === 'running') return 'active'
  return 'muted'
}

function formatStage(stage: string) {
  const normalized = String(stage || '').toLowerCase()
  const labels: Record<string, string> = {
    queued: '任务排队',
    docling_extracting: 'Docling 证据提取',
    extracting: '证据提取',
    distilling: '结论蒸馏',
    verifying: '证据验证',
    compiling_proposal: '生成 Wiki Patch',
    awaiting_approval: '风险审批',
    committing: '提交 Revision',
    reindexing: '重建 Resolver 索引',
    done: 'Wiki 已更新'
  }
  return labels[normalized] || stage || '准备中'
}

function formatImpactKind(kind: string) {
  return ({ created: '新建', updated: '更新', linked: '关联' } as Record<string, string>)[kind] || kind
}

function formatSeconds(timings: Record<string, number> | undefined) {
  if (!timings) return ''
  const total = Object.entries(timings)
    .filter(([key]) => key.endsWith('_seconds'))
    .reduce((sum, [, item]) => sum + Number(item || 0), 0)
  return total ? `${total.toFixed(1)}s` : ''
}

onMounted(() => {
  loadIngestionJobs().catch((error) => console.error('[CaptureNote] failed to load ingestion jobs:', error))
})

onBeforeUnmount(stopJobPolling)
</script>

<style scoped>
.capture-page {
  max-width: 1240px;
  margin: 0 auto;
  display: grid;
  gap: 16px;
}

.capture-header,
.capture-panel {
  border: 1px solid var(--line);
  background: rgba(21, 19, 15, 0.88);
  box-shadow: 0 22px 54px rgba(8, 7, 6, 0.28);
}

.capture-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 28px;
  min-height: 148px;
  padding: 28px 30px 32px;
  border-radius: 18px;
  background:
    radial-gradient(circle at 92% 12%, rgba(155, 184, 173, 0.12), transparent 34%),
    rgba(21, 19, 15, 0.9);
}

.capture-header h1 {
  margin: 2px 0 8px;
  color: #fff;
  font-size: clamp(32px, 4vw, 48px);
  line-height: 1.02;
  letter-spacing: -0.045em;
  text-wrap: balance;
}

.capture-intro > p:last-child {
  max-width: 630px;
  margin: 0;
  color: var(--text-muted);
  font-size: 14px;
  line-height: 1.75;
}

.capture-mode {
  display: flex;
  gap: 6px;
  padding: 4px;
  flex: 0 0 auto;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: rgba(11, 9, 8, 0.58);
}

.capture-mode button {
  min-height: 40px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--text-muted);
  font-weight: 600;
  cursor: pointer;
  transition: border-color 180ms ease, background 180ms ease, color 180ms ease, transform 180ms ease;
}

.capture-mode button:hover,
.capture-mode button.active {
  border-color: var(--line-strong);
  background: var(--accent-soft);
  color: #eef5f1;
}

.capture-mode button:active {
  transform: translateY(1px) scale(0.99);
}

.capture-mode button:focus-visible,
.file-label:focus-within,
.paper-impact-head button:focus-visible,
.job-review-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
}

.capture-layout {
  display: grid;
  grid-template-columns: 1fr;
}

.capture-panel {
  display: grid;
  gap: 18px;
  padding: 26px;
  border-radius: 18px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: center;
}

.panel-head > div {
  display: flex;
  align-items: center;
  gap: 14px;
}

.panel-head h2 {
  margin: 0;
  color: #fff;
  font-size: 23px;
  letter-spacing: -0.025em;
}

.panel-head p {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.5;
}

.system-badge {
  flex: 0 0 auto;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: rgba(155, 184, 173, 0.08);
  color: #bfd0c8;
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.capture-icon {
  width: 46px;
  height: 46px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border: 1px solid var(--line-strong);
  border-radius: 11px;
  background: var(--accent-soft);
  color: #dce8e2;
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 12px;
  font-weight: 800;
}

.compiler-flow {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1px;
  margin: 0;
  padding: 1px;
  list-style: none;
  border-radius: 13px;
  background: var(--line);
  overflow: hidden;
}

.compiler-flow li {
  min-width: 0;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 11px;
  padding: 15px;
  background: #12100d;
}

.compiler-flow li > span {
  color: var(--accent);
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 12px;
  font-weight: 700;
}

.compiler-flow li > div {
  display: grid;
  gap: 4px;
}

.compiler-flow strong {
  color: #edf2ef;
  font-size: 13px;
  font-weight: 650;
}

.compiler-flow small {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.55;
}

.upload-card {
  display: grid;
  gap: 10px;
  padding: 18px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(11, 9, 8, 0.48);
}

.field-label {
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 650;
}

.field-label span {
  margin-left: 5px;
  color: var(--text-muted);
  font-weight: 500;
}

.file-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px;
  gap: 10px;
  align-items: stretch;
}

.file-label {
  min-width: 0;
  min-height: 74px;
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px dashed var(--line-strong);
  border-radius: 12px;
  color: var(--text-soft);
  cursor: pointer;
  transition: border-color 180ms ease, color 180ms ease, background 180ms ease;
}

.file-label:hover,
.file-label.selected {
  border-color: rgba(155, 184, 173, 0.54);
  background: rgba(155, 184, 173, 0.08);
  color: var(--text);
}

.file-mark {
  width: 42px;
  height: 42px;
  display: inline-grid;
  place-items: center;
  border-radius: 9px;
  background: rgba(155, 184, 173, 0.12);
  color: #d4e3d8;
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 10px;
  font-weight: 750;
}

.file-copy {
  min-width: 0;
  display: grid;
  gap: 4px;
}

.file-copy strong,
.file-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-copy strong {
  color: #eef2f0;
  font-size: 13px;
  font-weight: 650;
}

.file-copy small,
.file-action {
  color: var(--text-muted);
  font-size: 11px;
}

.file-action {
  color: #b8ccc3;
  font-weight: 650;
}

.policy-note {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  gap: 11px;
  align-items: start;
  padding: 3px 2px;
}

.policy-dot {
  width: 7px;
  height: 7px;
  margin-top: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 4px rgba(155, 184, 173, 0.1);
}

.policy-note p {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.policy-note strong {
  color: var(--text-soft);
  font-weight: 650;
}

.import-tip {
  font-size: 13px;
  line-height: 1.6;
}

.paper-impact,
.job-panel,
.xhs-route-note {
  display: grid;
  gap: 13px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: rgba(11, 9, 8, 0.38);
}

.paper-impact-head,
.paper-impact-grid {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.paper-impact-head > div {
  display: grid;
  gap: 3px;
}

.paper-impact-head strong,
.xhs-route-note strong {
  color: #e8eeeb;
  font-size: 14px;
  font-weight: 650;
}

.paper-impact-head span,
.xhs-route-note span {
  color: var(--text-muted);
  font-size: 12px;
}

.paper-impact-head button {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(155, 184, 173, 0.08);
  color: var(--text-soft);
  cursor: pointer;
  padding: 7px 10px;
  font-size: 12px;
  transition: border-color 180ms ease, background 180ms ease, color 180ms ease;
}

.paper-impact-head button:hover {
  border-color: var(--line-strong);
  background: var(--accent-soft);
  color: var(--text);
}

.job-list {
  display: grid;
  gap: 8px;
}

.job-row {
  display: grid;
  gap: 9px;
  padding: 12px 13px;
  border: 1px solid rgba(195, 214, 202, 0.1);
  border-radius: 10px;
  background: rgba(18, 16, 13, 0.72);
  transition: border-color 180ms ease, background 180ms ease;
}

.job-row.current {
  border-color: var(--line-strong);
  background: rgba(155, 184, 173, 0.07);
}

.job-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.job-title {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.job-title strong {
  overflow: hidden;
  color: #e7ece9;
  font-size: 13px;
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-title > span,
.job-error {
  color: var(--text-muted);
  font-size: 12px;
}

.job-error {
  color: #fecdd3;
}

.status-chip {
  flex: 0 0 auto;
  padding: 4px 7px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 650;
}

.status-chip[data-tone="success"] {
  border-color: rgba(74, 222, 128, 0.2);
  background: rgba(34, 197, 94, 0.08);
  color: #a7d8b5;
}

.status-chip[data-tone="warning"] {
  border-color: rgba(245, 158, 11, 0.25);
  background: rgba(245, 158, 11, 0.08);
  color: #f0cb84;
}

.status-chip[data-tone="danger"] {
  border-color: rgba(244, 63, 94, 0.22);
  background: rgba(244, 63, 94, 0.08);
  color: #fda4af;
}

.status-chip[data-tone="active"] {
  border-color: var(--line-strong);
  background: var(--accent-soft);
  color: #d4e3d8;
}

.job-progress {
  height: 4px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.12);
}

.job-progress span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #78968b, #b1c7be);
  transition: width 260ms ease;
}

.job-review-link {
  width: fit-content;
  color: #e9c985;
  font-size: 12px;
  font-weight: 650;
  text-decoration: none;
}

.job-review-link:hover {
  text-decoration: underline;
}

.job-empty {
  min-height: 106px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  border: 1px dashed var(--line);
  border-radius: 10px;
  color: var(--text-muted);
}

.job-empty > span {
  color: rgba(155, 184, 173, 0.42);
  font-family: "Cascadia Code", "SFMono-Regular", Consolas, monospace;
  font-size: 24px;
}

.job-empty p {
  display: grid;
  gap: 4px;
  margin: 0;
}

.job-empty strong {
  color: var(--text-soft);
  font-size: 13px;
}

.job-empty small {
  font-size: 11px;
}

.paper-impact-grid > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.paper-impact-grid b {
  color: #fff;
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}

.paper-impact-grid span,
.paper-impact-list {
  color: var(--text-soft);
  font-size: 12px;
}

.paper-impact-list {
  display: grid;
  gap: 5px;
  margin: 0;
  padding-left: 16px;
}

.paper-impact-list span {
  margin-right: 6px;
  color: var(--accent);
  font-weight: 700;
}

@media (max-width: 860px) {
  .capture-header {
    display: grid;
    padding: 24px;
  }

  .compiler-flow,
  .file-row,
  .paper-impact-grid {
    grid-template-columns: 1fr;
  }

  .compiler-flow {
    gap: 1px;
  }

  .capture-mode {
    width: 100%;
  }

  .capture-mode button {
    flex: 1;
  }
}

@media (max-width: 560px) {
  .capture-header,
  .capture-panel {
    padding: 18px;
    border-radius: 14px;
  }

  .panel-head {
    align-items: flex-start;
  }

  .system-badge {
    display: none;
  }

  .file-label {
    grid-template-columns: 38px minmax(0, 1fr);
  }

  .file-action {
    display: none;
  }

  .job-title-row {
    align-items: flex-start;
  }
}
</style>
