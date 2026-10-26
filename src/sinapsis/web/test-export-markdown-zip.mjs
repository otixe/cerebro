import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { createServer } from 'vite'
import { unzipSync } from 'fflate'

const server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
try {
  const { exportMarkdownZip } = await server.ssrLoadModule('/src/lib/exportMarkdownZip.ts')
  const content = `---\ntitle: Prueba\n---\n![x](../shared/pic.png) ![x](../shared/pic.png)\n![sub](assets/other.gif)\n![ref][p]\n\n[p]: ../shared/pic.png\n`
  const calls = []
  const image = async p => { calls.push(p); return new Blob([`image:${p}`]) }
  const blob = await exportMarkdownZip('notes/deep/nota.md', content, false, image)
  assert.equal(blob.type, 'application/zip')
  const files = unzipSync(new Uint8Array(await blob.arrayBuffer()))
  assert.deepEqual(Object.keys(files).sort(), ['notes/deep/assets/other.gif', 'notes/deep/nota.md', 'notes/shared/pic.png'])
  assert.deepEqual(calls, ['notes/shared/pic.png', 'notes/deep/assets/other.gif'])
  assert.equal(new TextDecoder().decode(files['notes/deep/nota.md']), content)
  const dir = await mkdtemp(join(tmpdir(), 'sinapsis-zip-test-'))
  const archive = join(dir, 'export.zip')
  const { writeFile } = await import('node:fs/promises')
  await writeFile(archive, Buffer.from(await blob.arrayBuffer()))
  execFileSync('unzip', ['-q', archive, '-d', dir])
  assert.equal(await readFile(join(dir, 'notes/deep/nota.md'), 'utf8'), content)
  assert.equal(await readFile(join(dir, 'notes/deep/../shared/pic.png'), 'utf8'), 'image:notes/shared/pic.png')
  assert.equal(await readFile(join(dir, 'notes/deep/assets/other.gif'), 'utf8'), 'image:notes/deep/assets/other.gif')
  const remoteOnly = '![externa](https://example.org/icon.png) ![cdn](//example.org/pic.gif)'
  const beforeRemote = calls.length
  const remoteZip = unzipSync(new Uint8Array(await (await exportMarkdownZip('notes/deep/nota.md', remoteOnly, false, image)).arrayBuffer()))
  assert.deepEqual(Object.keys(remoteZip), ['notes/deep/nota.md'])
  assert.equal(new TextDecoder().decode(remoteZip['notes/deep/nota.md']), remoteOnly)
  assert.equal(calls.length, beforeRemote, 'no remote request')
  const mixed = '![local](../shared/pic.png) ![externa](https://example.org/icon.png) ![ref][remote]\n\n[remote]: https://example.org/ref.gif\n'
  const beforeMixed = calls.length
  const mixedZip = unzipSync(new Uint8Array(await (await exportMarkdownZip('notes/deep/nota.md', mixed, false, image)).arrayBuffer()))
  assert.deepEqual(Object.keys(mixedZip).sort(), ['notes/deep/nota.md', 'notes/shared/pic.png'])
  assert.equal(new TextDecoder().decode(mixedZip['notes/deep/nota.md']), mixed)
  assert.deepEqual(calls.slice(beforeMixed), ['notes/shared/pic.png'], 'only local fetched')
  for (const text of ['![bad](../../../../etc/passwd.png)', '![svg](assets/x.svg)', '![absolute](/assets/x.png)']) {
    await assert.rejects(exportMarkdownZip('notes/deep/nota.md', text, false, image))
  }
  await assert.rejects(exportMarkdownZip('notes/deep/nota.md', content, true, image), /lossy/)
  await assert.rejects(exportMarkdownZip('notes/deep/nota.md', content, false, async () => { throw new Error('403 denied') }), /403 denied/)
  await assert.rejects(exportMarkdownZip('notes/deep/nota.md', content, false, async () => { throw new Error('404 missing') }), /404 missing/)
  assert.deepEqual(await readdir(dir), ['export.zip', 'notes'])
  console.log('PASS ZIP extraíble, UTF-8 exacto, rutas ../, dedup, externas ignoradas, SVG/traversal/lossy/403/404 bloqueados')
} finally { await server.close() }
