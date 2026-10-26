import { useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link2, RefreshCw, Loader2, FileText } from 'lucide-react'
import { fetchTree, fetchNode, type TreeNode } from '../lib/api'
import { buildWikiIndex, resolveWikilink } from '../lib/wikilink'

interface Props {
  activePath: string
  onNavigate: (path: string) => void
}

interface BacklinkRef {
  sourcePath: string
  snippet: string
}

function stemOf(path: string): string {
  return (path.split('/').pop() ?? path).replace(/\.md$/i, '')
}

function dirOf(path: string): string {
  const i = path.lastIndexOf('/')
  return i === -1 ? '' : path.slice(0, i)
}

/** Todas las rutas .md del árbol. */
function collectMdPaths(tree: TreeNode | undefined): string[] {
  const out: string[] = []
  const walk = (n: TreeNode) => {
    if (n.type === 'file' && n.path.endsWith('.md')) out.push(n.path)
    n.children?.forEach(walk)
  }
  if (tree) walk(tree)
  return out
}

/** Targets crudos de wikilink de una línea (sin #sección ni |alias). */
function rawTargets(line: string): string[] {
  const out: string[] = []
  const re = /\[\[([^\]]+?)\]\]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(line)) !== null) {
    const t = m[1].split('#')[0].split('|')[0].trim()
    if (t) out.push(t)
  }
  return out
}

async function pMap<T>(items: T[], limit: number, fn: (t: T) => Promise<void>): Promise<void> {
  let idx = 0
  const workers = new Array(Math.min(limit, items.length)).fill(0).map(async () => {
    for (;;) {
      const i = idx++
      if (i >= items.length) return
      await fn(items[i])
    }
  })
  await Promise.all(workers)
}

/**
 * Índice global de backlinks construido client-side recorriendo todas las notas
 * una vez (reusa el índice de wikilinks para resolver por stem, igual que el
 * render de la nota → captura enlaces con o sin sufijo `.md`). Resultado
 * cacheado y sólo reconstruido cuando cambia el árbol o vía refresh manual.
 */
type BacklinkIndex = Map<string, BacklinkRef[]>

export default function BacklinksPanel({ activePath, onNavigate }: Props) {
  const qc = useQueryClient()

  const { data: tree } = useQuery({ queryKey: ['tree'], queryFn: fetchTree })
  const paths = useMemo(() => collectMdPaths(tree), [tree])
  const wikiIndex = useMemo(() => buildWikiIndex(tree), [tree])

  // Firma barata del árbol: si cambia (alta/baja/rename), se reconstruye.
  const sig = useMemo(() => {
    let h = paths.length >>> 0
    for (const p of paths) {
      for (let i = 0; i < p.length; i++) h = (Math.imul(h, 31) + p.charCodeAt(i)) >>> 0
    }
    return h
  }, [paths])

  const {
    data: index,
    isLoading,
    isFetching,
    isError,
  } = useQuery<BacklinkIndex>({
    queryKey: ['backlinkIndex', sig],
    enabled: paths.length > 0,
    staleTime: Infinity,
    gcTime: 30 * 60_000,
    queryFn: async () => {
      const map: BacklinkIndex = new Map()
      await pMap(paths, 8, async (sp) => {
        let content: string
        try {
          content = (await fetchNode(sp)).content
        } catch {
          return
        }
        if (!content.includes('[[')) return
        const lines = content.split(/\r?\n/)
        const seen = new Set<string>()
        for (const line of lines) {
          if (!line.includes('[[')) continue
          for (const t of rawTargets(line)) {
            const resolved = resolveWikilink(t, wikiIndex)
            if (!resolved || resolved === sp || seen.has(resolved)) continue
            seen.add(resolved)
            const arr = map.get(resolved) ?? []
            arr.push({
              sourcePath: sp,
              snippet: line.trim().replace(/^[#>\-*\s]+/, '').slice(0, 160),
            })
            map.set(resolved, arr)
          }
        }
      })
      return map
    },
  })

  const backlinks = useMemo<BacklinkRef[]>(() => {
    const list = index?.get(activePath) ?? []
    return [...list].sort((a, b) => a.sourcePath.localeCompare(b.sourcePath))
  }, [index, activePath])

  function onRefresh() {
    qc.invalidateQueries({ queryKey: ['tree'] })
    qc.invalidateQueries({ queryKey: ['backlinkIndex'] })
  }

  if (isLoading) {
    return (
      <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
        <Loader2 size={18} className="animate-spin text-fg-subtle" />
        <p className="text-[13px] text-fg-subtle">Indexando enlaces…</p>
        <p className="text-[11px] text-fg-faint">Se hace una vez y queda en caché.</p>
      </div>
    )
  }

  if (isError) return <Empty text="No se pudo construir el índice de enlaces." />

  return (
    <div>
      <div className="mb-2 flex items-center justify-between px-1">
        <span className="text-[11px] font-medium uppercase tracking-wide text-fg-subtle">
          Backlinks {backlinks.length > 0 && `(${backlinks.length})`}
        </span>
        <button
          onClick={onRefresh}
          title="Reconstruir índice de enlaces"
          className="flex h-6 w-6 items-center justify-center rounded-md text-fg-faint transition hover:bg-surface-hover hover:text-fg"
        >
          <RefreshCw size={13} className={isFetching ? 'animate-spin' : ''} />
        </button>
      </div>

      {backlinks.length === 0 ? (
        <Empty text="Ninguna nota enlaza aquí todavía." />
      ) : (
        <ul className="space-y-1">
          {backlinks.map((b) => (
            <li key={b.sourcePath}>
              <button
                onClick={() => onNavigate(b.sourcePath)}
                className="group flex w-full flex-col gap-0.5 rounded-md border border-transparent px-2 py-1.5 text-left transition hover:border-[var(--border-subtle)] hover:bg-surface-hover"
              >
                <span className="flex items-center gap-1.5 text-[13px] text-fg-muted group-hover:text-fg">
                  <Link2 size={13} className="shrink-0 text-accent" />
                  <span className="truncate">{stemOf(b.sourcePath)}</span>
                </span>
                {b.snippet ? (
                  <span className="line-clamp-2 pl-[19px] text-[11px] leading-snug text-fg-subtle">
                    {b.snippet}
                  </span>
                ) : (
                  <span className="truncate pl-[19px] text-[11px] text-fg-faint">
                    {dirOf(b.sourcePath) || '/'}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-center gap-3 px-4 py-10 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface">
        <FileText size={20} className="text-fg-faint" />
      </div>
      <p className="max-w-[220px] text-[13px] text-fg-subtle">{text}</p>
    </div>
  )
}
