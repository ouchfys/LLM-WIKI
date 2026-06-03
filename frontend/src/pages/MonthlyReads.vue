<template>
  <section class="page-shell daily-page">
    <header class="hero-panel daily-hero">
      <div class="hero-copy">
        <p class="eyebrow">Daily Recommendations</p>
        <h2 class="section-title">今天值得看的内容，先由系统筛，再由你决定读不读。</h2>
        <p class="section-copy">
          冷启动时优先抓热门 GitHub 项目、近期 arXiv 论文和公开技术内容；有画像后，
          再按你的目标方向、近期保存内容和阅读行为做二次过滤。
        </p>
      </div>

      <div class="crawl-box">
        <div class="crawl-actions">
          <n-button type="primary" :loading="crawling" @click="crawlSources">开始抓取</n-button>
          <n-button :loading="seeding" @click="seedDemo">生成演示数据</n-button>
        </div>
        <div class="crawl-switches">
          <n-checkbox v-model:checked="includeArxiv">包含 arXiv</n-checkbox>
          <n-checkbox v-model:checked="includeFeeds">包含博客 / RSS</n-checkbox>
        </div>
        <n-input
          v-model:value="crawlTopics"
          placeholder="补充主题，例如：RAG evaluation, Agent memory"
        />
      </div>
    </header>

    <div class="metric-grid">
      <article class="metric-tile">
        <span>当前条目</span>
        <strong>{{ items.length }}</strong>
        <p>当前筛选条件下的推荐数。</p>
      </article>
      <article class="metric-tile">
        <span>已入库</span>
        <strong>{{ counts.saved || 0 }}</strong>
        <p>已经转入个人知识库的推荐。</p>
      </article>
      <article class="metric-tile">
        <span>已读</span>
        <strong>{{ counts.read || 0 }}</strong>
        <p>已经读完并进入复盘阶段的内容。</p>
      </article>
      <article class="metric-tile">
        <span>待精读</span>
        <strong>{{ counts.deep_read || 0 }}</strong>
        <p>后续适合继续进入精读流程的条目。</p>
      </article>
    </div>

    <section class="surface-panel controls-panel">
      <div class="panel-head">
        <div>
          <h3>筛选与抓取设置</h3>
          <p>这里决定今天这批候选是更偏前沿论文，还是更偏工程经验。</p>
        </div>
      </div>

      <div class="controls-grid">
        <n-select v-model:value="status" :options="statusOptions" @update:value="load" />
        <n-input-number v-model:value="perQueryLimit" :min="1" :max="10" />
        <n-input-number v-model:value="maxItems" :min="4" :max="30" />
      </div>
    </section>

    <n-alert v-if="crawlReport" type="success" closable @close="crawlReport = null">
      本次抓取发现 {{ crawlReport.discovered }} 条内容，进入排序 {{ crawlReport.ranked }} 条，实际新增 {{ crawlReport.created }} 条。
      查询词：{{ crawlReport.queries.slice(0, 4).join(' / ') }}
    </n-alert>

    <n-alert v-if="saveNotice" type="info" closable @close="saveNotice = ''">
      {{ saveNotice }}
    </n-alert>

    <n-empty v-if="!items.length" description="当前没有推荐条目。">
      <template #extra>
        <n-space>
          <n-button type="primary" :loading="crawling" @click="crawlSources">开始抓取</n-button>
          <n-button :loading="seeding" @click="seedDemo">生成演示数据</n-button>
        </n-space>
      </template>
    </n-empty>

    <n-grid v-else :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
      <n-gi v-for="item in items" :key="item.id">
        <n-card class="daily-card" :segmented="{ content: true, footer: true }">
          <template #header>
            <div class="card-header">
              <span>{{ item.title }}</span>
              <n-tag size="small" :type="tagType(item.source_level)">{{ sourceLevelLabel(item.source_level) }}</n-tag>
            </div>
          </template>

          <div class="meta-row">
            <span v-if="authors(item).length">{{ authors(item).slice(0, 3).join(', ') }}</span>
            <span v-if="item.metadata?.year">{{ item.metadata.year }}</span>
            <span v-if="item.metadata?.venue">{{ item.metadata.venue }}</span>
          </div>

          <p class="card-summary">{{ item.summary || '暂无摘要。' }}</p>

          <div class="recommendation-row">
            <n-tag size="small">{{ statusLabel(item.status) }}</n-tag>
            <span>推荐分 {{ Number(item.score || 0).toFixed(1) }}</span>
          </div>

          <p v-if="item.reasons?.length" class="reason-copy">{{ item.reasons.slice(0, 3).join(' / ') }}</p>

          <n-space class="link-row">
            <n-button v-if="item.url" size="small" tag="a" :href="item.url" target="_blank">
              打开来源
            </n-button>
            <n-button
              v-if="pdfUrl(item.url)"
              size="small"
              tag="a"
              :href="pdfUrl(item.url)"
              target="_blank"
              type="primary"
            >
              下载 PDF
            </n-button>
          </n-space>

          <n-collapse>
            <n-collapse-item title="阅读笔记" name="note">
              <div class="note-stack">
                <n-input v-model:value="drafts[item.id].note_summary" placeholder="一句话总结" />
                <n-input v-model:value="drafts[item.id].takeaways" type="textarea" placeholder="关键 takeaways，每行一条" />
                <n-input v-model:value="drafts[item.id].open_questions" type="textarea" placeholder="没搞懂的问题，每行一条" />
                <n-input v-model:value="drafts[item.id].interview_points" type="textarea" placeholder="可用于面试表达的要点，每行一条" />
                <n-checkbox v-model:checked="drafts[item.id].deep_read_worthy">
                  值得进一步精读
                </n-checkbox>
                <n-button size="small" type="primary" @click="saveNotes(item)">保存笔记</n-button>
              </div>
            </n-collapse-item>
          </n-collapse>

          <template #footer>
            <n-space>
              <n-button size="small" @click="saveWiki(item)">存入知识库</n-button>
              <n-button size="small" @click="setStatus(item, 'read')">标记已读</n-button>
              <n-button size="small" @click="setStatus(item, 'deep_read')">转入精读</n-button>
              <n-button size="small" tertiary @click="setStatus(item, 'ignored')">忽略</n-button>
            </n-space>
          </template>
        </n-card>
      </n-gi>
    </n-grid>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NGi,
  NGrid,
  NInput,
  NInputNumber,
  NSelect,
  NSpace,
  NTag
} from 'naive-ui'
import { api, type ReadingItem } from '../api'

