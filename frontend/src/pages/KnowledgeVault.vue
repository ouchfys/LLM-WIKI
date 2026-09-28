<template>
  <section class="vault-page">
    <div class="vault-stage">
      <div class="mobile-library-picker">
        <label for="vault-library-select">资料库</label>
        <select id="vault-library-select" :value="selectedGroupKey" @change="selectGroup(($event.target as HTMLSelectElement).value)">
          <option v-for="group in libraryGroups" :key="group.key" :value="group.key">
            {{ group.label }} ({{ group.items.length }})
          </option>
        </select>
      </div>

      <div class="vault-layout">
        <aside class="vault-library" aria-label="资料库">
          <div class="column-head">
            <div>
              <span>资料库</span>
              <strong>{{ currentGroup?.label || '资料库' }}</strong>
            </div>
            <small>{{ currentGroup?.items.length || 0 }} 张</small>
          </div>

          <input
            v-model="query"
            class="library-search"
            type="search"
            aria-label="搜索知识库"
            placeholder="搜索论文、主题或关键词"
            @keyup.enter="loadCards"
          />

          <div class="library-tree">
            <section v-for="group in libraryGroups" :key="group.key" class="library-group">
              <button
                type="button"
                class="library-group-head"
                :class="{ active: selectedGroupKey === group.key }"
                @click="selectGroup(group.key)"
              >
                <span>{{ group.label }}</span>
                <strong>{{ group.items.length }}</strong>
              </button>
              <Transition name="library-collapse">
                <div v-if="selectedGroupKey === group.key" class="library-collapse">
                  <TransitionGroup name="library-row-stagger" tag="div" class="library-items">
                    <button
                      v-for="(card, index) in group.items"
                      :key="card.id"
                      type="button"
                      class="library-row"
                      :class="{ active: selectedCard?.id === card.id }"
                      :style="{ '--row-index': index }"
                      @click="selectCard(card)"
                    >
                      <span v-html="readerInline(card.title)"></span>
                      <small>{{ cardSubtitle(card) }}</small>
                    </button>
                    <p v-if="!group.items.length" key="__empty" class="empty-note">这个分类还没有内容。</p>
                  </TransitionGroup>
                </div>
              </Transition>
            </section>
          </div>

          <div class="library-stats" aria-label="资料库统计">
            <span>{{ allCards.length }} 卡片</span>
            <span>{{ aliasItems.length }} 关键词</span>
    <span>{{ selectedCard ? selectedTypeLabel : '-' }}</span>
          </div>
        </aside>

        <main class="vault-reader" aria-label="Reader">
          <article v-if="selectedCard" class="wiki-document">
            <header class="reader-head">
              <div class="reader-meta-line">
                <span>{{ selectedTypeLabel }}</span>
              </div>
              <h1 v-html="readerInline(selectedCard.title)"></h1>
              <p v-if="isRepositoryCard" class="repository-status" aria-label="保存状态">
                <span>{{ selectedCard.current_revision_id ? '已保存' : '保存状态未确认' }}</span>
                <span v-if="repositoryReviewLabel(selectedCard.repository_review)">{{ repositoryReviewLabel(selectedCard.repository_review) }}</span>
              </p>
              <div v-if="!isRepositoryCard" class="reader-control-row">
                <span :class="['source-level-chip', selectedCard.source_level || 'neutral']">
                  {{ sourceLevelLabel(selectedCard.source_level) }}
                </span>

              </div>
              <div v-if="sourcePapers.length" class="reader-origin" aria-label="来源论文">
                <span class="origin-label">来自论文</span>
                <div v-for="paper in sourcePapers" :key="paper.id" class="origin-paper">
                  <span>{{ paper.title }}</span>
                  <button type="button" @click="selectCardById(paper.id)">查看来源论文 ↗</button>
                </div>
              </div>
              <div class="reader-actions">
                <button type="button" class="primary-action" @click.stop="askAbout(selectedCard)">基于此页提问</button>
                <button type="button" @click.stop="openRaw(selectedCard)">原始资料</button>
                <button type="button" class="danger" @click.stop="deleteCard(selectedCard)">删除</button>
              </div>
            </header>

            <details v-if="!isRepositoryCard" class="reader-detail-section">
              <summary>关联与来源 <span>查看证据与相关知识</span></summary>
              <div class="detail-stack">
                <section class="trace-card">
                  <div class="trace-head">
                    <strong>关联卡片</strong>
                    <small>{{ relatedCards.length || linkedKnowledge.length }}</small>
                  </div>
                  <div v-if="relatedCards.length" class="trace-list">
                    <button v-for="item in relatedCards" :key="item.key" type="button" class="trace-row" @click.stop="selectCardById(item.cardId)">
                      <span>{{ item.title }}</span>
                      <small>{{ relationLabel(item.meta) }}</small>
                    </button>
                  </div>
                  <div v-else-if="linkedKnowledge.length" class="trace-list">
                    <button v-for="item in linkedKnowledge" :key="item.id" type="button" class="trace-row" @click.stop="selectCardById(item.id)">
                      <span>{{ item.title }}</span>
                      <small>{{ typeLabel(item.pageType) }} · {{ relationLabel(item.relationType) }}</small>
                    </button>
                  </div>
                  <p v-else class="empty-note">暂无关联卡片。</p>
                </section>

                <section class="trace-card">
                  <div class="trace-head">
                    <strong>来源追踪</strong>
                    <small>{{ selectedSourceCount }}</small>
                  </div>
                  <dl class="source-facts">
                    <div><dt>类型</dt><dd>{{ typeLabel(selectedCard.page_type) }}</dd></div>
                    <div><dt>层级</dt><dd>{{ sourceLevelLabel(selectedCard.source_level) }}</dd></div>
                  </dl>
                  <details class="source-technical">
                    <summary>存储位置</summary>
                    <p>{{ selectedCard.markdown_path || firstSourceLabel || '尚未记录' }}</p>
                  </details>
                  <ul v-if="showSourceTrace && selectedCard.source_urls?.length" class="source-link-list">
                    <li v-for="url in selectedCard.source_urls" :key="url">
                      <a :href="normalUrl(url)" target="_blank" rel="noreferrer">{{ readableUrl(url) }}</a>
                    </li>
                  </ul>
                  <ul v-else-if="showSourceTrace && sourceEvidence.length" class="source-link-list">
                    <li v-for="item in sourceEvidence" :key="item.id">
                      <strong>{{ item.source_card_title || item.section_id || '证据' }}</strong>
                      <p>{{ item.claim_text || item.evidence_text }}</p>
                    </li>
                  </ul>
                </section>
              </div>
            </details>

            <details v-if="importImpact" class="paper-pipeline">
              <summary>导入影响 <span>{{ impactSummary }}</span></summary>
              <p class="pipeline-meta">
                创建 {{ uniqueImpact.created.length }}
                · 更新 {{ uniqueImpact.updated.length }}
                · 关联 {{ uniqueImpact.linked.length }}
                · 退回 {{ uniqueImpact.rejected.length }}
              </p>
              <ul v-if="impactRows.length" class="impact-list">
                <li v-for="row in impactRows" :key="row.key">
                  <b>{{ row.kind }}</b>
                  <button v-if="row.cardId" type="button" @click.stop="selectCardById(row.cardId)">
                    {{ row.title }}
                  </button>
                  <span v-else>{{ row.title }}</span>
                </li>
              </ul>
            </details>

            <section v-if="isRepositoryCard && !readingGuide" class="wiki-section repository-conclusion">
              <h2>核心解读</h2>
              <div v-html="readerText(repositoryConclusion)"></div>
            </section>
            <section v-if="showSummarySection" class="wiki-section">
              <h2>先读这一段</h2>
              <div v-html="readerText(selectedSummary)"></div>
            </section>

            <section v-if="readingGuide" class="wiki-section reading-guide">
              <div class="reading-guide-label">核心解读</div>
              <div v-html="readerText(readingGuide)"></div>
            </section>
            <section v-for="section in readingSections" :key="section.key" class="wiki-section">
              <h2>{{ section.title }}</h2>
              <div v-html="section.html"></div>
            </section>
            <div v-if="detailSections.length" class="reading-details">
              <h2>深入阅读</h2>
              <p class="detail-hint">方法、实验与原文证据按需展开，完整内容始终保留。</p>
              <details v-for="section in detailSections" :key="`${selectedCard.id}-${section.key}`" class="wiki-section reader-fold">
                <summary>{{ section.title }}</summary>
                <div v-html="section.html"></div>
              </details>
            </div>

            <section v-if="imagePreviewSources.length" class="wiki-section">
              <h2>图片</h2>
              <div class="image-strip">
                <a v-for="url in imagePreviewSources" :key="url" :href="normalUrl(url)" target="_blank" rel="noreferrer">
                  <img :src="normalUrl(url)" :alt="selectedCard.title" />
                </a>
              </div>
            </section>

            <footer v-if="isRepositoryCard" class="repository-snapshot">
              代码仓库：<a v-if="repositoryName" :href="repositoryUrl" target="_blank" rel="noreferrer">{{ repositoryName }}</a><span v-else>未记录</span>
              <span>· 阅读日期（UTC）：{{ repositoryReadDate }}</span>
              <span>· 版本：<code>{{ repositoryCommit ? repositoryCommit.slice(0, 12) : '未记录' }}</code></span>
            </footer>

          </article>

          <div v-else class="reader-empty">
            <span>知识库</span>
            <h1>选择一张卡片开始阅读</h1>
            <p>论文页和主题页会在这里形成可跳转的阅读视图。</p>
          </div>
        </main>


      </div>
    </div>

    <n-modal v-model:show="rawModalVisible" preset="card" style="width: 960px; max-width: 95vw;" :bordered="false">
      <template #header>
        <div class="modal-head">
          <span>{{ rawModalTitle }}</span>
          <n-tag size="small">Markdown 原文</n-tag>
        </div>
      </template>
      <pre class="raw-viewer"><code>{{ rawMarkdown }}</code></pre>
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NModal, NTag } from 'naive-ui'
import { api, type WikiCard } from '../api'
import { isReaderContentField, hasReaderContent } from '../lib/wikiContent'
import { readerMarkdown, readerInline } from '../lib/readerMarkdown'
import { repositoryReviewLabel } from '../lib/repositoryReview'
import 'katex/dist/katex.min.css'

