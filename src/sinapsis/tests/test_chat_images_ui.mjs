import assert from 'node:assert/strict'
import { test } from 'node:test'
import { validateImageFiles, MAX_IMAGE_B64, shouldClearSentDraft, shouldInterceptImagePaste } from '../web/src/lib/chatImages.ts'
import { readFileSync } from 'node:fs'
const view = readFileSync(new URL('../web/src/components/ChatView.tsx', import.meta.url), 'utf8')

const cap = { supported: true, max_b64: MAX_IMAGE_B64, mimes: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] }
const file = (size, type = 'image/png') => ({size, type})
test('file count limit and base64 encoded size limit', () => {
  assert.match(validateImageFiles(Array(9).fill(file(1)), cap), /8 imágenes/)
  assert.match(validateImageFiles([file(Math.floor(MAX_IMAGE_B64 * 3 / 4) + 1)], cap), /8 MB/)
  assert.equal(validateImageFiles([file(3)], cap), null)
})
test('pending send snapshots agent and draft; later edits or switches survive completion', () => {
  const sent = {agent: 'assistant-g', draft: 'before', files: [file(1)]}
  assert.equal(shouldClearSentDraft(sent, {agent: 'assistant-g', draft: 'before', files: sent.files}), true)
  assert.equal(shouldClearSentDraft(sent, {agent: 'other', draft: 'before', files: sent.files}), false)
  assert.equal(shouldClearSentDraft(sent, {agent: 'assistant-g', draft: 'after', files: sent.files}), false)
  assert.equal(shouldClearSentDraft(sent, {agent: 'assistant-g', draft: 'before', files: [file(1)]}), false)
  assert.match(view, /sendChatMessage\(agent, text/)
  assert.match(view, /fetchChatCapabilities\(agent\)/)
  assert.match(view, /shouldClearSentDraft\(/)
})
test('unsupported paste preserves native text; supported image paste intercepted', () => {
  assert.equal(shouldInterceptImagePaste(undefined, [file(1)]), false)
  assert.equal(shouldInterceptImagePaste({...cap, supported:false}, [file(1)]), false)
  assert.equal(shouldInterceptImagePaste(cap, []), false)
  assert.equal(shouldInterceptImagePaste(cap, [file(1)]), true)
  assert.match(view, /if \(shouldInterceptImagePaste\(imageCap, files\)\) \{ e\.preventDefault\(\)/)
})
test('unsupported images blocked; text-only needs no capability', () => {
  assert.match(validateImageFiles([file(1)], undefined), /No se comprobó/)
  assert.match(validateImageFiles([file(1)], {...cap, supported:false, reason:'kiro-gateway descarta imágenes'}), /kiro-gateway/)
  assert.match(validateImageFiles([file(1, 'image/svg+xml')], cap), /Sólo PNG/)
})
