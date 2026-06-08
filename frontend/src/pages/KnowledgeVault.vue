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
            <small>{{ allCards.length }} 条</small>
          </div>

          <input
            v-model="query"
            class="library-search"
            type="search"
            placeholder="搜索论文、概念、方法或关键词"
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
                      <span>{{ card.title }}</span>
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
            <span>{{ selectedCard ? typeLabel(selectedCard.page_type) : '-' }}</span>
          </div>
        </aside>

        <main class="vault-reader" aria-label="Reader">
          <article v-if="selectedCard" class="wiki-document" @click="handleDocumentClick">
            <header class="reader-head">
              <div class="reader-meta-line">
                <span>{{ groupLabelForCard(selectedCard) }}</span>
                <span>{{ typeLabel(selectedCard.page_type) }}</span>
              </div>
              <h1>{{ selectedCard.title }}</h1>
              <div class="reader-control-row">
                <span :class="['source-level-chip', selectedCard.source_level || 'neutral']">
                  {{ sourceLevelLabel(selectedCard.source_level) }}
                </span>
                <span v-for="topic in selectedCard.related_topics || []" :key="topic" class="reader-tag">{{ topic }}</span>
              </div>
              <div class="reader-actions">
                <button type="button" class="primary-action" @click.stop="askAbout(selectedCard)">基于此页提问</button>
                <button type="button" @click.stop="openRaw(selectedCard)">原始资料</button>
                <button type="button" class="danger" @click.stop="deleteCard(selectedCard)">删除</button>
              </div>
            </header>

            <section v-if="importImpact" class="paper-pipeline">
              <div class="pipeline-head">
                <div>
                  <span>导入流水线</span>
                  <h2>导入影响</h2>
                </div>
                <small>{{ impactSummary }}</small>
              </div>
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
            </section>

            <section v-if="showSummarySection" class="wiki-section">
              <h2>摘要</h2>
              <div v-html="linkifiedText(selectedSummary)"></div>
            </section>

            <section v-for="section in compiledSections" :key="section.key" class="wiki-section">
              <h2>{{ section.title }}</h2>
              <div v-html="section.html"></div>
            </section>

            <section v-if="imagePreviewSources.length" class="wiki-section">
              <h2>图片</h2>
              <div class="image-strip">
                <a v-for="url in imagePreviewSources" :key="url" :href="normalUrl(url)" target="_blank" rel="noreferrer">
                  <img :src="normalUrl(url)" :alt="selectedCard.title" />
                </a>
              </div>
            </section>

            <section class="reader-detail-section">
              <h2>详情</h2>
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
                    <div><dt>Markdown</dt><dd class="source-path" :title="selectedCard.markdown_path || firstSourceLabel || '尚未记录'">{{ selectedCard.markdown_path || firstSourceLabel || '尚未记录' }}</dd></div>
                  </dl>
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
            </section>
          </article>

          <div v-else class="reader-empty">
            <span>知识库</span>
            <h1>选择一张卡片开始阅读</h1>
            <p>论文、概念卡和方法卡会在这里形成可跳转的阅读视图。</p>
          </div>
        </main>

        <aside v-if="selectedCard" class="vault-trace" aria-label="关联与来源追踪">
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
              <div><dt>来源数</dt><dd>{{ selectedSourceCount }}</dd></div>
              <div><dt>Markdown</dt><dd class="source-path" :title="selectedCard.markdown_path || firstSourceLabel || '尚未记录'">{{ selectedCard.markdown_path || firstSourceLabel || '尚未记录' }}</dd></div>
            </dl>
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
            <p v-else class="empty-note">暂无来源证据。</p>
          </section>
        </aside>
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
    { key: 'concepts', label: '概念', items: [] },
    { key: 'methods', label: '方法', items: [] },
    { key: 'interviews', label: '面经', items: [] },
    { key: 'sources', label: '资料', items: [] }
  ]
  for (const card of allCards.value) {
    groups.find((group) => group.key === cardGroup(card))?.items.push(card)
  }
  return groups
})