type LibraryGroup = { key: string; label: string; items: WikiCard[] }
type AliasItem = { card_id: string; title: string; alias: string; normalized_alias: string; page_type: string }
type RelatedRow = { key: string; cardId: string; title: string; meta: string }
type LinkedKnowledgeRow = { id: string; title: string; pageType: string; relationType: string }

const router = useRouter()

const allCards = ref<WikiCard[]>([])
const aliasItems = ref<AliasItem[]>([])
const selectedCard = ref<WikiCard | null>(null)
const selectedGroupKey = ref('papers')
const query = ref('')
const rawModalVisible = ref(false)
const rawModalTitle = ref('')
const rawMarkdown = ref('')
const cardLinks = ref<any | null>(null)

const libraryGroups = computed<LibraryGroup[]>(() => {
  const groups: LibraryGroup[] = [
    { key: 'papers', label: '论文', items: [] },
    { key: 'topics', label: '主题', items: [] },
    { key: 'interviews', label: '面经', items: [] },
    { key: 'insights', label: '我的洞见', items: [] },
    { key: 'sources', label: '资料', items: [] }
  ]
  for (const card of allCards.value) {
    groups.find((group) => group.key === cardGroup(card))?.items.push(card)
  }
  return groups
})

const readingGuide = computed(() => String(selectedCard.value?.content_json?.reading_guide || ''))
const repositoryMetadata = computed<Record<string, unknown>>(() => {
  const value = selectedCard.value?.content_json?.repository_research
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
})
const isRepositoryCard = computed(() => Boolean(repositoryMetadata.value?.repository))
const selectedTypeLabel = computed(() => typeLabel(selectedCard.value?.page_type || ''))
const repositoryName = computed(() => String(repositoryMetadata.value?.repository || ''))
const repositoryCommit = computed(() => String(repositoryMetadata.value?.commit || ''))
const repositoryReadDate = computed(() => String(repositoryMetadata.value?.read_date || '未记录'))
const repositoryUrl = computed(() => repositoryName.value && repositoryCommit.value
  ? `https://github.com/${repositoryName.value}/tree/${repositoryCommit.value}`
  : `https://github.com/${repositoryName.value}`)
const repositoryConclusion = computed(() => {
  const content = selectedCard.value?.content_json || {}
  const current = String(content.conclusion || '')
  if (current) return current
  const legacy = String(content['研究内容'] || '')
  return legacy.replace(/^研究版本：.*$/m, '').replace(/^依据：.*$/gm, '').trim()
})
const sourcePapers = computed(() => {
  if (!selectedCard.value || selectedCard.value.page_type === 'PaperPage') return []
  const sources = new Map<string, { id: string; title: string }>()
  for (const paper of cardLinks.value?.source_papers || []) {
    sources.set(paper.id, { id: paper.id, title: paper.title })
  }
  for (const source of cardLinks.value?.sources || []) {
    if (source.source_card_id && allCards.value.some(card => card.id === source.source_card_id && card.page_type === 'PaperPage')) {
      sources.set(source.source_card_id, { id: source.source_card_id, title: source.source_card_title || '来源论文' })
    }
  }
  // Relation rows are a fallback for older imports lacking source mappings.
  for (const link of cardLinks.value?.incoming || []) {
    const paper = allCards.value.find(card => card.id === link.from_card_id && card.page_type === 'PaperPage')
    if (paper) sources.set(paper.id, { id: paper.id, title: paper.title })
  }
  return [...sources.values()]
})
const selectedSummary = computed(() => selectedCard.value ? cleanText(selectedCard.value.summary) : '')
const hasProblemSection = computed(() => hasContent(selectedCard.value?.content_json?.problem))
const selectedSourceType = computed(() => String(selectedCard.value?.content_json?.source_type || '').toLowerCase())
const showSummarySection = computed(() =>
  Boolean(!isRepositoryCard.value && selectedSummary.value && !hasProblemSection.value && !readingGuide.value)
)
const importImpact = computed(() => {
  const value = selectedCard.value?.content_json?.import_impact
  return typeof value === 'object' && value !== null ? value as any : null
})

