import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { remarkWikilink, resolveWikilink } from '../lib/wikilink'
import { fetchImageBytes, fetchNode } from '../lib/api'
import { parseFrontmatter } from '../lib/frontmatter'

interface Props {
  content: string
  notePath: string
  wikiIndex: Map<string, string>
  onNavigate: (path: string) => void
}

const WIKILINK_PREFIX = 'wikilink:'
const HOVER_DELAY = 400

interface PreviewState {
  path: string
  x: number
  y: number
  text: string | null
  loading: boolean
}

// Cache simple de previews (evita refetch al pasar varias veces).
const previewCache = new Map<string, string>()

// Only local, vault-relative raster images may reach the authenticated endpoint.
// Decode once before normalizing to reject encoded absolute paths and traversal.
export function localImagePath(notePath: string, src: string): string | null {
  if (!src || !notePath || /[\\\u0000-\u001f]/.test(src)) return null
  let decoded: string
  try { decoded = decodeURIComponent(src.split(/[?#]/, 1)[0]) } catch { return null }
  if (!decoded || decoded.startsWith('/') || /[\\\u0000-\u001f]/.test(decoded) || /^[a-z][a-z\d+.-]*:/i.test(decoded)) return null
  const parts = notePath.split('/').slice(0, -1)
  for (const part of decoded.split('/')) {
    if (part === '..') { if (!parts.length) return null; parts.pop() }
    else if (part && part !== '.') parts.push(part)
  }
  const path = parts.join('/')
  return /\.(png|jpe?g|gif|webp)$/i.test(path) ? path : null
}

const rasterTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

function LocalImage({ src, alt, notePath }: { src?: string; alt?: string; notePath: string }) {
  const path = localImagePath(notePath, src ?? '')
  const [image, setImage] = useState<{ path: string; url: string } | null>(null)
  useEffect(() => {
    if (!path) return
    let active = true
    let objectUrl: string | null = null
    fetchImageBytes(path).then(blob => {
      if (!active || !rasterTypes.has(blob.type.toLowerCase())) return
      objectUrl = URL.createObjectURL(blob)
      setImage({ path, url: objectUrl })
    }).catch(() => {
      // No network fallback: absent/denied images remain inert text.
    })
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [path])
  // A path change must not display the previous note's image while the effect runs.
  return image?.path === path && path
    ? <img src={image.url} alt={alt ?? ''} />
    : <span>[Imagen no disponible: {alt || src}]</span>
}

function excerpt(raw: string): string {
  const body = parseFrontmatter(raw).body
  const lines = body.split(/\r?\n/).filter((l) => l.trim() !== '')
  return lines.slice(0, 8).join('\n').slice(0, 600)
}

export default function MarkdownView({ content, notePath, wikiIndex, onNavigate }: Props) {
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  function clearTimer() {
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
  }

  function onEnter(path: string, e: React.MouseEvent) {
    clearTimer()
    const x = e.clientX
    const y = e.clientY
    timer.current = setTimeout(async () => {
      const cached = previewCache.get(path)
      if (cached !== undefined) {
        setPreview({ path, x, y, text: cached, loading: false })
        return
      }
      setPreview({ path, x, y, text: null, loading: true })
      try {
        const node = await fetchNode(path)
        const text = excerpt(node.content)
        previewCache.set(path, text)
        setPreview((p) => (p && p.path === path ? { ...p, text, loading: false } : p))
      } catch {
        // Degradar silenciosamente: ocultar el tooltip si falla.
        setPreview((p) => (p && p.path === path ? null : p))
      }
    }, HOVER_DELAY)
  }

  function onLeave() {
    clearTimer()
    setPreview(null)
  }

  // Keep the img renderer stable even when navigation callbacks change on parent renders.
  const renderImage = useMemo<NonNullable<Components['img']>>(() =>
    ({ src, alt }) => <LocalImage src={src} alt={alt} notePath={notePath} />,
  [notePath])
  const components: Components = {
    img: renderImage,
    a({ href, children, node: _node, ...rest }) {
      if (href && href.startsWith(WIKILINK_PREFIX)) {
        const target = href.slice(WIKILINK_PREFIX.length)
        const resolved = resolveWikilink(target, wikiIndex)
        if (resolved) {
          return (
            <a
              href="#"
              className="wikilink"
              onClick={(e) => {
                e.preventDefault()
                onNavigate(resolved)
              }}
              onMouseEnter={(e) => onEnter(resolved, e)}
              onMouseLeave={onLeave}
            >
              {children}
            </a>
          )
        }
        return (
          <span className="wikilink wikilink-broken" title="Nota no encontrada">
            {children}
          </span>
        )
      }
      return (
        <a href={href} target="_blank" rel="noreferrer noopener" {...rest}>
          {children}
        </a>
      )
    },
  }

  return (
    <div className="prose-note">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkWikilink]} components={components}>
        {content}
      </ReactMarkdown>

      {preview && (
        <div
          className="palette-surface pointer-events-none fixed z-[70] w-72 p-3"
          style={{
            left: Math.min(preview.x + 14, window.innerWidth - 300),
            top: Math.min(preview.y + 16, window.innerHeight - 180),
          }}
        >
          <div className="mb-1 truncate text-[11px] font-medium text-fg-subtle">
            {(preview.path.split('/').pop() ?? '').replace(/\.md$/, '')}
          </div>
          {preview.loading ? (
            <div className="text-[12px] text-fg-faint">Cargando…</div>
          ) : (
            <pre className="max-h-40 overflow-hidden whitespace-pre-wrap break-words font-sans text-[12px] leading-snug text-fg-muted">
              {preview.text}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
