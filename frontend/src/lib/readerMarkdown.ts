import MarkdownIt from 'markdown-it'
import katex from 'katex'

// HTML from papers stays inert. Math may not load resources or execute HTML.
const md = new MarkdownIt({ html: false, linkify: false, breaks: false })
function mathHtml(source: string, display: boolean) {
  return katex.renderToString(source, {
    displayMode: display, throwOnError: false, trust: false,
    strict: 'ignore', maxExpand: 1000, maxSize: 20
  })
}

md.inline.ruler.before('escape', 'reader_math', (state, silent) => {
  const start = state.pos
  const bracket = state.src.startsWith('\\(', start)
  if (!bracket && state.src[start] !== '$') return false
  const open = bracket ? '\\(' : '$'
  const close = bracket ? '\\)' : '$'
  // Display math is handled by the block rule. Avoid interpreting prices as math.
  if (!bracket && (state.src[start + 1] === '$' || /\s/.test(state.src[start + 1] || ' '))) return false
  let end = state.src.indexOf(close, start + open.length)
  while (end > 0 && state.src[end - 1] === '\\') end = state.src.indexOf(close, end + close.length)
  if (end < 0 || state.src.slice(start, end).includes('\n')) return false
  const content = state.src.slice(start + open.length, end)
  if (!content.trim() || (!bracket && (/\s$/.test(content) || /\d/.test(state.src[end + 1] || '')))) return false
  if (!silent) {
    const token = state.push('reader_math', 'math', 0)
    token.content = content
  }
  state.pos = end + close.length
  return true
})
md.renderer.rules.reader_math = (tokens, index) => mathHtml(tokens[index].content, false)

md.block.ruler.before('fence', 'reader_math_block', (state, start, end, silent) => {
  const first = state.src.slice(state.bMarks[start] + state.tShift[start], state.eMarks[start]).trim()
  const open = first.startsWith('$$') ? '$$' : first.startsWith('\\[') ? '\\[' : ''
  if (!open) return false
  const close = open === '$$' ? '$$' : '\\]'
  let line = start
  let content = first.slice(open.length)
  while (!content.trimEnd().endsWith(close) && ++line < end) {
    content += '\n' + state.src.slice(state.bMarks[line] + state.tShift[line], state.eMarks[line])
  }
  if (line >= end) return false // Keep malformed source visible.
  if (silent) return true
  const token = state.push('reader_math_block', 'math', 0)
  token.content = content.trimEnd().slice(0, -close.length).trim()
  token.map = [start, line + 1]
  state.line = line + 1
  return true
}, { alt: ['paragraph'] })
md.renderer.rules.reader_math_block = (tokens, index) => mathHtml(tokens[index].content, true)

export function readerMarkdown(source: string): string { return md.render(source) }
export function readerInline(source: string): string { return md.renderInline(source) }
