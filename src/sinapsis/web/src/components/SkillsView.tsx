import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BookOpen,
  Search,
  X,
  Tag,
  User,
  Layers,
  Loader2,
  Copy,
  Check,
  AlertCircle,
} from 'lucide-react'
import { fetchSkills, fetchSkillContent, type SkillItem } from '../lib/api'

const PAGE_SIZE = 50

/** Color estable (hash-based) para el chip de owner. Mismo espíritu que projectChip. */
const OWNER_CHIP_PALETTE: string[] = [
  'bg-violet-500/15 text-violet-300',
  'bg-emerald-500/15 text-emerald-300',
  'bg-sky-500/15 text-sky-300',
  'bg-pink-500/15 text-pink-300',
  'bg-amber-500/15 text-amber-300',
  'bg-teal-500/15 text-teal-300',
]
function ownerChip(owner?: string | null): string {
  const name = (owner || '').trim()
  if (!name) return 'bg-surface-hover text-fg-subtle'
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return OWNER_CHIP_PALETTE[h % OWNER_CHIP_PALETTE.length]
}

/** Fecha ISO → 'YYYY-MM-DD' (o '' si no hay). */
function shortDate(iso?: string | null): string {
  if (!iso) return ''
  return iso.slice(0, 10)
}

/** Estado del botón de copiar de una card: idle → copying → copied|error → idle. */
type CopyState = 'idle' | 'copying' | 'copied' | 'error'

function SkillCard({ skill }: { skill: SkillItem }) {
  const [open, setOpen] = useState(false)
  const [copyState, setCopyState] = useState<CopyState>('idle')
  const updated = shortDate(skill.updated_at) || shortDate(skill.created_at)

  // Timer del reset visual: se limpia al desmontar para no setear estado en
  // un componente ya desmontado (la lista se recicla al paginar/filtrar).
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    return () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    }
  }, [])

  /** Copia el CONTENIDO COMPLETO de la skill al portapapeles.
   *  stopPropagation: la card entera es un <button> que togglea `open`; copiar
   *  NO debe expandir/colapsar. */
  async function handleCopy(e: React.MouseEvent) {
    e.stopPropagation()
    e.preventDefault()
    if (copyState === 'copying') return
    if (resetTimer.current) clearTimeout(resetTimer.current)
    setCopyState('copying')
    try {
      const res = await fetchSkillContent(skill.name)
      await navigator.clipboard.writeText(res.content)
      setCopyState('copied')
    } catch {
      // Fetch o clipboard fallaron (401/404/permiso denegado): feedback breve,
      // la card sigue funcionando.
      setCopyState('error')
    }
    resetTimer.current = setTimeout(() => setCopyState('idle'), 2000)
  }

  const copyTitle =
    copyState === 'copied'
      ? '¡Copiado!'
      : copyState === 'error'
        ? 'No se pudo copiar'
        : 'Copiar skill completa'

  return (
    <button
      onClick={() => setOpen((v) => !v)}
      aria-expanded={open}
      className="group flex flex-col gap-2 rounded-lg border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] p-3 text-left transition hover:border-[rgba(255,255,255,0.12)] hover:bg-[rgba(255,255,255,0.03)]"
    >
      <div className="flex items-start gap-2">
        <BookOpen size={15} className="mt-0.5 shrink-0 text-accent" />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg-muted group-hover:text-fg">
          {skill.name}
        </span>
        {skill.agent_owner && (
          <span
            className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${ownerChip(
              skill.agent_owner
            )}`}
            title={`Publicado por ${skill.agent_owner}`}
          >
            {skill.agent_owner}
          </span>
        )}
        {/* Copiar skill completa (patrón Copy → Copied). Discreto: se realza en
            hover de la card y al copiar. */}
        <span
          role="button"
          tabIndex={0}
          onClick={handleCopy}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.stopPropagation()
              e.preventDefault()
              void handleCopy(e as unknown as React.MouseEvent)
            }
          }}
          aria-label="Copiar skill completa"
          title={copyTitle}
          className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] transition ${
            copyState === 'copied'
              ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-400'
              : copyState === 'error'
                ? 'border-red-500/40 bg-red-500/10 text-red-400'
                : 'border-[var(--border-subtle)] text-fg-faint opacity-60 hover:bg-surface-hover hover:text-fg-muted group-hover:opacity-100'
          }`}
        >
          {copyState === 'copying' ? (
            <Loader2 size={11} className="animate-spin" />
          ) : copyState === 'copied' ? (
            <Check size={11} />
          ) : copyState === 'error' ? (
            <AlertCircle size={11} />
          ) : (
            <Copy size={11} />
          )}
          {copyState === 'copied' && <span>Copiado</span>}
          {copyState === 'error' && <span>Error</span>}
        </span>
      </div>

      <p
        className={`text-[12px] leading-snug text-fg-subtle ${
          open ? '' : 'line-clamp-3'
        }`}
      >
        {skill.description}
      </p>

      {skill.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {skill.tags.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] px-1.5 py-0.5 text-[10px] text-fg-subtle"
            >
              <Tag size={9} className="text-fg-faint" />
              {t}
            </span>
          ))}
        </div>
      )}

      {open && (
        <div className="flex flex-wrap items-center gap-3 border-t border-[var(--border-subtle)] pt-2 text-[10px] text-fg-subtle">
          {skill.title !== skill.name && (
            <span className="min-w-0 truncate text-fg-muted">{skill.title}</span>
          )}
          {skill.category && (
            <span className="inline-flex items-center gap-1">
              <Layers size={10} className="text-fg-faint" />
              {skill.category}
            </span>
          )}
          <span className="inline-flex items-center gap-1 tabular-nums">
            <Layers size={10} className="text-fg-faint" />
            {skill.chunk_count} chunk{skill.chunk_count === 1 ? '' : 's'}
          </span>
          {updated && <span className="tabular-nums">{updated}</span>}
          <span className="font-mono text-fg-subtle/60">{skill.source_path}</span>
        </div>
      )}
    </button>
  )
}

