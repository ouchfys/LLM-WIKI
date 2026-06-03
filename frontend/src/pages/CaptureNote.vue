<template>
  <section class="capture-page">
    <header class="capture-header">
      <div>
        <p class="eyebrow">CAPTURE</p>
        <h1>采集台</h1>
      </div>
      <div class="capture-mode">
        <button type="button" :class="{ active: activeMode === 'paper' }" @click="activeMode = 'paper'">论文 PDF</button>
        <button type="button" :class="{ active: activeMode === 'xhs' }" @click="activeMode = 'xhs'">小红书链接</button>
      </div>
    </header>

    <div class="capture-layout">
      <section v-show="activeMode === 'paper'" class="capture-panel paper-panel">
        <div class="panel-head">
          <div>
            <span class="capture-icon">PDF</span>
            <h2>论文导入</h2>
          </div>
          <small>Docling + four-agent</small>
        </div>

        <n-input v-model:value="paperSourceUrl" placeholder="arXiv / DOI / 论文来源链接，可选" />
        <div class="file-row">
          <label class="file-label">
            <input type="file" accept="application/pdf" hidden @change="onFileChange" />
            {{ selectedFile ? selectedFile.name : '选择 PDF 文件' }}
          </label>
          <n-button type="primary" :loading="uploading" :disabled="!selectedFile || paperBusy" @click="uploadPaper">
            上传解析
          </n-button>
        </div>

        <div class="divider"><span>或</span></div>

        <div class="file-row">
          <n-select
            v-model:value="selectedLocalPdf"
            filterable
            clearable
            placeholder="从 data/ 选择已有 PDF"
            :options="localPdfOptions"
          />
          <n-button :loading="indexing" :disabled="!selectedLocalPdf || paperBusy" @click="indexLocalPaper">
            解析
          </n-button>
        </div>

        <n-alert v-if="paperBusy" type="info" class="import-tip" :bordered="false">
          正在解析论文并生成知识卡片，通常需要 2-5 分钟。
        </n-alert>
        <n-alert v-if="paperNotice" type="success" closable @close="paperNotice = ''">{{ paperNotice }}</n-alert>

        <div v-if="ingestionJobs.length" class="job-panel">
          <div class="paper-impact-head">
            <strong>Ingestion jobs</strong>
            <button type="button" @click="loadIngestionJobs">Refresh</button>
          </div>
          <div v-for="job in ingestionJobs.slice(0, 4)" :key="job.id" class="job-row">
            <div>
              <strong>{{ job.metadata?.filename || job.source_uri }}</strong>
              <span>{{ job.stage }} · {{ job.status }}</span>
            </div>
            <div class="job-progress">
              <span :style="{ width: `${Math.round((job.progress || 0) * 100)}%` }"></span>
            </div>
            <small v-if="job.error">{{ job.error }}</small>
          </div>
        </div>

        <div v-if="paperImpact" class="paper-impact">
          <div class="paper-impact-head">
            <strong>Paper merge</strong>
            <span>{{ formatSeconds(paperImpact.timings) }}</span>
          </div>
          <div class="paper-impact-grid">
            <div><b>{{ paperImpact.created_cards?.length || 0 }}</b><span>Created</span></div>
            <div><b>{{ paperImpact.updated_cards?.length || 0 }}</b><span>Updated</span></div>
            <div><b>{{ paperImpact.linked_cards?.length || 0 }}</b><span>Linked</span></div>
            <div><b>{{ paperImpact.review_rejections?.length || 0 }}</b><span>Rejected</span></div>
          </div>
          <ul v-if="impactItems.length" class="paper-impact-list">
            <li v-for="item in impactItems" :key="item.key"><span>{{ item.kind }}</span>{{ item.title }}</li>
          </ul>
        </div>
      </section>

      <section v-show="activeMode === 'xhs'" class="capture-panel xhs-panel">
        <div class="panel-head">
          <div>
            <span class="capture-icon">XHS</span>
            <h2>小红书链接</h2>
          </div>
          <small>direct source card</small>
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
          <strong>轻量链路</strong>
          <span>只保存链接、分享内容、图片和 OCR，不做论文四 agent 流程。</span>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NAlert, NButton, NInput, NSelect } from 'naive-ui'
import { api, type LocalPdf } from '../api'

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
  }
}

