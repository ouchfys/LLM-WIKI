<template>
  <section class="vault-page">
    <header class="vault-header">
      <div class="vault-title-block">
        <p class="eyebrow">KNOWLEDGE VAULT</p>
        <h1>论文知识库</h1>
      </div>

      <div class="vault-status">
        <div><span>Cards</span><strong>{{ allCards.length }}</strong></div>
        <div><span>Keywords</span><strong>{{ aliasItems.length }}</strong></div>
        <div><span>Active</span><strong>{{ selectedCard ? typeLabel(selectedCard.page_type) : '-' }}</strong></div>
      </div>

      <div class="graph-visual" aria-hidden="true">
        <span class="graph-node n1"></span>
        <span class="graph-node n2"></span>
        <span class="graph-node n3"></span>
        <span class="graph-node n4"></span>
        <span class="graph-line l1"></span>
        <span class="graph-line l2"></span>
        <span class="graph-line l3"></span>
      </div>
    </header>

    <div class="vault-toolbar">
      <div class="toolbar-tabs" aria-label="Knowledge navigation">
        <button type="button" class="active">阅读器</button>
        <button type="button" @click="openGraphSearch">关联检索</button>
        <button type="button" @click="openDashboard">推荐队列</button>
      </div>
      <n-input
        v-model:value="query"
        class="vault-search"
        clearable
        placeholder="搜索论文、概念、方法或关键词"
        @keyup.enter="loadCards"
      />
      <button type="button" class="refresh-button" @click="loadCards">刷新</button>
    </div>

    <div class="vault-layout">
      <aside class="vault-sidebar">
        <div class="panel-title">
          <span>Library</span>
          <small>{{ allCards.length }} nodes</small>
        </div>
        <div class="library-tree">
          <section v-for="group in libraryGroups" :key="group.key" class="library-group">
            <button
              type="button"
              class="library-head"
              :class="{ active: selectedGroupKey === group.key }"
              @click="selectGroup(group.key)"
            >
              <span>{{ group.label }}</span>
              <strong>{{ group.items.length }}</strong>
            </button>
            <div v-if="selectedGroupKey === group.key" class="library-items">
              <button
                v-for="card in group.items"
                :key="card.id"
                type="button"
                class="card-row"
                :class="{ active: selectedCard?.id === card.id }"
                @click="selectCard(card)"
              >
                <span>{{ card.title }}</span>
                <small>{{ cardSubtitle(card) }}</small>
              </button>
              <p v-if="!group.items.length" class="empty-note">这个分类还没有内容。</p>
            </div>
          </section>
        </div>
      </aside>

      <main class="vault-reader">
        <article v-if="selectedCard" class="wiki-document" @click="handleDocumentClick">
          <div class="reader-topline">
            <span>{{ groupLabelForCard(selectedCard) }}</span>
            <span>{{ typeLabel(selectedCard.page_type) }}</span>
          </div>

          <div class="reader-title-row">
            <div>
              <h1>{{ selectedCard.title }}</h1>
              <div class="wiki-tags">
                <span :class="['level-tag', selectedCard.source_level || 'neutral']">
                  {{ sourceLevelLabel(selectedCard.source_level) }}
                </span>
                <span v-for="topic in selectedCard.related_topics || []" :key="topic">{{ topic }}</span>
              </div>
            </div>
            <div class="reader-actions">
              <button type="button" @click.stop="askAbout(selectedCard)">问 Jarvis</button>
              <button type="button" @click.stop="openRaw(selectedCard)">原始资料</button>
              <button type="button" class="danger" @click.stop="deleteCard(selectedCard)">删除</button>
            </div>
          </div>

          <section v-if="importImpact" class="impact-module">
            <div class="impact-head">
              <div>
                <p class="module-label">PAPER PIPELINE</p>
                <h2>导入影响</h2>
              </div>
              <span>{{ impactSummary }}</span>
            </div>
            <div class="impact-stats">
              <div><strong>{{ uniqueImpact.created.length }}</strong><span>Created</span></div>
              <div><strong>{{ uniqueImpact.updated.length }}</strong><span>Updated</span></div>
              <div><strong>{{ uniqueImpact.linked.length }}</strong><span>Linked</span></div>
              <div><strong>{{ uniqueImpact.rejected.length }}</strong><span>Rejected</span></div>
            </div>
            <ul v-if="impactRows.length" class="impact-list">
              <li v-for="row in impactRows" :key="row.key">
                <b>{{ row.kind }}</b>
                <button v-if="row.cardId" type="button" @click.stop="selectCardById(row.cardId)">
                  {{ row.title }}
                </button>
                <span v-else>{{ row.title }}</span>
              </li>
            </ul>
          </section>

          <section v-if="selectedSummary && !hasProblemSection" class="wiki-section summary-section">
            <h2>摘要</h2>
            <div v-html="linkifiedText(selectedSummary)"></div>
          </section>

          <section
            v-for="section in compiledSections"
            :key="section.key"
            class="wiki-section"
          >
            <h2>{{ section.title }}</h2>
            <div v-html="section.html"></div>
          </section>

          <section v-if="linkedKnowledge.length" class="wiki-section linked-knowledge-section">
            <h2>Linked Knowledge</h2>
            <div class="knowledge-link-grid">
              <button
                v-for="item in linkedKnowledge"
                :key="item.id"
                type="button"
                class="knowledge-link-card"
                @click.stop="selectCardById(item.id)"
              >
                <span>{{ item.title }}</span>
                <small>{{ typeLabel(item.pageType) }} · {{ relationLabel(item.relationType) }}</small>
              </button>
            </div>
          </section>

          <section v-if="imagePreviewSources.length" class="wiki-section">
            <h2>图片</h2>
            <div class="image-strip">
              <a v-for="url in imagePreviewSources" :key="url" :href="normalUrl(url)" target="_blank" rel="noreferrer">
                <img :src="normalUrl(url)" :alt="selectedCard.title" />
              </a>
            </div>
          </section>

          <section v-if="sourceEvidence.length" class="wiki-section evidence-section">
            <h2>Source evidence</h2>
            <ul>
              <li v-for="item in sourceEvidence" :key="item.id">
                <strong>{{ item.source_card_title || item.section_id || 'Evidence' }}</strong>
                <p>{{ item.claim_text || item.evidence_text }}</p>
              </li>
            </ul>
          </section>

          <section v-if="selectedCard.source_urls?.length" class="wiki-section">
            <h2>来源</h2>
            <ul>
              <li v-for="url in selectedCard.source_urls" :key="url">
                <a :href="normalUrl(url)" target="_blank" rel="noreferrer">{{ readableUrl(url) }}</a>
              </li>
            </ul>
          </section>
        </article>

        <div v-else class="reader-empty">
          <h2>选择一张卡片开始阅读</h2>
          <p>论文、概念卡和方法卡会在这里形成可跳转的阅读视图。</p>
        </div>
      </main>

      <aside class="vault-aside">
        <section class="side-panel">
          <div class="panel-title">
            <span>Related</span>
            <small>{{ relatedCards.length }}</small>
          </div>
          <ul v-if="relatedCards.length" class="related-list">
            <li v-for="item in relatedCards" :key="item.key">
              <button type="button" @click="selectCardById(item.cardId)">{{ item.title }}</button>
              <span>{{ item.meta }}</span>
            </li>
          </ul>
          <n-empty v-else description="暂无关联卡片" size="small" />
        </section>

        <section class="side-panel">
          <div class="panel-title">
            <span>Source</span>
            <small>trace</small>
          </div>
          <dl v-if="selectedCard" class="meta-list">
            <div><dt>类型</dt><dd>{{ typeLabel(selectedCard.page_type) }}</dd></div>
            <div><dt>层级</dt><dd>{{ sourceLevelLabel(selectedCard.source_level) }}</dd></div>
            <div><dt>Markdown</dt><dd>{{ selectedCard.markdown_path || '-' }}</dd></div>
          </dl>
        </section>
      </aside>
    </div>

    <n-modal v-model:show="rawModalVisible" preset="card" style="width: 960px; max-width: 95vw;" :bordered="false">
      <template #header>
        <div class="modal-head">
          <span>{{ rawModalTitle }}</span>
          <n-tag size="small">Markdown Source</n-tag>
        </div>
      </template>
      <pre class="raw-viewer"><code>{{ rawMarkdown }}</code></pre>
    </n-modal>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { NEmpty, NInput, NModal, NTag } from 'naive-ui'