const selectedSummary = computed(() => selectedCard.value ? cleanText(selectedCard.value.summary) : '')
const hasProblemSection = computed(() => hasContent(selectedCard.value?.content_json?.problem))
const selectedSourceType = computed(() => String(selectedCard.value?.content_json?.source_type || '').toLowerCase())
const showSummarySection = computed(() =>
  Boolean(selectedSummary.value && !hasProblemSection.value && selectedSourceType.value !== 'user_selection')
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
const showSourceTrace = computed(() => {
  return selectedSourceType.value !== 'user_selection'
})
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
  router.push({ path: '/', query: { ask: `基于 Wiki 页面《${card.title}》，整理一版适合面试展示的解释。` } })
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
  const candidates: Array<{ start: number; end: number; alias: AliasItem; score: number }> = []
  const lower = source.toLowerCase()
  for (const alias of aliases) {
    const needle = alias.alias.toLowerCase()
    let index = lower.indexOf(needle)
    while (index !== -1) {
      const end = index + needle.length
      if (isAliasBoundary(source, index, end)) {
        candidates.push({
          start: index,
          end,
          alias,
          score: aliasLinkScore(alias, source.slice(index, end))
        })
      }
      index = lower.indexOf(needle, index + needle.length)
    }
  }
  const matches: Array<{ start: number; end: number; alias: AliasItem }> = []
  for (const candidate of candidates.sort(compareAliasCandidates)) {
    if (!matches.some((match) => rangesOverlap(candidate.start, candidate.end, match.start, match.end))) {
      matches.push({ start: candidate.start, end: candidate.end, alias: candidate.alias })
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

function compareAliasCandidates(
  a: { start: number; end: number; alias: AliasItem; score: number },
  b: { start: number; end: number; alias: AliasItem; score: number }
) {
  if (b.score !== a.score) return b.score - a.score
  const lengthDiff = (b.end - b.start) - (a.end - a.start)
  if (lengthDiff !== 0) return lengthDiff
  return a.start - b.start
}

function aliasLinkScore(alias: AliasItem, matchedText: string) {
  const aliasText = normalizeLinkText(alias.alias)
  const titleText = normalizeLinkText(alias.title)
  const matched = normalizeLinkText(matchedText)
  const exactTitleScore = titleText && titleText === matched ? 2000 : 0
  const exactAliasScore = aliasText && aliasText === matched ? 1000 : 0
  return exactTitleScore + exactAliasScore + pageTypeLinkRank(alias.page_type) + matchedText.length * 10
}

function pageTypeLinkRank(pageType: string) {
  if (pageType === 'ConceptPage') return 90
  if (pageType === 'MethodPage') return 80
  if (pageType === 'InterviewQA') return 50
  if (pageType === 'PaperPage') return 20
  return 40
}

function normalizeLinkText(value: string) {
  return String(value || '').toLowerCase().replace(/\s+/g, ' ').trim()
}

function isAliasBoundary(source: string, start: number, end: number) {
  const before = start > 0 ? source[start - 1] : ''
  const after = end < source.length ? source[end] : ''
  return !isAsciiWord(before) && !isAsciiWord(after)
}

function isAsciiWord(char: string) {
  return /^[a-zA-Z0-9_]$/.test(char)
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
  return ({ primary: '一手来源', secondary: '二手整理', tertiary: '三手线索' } as Record<string, string>)[value] || 'Wiki'
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
  if (selectedSourceType.value === 'user_selection' && key === 'notes') return '内容'
  const labels: Record<string, string> = {
    problem: '问题',
    key_idea: '核心观点',
    method: '方法',
    methods: '方法',
    mechanism: '机制',
    results: '结果',
    findings: '发现',
    limitations: '局限',
    key_takeaways: '要点',
    interview_notes: '面试笔记',
    notes: '笔记',
    definition: '定义',
    question_context: '问题语境',
    core_points: '核心要点',
    interview_questions: '面试问题',
    answer_frame: '回答框架',
    learning_value: '学习价值',
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
    'source_query_id',
    'artifact_uri',
    'maintenance_candidate_id',
    'maintenance_candidate_type',
    'evidence',
    'question',
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
  --ink-text: #f8fafc;
  --ink-text-soft: #cbd5e1;
  --ink-text-muted: #94a3b8;
  --desk-accent: #9bb8ad;
  --desk-accent-bright: #d4e3d8;
  --line-quiet: rgba(195, 214, 202, 0.1);
  --line-hover: rgba(195, 214, 202, 0.24);
  --line-active: rgba(195, 214, 202, 0.32);
  --reader-serif: "Source Serif 4", "Noto Serif SC", "Songti SC", Georgia, serif;
  position: relative;
  z-index: 1;
  min-height: calc(100dvh - 84px);
  padding-top: 48px;
  padding-bottom: 48px;
  background: transparent;
  color: var(--ink-text);
}

.vault-stage {
  position: relative;
  z-index: 1;
  max-width: 1440px;
  min-height: calc(100dvh - 120px);
  margin: 0 auto;
  padding: 18px;
  border: 1px solid var(--line-quiet);
  border-radius: 20px;
  background: transparent;
}

.vault-layout {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr) 320px;
  gap: 24px;
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
  min-height: calc(100dvh - 156px);
  padding: 20px 16px;
}

.vault-reader {
  min-width: 0;
  padding: 32px 40px;
}

.vault-trace {
  display: grid;
  gap: 14px;
  padding: 24px 20px;
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

.wiki-document h1 {
  max-width: 780px;
  margin: 0;
  color: var(--ink-text);
  font-family: var(--reader-serif);
  font-size: 28px;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
  overflow-wrap: anywhere;
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
  padding: 32px 40px;
  border-radius: 16px;
}

.reader-empty span {
  color: var(--ink-text-muted);
  font-size: 13px;
}

.reader-empty h1 {
  margin: 8px 0 0;
  font-family: var(--reader-serif);
  font-size: 28px;
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

@media (min-width: 1024px) and (max-width: 1279px) {
  .vault-layout {
    grid-template-columns: 220px minmax(0, 1fr) 280px;
    gap: 20px;
  }

  .vault-library {
    padding: 20px 16px;
  }

  .vault-reader {
    padding: 32px 40px;
  }

  .vault-trace {
    padding: 24px 20px;
  }
}

@media (max-width: 1023px) {
  .vault-stage {
    padding: 14px;
  }

  .mobile-library-picker {
    display: block;
  }

  .vault-layout {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  .vault-library,
  .vault-trace {
    display: none;
  }

  .vault-reader {
    padding: 28px 24px;
  }

  .reader-detail-section {
    display: block;
  }

  .detail-stack {
    display: grid;
    gap: 14px;
  }
}

@media (max-width: 720px) {
  .vault-stage {
    padding: 10px;
    border-radius: 16px;
  }

  .vault-reader {
    padding: 22px 18px;
  }

  .wiki-document h1,
  .reader-empty h1 {
    font-size: 25px;
  }

  .pipeline-head,
  .reader-actions {
    align-items: flex-start;
  }
}

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


