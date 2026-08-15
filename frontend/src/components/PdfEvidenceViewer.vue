<template>
  <section class="evidence-viewer">
    <header>
      <div>
        <strong>PDF Evidence</strong>
        <span v-if="evidence">第 {{ evidence.page }} 页 · {{ evidence.evidence_kind }}</span>
      </div>
      <a v-if="evidence?.pdf_url" :href="evidence.pdf_url" target="_blank" rel="noreferrer">打开原 PDF</a>
    </header>

    <div v-if="loading" class="viewer-empty">正在渲染证据页…</div>
    <div v-else-if="error" class="viewer-empty error">{{ error }}</div>
    <div v-else-if="!evidence" class="viewer-empty">点击一条 claim 查看原文位置。</div>
    <div v-show="evidence && !loading && !error" class="pdf-scroll">
      <div ref="pageWrap" class="pdf-page">
        <canvas ref="canvas"></canvas>
        <div
          v-if="hasBox"
          class="bbox-highlight"
          :style="bboxStyle"
          :title="evidence?.text || 'Evidence'"
        ></div>
      </div>
    </div>

    <blockquote v-if="evidence?.text" class="evidence-quote">{{ evidence.text }}</blockquote>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist'
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = pdfWorker

export type EvidenceDetail = {
  element_id: string
  evidence_kind: string
  source_title: string
  text: string
  page: number
  bbox_normalized?: { x: number; y: number; width: number; height: number }
  pdf_url: string
  table_id?: string
  row_index?: number
  column_index?: number
}

const props = defineProps<{ evidence: EvidenceDetail | null }>()
const canvas = ref<HTMLCanvasElement | null>(null)
const pageWrap = ref<HTMLElement | null>(null)
const loading = ref(false)
const error = ref('')
let documentRef: PDFDocumentProxy | null = null
let loadedUrl = ''
let renderVersion = 0

const hasBox = computed(() => {
  const box = props.evidence?.bbox_normalized
  return Boolean(box && box.width > 0 && box.height > 0)
})

const bboxStyle = computed(() => {
  const box = props.evidence?.bbox_normalized || { x: 0, y: 0, width: 0, height: 0 }
  return {
    left: `${box.x * 100}%`,
    top: `${box.y * 100}%`,
    width: `${box.width * 100}%`,
    height: `${box.height * 100}%`
  }
})

async function renderEvidence() {
  const evidence = props.evidence
  const version = ++renderVersion
  error.value = ''
  if (!evidence?.pdf_url) return
  loading.value = true
  try {
    if (!documentRef || loadedUrl !== evidence.pdf_url) {
      documentRef = await getDocument(evidence.pdf_url).promise
      loadedUrl = evidence.pdf_url
    }
    const pageNumber = Math.max(1, Math.min(evidence.page || 1, documentRef.numPages))
    const page = await documentRef.getPage(pageNumber)
    if (version !== renderVersion) return
    await nextTick()
    if (!canvas.value || !pageWrap.value) return
    const baseViewport = page.getViewport({ scale: 1 })
    const available = Math.max(320, pageWrap.value.parentElement?.clientWidth || 620)
    const scale = Math.min(1.7, Math.max(0.65, (available - 24) / baseViewport.width))
    const viewport = page.getViewport({ scale })
    const ratio = window.devicePixelRatio || 1
    const context = canvas.value.getContext('2d')
    if (!context) throw new Error('浏览器无法创建 PDF canvas。')
    canvas.value.width = Math.floor(viewport.width * ratio)
    canvas.value.height = Math.floor(viewport.height * ratio)
    canvas.value.style.width = `${viewport.width}px`
    canvas.value.style.height = `${viewport.height}px`
    pageWrap.value.style.width = `${viewport.width}px`
    pageWrap.value.style.height = `${viewport.height}px`
    await page.render({ canvasContext: context, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] }).promise
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'PDF 证据页渲染失败。'
  } finally {
    if (version === renderVersion) loading.value = false
  }
}

watch(() => [props.evidence?.pdf_url, props.evidence?.page], renderEvidence, { immediate: true })
</script>

<style scoped>
.evidence-viewer { display: grid; gap: 12px; min-height: 0; }
.evidence-viewer > header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.evidence-viewer header div { display: grid; gap: 3px; }
.evidence-viewer header strong { color: #f4f1e8; font-size: 14px; }
.evidence-viewer header span, .evidence-viewer header a { color: #8ea69e; font-size: 12px; }
.pdf-scroll { overflow: auto; max-height: 62vh; padding: 10px; border: 1px solid rgba(195,214,202,.13); background: #090a08; border-radius: 12px; }
.pdf-page { position: relative; margin: 0 auto; background: white; box-shadow: 0 14px 40px rgba(0,0,0,.32); }
.pdf-page canvas { display: block; }
.bbox-highlight { position: absolute; box-sizing: border-box; border: 2px solid #f2b84b; background: rgba(242,184,75,.25); box-shadow: 0 0 0 2px rgba(20,18,13,.2); pointer-events: none; animation: evidence-pulse 1.4s ease-out 1; }
.viewer-empty { min-height: 280px; display: grid; place-items: center; color: #7f8d86; border: 1px dashed rgba(195,214,202,.16); border-radius: 12px; }
.viewer-empty.error { color: #fda4af; }
.evidence-quote { margin: 0; padding: 12px 14px; max-height: 150px; overflow: auto; border-left: 3px solid #9bb8ad; background: rgba(155,184,173,.07); color: #c9d2ce; font-size: 12px; line-height: 1.65; }
@keyframes evidence-pulse { from { background: rgba(242,184,75,.62); } to { background: rgba(242,184,75,.25); } }
</style>