const localPdfs = ref<LocalPdf[]>([])
const selectedLocalPdf = ref<string | null>(null)
const selectedFile = ref<File | null>(null)
const paperSourceUrl = ref('')
const uploading = ref(false)
const indexing = ref(false)
const paperNotice = ref('')
const paperImpact = ref<any | null>(null)
const ingestionJobs = ref<IngestionJob[]>([])
const currentJobId = ref('')
let jobPollTimer: number | null = null
const hasActivePaperJob = computed(() => ingestionJobs.value.some((job) => ['queued', 'running'].includes(job.status)))
const paperBusy = computed(() => uploading.value || indexing.value || hasActivePaperJob.value)

const xhsText = ref('')
const xhsTagsText = ref('')
const importingXhs = ref(false)
const xhsNotice = ref('')

const localPdfOptions = computed(() => localPdfs.value.map((file) => ({
  label: `${file.name} (${formatSize(file.size)})`,
  value: file.path
})))

const impactItems = computed(() => {
  if (!paperImpact.value) return []
  const created = (paperImpact.value.created_cards || []).map((item: any) => ({ key: `created:${item.id}`, kind: 'created', title: item.title }))
  const updated = (paperImpact.value.updated_cards || []).map((item: any) => ({ key: `updated:${item.id}`, kind: 'updated', title: item.title }))
  const linked = (paperImpact.value.linked_cards || []).map((item: any, index: number) => ({ key: `linked:${item.to || index}`, kind: 'linked', title: item.title }))
  return [...created, ...updated, ...linked].slice(0, 10)
})

async function loadLocalPdfs() {
  const { data } = await api.get('/papers/files')
  localPdfs.value = data.items || []
}

async function loadIngestionJobs() {
  const { data } = await api.get('/wiki/ingest/jobs', { params: { limit: 20 } })
  ingestionJobs.value = data.items || []
  const current = ingestionJobs.value.find((job) => job.id === currentJobId.value)
  if (current?.status === 'done') {
    paperImpact.value = current.result?.pipeline === 'four_agent' ? current.result : null
    paperNotice.value = `论文已入库：新建 ${current.result?.created_cards?.length || 0}，更新 ${current.result?.updated_cards?.length || 0}，关联 ${current.result?.linked_cards?.length || 0}。`
    currentJobId.value = ''
    await loadLocalPdfs()
  } else if (current?.status === 'failed') {
    paperNotice.value = `论文入库失败：${current.error || 'unknown error'}`
    currentJobId.value = ''
  }
  if (hasActivePaperJob.value) {
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
  paperNotice.value = `已创建后台入库任务：${job.metadata?.filename || job.source_uri}`
  startJobPolling()
}

function onFileChange(event: Event) {
  selectedFile.value = (event.target as HTMLInputElement).files?.[0] || null
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
    form.append('pipeline', 'four_agent')
    const { data } = await api.post('/wiki/ingest', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 20000
    })
    trackCreatedJob(data.job)
    selectedFile.value = null
    paperSourceUrl.value = ''
    await loadLocalPdfs()
  } finally {
    uploading.value = false
  }
}

async function indexLocalPaper() {
  if (!selectedLocalPdf.value) return
  indexing.value = true
  paperNotice.value = ''
  paperImpact.value = null
  try {
    const form = new FormData()
    form.append('local_path', selectedLocalPdf.value)
    form.append('source_url', paperSourceUrl.value)
    form.append('pipeline', 'four_agent')
    const { data } = await api.post('/wiki/ingest', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 20000
    })
    trackCreatedJob(data.job)
    selectedLocalPdf.value = null
    paperSourceUrl.value = ''
  } finally {
    indexing.value = false
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

function formatSeconds(timings: Record<string, number> | undefined) {
  if (!timings) return ''
  const total = Object.entries(timings)
    .filter(([key]) => key.endsWith('_seconds'))
    .reduce((sum, [, item]) => sum + Number(item || 0), 0)
  return total ? `${total.toFixed(1)}s` : ''
}

onMounted(() => {
  loadLocalPdfs()
  loadIngestionJobs().catch((error) => console.error('[CaptureNote] failed to load ingestion jobs:', error))
})

onBeforeUnmount(stopJobPolling)
</script>

<style scoped>
.capture-page {
  max-width: 1180px;
  margin: 0 auto;
  display: grid;
  gap: 14px;
}

.capture-header,
.capture-panel {
  border: 1px solid rgba(125, 211, 252, 0.14);
  border-radius: 16px;
  background:
    linear-gradient(180deg, rgba(8, 18, 32, 0.94), rgba(5, 10, 19, 0.98)),
    #07111f;
  box-shadow: 0 18px 48px rgba(2, 6, 23, 0.28);
}

.capture-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 112px;
  padding: 20px 22px;
}