const uniqueImpact = computed(() => ({
  created: uniqueById(importImpact.value?.created_cards || []),
  updated: uniqueById(importImpact.value?.updated_cards || []),
  linked: uniqueById(importImpact.value?.linked_cards || [], 'to'),
  rejected: uniqueById(importImpact.value?.review_rejections || [], 'title')
}))

const impactSummary = computed(() =>
  `新建 ${uniqueImpact.value.created.length} / 更新 ${uniqueImpact.value.updated.length} / 关联 ${uniqueImpact.value.linked.length}`
)

const impactRows = computed(() => [
  ...uniqueImpact.value.created.map((item: any) => ({ key: `created:${item.id}`, kind: '新建', title: item.title, cardId: item.id })),
  ...uniqueImpact.value.updated.map((item: any) => ({ key: `updated:${item.id}`, kind: '更新', title: item.title, cardId: item.id })),
  ...uniqueImpact.value.linked.map((item: any) => ({ key: `linked:${item.to || item.id}`, kind: '关联', title: item.title, cardId: item.to || item.id }))
].slice(0, 12))

const compiledSections = computed(() => {
  if (!selectedCard.value) return []
  const content = selectedCard.value.content_json || {}
  const order = [
    'reading_guide', 'paper_type', 'research_problem', 'problem', 'motivation', 'contributions',
    'method_overview', 'method_components', 'execution_flow', 'experiment_setup',
    'key_results', 'key_tables', 'figure_notes', 'ablations',
    'comparison_to_prior_work', 'definition', 'question_context', 'knowledge_kind',
    'main_points', 'conversation_insights', 'open_questions', 'content', 'core_points',
    'interview_questions', 'answer_frame', 'learning_value', 'key_idea', 'method',
    'mechanism', 'results', 'findings', 'key_points', 'limitations',
    'key_takeaways', 'interview_notes', 'notes'
  ]
  return Object.entries(content)
    .filter(([key, value]) => shouldRenderContentField(key, value))
    .sort(([a], [b]) => orderIndex(a, order) - orderIndex(b, order))
    .map(([key, value]) => ({
      key,
      title: sectionTitle(key),
      html: contentValueToHtml(key, value)
    }))
    .filter((section) => section.html)
})

const overviewKeys = computed(() => selectedCard.value?.page_type === 'PaperPage'
  ? ['research_problem', 'method_overview', 'limitations']
  : ['mechanism', 'limitations'])
const readingSections = computed(() => isRepositoryCard.value || readingGuide.value ? [] : compiledSections.value.filter(s => overviewKeys.value.includes(s.key)))
const detailSections = computed(() => isRepositoryCard.value ? [] : compiledSections.value.filter(s => s.key !== 'reading_guide'
  && (readingGuide.value || !overviewKeys.value.includes(s.key))))

const linkedKnowledge = computed<LinkedKnowledgeRow[]>(() => {
  const content = selectedCard.value?.content_json || {}
  return arrayOfObjects(content.linked_knowledge)
    .map((item) => ({
      id: String(item.id || item.to || item.card_id || ''),
      title: cleanText(String(item.title || item.alias || '关联卡片')),
      pageType: String(item.page_type || item.pageType || ''),
      relationType: String(item.relation_type || item.relationType || 'related')
    }))
    .filter((item) => item.id && item.title)
})

const imagePreviewSources = computed(() => {
  const content = selectedCard.value?.content_json || {}
  const downloaded = [
    ...arrayOfStrings(content.attachments),
    ...arrayOfStrings(content.downloaded_images)
  ]
  const urls = downloaded.length ? downloaded : arrayOfStrings(content.image_urls)
  return uniqueStrings(urls).slice(0, 6)
})

const sourceEvidence = computed(() => (cardLinks.value?.sources || []).slice(0, 8))
const showSourceTrace = computed(() => true)
const firstSourceLabel = computed(() => {
  const source = selectedCard.value?.source_urls?.[0]
  return source ? readableUrl(source) : ''
})
const selectedSourceCount = computed(() => {
  const sourceUrls = selectedCard.value?.source_urls?.length || 0
  const sourceLinks = cardLinks.value?.sources?.length || 0
  return Math.max(sourceUrls, sourceLinks)
})
const currentGroup = computed(() =>
  libraryGroups.value.find((group) => group.key === selectedGroupKey.value) || libraryGroups.value.find((group) => group.items.length)
)

const relatedCards = computed<RelatedRow[]>(() => {
  const rows: RelatedRow[] = []
  for (const item of cardLinks.value?.outgoing || []) {
    if (item.to_card_id) rows.push({ key: `out:${item.id}`, cardId: item.to_card_id, title: item.target_title || item.to_card_id, meta: item.relation_type || 'related' })
  }
  for (const item of cardLinks.value?.incoming || []) {
    if (item.from_card_id) rows.push({ key: `in:${item.id}`, cardId: item.from_card_id, title: item.source_title || item.from_card_id, meta: item.relation_type || 'related' })
  }
  return rows.slice(0, 12)
})

async function loadCards() {
  const params: Record<string, string> = {}
  if (query.value.trim()) params.query = query.value.trim()
  const [{ data: cardsData }, { data: aliasesData }] = await Promise.all([
    api.get('/wiki', { params }),
    api.get('/wiki/aliases')
  ])
  allCards.value = cardsData.items || []
  aliasItems.value = aliasesData.items || []
  selectBestCard()
}

function selectBestCard() {
  if (selectedCard.value && allCards.value.some((card) => card.id === selectedCard.value?.id)) {
    loadSelectedDetails(selectedCard.value.id)
    return
  }
  const group = libraryGroups.value.find((item) => item.key === selectedGroupKey.value)
  selectedCard.value = group?.items[0] || libraryGroups.value.find((item) => item.items.length)?.items[0] || null
  if (selectedCard.value) {
    selectedGroupKey.value = cardGroup(selectedCard.value)
    loadSelectedDetails(selectedCard.value.id)
  }
}

