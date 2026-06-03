<template>
  <section>
    <div class="page-heading">
      <div>
        <p class="eyebrow">PRECISION READING</p>
        <h2>论文精读</h2>
      </div>
      <n-button :loading="loading" @click="refresh">刷新</n-button>
    </div>

    <n-card title="快速加入论文" class="section-card">
      <n-grid :cols="2" :x-gap="16" :y-gap="16" responsive="screen">
        <n-gi>
          <div class="ingest-panel">
            <label class="field-label">上传 PDF 并快速解析</label>
            <input class="file-input" type="file" accept="application/pdf" @change="onFileChange" />
            <n-input v-model:value="sourceUrl" placeholder="arXiv / PDF 来源链接，可选" />
            <n-button type="primary" :loading="uploading" :disabled="!selectedFile" @click="uploadPaper">
              上传并建立 PaperIndex
            </n-button>
          </div>
        </n-gi>
        <n-gi>
          <div class="ingest-panel">
            <label class="field-label">从 data/ 目录选择 PDF</label>
            <n-select
              v-model:value="selectedLocalPdf"
              filterable
              clearable
              placeholder="选择一个本地 PDF"
              :options="localPdfOptions"
            />
            <n-input v-model:value="sourceUrl" placeholder="来源链接，可选" />
            <n-button :loading="indexing" :disabled="!selectedLocalPdf" @click="indexLocalPaper">
              快速解析
            </n-button>
          </div>
        </n-gi>
      </n-grid>
    </n-card>

    <div class="paper-library-layout section-card">
      <n-card title="论文列表">
        <n-empty v-if="!papers.length" description="还没有论文。先上传 PDF 或从 data/ 目录选择一篇。" />
        <n-list v-else clickable>
          <n-list-item
            v-for="paper in papers"
            :key="paper.id"
            :class="{ active: selectedPaper?.id === paper.id }"
            @click="selectPaper(paper)"
          >
            <n-thing :title="paper.title" :description="paper.summary || '暂无摘要。'">
              <template #header-extra>
                <n-tag size="small">PaperIndex</n-tag>
              </template>
            </n-thing>
          </n-list-item>
        </n-list>
      </n-card>

      <n-card v-if="selectedPaper" class="paper-reader">
        <template #header>
          <div class="card-header">
            <span>{{ selectedPaper.title }}</span>
            <n-space>
              <n-button v-if="selectedPaper.pdf_path" size="small" @click="openLocalPdf(selectedPaper.pdf_path)">
                PDF 路径
              </n-button>
              <n-button size="small" type="error" secondary @click="deletePaper(selectedPaper)">
                删除
              </n-button>
            </n-space>
          </div>
        </template>

        <p class="summary">{{ selectedPaper.summary || '暂无摘要。' }}</p>
        <p class="path">{{ selectedPaper.pdf_path }}</p>

        <div class="reader-tools">
          <n-select
            v-model:value="selectedSection"
            clearable
            placeholder="选择章节"
            :options="sectionOptions"
            @update:value="loadBlocks"
          />
          <n-input v-model:value="searchQuery" placeholder="在这篇论文里搜索" @keyup.enter="searchPaper" />
          <n-button @click="searchPaper">搜索</n-button>
        </div>

        <n-tabs type="line" animated>
          <n-tab-pane name="sections" tab="章节">
            <n-empty v-if="!sections.length" description="没有识别到明确章节，已按页面建立索引。" />
            <n-list v-else>
              <n-list-item v-for="section in sections" :key="section.section" @click="chooseSection(section.section)">
                <n-thing :title="section.section" :description="`从第 ${section.start_page} 页开始，${section.blocks} 个 block`" />
              </n-list-item>
            </n-list>
          </n-tab-pane>
          <n-tab-pane name="blocks" tab="正文片段">
            <n-list>
              <n-list-item v-for="block in blocks" :key="block.id">
                <n-thing>
                  <template #header>
                    <span>第 {{ block.page }} 页 / {{ block.section || '未分节' }}</span>
                  </template>
                  <p class="evidence-snippet">{{ block.text }}</p>
                </n-thing>
              </n-list-item>
            </n-list>
          </n-tab-pane>
          <n-tab-pane name="search" tab="搜索结果">
            <n-empty v-if="!searchResults.length" description="暂无搜索结果。" />
            <n-list v-else>
              <n-list-item v-for="block in searchResults" :key="block.id">
                <n-thing>
                  <template #header>
                    <span>第 {{ block.page }} 页 / {{ block.section || '未分节' }}</span>
                  </template>
                  <p class="evidence-snippet">{{ block.text }}</p>
                </n-thing>
              </n-list-item>
            </n-list>
          </n-tab-pane>
        </n-tabs>
      </n-card>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api, type LocalPdf, type Paper, type PaperBlock, type PaperSection } from '../api'