import { api, type WikiCard } from '../api'

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
    { key: 'papers', label: 'Papers', items: [] },
    { key: 'concepts', label: 'Concepts', items: [] },
    { key: 'methods', label: 'Methods', items: [] },
    { key: 'interviews', label: 'Interviews', items: [] },
    { key: 'sources', label: 'Sources', items: [] }
  ]
  for (const card of allCards.value) {
    groups.find((group) => group.key === cardGroup(card))?.items.push(card)
  }
  return groups
})

const selectedSummary = computed(() => selectedCard.value ? cleanText(selectedCard.value.summary) : '')
const hasProblemSection = computed(() => hasContent(selectedCard.value?.content_json?.problem))
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
  `Created ${uniqueImpact.value.created.length} / Updated ${uniqueImpact.value.updated.length} / Linked ${uniqueImpact.value.linked.length}`
)

const impactRows = computed(() => [
  ...uniqueImpact.value.created.map((item: any) => ({ key: `created:${item.id}`, kind: 'created', title: item.title, cardId: item.id })),
  ...uniqueImpact.value.updated.map((item: any) => ({ key: `updated:${item.id}`, kind: 'updated', title: item.title, cardId: item.id })),
  ...uniqueImpact.value.linked.map((item: any) => ({ key: `linked:${item.to || item.id}`, kind: 'linked', title: item.title, cardId: item.to || item.id }))
].slice(0, 12))