async function selectCard(card: WikiCard) {
  cardLinks.value = null
  selectedCard.value = card
  selectedGroupKey.value = cardGroup(card)
  await loadSelectedDetails(card.id)
}

async function selectCardById(cardId: string) {
  const existing = allCards.value.find((card) => card.id === cardId)
  if (existing) {
    await selectCard(existing)
    return
  }
  const { data } = await api.get(`/wiki/${cardId}`)
  allCards.value = [data, ...allCards.value.filter((card) => card.id !== data.id)]
  await selectCard(data)
}

async function loadSelectedDetails(cardId: string) {
  try {
    const [{ data: cardData }, { data: linksData }] = await Promise.all([
      api.get(`/wiki/${cardId}`),
      api.get(`/wiki/${cardId}/links`)
    ])
    if (selectedCard.value?.id !== cardId) return
    selectedCard.value = cardData
    cardLinks.value = linksData
  } catch {
    cardLinks.value = null
  }
}

function selectGroup(groupKey: string) {
  if (selectedGroupKey.value === groupKey) {
    selectedGroupKey.value = ''
    return
  }
  selectedGroupKey.value = groupKey
  const group = libraryGroups.value.find((item) => item.key === groupKey)
  if (group?.items[0]) selectCard(group.items[0])
  else selectedCard.value = null
}

async function openRaw(card: WikiCard) {
  const { data } = await api.get(`/wiki/${card.id}/raw-source`)
  rawModalTitle.value = card.title
  rawMarkdown.value = data.markdown
  rawModalVisible.value = true
}

async function deleteCard(card: WikiCard) {
  if (!window.confirm(`确定删除《${card.title}》吗？`)) return
  await api.delete(`/wiki/${card.id}`)
  selectedCard.value = null
  await loadCards()
}

function askAbout(card: WikiCard) {
  router.push({ path: '/', query: { ask: `基于 Wiki 页面《${card.title}》，用通俗中文解释核心思路、关键证据与局限。` } })
}

function valueToHtml(value: unknown): string {
  if (Array.isArray(value)) {
    const items = value.map((item) => cleanText(renderInline(item))).filter(Boolean)
    return items.length ? `<ul>${items.map((item) => `<li>${readerText(item)}</li>`).join('')}</ul>` : ''
  }
  if (typeof value === 'object' && value !== null) {
    const rows = Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => {
        const text = cleanText(renderInline(nested))
        return text ? `<li><strong>${escapeHtml(sectionTitle(key))}</strong>: ${readerText(text)}</li>` : ''
      })
      .filter(Boolean)
    return rows.length ? `<ul>${rows.join('')}</ul>` : ''
  }
  const text = cleanText(String(value))
  return text ? readerText(text) : ''
}

function contentValueToHtml(key: string, value: unknown): string {
  if (key === 'key_tables') return keyTablesToHtml(value)
  if (key === 'figure_notes') return figureNotesToHtml(value)
  if (key === 'knowledge_kind') return valueToHtml(knowledgeKindLabel(String(value)))
  if (key === 'paper_type') return valueToHtml(paperTypeLabel(String(value)))
  return valueToHtml(value)
}

function keyTablesToHtml(value: unknown): string {
  const tables = Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    : parseStoredTableSections(String(value || ''))
  return tables.map((table, index) => {
    const caption = compactTableCaption(String(table.caption || ''), index)
    const location = cleanText(String(table.location || table.section || ''))
    const page = String(table.page || '').trim()
    const locationText = [location, page ? `第 ${page} 页` : ''].filter(Boolean).join(' · ')
    const tableHtml = markdownTableToHtml(String(table.markdown || ''))
    if (!tableHtml) return ''
    return [
      `<section class="knowledge-table">`,
      caption ? `<h3>${escapeHtml(caption)}</h3>` : '',
      locationText ? `<p class="table-source">来源：${escapeHtml(locationText)}</p>` : '',
      `<div class="table-scroll">${tableHtml}</div>`,
      `</section>`
    ].join('')
  }).filter(Boolean).join('')
}

function parseStoredTableSections(markdown: string): Array<Record<string, unknown>> {
  const blocks = markdown.replace(/\r\n/g, '\n').split(/(?=^###\s+)/m).map((item) => item.trim()).filter(Boolean)
  return blocks.map((block, index) => {
    const lines = block.split('\n')
    const heading = lines[0]?.match(/^###\s+(.+)$/)?.[1] || `表格 ${index + 1}`
    const locationLine = lines.find((line) => /来源位置/.test(line)) || ''
    const location = locationLine.replace(/^\*|\*$/g, '').replace(/^来源位置[:：]\s*/, '')
    const tableLines = lines.filter((line) => line.trim().startsWith('|')).join('\n')
    return { caption: heading, location, markdown: tableLines }
  })
}

function compactTableCaption(caption: string, index: number): string {
  const text = cleanText(caption)
  const numbered = text.match(/^(?:Table|表)\s*([\w.-]+)/i)
  return numbered ? `表 ${numbered[1].replace(/[.:：]$/, '')}` : (text || `表 ${index + 1}`)
}

function markdownTableToHtml(markdown: string): string {
  const rows = markdown.replace(/\r\n/g, '\n').split('\n')
    .map(parseMarkdownTableRow)
    .filter((cells): cells is string[] => Boolean(cells?.some((cell) => cell.trim())))
    .filter((cells) => !cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim())))
  if (!rows.length) return ''

  const width = Math.max(...rows.map((row) => row.length))
  if (width < 2) return ''
  const normalized = rows.map((row) => {
    if (row.length >= width) return row.slice(0, width)
    // PDF tables commonly omit repeated row-group labels in the leading columns.
    // Left-padding preserves the numeric columns instead of shifting every value.
    return [...Array(width - row.length).fill(''), ...row]
  })
  const [header, ...body] = normalized
  const headHtml = `<thead><tr>${header.map((cell) => `<th>${readerInline(cell)}</th>`).join('')}</tr></thead>`
  const bodyHtml = body.length
    ? `<tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${readerInline(cell)}</td>`).join('')}</tr>`).join('')}</tbody>`
    : ''
  return `<table>${headHtml}${bodyHtml}</table>`
}

function parseMarkdownTableRow(line: string): string[] | null {
  const trimmed = line.trim()
  if (!trimmed.startsWith('|')) return null
  const inner = trimmed.replace(/^\|/, '').replace(/\|$/, '')
  const cells = inner.split('|').map((cell) => cell.trim())
  return cells.some(Boolean) ? cells : null
}