.capture-header h1 {
  margin: 0;
  color: #fff;
  font-size: 34px;
  line-height: 1.12;
}

.capture-mode {
  display: flex;
  gap: 6px;
  padding: 4px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 12px;
  background: rgba(2, 6, 23, 0.26);
}

.capture-mode button {
  min-height: 36px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: border-color 180ms ease, background 180ms ease, color 180ms ease, transform 180ms ease;
}

.capture-mode button:hover,
.capture-mode button.active {
  border-color: rgba(125, 211, 252, 0.22);
  background: rgba(56, 189, 248, 0.1);
  color: #ecfeff;
}

.capture-mode button:active {
  transform: translateY(1px) scale(0.99);
}

.capture-layout {
  display: grid;
  grid-template-columns: 1fr;
}

.capture-panel {
  display: grid;
  gap: 13px;
  padding: 20px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  align-items: flex-start;
}

.panel-head > div {
  display: flex;
  align-items: center;
  gap: 12px;
}

.panel-head h2 {
  margin: 0;
  color: #fff;
  font-size: 22px;
}

.panel-head small {
  color: #7dd3fc;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.capture-icon {
  width: 42px;
  height: 42px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  border: 1px solid rgba(125, 211, 252, 0.18);
  border-radius: 10px;
  background: rgba(56, 189, 248, 0.1);
  color: #cffafe;
  font-size: 12px;
  font-weight: 850;
}

.file-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px;
  gap: 10px;
  align-items: center;
}

.file-label {
  min-height: 40px;
  display: flex;
  align-items: center;
  padding: 0 12px;
  border: 1px dashed rgba(125, 211, 252, 0.24);
  border-radius: 10px;
  color: var(--text-soft);
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  transition: border-color 180ms ease, color 180ms ease, background 180ms ease;
}

.file-label:hover {
  border-color: rgba(125, 211, 252, 0.46);
  background: rgba(56, 189, 248, 0.08);
  color: var(--text);
}

.divider {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--text-muted);
  font-size: 12px;
}

.divider::before,
.divider::after {
  content: "";
  height: 1px;
  flex: 1;
  background: rgba(148, 163, 184, 0.14);
}

.import-tip {
  font-size: 13px;
  line-height: 1.6;
}

.paper-impact,
.job-panel,
.xhs-route-note {
  display: grid;
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(125, 211, 252, 0.18);
  border-radius: 10px;
  background: rgba(15, 23, 42, 0.46);
}

.paper-impact-head,
.paper-impact-grid {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.paper-impact-head strong,
.xhs-route-note strong {
  color: #e5eefc;
  font-size: 14px;
}

.paper-impact-head span,
.xhs-route-note span {
  color: var(--text-muted);
  font-size: 12px;
}

.paper-impact-head button {
  border: 1px solid rgba(125, 211, 252, 0.18);
  border-radius: 8px;
  background: rgba(56, 189, 248, 0.08);
  color: var(--text-soft);
  cursor: pointer;
  padding: 5px 9px;
  font-size: 12px;
}

.paper-impact-head button:hover {
  border-color: rgba(125, 211, 252, 0.38);
  color: var(--text);
}

.job-row {
  display: grid;
  gap: 7px;
  padding: 10px;
  border: 1px solid rgba(125, 211, 252, 0.13);
  border-radius: 9px;
  background: rgba(3, 7, 18, 0.28);
}

.job-row > div:first-child {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.job-row strong {
  min-width: 0;
  color: #e5eefc;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-row span,
.job-row small {
  color: var(--text-muted);
  font-size: 12px;
}

.job-row small {
  color: #fecdd3;
}

.job-progress {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.18);
}

.job-progress span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #38bdf8, #22c55e);
  transition: width 260ms ease;
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
  color: #7dd3fc;
  font-weight: 700;
}

@media (max-width: 860px) {
  .capture-header,
  .file-row,
  .paper-impact-grid {
    grid-template-columns: 1fr;
  }

  .capture-header {
    display: grid;
  }

  .capture-mode {
    width: 100%;
  }

  .capture-mode button {
    flex: 1;
  }
}
</style>