const items = ref<ReadingItem[]>([])
const counts = ref<Record<string, number>>({})
const status = ref('all')
const seeding = ref(false)
const crawling = ref(false)
const crawlTopics = ref('')
const includeArxiv = ref(true)
const includeFeeds = ref(true)
const perQueryLimit = ref(4)
const maxItems = ref(16)
const crawlReport = ref<{
  created: number
  discovered: number
  ranked: number
  queries: string[]
  sources: string[]
} | null>(null)
const saveNotice = ref('')
const drafts = reactive<Record<string, {
  note_summary: string
  takeaways: string
  open_questions: string
  interview_points: string
  deep_read_worthy: boolean
}>>({})

const statusOptions = [
  { label: '全部', value: 'all' },
  { label: '候选', value: 'candidate' },
  { label: '已入库', value: 'saved' },
  { label: '已读', value: 'read' },
  { label: '待精读', value: 'deep_read' },
  { label: '已忽略', value: 'ignored' }
]

function ensureDraft(item: ReadingItem) {
  drafts[item.id] = {
    note_summary: item.note_summary || '',
    takeaways: (item.takeaways || []).join('\n'),
    open_questions: (item.open_questions || []).join('\n'),
    interview_points: (item.interview_points || []).join('\n'),
    deep_read_worthy: Boolean(item.deep_read_worthy)
  }
}

async function load() {
  const { data } = await api.get('/monthly-reads', { params: { status: status.value } })
  items.value = data.items || []
  counts.value = data.counts || {}
  items.value.forEach(ensureDraft)
}

async function seedDemo() {
  seeding.value = true
  try {
    await api.post('/monthly-reads/seed-demo')
    await load()
  } finally {
    seeding.value = false
  }
}

async function crawlSources() {
  crawling.value = true
  try {
    const { data } = await api.post('/monthly-reads/crawl', {
      topics: crawlTopics.value.split(',').map((item) => item.trim()).filter(Boolean),
      include_arxiv: includeArxiv.value,
      include_feeds: includeFeeds.value,
      per_query_limit: perQueryLimit.value,
      max_items: maxItems.value
    })
    crawlReport.value = data
    await load()
  } finally {
    crawling.value = false
  }
}

async function setStatus(item: ReadingItem, next: string) {
  await api.patch(`/monthly-reads/${item.id}/status`, { status: next })
  await load()
}

async function saveWiki(item: ReadingItem) {
  const { data } = await api.post(`/monthly-reads/${item.id}/save-wiki`)
  saveNotice.value = data.deduped
    ? `《${item.title}》已存在于个人 Wiki，已同步更新。`
    : `《${item.title}》已存入个人 Wiki。`
  await load()
}

async function saveNotes(item: ReadingItem) {
  const draft = drafts[item.id]
  await api.patch(`/monthly-reads/${item.id}/notes`, {
    note_summary: draft.note_summary,
    takeaways: lines(draft.takeaways),
    open_questions: lines(draft.open_questions),
    interview_points: lines(draft.interview_points),
    deep_read_worthy: draft.deep_read_worthy
  })
  await load()
}

function lines(text: string) {
  return text.split('\n').map((line) => line.trim()).filter(Boolean)
}

function authors(item: ReadingItem) {
  return Array.isArray(item.metadata?.authors) ? (item.metadata.authors as string[]) : []
}

function pdfUrl(url: string) {
  if (!url) return ''
  const arxivMatch = url.match(/arxiv\.org\/abs\/([^?#]+)/)
  if (arxivMatch) return `https://arxiv.org/pdf/${arxivMatch[1]}.pdf`
  if (url.toLowerCase().endsWith('.pdf')) return url
  return ''
}

function tagType(level: string) {
  if (level === 'primary') return 'success'
  if (level === 'secondary') return 'warning'
  if (level === 'tertiary') return 'error'
  return 'default'
}

function statusLabel(value: string) {
  return {
    candidate: '候选',
    saved: '已入库',
    read: '已读',
    deep_read: '待精读',
    ignored: '已忽略'
  }[value] || value
}

function sourceLevelLabel(value: string) {
  return {
    primary: '一手来源',
    secondary: '二手解读',
    tertiary: '泛化资料'
  }[value] || '未评级'
}

onMounted(load)
</script>