function figureNotesToHtml(value: unknown): string {
  if (Array.isArray(value)) {
    return value
      .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
      .map((figure, index) => {
        const caption = compactFigureCaption(String(figure.caption || ''), index)
        const description = cleanText(String(figure.description || ''))
        const details = [
          ['趋势', figure.trend],
          ['适用条件', figure.conditions],
          ['关键数值', Array.isArray(figure.key_values) ? figure.key_values.join('；') : figure.key_values]
        ].filter(([, item]) => cleanText(String(item || '')))
        return `<section class="figure-note"><h3>${escapeHtml(caption)}</h3>${description ? `<p>${readerText(description)}</p>` : ''}${details.length ? `<ul>${details.map(([label, item]) => `<li><strong>${label}</strong>：${readerText(cleanText(String(item)))}</li>`).join('')}</ul>` : ''}</section>`
      }).join('')
  }

  const safeLines = String(value || '').replace(/\r\n/g, '\n').split('\n')
  const output: string[] = []
  for (const line of safeLines) {
    if (/论文正文说明/.test(line)) continue
    const heading = line.match(/^###\s+(.+)$/)
    if (heading) {
      output.push(`<h3>${escapeHtml(compactFigureCaption(heading[1], output.length))}</h3>`)
      continue
    }
    const bullet = line.match(/^[-*]\s+(.*)$/)
    if (bullet) {
      output.push(`<p class="figure-detail">${readerText(cleanText(bullet[1]))}</p>`)
      continue
    }
    const text = cleanText(line)
    if (text) output.push(`<p>${readerText(text)}</p>`)
  }
  return output.join('')
}

function compactFigureCaption(caption: string, index: number): string {
  const text = cleanText(caption)
  const numbered = text.match(/^(?:Figure|Fig\.?|图)\s*([\w.-]+)/i)
  return numbered ? `图 ${numbered[1].replace(/[.:：]$/, '')}` : (text || `图 ${index + 1}`)
}

function readerText(text: string) {
  return readerMarkdown(cleanText(text))
}

function cardGroup(card: WikiCard) {
  const sourceType = String(card.content_json?.source_type || '').toLowerCase()
  const urls = (card.source_urls || []).join(' ').toLowerCase()
  if (card.content_json?.repository_research) return 'topics'
  if (sourceType.startsWith('conversation_insight')) return 'insights'
  if (card.page_type === 'PaperPage') return 'papers'
  if (['TopicPage', 'ConceptPage', 'MethodPage'].includes(card.page_type)) return 'topics'
  if (card.page_type === 'InterviewQA') return 'interviews'
  if (sourceType.includes('paper') || /arxiv|\.pdf|doi\.org/.test(urls)) return 'papers'
  return 'sources'
}

function groupLabelForCard(card: WikiCard) {
  return libraryGroups.value.find((group) => group.key === cardGroup(card))?.label || 'Wiki'
}

function cardSubtitle(card: WikiCard) {
  if (card.summary) return cleanText(card.summary).slice(0, 64)
  if (card.content_json?.repository_research) {
    const snapshot = card.content_json?.repository_research as Record<string, unknown> | undefined
    return String(snapshot?.repository || '代码仓库')
  }
  if (card.source_urls?.[0]) return readableUrl(card.source_urls[0])
  return typeLabel(card.page_type)
}

function typeLabel(value: string) {
  return ({
    PaperPage: '论文',
    TopicPage: '主题',
    ConceptPage: '主题',
    MethodPage: '主题',
    ComparePage: '对比',
    InterviewQA: '面经',
    MistakeNote: '错题',
    SourceNote: '笔记'
  } as Record<string, string>)[value] || value
}

function sourceLevelLabel(value: string) {
  return ({ primary: '一手来源', secondary: '二手整理', tertiary: '三手线索' } as Record<string, string>)[value] || 'Wiki'
}

function knowledgeKindLabel(value: string) {
  return ({
    user_idea: '用户想法',
    discussion_conclusion: '讨论结论',
    source_backed_conclusion: '有资料支撑的结论',
    open_question: '待验证问题'
  } as Record<string, string>)[value] || value
}

function paperTypeLabel(value: string) {
  return ({
    empirical: '实证研究',
    system: '系统研究',
    theory: '理论研究',
    survey: '综述',
    other: '其他'
  } as Record<string, string>)[value.toLowerCase()] || value
}

function relationLabel(value: string) {
  return ({
    topic_related: '主题相关',
    mentions: '提及',
    introduces: '引入',
    uses: '使用'
  } as Record<string, string>)[value] || value || '相关'
}

function sectionTitle(key: string) {
  const labels: Record<string, string> = {
    reading_guide: '核心解读',
    problem: '问题',
    paper_type: '论文类型',
    research_problem: '研究问题',
    motivation: '研究动机',
    contributions: '主要贡献',
    method_overview: '方法概览',
    method_components: '方法组成',
    execution_flow: '执行流程',
    experiment_setup: '实验设置',
    key_results: '关键结果',
    key_tables: '关键表格',
    figure_notes: '图表解读',
    ablations: '消融实验',
    comparison_to_prior_work: '与既有工作的比较',
    key_idea: '核心观点',
    method: '方法',
    methods: '方法',
    mechanism: '机制',
    results: '结果',
    findings: '发现',
    limitations: '局限',
    key_takeaways: '要点',
    interview_notes: '面试笔记',
    notes: '补充说明',
    definition: '定义',
    question_context: '问题语境',
    core_points: '核心要点',
    interview_questions: '面试问题',
    answer_frame: '回答框架',
    learning_value: '学习价值',
    knowledge_kind: '知识类型',
    main_points: '沉淀要点',
    conversation_insights: '对话洞见',
    open_questions: '待验证问题',
    content: '内容',
    ocr_excerpt: 'OCR 摘录',
    image_notes: '图片笔记',
    source_url: '来源链接'
  }
  return labels[key] || key.replace(/_/g, ' ')
}

function shouldRenderContentField(key: string, value: unknown) {
  const sourceType = String(selectedCard.value?.content_json?.source_type || '').toLowerCase()
  if (sourceType === 'xiaohongshu' && ['notes', 'ocr_excerpt', 'image_notes', 'source_url'].includes(key)) {
    return false
  }
  return isReaderContentField(key) && hasContent(value)
}

function hasContent(value: unknown) {
  if (!hasReaderContent(value)) return false
  if (Array.isArray(value)) return value.some((item) => cleanText(renderInline(item)).trim())
  if (typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0
  return Boolean(cleanText(String(value)).trim())
}

function renderInline(value: unknown): string {
  if (Array.isArray(value)) return value.map(renderInline).join('; ')
  if (typeof value === 'object' && value !== null) return Object.values(value as Record<string, unknown>).map(renderInline).join(' · ')
  return String(value ?? '')
}

function cleanText(value: string) {
  return (value || '')
    .replace(/!\[[^\]]*\]\(data:image\/[^)]+\)/gi, '')
    .replace(/!\[[^\]]*\]\([^)]+\)/g, '')
    .replace(/data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+/g, '')
    .replace(/^\s*---+\s*$/gm, '\n')
    .replace(/^\s*-\s*\*\*Table Id\*\*[:：].*$/gim, '')
    .replace(/\*\*Purpose\*\*/gi, '用途')
    .replace(/\*\*Mechanism\*\*/gi, '机制')
    .replace(/\*\*Details\*\*/gi, '说明')
    .replace(/\*\*Evidence\*\*/gi, '证据')
    .replace(/\*\*Conditions\*\*/gi, '条件')
    .replace(/\*\*Implication\*\*/gi, '含义')
    .replace(/（已过）/g, '')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function arrayOfStrings(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim())) : []
}

