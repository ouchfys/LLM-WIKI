import assert from 'node:assert/strict'
import test from 'node:test'
import { readerMarkdown, readerInline } from '../src/lib/readerMarkdown.ts'

test('headings, lists, emphasis and equations are rendered as reading content', () => {
  const html = readerMarkdown('### 核心机制\n\n- **预算**：$B_k=\\max(0,W-S-G-R)$\n\n$$\nd(v)=\\sum_{u} r_u\n$$')
  assert.match(html, /<h3>核心机制<\/h3>/)
  assert.match(html, /<strong>预算<\/strong>/)
  assert.match(html, /class="katex"/)
  assert.match(html, /katex-display/)
  assert.doesNotMatch(html, /<button|keyword-link/)
})

test('math cannot enable HTML, external links or script execution', () => {
  const html = readerMarkdown('<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))\n\n$\\href{javascript:alert(1)}{x}$')
  assert.doesNotMatch(html, /<img|href="javascript:|<script/)
  assert.match(html, /&lt;img/)
})

test('code, prices and broken delimiters stay visible', () => {
  assert.doesNotMatch(readerMarkdown('`$x$`'), /class="katex"/)
  assert.doesNotMatch(readerMarkdown('cost $20 and $30'), /class="katex"/)
  assert.match(readerMarkdown('unfinished $x'), /unfinished \$x/)
  assert.match(readerInline('\\(x_i^2\\)'), /class="katex"/)
  assert.match(readerMarkdown('\\[\nx^2\n\\]'), /katex-display/)
})
