import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
  timeout: 20000
})

export interface ReadingItem {
  id: string
  title: string
  url: string
  summary: string
  source_type: string
  source_level: string
  status: string
  score: number
  reasons: string[]
  note_summary: string
  takeaways: string[]
  open_questions: string[]
  interview_points: string[]
  deep_read_worthy: boolean
  metadata: Record<string, unknown>
}

export interface WikiCard {
  id: string
  title: string
  page_type: string
  markdown_path: string
  obsidian_uri?: string
  summary: string
  content_json: Record<string, unknown>
  source_level: string
  related_topics: string[]
  source_urls: string[]
}

export interface LocalPdf {
  name: string
  path: string
  size: number
}

export interface Paper {
  id: string
  title: string
  source_url: string
  pdf_path: string
  status: string
  summary: string
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface PaperSection {
  section: string
  start_page: number
  blocks: number
}

export interface PaperBlock {
  id: string
  paper_id: string
  page: number
  section: string
  block_type: string
  text: string
  metadata: Record<string, unknown>
}