function arrayOfObjects(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    : []
}

function uniqueStrings(values: string[]) {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))]
}

function readableUrl(url: string) {
  if (/^(oss|local|file):\/\//i.test(url) || /^[A-Za-z]:[\\/]/.test(url)) {
    const filename = url.replace(/\\/g, '/').split('/').pop() || '原始资料'
    try { return decodeURIComponent(filename) } catch { return filename }
  }
  try {
    const parsed = new URL(url)
    return parsed.hostname + parsed.pathname
  } catch {
    return url
  }
}

function normalUrl(url: string) {
  if (url.startsWith('oss://') || url.startsWith('local://')) {
    return `/api/wiki/object?ref=${encodeURIComponent(url)}`
  }
  if (!/^https?:\/\//i.test(url) && !url.startsWith('file://')) {
    return `/api/wiki/object?ref=${encodeURIComponent(url)}`
  }
  return url.startsWith('file://') ? url : url
}

function orderIndex(key: string, order: string[]) {
  const index = order.indexOf(key)
  return index === -1 ? 999 : index
}

function uniqueById(items: any[], key = 'id') {
  const seen = new Set<string>()
  const output: any[] = []
  for (const item of items) {
    const id = String(item?.[key] || item?.id || item?.title || '')
    if (!id || seen.has(id)) continue
    seen.add(id)
    output.push(item)
  }
  return output
}

onMounted(() => {
  loadCards()
})
</script>

<style scoped>
.vault-page {
  --ink-bg-deep: #080706;
  --ink-bg: #0b0908;
  --ink-panel: #12100d;
  --ink-control: #15130f;
  --ink-text: var(--text);
  --ink-text-soft: var(--text-soft);
  --ink-text-muted: var(--text-muted);
  --desk-accent: #9bb8ad;
  --desk-accent-bright: #d4e3d8;
  --line-quiet: rgba(195, 214, 202, 0.1);
  --line-hover: rgba(195, 214, 202, 0.24);
  --line-active: rgba(195, 214, 202, 0.32);
  --reader-serif: var(--font-sans);
  position: relative;
  z-index: 1;
  min-height: calc(100dvh - 84px);
  padding-top: 0;
  padding-bottom: 16px;
  container-type: inline-size;
  container-name: vault;
  background: transparent;
  color: var(--ink-text);
}

.vault-stage {
  position: relative;
  z-index: 1;
  max-width: 1440px;
  min-height: calc(100dvh - 120px);
  margin: 0 auto;
  padding: 0;
  border: 0;
  border-radius: 20px;
  background: transparent;
}

.vault-layout {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 260px;
  gap: 16px;
  align-items: start;
}

.vault-library,
.vault-reader,
.vault-trace,
.trace-card,
.paper-pipeline,
.reader-empty,
.mobile-library-picker {
  border: 1px solid var(--line-quiet);
  background: #0b0908;
  color: var(--ink-text);
}

.vault-library,
.vault-reader,
.vault-trace {
  border-radius: 16px;
}

.vault-library {
  position: sticky;
  top: 32px;
  height: calc(100dvh - 96px);
  min-height: 0;
  overflow: auto;
  padding: 20px 16px;
}

.vault-reader {
  min-width: 0;
  padding: 28px;
}

.vault-trace {
  display: grid;
  gap: 14px;
  padding: 0;
  border: 0;
  background: transparent;
}

.column-head,
.trace-head,
.pipeline-head,
.reader-control-row,
.reader-actions,
.library-stats {
  display: flex;
  align-items: center;
  gap: 10px;
}

.column-head,
.trace-head,
.pipeline-head {
  justify-content: space-between;
  align-items: flex-start;
}

.column-head span,
.trace-head small,
.pipeline-head span,
.library-stats,
.reader-meta-line,
.source-facts dt,
.source-level-chip,
.reader-tag {
  color: var(--ink-text-muted);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 13px;
  font-weight: 400;
  line-height: 1.5;
}

.column-head strong,
.trace-head strong {
  display: block;
  margin-top: 2px;
  color: var(--ink-text);
  font-size: 15px;
  line-height: 1.35;
}

.column-head small {
  color: var(--desk-accent-bright);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}

.library-search,
.mobile-library-picker select {
  width: 100%;
  height: 32px;
  margin-top: 14px;
  padding: 0 11px;
  border: 1px solid var(--line-quiet);
  border-radius: 8px;
  outline: none;
  background: #15130f;
  color: var(--ink-text);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 13px;
}

.library-search:focus,
.mobile-library-picker select:focus {
  border-color: var(--line-active);
}

.library-tree {
  display: grid;
  gap: 8px;
  margin-top: 16px;
}

.library-group-head,
.library-row,
.trace-row,
.reader-actions button,
.impact-list button {
  border: 1px solid var(--line-quiet);
  border-radius: 8px;
  background: #15130f;
  color: var(--ink-text-soft);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  cursor: pointer;
  transition: border-color 180ms ease, color 180ms ease, background-color 180ms ease, transform 180ms ease;
}

.library-group-head:hover,
.library-row:hover,
.trace-row:hover,
.reader-actions button:hover,
.impact-list button:hover {
  border-color: var(--line-hover);
  color: var(--ink-text);
  transform: translateY(-0.5px);
}

.library-group-head.active,
.library-row.active,
.trace-row.active {
  border-color: var(--line-active);
  background: #1d1913;
  color: var(--ink-text);
}

.library-group-head {
  width: 100%;
  height: 32px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 11px;
  text-align: left;
}

.library-group-head span,
.library-group-head strong {
  font-size: 13px;
  line-height: 1.2;
}

.library-items {
  display: grid;
  gap: 6px;
  margin-top: 7px;
}

.library-row,
.trace-row {
  width: 100%;
  display: grid;
  gap: 4px;
  padding: 10px;
  text-align: left;
}

.library-row span,
.trace-row span {
  overflow: hidden;
  color: inherit;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.35;
  text-overflow: ellipsis;
}

.library-row small,
.trace-row small {
  overflow: hidden;
  color: var(--ink-text-muted);
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.library-stats {
  flex-wrap: wrap;
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--line-quiet);
}

.library-stats span {
  padding: 3px 0;
}

.reader-head {
  padding-bottom: 22px;
  border-bottom: 1px solid var(--line-quiet);
}

