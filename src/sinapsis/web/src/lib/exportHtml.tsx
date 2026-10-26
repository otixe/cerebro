import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown'
import { renderToStaticMarkup } from 'react-dom/server'
import remarkGfm from 'remark-gfm'
import { fetchImageBytes } from './api'
import { parseFrontmatter } from './frontmatter'
import { remarkWikilink, resolveWikilink } from './wikilink'
import { verifyRaster } from './rasterMime'

const css = `
:root{color-scheme:light}body{margin:2rem auto;padding:0 1.2rem;max-width:820px;color:#20232a;background:#fff;font:16px/1.65 system-ui,-apple-system,sans-serif}
h1,h2,h3,h4{line-height:1.25;margin:1.5em 0 .5em}p,ul,ol,blockquote,pre,table{margin:1em 0}a{color:#2358a8}blockquote{border-left:3px solid #aaa;padding-left:1em;color:#555}img{max-width:100%;height:auto}pre,code{font-family:ui-monospace,SFMono-Regular,monospace}pre{background:#f2f4f7;padding:1em;overflow:auto}code{background:#f2f4f7;padding:.12em .3em}pre code{padding:0}table{border-collapse:collapse;display:block;overflow-x:auto}th,td{border:1px solid #c9cdd4;padding:.35em .7em;text-align:left}tr:nth-child(even){background:#f7f8fa}hr{border:0;border-top:1px solid #bbb}
@media print{body{margin:0;max-width:none}pre,blockquote,img{break-inside:avoid}}
`

function escapeText(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/** A relative vault image only. Never fetch remote/absolute URLs, including protocol-relative. */
export function imagePath(notePath: string, src: string): string | null {
  if (!src || src.startsWith('/') || src.startsWith('\\') || /[\\\u0000-\u001f]/.test(src) || /^[a-z][a-z\d+.-]*:/i.test(src)) return null
  let decoded: string
  try { decoded = decodeURIComponent(src.split(/[?#]/, 1)[0]) } catch { return null }
  if (!decoded || decoded.startsWith('/') || decoded.startsWith('\\') || /[\\\u0000-\u001f]/.test(decoded)) return null
  const parts = notePath.split('/').slice(0, -1)
  for (const part of decoded.split('/')) {
    if (part === '..') { if (!parts.length) return null; parts.pop() }
    else if (part && part !== '.') parts.push(part)
  }
  return parts.join('/')
}

function blobDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

/** Export only if all referenced local images embed successfully; no partial/broken download. */
export async function exportNoteHtml(path: string, content: string, wikiIndex = new Map<string, string>()): Promise<Blob> {
  const images: string[] = []
  const components: Components = {
    img({ src, alt }) {
      const p = imagePath(path, src ?? '')
      if (!p) return <span>[Imagen externa o inválida: {alt || src}]</span>
      const index = images.push(p) - 1
      return <img data-export-image={index} alt={alt ?? ''} />
    },
    a({ href, children }) {
      // A downloaded file cannot navigate the app; never emit href="#", network links,
      // custom protocols or relative references which would break outside the vault.
      const label = href?.startsWith('wikilink:')
        ? resolveWikilink(href.slice('wikilink:'.length), wikiIndex) ?? 'Nota no encontrada'
        : href
      return <span title={label}>{children}</span>
    },
  }
  const markup = renderToStaticMarkup(
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkWikilink]}
      urlTransform={url => url.startsWith('wikilink:') ? url : defaultUrlTransform(url)}
      components={components}>
      {parseFrontmatter(content).body}
    </ReactMarkdown>
  )
  // URLs only come from a local path derived from a note image, and pass through the
  // authenticated backend's resolved-path/scope/MIME checks. No browser request is
  // initiated from Markdown itself (img markup has no src until replaced with data:).
  const embedded = await Promise.all(images.map(async p => {
    const blob = await fetchImageBytes(p)
    const bytes = new Uint8Array(await blob.arrayBuffer())
    const mime = await verifyRaster(new Blob([bytes], { type: blob.type }))
    if (!mime) throw new Error(`No se puede exportar: la imagen ${p} no es raster PNG, JPEG, GIF o WebP válido`)
    return blobDataUrl(new Blob([bytes], { type: mime }))
  }))
  const body = markup.replace(/<img data-export-image="(\d+)"/g, (_match, n: string) =>
    `<img src="${embedded[Number(n)]}"`)
  const title = escapeText(path.split('/').pop()?.replace(/\.md$/i, '') ?? 'Nota')
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>${title}</title><style>${css}</style></head><body><main>${body}</main></body></html>`
  return new Blob([html], { type: 'text/html;charset=utf-8' })
}