const compiledSections = computed(() => {
  if (!selectedCard.value) return []
  const content = selectedCard.value.content_json || {}
  const order = ['problem', 'definition', 'question_context', 'content', 'core_points', 'interview_questions', 'answer_frame', 'learning_value', 'key_idea', 'method', 'mechanism', 'results', 'findings', 'key_points', 'limitations', 'key_takeaways', 'interview_notes', 'notes']
  return Object.entries(content)
    .filter(([key, value]) => shouldRenderContentField(key, value))
    .sort(([a], [b]) => orderIndex(a, order) - orderIndex(b, order))
    .map(([key, value]) => ({ key, title: sectionTitle(key), html: valueToHtml(value) }))
    .filter((section) => section.html)
})

const linkedKnowledge = computed<LinkedKnowledgeRow[]>(() => {
  const content = selectedCard.value?.content_json || {}
  return arrayOfObjects(content.linked_knowledge)
    .map((item) => ({
      id: String(item.id || item.to || item.card_id || ''),
      title: cleanText(String(item.title || item.alias || 'Linked card')),
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
    selectedCard.value = cardData
    cardLinks.value = linksData
  } catch {
    cardLinks.value = null
  }
}

function selectGroup(groupKey: string) {
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
  router.push({ path: '/', query: { ask: `基于 Wiki 页面《${card.title}》，整理一版适合面试展示的解释。` } })
}

function openGraphSearch() {
  if (!selectedCard.value?.related_topics?.[0]) return
  query.value = selectedCard.value.related_topics[0]
  loadCards()
}

function openDashboard() {
  router.push('/daily')
}

function handleDocumentClick(event: MouseEvent) {
  const target = event.target as HTMLElement
  const button = target.closest<HTMLButtonElement>('[data-keyword-card-id]')
  if (button?.dataset.keywordCardId) {
    selectCardById(button.dataset.keywordCardId)
  }
}

function valueToHtml(value: unknown): string {
  if (Array.isArray(value)) {
    const items = value.map((item) => cleanText(renderInline(item))).filter(Boolean)
    return items.length ? `<ul>${items.map((item) => `<li>${linkifiedText(item)}</li>`).join('')}</ul>` : ''
  }
  if (typeof value === 'object' && value !== null) {
    const rows = Object.entries(value as Record<string, unknown>)
      .map(([key, nested]) => {
        const text = cleanText(renderInline(nested))
        return text ? `<li><strong>${escapeHtml(sectionTitle(key))}</strong>: ${linkifiedText(text)}</li>` : ''
      })
      .filter(Boolean)
    return rows.length ? `<ul>${rows.join('')}</ul>` : ''
  }
  const text = cleanText(String(value))
  return text ? `<p>${linkifiedText(text)}</p>` : ''
}

function linkifiedText(text: string) {
  const source = cleanText(text)
  if (!source) return ''
  const aliases = aliasItems.value
    .filter((item) => item.card_id !== selectedCard.value?.id && item.alias && item.alias.trim().length >= 3)
    .sort((a, b) => b.alias.length - a.alias.length)
  const matches: Array<{ start: number; end: number; alias: AliasItem }> = []
  const lower = source.toLowerCase()
  for (const alias of aliases) {
    const needle = alias.alias.toLowerCase()
    let index = lower.indexOf(needle)
    while (index !== -1) {
      const end = index + needle.length
      if (!matches.some((match) => rangesOverlap(index, end, match.start, match.end))) {
        matches.push({ start: index, end, alias })
      }
      index = lower.indexOf(needle, index + needle.length)
    }
  }
  if (!matches.length) return paragraphsToHtml(source)
  matches.sort((a, b) => a.start - b.start)
  let cursor = 0
  let html = ''
  for (const match of matches) {
    html += escapeHtml(source.slice(cursor, match.start))
    html += `<button type="button" class="keyword-link" data-keyword-card-id="${escapeHtml(match.alias.card_id)}">${escapeHtml(source.slice(match.start, match.end))}</button>`
    cursor = match.end
  }
  html += escapeHtml(source.slice(cursor))
  return paragraphsToHtmlFromEscaped(html)
}

function paragraphsToHtml(text: string) {
  return paragraphsToHtmlFromEscaped(escapeHtml(text))
}

function paragraphsToHtmlFromEscaped(escaped: string) {
  const parts = escaped.split(/\n{2,}/).map((part) => part.trim()).filter(Boolean)
  return parts.map((part) => `<p>${part.replace(/\n/g, '<br>')}</p>`).join('')
}

function rangesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number) {
  return aStart < bEnd && bStart < aEnd
}

function cardGroup(card: WikiCard) {
  const sourceType = String(card.content_json?.source_type || '').toLowerCase()
  const urls = (card.source_urls || []).join(' ').toLowerCase()
  if (card.page_type === 'PaperPage' || sourceType.includes('paper') || /arxiv|\.pdf|doi\.org/.test(urls)) return 'papers'
  if (card.page_type === 'ConceptPage') return 'concepts'
  if (card.page_type === 'MethodPage') return 'methods'
  if (card.page_type === 'InterviewQA') return 'interviews'
  return 'sources'
}

function groupLabelForCard(card: WikiCard) {
  return libraryGroups.value.find((group) => group.key === cardGroup(card))?.label || 'Wiki'
}

function cardSubtitle(card: WikiCard) {
  if (card.summary) return cleanText(card.summary).slice(0, 64)
  if (card.source_urls?.[0]) return readableUrl(card.source_urls[0])
  return typeLabel(card.page_type)
}

function typeLabel(value: string) {
  return ({
    PaperPage: '论文',
    ConceptPage: '概念',
    MethodPage: '方法',
    ComparePage: '对比',
    InterviewQA: '面经',
    MistakeNote: '错题',
    SourceNote: '资料'
  } as Record<string, string>)[value] || value
}

function sourceLevelLabel(value: string) {
  return ({ primary: 'primary', secondary: 'secondary', tertiary: 'tertiary' } as Record<string, string>)[value] || 'wiki'
}

function relationLabel(value: string) {
  return ({
    topic_related: 'topic related',
    mentions: 'mentions',
    introduces: 'introduces',
    uses: 'uses'
  } as Record<string, string>)[value] || value || 'related'
}

function sectionTitle(key: string) {
  const labels: Record<string, string> = {
    problem: 'Problem',
    key_idea: 'Key idea',
    method: 'Method',
    methods: 'Method',
    mechanism: 'Mechanism',
    results: 'Results',
    findings: 'Findings',
    limitations: 'Limitations',
    key_takeaways: 'Key takeaways',
    interview_notes: 'Interview notes',
    notes: 'Notes',
    definition: 'Definition',
    question_context: 'Question context',
    core_points: 'Core points',
    interview_questions: 'Interview questions',
    answer_frame: 'Answer frame',
    learning_value: 'Learning value',
    content: '内容',
    ocr_excerpt: 'OCR excerpt',
    image_notes: 'Image notes',
    source_url: 'Source URL'
  }
  return labels[key] || key.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function shouldRenderContentField(key: string, value: unknown) {
  const sourceType = String(selectedCard.value?.content_json?.source_type || '').toLowerCase()
  if (sourceType === 'xiaohongshu' && ['notes', 'ocr_excerpt', 'image_notes', 'source_url'].includes(key)) {
    return false
  }
  const hidden = new Set([
    'schema_version',
    'compile_status',
    'compile_error',
    'source_packet_id',
    'raw_source_path',
    'pdf_storage_uri',
    'compiler_model',
    'parser_used',
    'pipeline',
    'extractor_agent',
    'distiller_agent',
    'reviewer_agent',
    'merge_agent',
    'review_status',
    'review_confidence',
    'review_hints',
    'distill_review',
    'source_kind',
    'source_type',
    'title',
    'import_impact',
    'linked_knowledge',
    'downloaded_images',
    'attachments',
    'image_urls',
    '_ocr_text',
    '_ocr_notes',
    '_ocr_status'
  ])
  return !key.startsWith('_') && !hidden.has(key) && hasContent(value)
}

function hasContent(value: unknown) {
  if (value === null || value === undefined || value === '') return false
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
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/\s*---+\s*/g, '\n')
    .replace(/（已过）/g, '')
    .replace(/\s+\n/g, '\n')
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
  if (url.startsWith('oss://') || url.startsWith('local://')) return url.replace(/^oss:\/\/[^/]+\//, '').replace(/^local:\/\//, '')
  if (url.startsWith('file://')) return url.replace(/^file:\/\//, '')
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

onMounted(loadCards)
</script>

<style scoped>
.vault-page {
  min-height: calc(100dvh - 84px);
  display: grid;
  grid-template-rows: auto auto minmax(0, 1fr);
  gap: 12px;
  color: var(--text);
}

.vault-header,
.vault-toolbar,
.vault-sidebar,
.vault-reader,
.side-panel {
  border: 1px solid rgba(125, 211, 252, 0.13);
  background:
    linear-gradient(180deg, rgba(8, 18, 32, 0.92), rgba(5, 10, 19, 0.96)),
    #07111f;
  box-shadow: 0 18px 48px rgba(2, 6, 23, 0.28);
}

.vault-header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto 180px;
  align-items: center;
  gap: 18px;
  min-height: 132px;
  overflow: hidden;
  position: relative;
  padding: 20px 22px;
  border-radius: 18px;
}

.vault-header::before {
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    linear-gradient(90deg, transparent, rgba(125, 211, 252, 0.16), transparent),
    linear-gradient(rgba(125, 211, 252, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(125, 211, 252, 0.03) 1px, transparent 1px);
  background-size: 220px 100%, 28px 28px, 28px 28px;
  animation: vault-scan 7s linear infinite;
  opacity: 0.7;
}

.vault-title-block,
.vault-status,
.graph-visual {
  position: relative;
}

.vault-title-block h1 {
  margin: 0;
  color: #f8fafc;
  font-size: clamp(26px, 2.5vw, 38px);
  line-height: 1.12;
  letter-spacing: 0;
}

.vault-status {
  display: grid;
  grid-template-columns: repeat(3, minmax(92px, 1fr));
  gap: 8px;
}

.vault-status div {
  min-width: 92px;
  padding: 10px 12px;
  border: 1px solid rgba(125, 211, 252, 0.12);
  border-radius: 10px;
  background: rgba(2, 6, 23, 0.38);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

.vault-status span {
  display: block;
  color: #8aa4bd;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.vault-status strong {
  display: block;
  margin-top: 5px;
  color: #ecfeff;
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}

.graph-visual {
  height: 88px;
  border: 1px solid rgba(125, 211, 252, 0.12);
  border-radius: 14px;
  background: rgba(2, 6, 23, 0.24);
}

.graph-node {
  position: absolute;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #67e8f9;
  box-shadow: 0 0 0 5px rgba(103, 232, 249, 0.08);
  animation: node-pulse 2.4s ease-in-out infinite;
}

.graph-node.n1 { left: 22px; top: 18px; }
.graph-node.n2 { left: 80px; top: 34px; animation-delay: 0.25s; }
.graph-node.n3 { right: 32px; top: 22px; animation-delay: 0.5s; }
.graph-node.n4 { right: 64px; bottom: 18px; animation-delay: 0.75s; }

.graph-line {
  position: absolute;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(103, 232, 249, 0.7), transparent);
  transform-origin: left center;
  opacity: 0.6;
}

.graph-line.l1 { left: 30px; top: 25px; width: 62px; transform: rotate(15deg); }
.graph-line.l2 { left: 88px; top: 39px; width: 66px; transform: rotate(-10deg); }
.graph-line.l3 { left: 95px; top: 58px; width: 56px; transform: rotate(22deg); }

.vault-toolbar {
  display: grid;
  grid-template-columns: auto minmax(240px, 1fr) auto;
  align-items: center;
  gap: 12px;
  min-height: 58px;
  padding: 10px 12px;
  border-radius: 14px;
}

.toolbar-tabs {
  display: flex;
  gap: 5px;
  padding: 4px;
  border: 1px solid rgba(148, 163, 184, 0.12);
  border-radius: 10px;
  background: rgba(2, 6, 23, 0.24);
}

.toolbar-tabs button,
.refresh-button,
.reader-actions button,
.library-head,
.card-row,
.related-list button,
.impact-list button {
  transition: border-color 180ms ease, background 180ms ease, color 180ms ease, transform 180ms ease;
}

.toolbar-tabs button,
.refresh-button {
  min-height: 32px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.toolbar-tabs button {
  padding: 0 11px;
}

.toolbar-tabs button:hover,
.toolbar-tabs button.active,
.refresh-button:hover {
  border-color: rgba(125, 211, 252, 0.22);
  background: rgba(56, 189, 248, 0.1);
  color: #ecfeff;
}

.toolbar-tabs button:active,
.refresh-button:active,
.reader-actions button:active,
.card-row:active,
.library-head:active {
  transform: translateY(1px) scale(0.99);
}

.refresh-button {
  padding: 0 14px;
}

.vault-search {
  justify-self: stretch;
}

.vault-layout {
  min-height: 0;
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr) 300px;
  gap: 14px;
}

.vault-sidebar,
.side-panel {
  padding: 15px;
  border-radius: 14px;
}

.side-panel + .side-panel {
  margin-top: 12px;
}

.panel-title {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 11px;
}

.panel-title span {
  color: #f8fafc;
  font-size: 14px;
  font-weight: 750;
}

.panel-title small {
  color: #7dd3fc;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.library-tree,
.library-items {
  display: grid;
  gap: 6px;
}

.library-head,
.card-row {
  width: 100%;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-soft);
  text-align: left;
  cursor: pointer;
}

.library-head {
  display: flex;
  justify-content: space-between;
  padding: 9px 10px;
  border-radius: 9px;
  font-weight: 700;
}

.library-head strong {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.library-head:hover,
.library-head.active,
.card-row:hover,
.card-row.active {
  border-color: rgba(125, 211, 252, 0.22);
  background: rgba(56, 189, 248, 0.1);
  color: #fff;
}

.library-items {
  max-height: min(48vh, 440px);
  overflow: auto;
  padding-left: 10px;
}

.card-row {
  display: grid;
  gap: 4px;
  padding: 9px 10px;
  border-radius: 9px;
}

.card-row span,
.card-row small {
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-row span {
  font-size: 13px;
  line-height: 1.42;
}

.card-row small {
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.empty-note {
  margin: 8px 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.vault-reader {
  min-width: 0;
  overflow: auto;
  padding: 24px 28px 40px;
  border-radius: 16px;
}

.wiki-document {
  max-width: 940px;
}

.reader-topline {
  display: flex;
  gap: 8px;
  margin: 0 0 12px;
  color: var(--text-muted);
  font-size: 12px;
}

.reader-topline span + span::before {
  content: "/";
  margin-right: 8px;
  color: rgba(148, 163, 184, 0.5);
}

.reader-title-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 12px;
  align-items: start;
}

.wiki-document h1 {
  max-width: 100%;
  margin: 0 0 12px;
  color: #fff;
  font-size: clamp(24px, 1.7vw, 32px);
  line-height: 1.18;
  letter-spacing: 0;
  overflow-wrap: anywhere;
  text-wrap: pretty;
}

.reader-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-start;
  gap: 8px;
}

.reader-actions button {
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid rgba(125, 211, 252, 0.2);
  border-radius: 8px;
  background: rgba(56, 189, 248, 0.08);
  color: #cffafe;
  cursor: pointer;
}

.reader-actions button:hover {
  background: rgba(56, 189, 248, 0.16);
}

.reader-actions .danger {
  border-color: rgba(244, 63, 94, 0.24);
  color: #fecdd3;
}

.wiki-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-bottom: 20px;
}

.wiki-tags span {
  padding: 4px 8px;
  border: 1px solid rgba(148, 163, 184, 0.1);
  border-radius: 7px;
  background: rgba(148, 163, 184, 0.1);
  color: #cbd5e1;
  font-size: 12px;
}

.level-tag.primary {
  border-color: rgba(34, 197, 94, 0.2);
  background: rgba(34, 197, 94, 0.12);
  color: #bbf7d0;
}

.level-tag.secondary {
  border-color: rgba(245, 158, 11, 0.18);
  background: rgba(245, 158, 11, 0.12);
  color: #fde68a;
}

.impact-module {
  margin: 22px 0 26px;
  padding: 15px;
  border: 1px solid rgba(125, 211, 252, 0.18);
  border-radius: 12px;
  background:
    linear-gradient(135deg, rgba(8, 47, 73, 0.36), rgba(15, 23, 42, 0.56)),
    rgba(15, 23, 42, 0.52);
}

.impact-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.module-label {
  margin: 0 0 5px;
  color: #67e8f9;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.impact-head h2 {
  margin: 0;
  font-size: 17px;
}

.impact-head span {
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.impact-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.impact-stats div {
  display: grid;
  gap: 2px;
  padding: 9px;
  border: 1px solid rgba(148, 163, 184, 0.08);
  border-radius: 8px;
  background: rgba(2, 6, 23, 0.28);
}

.impact-stats strong {
  color: #fff;
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}

.impact-stats span,
.impact-list {
  color: var(--text-soft);
  font-size: 12px;
}

.impact-list {
  display: grid;
  gap: 7px;
  margin: 12px 0 0;
  padding-left: 16px;
}

.impact-list b {
  margin-right: 8px;
  color: #67e8f9;
}

.impact-list button,
.related-list button {
  padding: 0;
  border: 0;
  background: transparent;
  color: #cffafe;
  text-align: left;
  cursor: pointer;
}

.wiki-section {
  margin-top: 24px;
}

.wiki-section h2 {
  margin: 0 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid rgba(125, 211, 252, 0.14);
  color: #f8fafc;
  font-size: 18px;
  line-height: 1.25;
}

.wiki-section :deep(p),
.wiki-section :deep(li) {
  max-width: 78ch;
  color: #c9d7e8;
  font-size: 15px;
  line-height: 1.78;
}

.wiki-section :deep(p) {
  margin: 0 0 10px;
}

.wiki-section :deep(ul) {
  margin: 0;
  padding-left: 22px;
}

.wiki-section :deep(li + li) {
  margin-top: 7px;
}

.wiki-section :deep(strong) {
  color: #fff;
  font-weight: 700;
}

.wiki-section :deep(.keyword-link) {
  display: inline;
  margin: 0 1px;
  padding: 1px 5px;
  border: 1px solid rgba(125, 211, 252, 0.24);
  border-radius: 5px;
  background: rgba(56, 189, 248, 0.13);
  color: #e0f2fe;
  font: inherit;
  cursor: pointer;
}

.wiki-section :deep(.keyword-link:hover) {
  background: rgba(56, 189, 248, 0.24);
  color: #fff;
}

.wiki-section a {
  color: #7dd3fc;
  text-decoration: none;
}

.knowledge-link-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px;
  max-width: 860px;
}

.knowledge-link-card {
  display: grid;
  gap: 6px;
  min-height: 70px;
  padding: 12px 14px;
  border: 1px solid rgba(125, 211, 252, 0.16);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(14, 165, 233, 0.12), rgba(15, 23, 42, 0.62)),
    rgba(15, 23, 42, 0.72);
  color: #e6f7ff;
  text-align: left;
  cursor: pointer;
}

.knowledge-link-card:hover {
  border-color: rgba(125, 211, 252, 0.42);
  background:
    linear-gradient(135deg, rgba(14, 165, 233, 0.22), rgba(15, 23, 42, 0.72)),
    rgba(15, 23, 42, 0.82);
}

.knowledge-link-card span {
  font-size: 14px;
  font-weight: 800;
  line-height: 1.35;
}

.knowledge-link-card small {
  color: #8fb3c9;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.image-strip {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  max-width: 760px;
}

.image-strip a {
  display: block;
  overflow: hidden;
  border-radius: 10px;
  background: rgba(148, 163, 184, 0.12);
  aspect-ratio: 4 / 3;
}

.image-strip img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.evidence-section p {
  margin-top: 4px;
}

.related-list {
  display: grid;
  gap: 12px;
  margin: 0;
  padding-left: 18px;
}

.related-list span {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.meta-list {
  display: grid;
  gap: 12px;
  margin: 0;
}

.meta-list dt {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
}

.meta-list dd {
  margin: 2px 0 0;
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.45;
  word-break: break-word;
}

.reader-empty {
  display: grid;
  place-content: center;
  min-height: 420px;
  text-align: center;
}

.reader-empty h2 {
  margin: 0 0 8px;
  color: #fff;
}

.reader-empty p {
  margin: 0;
  color: var(--text-muted);
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
  color: #dbeafe;
}

@keyframes vault-scan {
  0% { background-position: -220px 0, 0 0, 0 0; }
  100% { background-position: 520px 0, 28px 28px, 28px 28px; }
}

@keyframes node-pulse {
  0%, 100% { transform: scale(1); opacity: 0.78; }
  50% { transform: scale(1.45); opacity: 1; }
}

@media (max-width: 1280px) {
  .vault-layout {
    grid-template-columns: 260px minmax(0, 1fr);
  }

  .vault-aside {
    display: none;
  }
}

@media (max-width: 940px) {
  .vault-header,
  .vault-toolbar,
  .vault-layout,
  .reader-title-row {
    grid-template-columns: 1fr;
  }

  .vault-status {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .graph-visual {
    display: none;
  }

  .reader-actions {
    justify-content: flex-start;
  }

  .wiki-document h1 {
    font-size: 28px;
  }

  .impact-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