.reader-meta-line {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.reader-meta-line span + span::before {
  content: "/";
  margin-right: 8px;
  color: rgba(195, 214, 202, 0.26);
}

.repository-status {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 12px 0 0;
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.6;
}

.wiki-document h1 {
  max-width: 780px;
  margin: 0;
  color: var(--ink-text);
  font-family: var(--reader-serif);
  font-size: clamp(22px, 2.2cqi, 28px);
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
  overflow-wrap: break-word;
  text-wrap: pretty;
}

.reader-control-row {
  flex-wrap: wrap;
  margin-top: 16px;
}

.source-level-chip,
.reader-tag {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  padding: 0 11px;
  border: 1px solid var(--line-quiet);
  border-radius: 8px;
  background: #15130f;
}

.source-level-chip.primary {
  color: #d4e3d8;
  border-color: rgba(155, 184, 173, 0.22);
  background: #1d1913;
}

.source-level-chip.secondary {
  color: #fde68a;
  border-color: rgba(245, 158, 11, 0.18);
  background: #1d1913;
}

.reader-actions {
  flex-wrap: wrap;
  margin-top: 18px;
}

.reader-actions button {
  height: 32px;
  padding: 0 12px;
}

.reader-actions .primary-action {
  border-color: rgba(195, 214, 202, 0.24);
  background: #9bb8ad;
  color: #080706;
  font-weight: 700;
}

.reader-actions .primary-action:hover {
  background: #d4e3d8;
  color: #080706;
}

.reader-actions .danger {
  border-color: rgba(244, 63, 94, 0.22);
  color: #fecaca;
}

.paper-pipeline {
  margin-top: 24px;
  padding: 16px;
  border-radius: 12px;
}

.pipeline-head h2,
.wiki-section h2,
.reader-detail-section h2,
.trace-card h2 {
  margin: 0;
  color: var(--ink-text);
  font-family: var(--reader-serif);
  font-size: 22px;
  font-weight: 600;
  line-height: 1.35;
}

.pipeline-head small {
  color: var(--ink-text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.pipeline-meta {
  margin: 14px 0 0;
  color: var(--ink-text-muted);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 13px;
  line-height: 1.55;
}

.impact-list {
  color: var(--ink-text-muted);
  font-size: 12px;
}

.impact-list {
  display: grid;
  gap: 7px;
  margin: 14px 0 0;
  padding-left: 18px;
}

.impact-list button {
  padding: 0 4px;
  border-color: transparent;
  background: transparent;
  color: var(--desk-accent-bright);
}

.wiki-section,
.reader-detail-section {
  margin-top: 28px;
}

.wiki-section h2,
.reader-detail-section h2 {
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line-quiet);
}

.wiki-section h3,
.wiki-section :deep(h3) {
  color: var(--ink-text);
  font-family: var(--reader-serif);
  font-size: 18px;
  font-weight: 600;
  line-height: 1.4;
}

.wiki-section :deep(p),
.wiki-section :deep(li) {
  max-width: 75ch;
  color: var(--ink-text-soft);
  font-family: var(--reader-serif);
  font-size: 16px;
  font-weight: 400;
  line-height: 1.7;
}

.wiki-section :deep(p) {
  margin: 0 0 12px;
}

.wiki-section :deep(ul) {
  margin: 0;
  padding-left: 22px;
}

.wiki-section :deep(li + li) {
  margin-top: 8px;
}

.wiki-section :deep(strong) {
  color: var(--ink-text);
  font-weight: 600;
}

.wiki-section :deep(.knowledge-table + .knowledge-table),
.wiki-section :deep(.figure-note + .figure-note) {
  margin-top: 24px;
}

.wiki-section :deep(.table-source) {
  margin-top: -4px;
  color: var(--ink-text-muted);
  font-size: 12px;
}

.wiki-section :deep(.table-scroll) {
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--line-quiet);
  border-radius: 10px;
}

.wiki-section :deep(table) {
  width: max-content;
  min-width: 100%;
  border-collapse: collapse;
  background: #0f0d0b;
  font-size: 13px;
}

.wiki-section :deep(th),
.wiki-section :deep(td) {
  max-width: 240px;
  padding: 9px 11px;
  border-right: 1px solid var(--line-quiet);
  border-bottom: 1px solid var(--line-quiet);
  color: var(--ink-text-soft);
  line-height: 1.45;
  text-align: left;
  vertical-align: top;
  white-space: normal;
}

.wiki-section :deep(th) {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #1d1913;
  color: var(--ink-text);
  font-weight: 650;
}

.wiki-section :deep(tr:last-child td) {
  border-bottom: 0;
}

.wiki-section :deep(th:last-child),
.wiki-section :deep(td:last-child) {
  border-right: 0;
}

.wiki-section :deep(.figure-detail) {
  padding-left: 12px;
  border-left: 2px solid rgba(155, 184, 173, 0.22);
  color: var(--ink-text-muted);
}

.wiki-section :deep(.keyword-link) {
  display: inline;
  margin: 0 1px;
  padding: 1px 5px;
  border: 1px solid rgba(195, 214, 202, 0.2);
  border-radius: 6px;
  background: #1d1913;
  color: var(--desk-accent-bright);
  font: inherit;
  cursor: pointer;
}

.wiki-section a,
.source-link-list a {
  color: var(--desk-accent-bright);
}

.image-strip {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.image-strip a {
  display: block;
  overflow: hidden;
  border-radius: 10px;
  background: #15130f;
  aspect-ratio: 4 / 3;
}

.image-strip img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.reader-detail-section {
  display: none;
}

.detail-stack,
.vault-trace {
  min-width: 0;
}

.trace-card {
  padding: 16px;
  border-radius: 12px;
}

.trace-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.source-facts {
  display: grid;
  gap: 10px;
  margin: 14px 0 0;
}

.source-facts div {
  min-width: 0;
}

.source-facts dt,
.source-facts dd {
  margin: 0;
}

.source-facts dd {
  margin-top: 3px;
  color: var(--ink-text-soft);
  font-size: 13px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.source-facts dd.source-path {
  overflow: hidden;
  max-width: 100%;
  color: var(--ink-text-muted);
  font-family: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
  overflow-wrap: normal;
}

.source-link-list {
  display: grid;
  gap: 8px;
  margin: 14px 0 0;
  padding-left: 18px;
  color: var(--ink-text-soft);
  font-size: 13px;
  line-height: 1.62;
}

.source-link-list p {
  margin: 4px 0 0;
  color: var(--ink-text-soft);
}

.empty-note {
  margin: 10px 0 0;
  color: var(--ink-text-muted);
  font-size: 12px;
  line-height: 1.55;
}

.reader-empty {
  min-height: 420px;
  display: grid;
  place-content: center;
  padding: 28px;
  border-radius: 16px;
}

.reader-empty span {
  color: var(--ink-text-muted);
  font-size: 13px;
}

.reader-empty h1 {
  margin: 8px 0 0;
  font-family: var(--reader-serif);
  font-size: clamp(22px, 2.2cqi, 28px);
  font-weight: 600;
  line-height: 1.3;
}

.reader-empty p {
  max-width: 44ch;
  margin: 12px 0 0;
  color: var(--ink-text-muted);
  line-height: 1.7;
}

.mobile-library-picker {
  display: none;
  margin-bottom: 14px;
  padding: 14px;
  border-radius: 12px;
}

.mobile-library-picker label {
  display: block;
  color: var(--ink-text-muted);
  font-size: 13px;
  line-height: 1.5;
}

.modal-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.raw-viewer {
  max-height: 70vh;
  overflow: auto;
  white-space: pre-wrap;
  color: var(--desk-accent-bright);
}


/* Respond to available workspace width, including a docked browser or sidebar. */
@container vault (max-width: 1150px) {
  .vault-layout { grid-template-columns: 210px minmax(0, 1fr); }
  .vault-trace { display: none; }
  .reader-detail-section { display: block; }
  .detail-stack { display: grid; gap: 12px; margin-top: 16px; }
}

@container vault (max-width: 650px) {
  .vault-layout { grid-template-columns: minmax(0, 1fr); gap: 14px; }
  .vault-library { position: static; height: 240px; padding: 16px; }
  .vault-reader { padding: 22px 18px; }
  .wiki-document h1 { font-size: 23px; }
}

.reader-detail-section {
  margin-top: 20px;
  padding: 12px 0;
  border-block: 1px solid var(--line-quiet);
}

.reader-detail-section summary {
  cursor: pointer;
  color: var(--desk-accent-bright);
  font-size: 14px;
}

.reader-detail-section summary span {
  margin-left: 12px;
  color: var(--ink-text-muted);
  font-size: 12px;
}

.paper-pipeline > summary { cursor: pointer; font-size: 14px; color: var(--desk-accent-bright); }
.paper-pipeline > summary span { margin-left: 12px; color: var(--ink-text-muted); font-size: 12px; }

.source-technical { margin-top: 12px; color: var(--ink-text-muted); font-size: 12px; }
.source-technical summary { cursor: pointer; }
.source-technical p { overflow-wrap: anywhere; }

.library-collapse {
  display: grid;
  grid-template-rows: 1fr;
  transition: grid-template-rows 240ms ease-out;
}

.library-collapse > .library-items {
  min-height: 0;
  overflow: hidden;
}

.library-collapse-enter-from,
.library-collapse-leave-to {
  grid-template-rows: 0fr;
}

.library-collapse-enter-to,
.library-collapse-leave-from {
  grid-template-rows: 1fr;
}

.library-row-stagger-enter-active {
  transition: opacity 240ms ease-out, transform 240ms ease-out;
  transition-delay: calc(var(--row-index, 0) * 24ms);
}

.library-row-stagger-enter-from {
  opacity: 0;
  transform: translateY(-4px);
}

.library-row-stagger-enter-to {
  opacity: 1;
  transform: translateY(0);
}

@media (prefers-reduced-motion: reduce) {
  .library-group-head,
  .library-row,
  .trace-row,
  .reader-actions button,
  .impact-list button {
    transition-duration: 1ms;
  }

  .library-collapse,
  .library-row-stagger-enter-active {
    transition: none;
  }
}
</style>



<style scoped>
/* Reading is the primary task; use the existing palette with quieter hierarchy. */
.vault-layout { grid-template-columns: 240px minmax(0, 1fr); }
.vault-reader { background: var(--surface, #12110e); }
.wiki-document { max-width: 880px; margin: 0 auto; padding: clamp(24px, 4vw, 54px); }
.reader-head { padding-bottom: 24px; }
.wiki-document h1 { font-size: clamp(23px, 2.3vw, 32px); line-height: 1.35; }
.reader-control-row { margin-top: 12px; }
.source-level-chip { min-height: auto; padding: 0; border: 0; background: transparent; font-size: 12px; }
.reader-origin { margin-top: 20px; padding-left: 14px; border-left: 2px solid #86a79a; }
.origin-label { color: var(--ink-muted); font-size: 12px; }
.origin-paper { margin-top: 6px; display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.origin-paper > span { flex: 1; min-width: 180px; font-size: 13px; line-height: 1.65; color: var(--ink-muted); }
.origin-paper button { flex-shrink: 0; border: 0; border-bottom: 1px solid #759488; background: transparent; color: #a7c5b8; padding: 2px 0; font-size: 12px; cursor: pointer; }
.reader-actions { gap: 14px; margin-top: 20px; }
.reader-actions button { min-height: 32px; padding: 5px 12px; font-size: 12px; }
.wiki-section { margin-top: 28px; }
.wiki-section h2 { font-size: 19px; border: 0; padding-bottom: 0; }
.wiki-section :deep(h3) { font-size: 17px; line-height: 1.5; margin: 24px 0 10px; font-weight: 600; }
.wiki-section :deep(p), .wiki-section :deep(li) { font-size: 15px; line-height: 1.95; color: #d3d1c9; overflow-wrap: anywhere; }
.wiki-section :deep(p) { margin: 0 0 14px; }
.wiki-section :deep(strong) { color: #eeece5; font-weight: 600; }
.wiki-section :deep(.katex) { font-size: 1.1em; }
.wiki-section :deep(.katex-display) { overflow-x: auto; overflow-y: hidden; padding: 10px 0; }
.reading-guide-label { font-size: 12px; letter-spacing: .08em; color: #a7c5b8; margin-bottom: 18px; }
.repository-conclusion { max-width: 760px; }
.repository-snapshot { margin-top: 44px; padding-top: 15px; border-top: 1px solid var(--line-quiet); color: var(--ink-muted); font-size: 11px; line-height: 1.8; }
.repository-snapshot a { color: #a7c5b8; text-decoration: none; }
.repository-snapshot a:hover { text-decoration: underline; }
.repository-snapshot code { color: inherit; font-size: inherit; }
.reading-details { margin-top: 36px; border-top: 1px solid var(--line-quiet); padding-top: 24px; }
.reading-details > h2 { font-size: 17px; margin: 0; }
.detail-hint { color: var(--ink-muted); font-size: 12px; margin: 8px 0 16px; }
.reader-fold { margin: 0; border-top: 1px solid var(--line-quiet); padding: 14px 0; }
.reader-fold > summary { font-size: 14px; cursor: pointer; color: #c9d5ce; }
.reader-fold[open] > summary { margin-bottom: 20px; }
.reader-detail-section { margin-top: 16px; }
@media(max-width: 760px) {
  .vault-layout { grid-template-columns: minmax(0, 1fr); }
  .wiki-document { padding: 22px 18px; }
  .wiki-document h1 { font-size: 23px; }
}
</style>
