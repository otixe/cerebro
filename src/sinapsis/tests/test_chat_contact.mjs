import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { chromium } from '../web/node_modules/playwright-core/index.mjs'

const server = spawn('npm', ['run', 'dev', '--', '--host', '127.0.0.1', '--port', '18731', '--strictPort'], {cwd: new URL('../web/', import.meta.url).pathname, stdio: 'ignore', detached: true})
let browser
try {
  let ready = false
  for (let i = 0; i < 100; i++) {
    try { const r = await fetch('http://127.0.0.1:18731/test-fixtures/chat-contact.html'); if (r.ok) { ready = true; break } } catch {}
    await new Promise(r => setTimeout(r, 100))
  }
  assert.ok(ready, 'fixture Vite online')
  browser = await chromium.launch({executablePath:'/usr/bin/chromium', headless:true, args:['--no-sandbox']})
  for (const mobile of [false, true]) for (const image of [false, true]) {
    const page = await browser.newPage({viewport:{width:mobile ? 390 : 1280, height:800}})
    const errors = []
    page.on('pageerror', e => errors.push(e.message))
    await page.goto('http://127.0.0.1:18731/test-fixtures/chat-contact.html')
    const textarea = page.getByRole('textbox', {name:'Mensaje para agent-a'})
    await textarea.waitFor()
    await textarea.fill('confidencial-A')
    if (image) {
      await page.getByLabel('Elegir imágenes').setInputFiles({name:'a.png', mimeType:'image/png', buffer:Buffer.from('image-a')})
      await page.getByText('a.png (').waitFor()
    }
    await page.getByRole('button', {name:'Enviar mensaje'}).click()
    await page.waitForFunction(() => window.fixture.requests.length === 1)
    const sent = await page.evaluate(() => window.fixture.requests[0])
    assert.equal(sent.agent, 'agent-a')
    assert.equal(sent.message, 'confidencial-A')
    assert.equal(!!sent.images?.length, image)
    if (mobile) {
      await page.getByRole('button', {name:'elegir agente'}).click()
      await page.locator('[data-agent-picker="1"]').getByRole('button', {name:/agent-b/}).click()
    } else {
      await page.locator('aside').getByRole('button', {name:/agent-b/}).click()
    }
    const b = page.getByRole('textbox', {name:'Mensaje para agent-b'})
    await b.waitFor()
    assert.equal(await b.inputValue(), '', `A leaked to B mobile=${mobile} image=${image}`)
    assert.equal(await page.getByText('a.png (').count(), 0)
    await b.fill('borrador-B')
    await page.evaluate(() => window.fixture.resolve())
    await page.waitForFunction(() => !document.querySelector('textarea')?.disabled)
    assert.equal(await b.inputValue(), 'borrador-B', 'completion of A must not clear B')
    if (mobile) {
      await page.getByRole('button', {name:'elegir agente'}).click()
      await page.locator('[data-agent-picker="1"]').getByRole('button', {name:/agent-a/}).click()
    } else {
      await page.locator('aside').getByRole('button', {name:/agent-a/}).click()
    }
    assert.equal(await page.getByRole('textbox', {name:'Mensaje para agent-a'}).inputValue(), '', 'A must stay discarded')
    assert.equal(await page.getByText('a.png (').count(), 0)
    assert.equal(await page.getByRole('button', {name:'Enviar mensaje'}).isDisabled(), true, 'A must not re-send')
    assert.equal(await page.evaluate(() => window.fixture.requests.length), 1, 'no duplicate request')
    assert.deepEqual(errors, [], 'browser runtime errors')
    console.log(`PASS interaction ${mobile ? 'mobile' : 'desktop'} ${image ? 'image' : 'text'}`)
    await page.close()
  }
} finally {
  await browser?.close()
  try { process.kill(-server.pid, 'SIGTERM') } catch {}
}