/**
 * Vista "Biblioteca": catálogo de skills VIGENTES del registro compartido
 * (GET /api/skills — dedup por skill, sin references). Búsqueda server-side
 * por `q` (debounce) y paginación incremental con "Cargar más".
 */
export default function SkillsView() {
  const [input, setInput] = useState('')
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const [items, setItems] = useState<SkillItem[]>([])

  // Debounce del input: evita una llamada por tecla.
  useEffect(() => {
    const t = setTimeout(() => setQ(input.trim()), 250)
    return () => clearTimeout(t)
  }, [input])

  // Al cambiar el filtro, la paginación arranca de cero.
  useEffect(() => {
    setOffset(0)
    setItems([])
  }, [q])

  const { data, isLoading, isError, isFetching } = useQuery({
    queryKey: ['skills', q, offset],
    queryFn: () => fetchSkills(q, PAGE_SIZE, offset),
  })

  // Acumula las páginas recibidas. Dedup por `name` para tolerar re-renders
  // (StrictMode) y refetches de la misma página.
  useEffect(() => {
    if (!data) return
    setItems((prev) => {
      const base = data.offset === 0 ? [] : prev
      const seen = new Set(base.map((s) => s.name))
      const merged = [...base]
      for (const s of data.skills) {
        if (!seen.has(s.name)) {
          seen.add(s.name)
          merged.push(s)
        }
      }
      return merged
    })
  }, [data])

  const total = data?.total ?? 0
  const hasMore = useMemo(
    () => (data ? items.length < data.total : false),
    [data, items.length]
  )
  const firstLoad = isLoading && items.length === 0

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-[var(--border-strong)] px-5 py-3">
        <h1 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <BookOpen size={15} className="text-accent" /> Biblioteca
        </h1>
        <div className="flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1">
          <Search size={13} className="text-fg-faint" />
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Buscar skills…"
            className="w-48 bg-transparent text-[12px] text-fg outline-none placeholder:text-fg-faint"
          />
          {input && (
            <button
              onClick={() => setInput('')}
              title="Limpiar"
              className="text-fg-faint transition hover:text-fg"
            >
              <X size={12} />
            </button>
          )}
        </div>
        {isFetching && !firstLoad && (
          <Loader2 size={13} className="animate-spin text-fg-faint" />
        )}
        <span className="ml-auto text-xs text-fg-subtle tabular-nums">
          {items.length}
          {total > items.length ? ` / ${total}` : ''} skill
          {total === 1 ? '' : 's'}
        </span>
      </div>

      {/* Contenido */}
      <div className="min-h-0 flex-1 overflow-auto p-5">
        {firstLoad && <p className="text-xs text-fg-subtle">Cargando skills…</p>}
        {isError && (
          <p className="text-xs text-red-400">No se pudo cargar la biblioteca.</p>
        )}
        {!firstLoad && !isError && items.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
              <BookOpen size={24} className="text-fg-faint" />
            </div>
            <p className="text-[13px] text-fg-muted">
              {q ? (
                <>
                  Sin skills que coincidan con{' '}
                  <span className="text-fg">{q}</span>.
                </>
              ) : (
                'El registro compartido todavía no tiene skills.'
              )}
            </p>
          </div>
        )}
        {items.length > 0 && (
          <>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {items.map((s) => (
                <SkillCard key={s.name} skill={s} />
              ))}
            </div>
            {hasMore && (
              <div className="mt-4 flex justify-center">
                <button
                  onClick={() => setOffset(items.length)}
                  disabled={isFetching}
                  className="flex items-center gap-2 rounded-md border border-[var(--border-strong)] bg-[rgba(255,255,255,0.02)] px-3 py-2 text-[12px] text-fg-muted transition hover:bg-surface-hover disabled:opacity-50"
                >
                  {isFetching && <Loader2 size={13} className="animate-spin" />}
                  Cargar más
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Nota de procedencia: la biblioteca es de solo lectura desde Sinapsis. */}
      <div className="flex shrink-0 items-center gap-1.5 border-t border-[var(--border-subtle)] px-5 py-2 text-[10px] text-fg-subtle/70">
        <User size={10} className="text-fg-faint" />
        Registro compartido de la flota · solo lectura
      </div>
    </div>
  )
}