const papers = ref<Paper[]>([])
const localPdfs = ref<LocalPdf[]>([])
const selectedPaper = ref<Paper | null>(null)
const sections = ref<PaperSection[]>([])
const blocks = ref<PaperBlock[]>([])
const searchResults = ref<PaperBlock[]>([])
const selectedLocalPdf = ref<string | null>(null)
const selectedFile = ref<File | null>(null)
const selectedSection = ref<string | null>(null)
const sourceUrl = ref('')
const searchQuery = ref('')
const loading = ref(false)
const uploading = ref(false)
const indexing = ref(false)

const localPdfOptions = computed(() => localPdfs.value.map((file) => ({
  label: `${file.name} (${formatSize(file.size)})`,
  value: file.path
})))

const sectionOptions = computed(() => sections.value.map((section) => ({
  label: `${section.section} / p.${section.start_page}`,
  value: section.section
})))

async function refresh() {
  loading.value = true
  try {
    await Promise.all([loadPapers(), loadLocalPdfs()])
  } finally {
    loading.value = false
  }
}

async function loadPapers() {
  const { data } = await api.get('/papers')
  papers.value = data.items || []
  if (!selectedPaper.value && papers.value.length) {
    await selectPaper(papers.value[0])
  }
}

async function loadLocalPdfs() {
  const { data } = await api.get('/papers/files')
  localPdfs.value = data.items || []
}

async function selectPaper(paper: Paper) {
  selectedPaper.value = paper
  selectedSection.value = null
  searchResults.value = []
  const { data } = await api.get(`/papers/${paper.id}`)
  sections.value = data.sections || []
  await loadBlocks()
}

async function loadBlocks() {
  if (!selectedPaper.value) return
  const { data } = await api.get(`/papers/${selectedPaper.value.id}/blocks`, {
    params: { section: selectedSection.value || '' }
  })
  blocks.value = data.items || []
}

async function searchPaper() {
  if (!selectedPaper.value || !searchQuery.value.trim()) return
  const { data } = await api.get(`/papers/${selectedPaper.value.id}/search`, {
    params: { query: searchQuery.value }
  })
  searchResults.value = data.items || []
}

function chooseSection(section: string) {
  selectedSection.value = section
  loadBlocks()
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] || null
}

async function uploadPaper() {
  if (!selectedFile.value) return
  uploading.value = true
  try {
    const form = new FormData()
    form.append('file', selectedFile.value)
    form.append('source_url', sourceUrl.value)
    await api.post('/papers/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000
    })
    selectedFile.value = null
    sourceUrl.value = ''
    await refresh()
  } finally {
    uploading.value = false
  }
}

async function indexLocalPaper() {
  if (!selectedLocalPdf.value) return
  indexing.value = true
  try {
    await api.post('/papers/index-local', {
      local_path: selectedLocalPdf.value,
      source_url: sourceUrl.value
    })
    await refresh()
  } finally {
    indexing.value = false
  }
}

async function deletePaper(paper: Paper) {
  const confirmed = window.confirm(`确定从论文库删除「${paper.title}」吗？这不会删除原始 PDF 文件。`)
  if (!confirmed) return
  await api.delete(`/papers/${paper.id}`)
  if (selectedPaper.value?.id === paper.id) {
    selectedPaper.value = null
    sections.value = []
    blocks.value = []
    searchResults.value = []
  }
  await refresh()
}

function openLocalPdf(path: string) {
  window.alert(path)
}

function formatSize(size: number) {
  if (size > 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024).toFixed(1)} KB`
}

onMounted(refresh)
</script>
