import { zipSync, strToU8 } from 'fflate'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import { fetchImageBytes } from './api'
import { imagePath } from './exportHtml'

interface MarkdownNode {
  type: string
  url?: string
  identifier?: string
  children?: MarkdownNode[]
}

function safeEntry(path: string): boolean {
  return !!path && !path.startsWith('/') && !path.includes('\\') &&
    !/[\u0000-\u001f\u007f]/.test(path) &&
    path.split('/').every(p => p !== '' && p !== '.' && p !== '..')
}

/** A note and its vault-relative images retain their original locations in the ZIP.
 *  Extract at the archive root: relative links (including ../) resolve unchanged.
 *  Never return a partial archive when an image cannot be fetched.
 */
export async function exportMarkdownZip(path: string, content: string, lossy = false,
  fetchImage: (path: string) => Promise<Blob> = fetchImageBytes): Promise<Blob> {
  if (lossy) throw new Error('La nota contiene bytes no UTF-8 (lossy): no se puede descargar Markdown fiel.')
  if (!safeEntry(path) || !/\.md$/i.test(path)) throw new Error('Ruta de nota no válida para ZIP.')

  const root = unified().use(remarkParse).use(remarkGfm).parse(content) as MarkdownNode
  const definitions = new Map<string, string>()
  const walk = (node: MarkdownNode, fn: (node: MarkdownNode) => void) => {
    fn(node)
    node.children?.forEach(child => walk(child, fn))
  }
  walk(root, node => {
    if (node.type === 'definition' && node.identifier && node.url !== undefined)
      definitions.set(node.identifier.toLowerCase(), node.url)
  })

  const paths = new Set<string>()
  walk(root, node => {
    if (node.type !== 'image' && node.type !== 'imageReference') return
    const src = node.type === 'image' ? node.url : definitions.get(node.identifier?.toLowerCase() ?? '')
    if (src === undefined) throw new Error(`Referencia de imagen sin definición: ${node.identifier ?? ''}`)
    // Remote images remain in the original Markdown, but are never fetched or packaged.
    // Invalid local paths must still fail rather than produce a broken archive.
    if (/^[a-z][a-z\d+.-]*:/i.test(src) || src.startsWith('//')) return
    const resolved = imagePath(path, src)
    if (!resolved || !safeEntry(resolved)) throw new Error(`Ruta de imagen inválida: ${src}`)
    if (/\.svg$/i.test(resolved)) throw new Error(`SVG no soportado en descarga ZIP: ${src}`)
    if (!/\.(?:png|jpe?g|gif|webp)$/i.test(resolved)) throw new Error(`Formato de imagen no soportado: ${src}`)
    paths.add(resolved)
  })

  const entries: Record<string, Uint8Array> = { [path]: strToU8(content) }
  // All authenticated reads must finish before constructing the archive. Never
  // write a file to disk or trigger a download on a missing/denied image.
  for (const image of paths) {
    if (image === path) throw new Error(`Imagen coincide con nota: ${image}`)
    try {
      const blob = await fetchImage(image)
      entries[image] = new Uint8Array(await blob.arrayBuffer())
    } catch (e) {
      throw new Error(`No se pudo incluir la imagen ${image}: ${e instanceof Error ? e.message : String(e)}`)
    }
  }
  return new Blob([new Uint8Array(zipSync(entries))], { type: 'application/zip' })
}
