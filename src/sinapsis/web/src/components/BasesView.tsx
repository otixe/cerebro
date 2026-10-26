import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  useSensor,
  useSensors,
  useDraggable,
  useDroppable,
  pointerWithin,
  type DragStartEvent,
  type DragEndEvent,
} from '@dnd-kit/core'
import { motion } from 'framer-motion'
import {
  Database,
  Table2,
  Columns3,
  LayoutGrid,
  Search,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Check,
  Minus,
  X,
  FileText,
} from 'lucide-react'
import {
  fetchTree,
  fetchCollection,
  fetchNode,
  type CollectionItem,
  type CollectionResponse,
  type PropValue,
  type NodeContent,
} from '../lib/api'
import { saveNoteOperation } from '../lib/noteSave'
import { parseFrontmatterEdit, buildDocument, type FMField } from '../lib/frontmatterEdit'
import { useShellStore } from '../store'
import { useToast } from './Toast'
import FolderPicker from './FolderPicker'

type ViewMode = 'table' | 'board' | 'gallery'

const LS_KEY = 'sinapsis.bases.state.v1'

interface PersistState {
  dir: string
  view: ViewMode
  recursive: boolean
  groupKey: string | null
}

function loadState(): Partial<PersistState> {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || '{}')
  } catch {
    return {}
  }
}
function saveState(s: PersistState) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(s))
  } catch {
    /* localStorage no disponible */
  }
}

// ---------- Tipado / render de valores ----------
const ISO_DATE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$/
const MONTHS_ES = [
  'ene', 'feb', 'mar', 'abr', 'may', 'jun',
  'jul', 'ago', 'sep', 'oct', 'nov', 'dic',
]

function fmtDate(s: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s)
  if (!m) return s
  return `${Number(m[3])} ${MONTHS_ES[Number(m[2]) - 1]} ${m[1]}`
}

/** Representación escalar en texto (para orden, filtro y agrupación). */
function scalarize(v: PropValue | undefined): string {
  if (v == null) return ''
  if (Array.isArray(v)) return v.map((x) => (x == null ? '' : String(x))).join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

/** Celda tipada: chips para listas, check para booleanos, fecha formateada, etc. */
function ValueCell({ value }: { value: PropValue | undefined }) {
  if (value == null || value === '') return <span className="text-fg-faint">—</span>
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-fg-faint">—</span>
    return (
      <div className="flex flex-wrap gap-1">
        {value.map((t, i) => (
          <span
            key={i}
            className="inline-flex items-center rounded-full border border-[var(--border-subtle)] bg-[rgba(168, 85, 247,0.1)] px-1.5 py-0.5 text-[11px] text-fg-muted"
          >
            {String(t)}
          </span>
        ))}
      </div>
    )
  }
  if (typeof value === 'boolean') {
    return value ? (
      <Check size={14} className="text-[var(--success)]" />
    ) : (
      <Minus size={14} className="text-fg-faint" />
    )
  }
  if (typeof value === 'number') {
    return <span className="tabular-nums text-fg-muted">{value}</span>
  }
  if (typeof value === 'object') {
    return (
      <span className="font-mono text-[11px] text-fg-subtle">{JSON.stringify(value)}</span>
    )
  }
  const s = String(value)
  if (ISO_DATE.test(s)) {
    return <span className="tabular-nums text-fg-muted">{fmtDate(s)}</span>
  }
  return <span className="text-fg-muted">{s}</span>
}

// ---------- View switcher (segmented, premium) ----------
function ViewSwitcher({ view, onChange }: { view: ViewMode; onChange: (v: ViewMode) => void }) {
  const opts: { key: ViewMode; label: string; icon: typeof Table2 }[] = [
    { key: 'table', label: 'Tabla', icon: Table2 },
    { key: 'board', label: 'Board', icon: Columns3 },
    { key: 'gallery', label: 'Galería', icon: LayoutGrid },
  ]
  return (
    <div className="flex items-center gap-0.5 rounded-lg border border-[var(--border-subtle)] bg-surface p-0.5">
      {opts.map((o) => {
        const Icon = o.icon
        const active = view === o.key
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] font-medium transition ${
              active ? 'bg-accent-bg text-fg shadow-sm' : 'text-fg-subtle hover:text-fg'
            }`}
          >
            <Icon size={14} />
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

// ---------- Empty state ----------
function EmptyState({ dir }: { dir: string }) {
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
          <Database size={24} className="text-fg-faint" />
        </div>
        <h2 className="text-[15px] font-medium text-fg-muted">Sin notas en esta base</h2>
        <p className="max-w-[360px] text-[13px] text-fg-subtle">
          {dir
            ? `La carpeta «${dir}» no tiene notas .md.`
            : 'Elegí una carpeta del panel izquierdo para verla como base de datos.'}
        </p>
      </div>
    </div>
  )
}

// ---------- Componente principal ----------
export default function BasesView() {
  const openNote = useShellStore((s) => s.openNote)
  const setBasesCurrentDir = useShellStore((s) => s.setBasesCurrentDir)
  const qc = useQueryClient()
  const toast = useToast()
  const init = useRef(loadState())

  const [dir, setDir] = useState<string>(init.current.dir ?? '')
  const [view, setView] = useState<ViewMode>(init.current.view ?? 'table')
  const [recursive, setRecursive] = useState<boolean>(init.current.recursive ?? false)
  const [groupKey, setGroupKey] = useState<string | null>(init.current.groupKey ?? null)
  const [query, setQuery] = useState('')
  const [propFilterKey, setPropFilterKey] = useState<string>('')
  const [propFilterVal, setPropFilterVal] = useState<string>('')
  const [sort, setSort] = useState<{ key: string; dir: 'asc' | 'desc' }>({
    key: '__title__',
    dir: 'asc',
  })
  const [activeId, setActiveId] = useState<string | null>(null)

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } })
  )

  // El contexto de creación sigue la carpeta visual seleccionada en Bases.
  useEffect(() => {
    setBasesCurrentDir(dir)
  }, [dir, setBasesCurrentDir])

  // Persistir preferencias.
  useEffect(() => {
    saveState({ dir, view, recursive, groupKey })
  }, [dir, view, recursive, groupKey])

  const { data: tree } = useQuery({ queryKey: ['tree'], queryFn: fetchTree })

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['collection', dir, recursive],
    queryFn: () => fetchCollection(dir, recursive),
    enabled: tree != null,
  })

  const items = useMemo(() => data?.items ?? [], [data])

  // Unión de claves de propiedad, ordenadas por frecuencia desc, luego alfabético.
  const propKeys = useMemo(() => {
    const freq = new Map<string, number>()
    for (const it of items)
      for (const k of Object.keys(it.properties)) freq.set(k, (freq.get(k) ?? 0) + 1)
    return [...freq.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([k]) => k)
  }, [items])

  // Claves aptas para agrupar (escalar simple: string o número).
  const groupableKeys = useMemo(
    () =>
      propKeys.filter((k) =>
        items.every((it) => {
          const v = it.properties[k]
          return v == null || typeof v === 'string' || typeof v === 'number'
        })
      ),
    [propKeys, items]
  )

  // Group key por defecto: status/estado si existe, si no la primera agrupable.
  const effectiveGroupKey = useMemo(() => {
    if (groupKey && groupableKeys.includes(groupKey)) return groupKey
    const pref = groupableKeys.find((k) => /^(status|estado|state|type|tipo)$/i.test(k))
    return pref ?? groupableKeys[0] ?? null
  }, [groupKey, groupableKeys])

  // Solo se puede arrastrar (reescribir frontmatter) si la clave es string puro.
  const groupDraggable = useMemo(() => {
    if (!effectiveGroupKey) return false
    return items.every((it) => {
      const v = it.properties[effectiveGroupKey]
      return v == null || typeof v === 'string'
    })
  }, [effectiveGroupKey, items])

  // Valores distintos de la propiedad de filtro (para el dropdown).
  const filterValues = useMemo(() => {
    if (!propFilterKey) return []
    const set = new Set<string>()
    for (const it of items) {
      const v = it.properties[propFilterKey]
      if (Array.isArray(v)) v.forEach((x) => set.add(String(x)))
      else if (v != null && v !== '') set.add(scalarize(v))
    }
    return [...set].sort((a, b) => a.localeCompare(b))
  }, [propFilterKey, items])

  // Filtro por texto + por propiedad.
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return items.filter((it) => {
      if (q) {
        const hay =
          it.title.toLowerCase().includes(q) ||
          Object.values(it.properties).some((v) => scalarize(v).toLowerCase().includes(q))
        if (!hay) return false
      }
      if (propFilterKey && propFilterVal) {
        const v = it.properties[propFilterKey]
        const match = Array.isArray(v)
          ? v.map(String).includes(propFilterVal)
          : scalarize(v) === propFilterVal
        if (!match) return false
      }
      return true
    })
  }, [items, query, propFilterKey, propFilterVal])

  // Orden (tabla).
  const sorted = useMemo(() => {
    const arr = [...filtered]
    const { key, dir: dr } = sort
    arr.sort((a, b) => {
      let av: string
      let bv: string
      if (key === '__title__') {
        av = a.title.toLowerCase()
        bv = b.title.toLowerCase()
      } else {
        av = scalarize(a.properties[key]).toLowerCase()
        bv = scalarize(b.properties[key]).toLowerCase()
      }
      if (av === '' && bv !== '') return 1
      if (bv === '' && av !== '') return -1
      const an = Number(av)
      const bn = Number(bv)
      let cmp: number
      if (av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn)) cmp = an - bn
      else cmp = av.localeCompare(bv, 'es')
      return dr === 'asc' ? cmp : -cmp
    })
    return arr
  }, [filtered, sort])

  function toggleSort(key: string) {
    setSort((s) =>
      s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }
    )
  }

  // ---------- Board: agrupación ----------
  const boardGroups = useMemo(() => {
    if (!effectiveGroupKey) return []
    const map = new Map<string, CollectionItem[]>()
    for (const it of filtered) {
      const val = scalarize(it.properties[effectiveGroupKey])
      const bucket = val === '' ? '__none__' : val
      if (!map.has(bucket)) map.set(bucket, [])
      map.get(bucket)!.push(it)
    }
    const keys = [...map.keys()].sort((a, b) => {
      if (a === '__none__') return 1
      if (b === '__none__') return -1
      return a.localeCompare(b, 'es')
    })
    return keys.map((k) => ({
      value: k,
      label: k === '__none__' ? 'Sin valor' : k,
      items: map.get(k)!,
    }))
  }, [filtered, effectiveGroupKey])

  const activeItem = activeId ? items.find((i) => i.path === activeId) ?? null : null

  function onDragStart(e: DragStartEvent) {
    setActiveId(String(e.active.id))
  }

  async function onDragEnd(e: DragEndEvent) {
    setActiveId(null)
    const { active, over } = e
    if (!over || !effectiveGroupKey || !groupDraggable) return
    const path = String(active.id)
    const target = String(over.id) // '__none__' o valor
    const it = items.find((i) => i.path === path)
    if (!it) return
    const cur = scalarize(it.properties[effectiveGroupKey])
    const targetVal = target === '__none__' ? '' : target
    if (cur === targetVal) return
    await rewriteProp(path, effectiveGroupKey, targetVal)
  }

  /** Reescribe SOLO el frontmatter (preserva cuerpo) contra el contenido fresco. */
  async function rewriteProp(path: string, key: string, value: string) {
    await saveNoteOperation(path, () => fetchNode(path), fresh => {
    // No es un merge de tres vías: sólo esta clave; en el mismo campo última gana.
    const parsed = parseFrontmatterEdit(fresh.content)
    let entries = parsed.entries
    const idx = entries.findIndex((en) => en.kind === 'field' && (en as FMField).key === key)
    if (idx >= 0) {
      entries = entries.map((en, i) =>
        i === idx && en.kind === 'field'
          ? { ...en, type: 'text', text: value, list: [], bool: false, dirty: true }
          : en
      )
    } else if (value !== '') {
      entries = [
        ...entries,
        {
          kind: 'field',
          key,
          type: 'text',
          text: value,
          list: [],
          bool: false,
          raw: '',
          dirty: true,
        } as FMField,
      ]
    } else {
      return fresh.content
    }
    return buildDocument(entries, parsed.body)
    }, saved => {
    // Cache sólo tras confirmación, nunca después de un conflicto.
    qc.setQueryData<CollectionResponse>(['collection', dir, recursive], (prev) =>
      prev
        ? {
            ...prev,
            items: prev.items.map((i) =>
              i.path === path
                ? { ...i, properties: { ...i.properties, [key]: value || null } }
                : i
            ),
          }
        : prev
    )
    qc.setQueryData<NodeContent>(['node', path], saved)
    toast(`${key} → ${value || 'sin valor'}`)
    }, toast)
    // No side effects after await; the coordinator validates generation.
  }

  const dirLabel = dir === '' ? 'Raíz del vault' : dir

  return (
    <div className="flex h-full min-h-0">
      {tree && <FolderPicker tree={tree} selected={dir} onSelect={setDir} />}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Header */}
        <div className="flex h-11 shrink-0 flex-wrap items-center gap-3 border-b border-[var(--border-strong)] px-4">
          <div className="flex min-w-0 items-center gap-1.5 text-[13px]">
            <Database size={15} className="shrink-0 text-accent" />
            <span className="truncate font-semibold text-fg">{dirLabel}</span>
            <span className="shrink-0 text-fg-subtle">· {filtered.length}</span>
          </div>
          <label className="flex items-center gap-1.5 text-[12px] text-fg-subtle">
            <input
              type="checkbox"
              checked={recursive}
              onChange={(e) => setRecursive(e.target.checked)}
              className="accent-[var(--accent)]"
            />
            Incluir subcarpetas
          </label>
          <div className="ml-auto">
            <ViewSwitcher view={view} onChange={setView} />
          </div>
        </div>

        {/* Toolbar por vista */}
        {items.length > 0 && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-subtle)] px-4 py-2">
            {view !== 'board' && (
              <>
                <div className="flex items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1">
                  <Search size={13} className="text-fg-faint" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Filtrar…"
                    className="w-40 bg-transparent text-[12px] text-fg outline-none placeholder:text-fg-faint"
                  />
                  {query && (
                    <button onClick={() => setQuery('')} className="text-fg-faint hover:text-fg">
                      <X size={12} />
                    </button>
                  )}
                </div>
                <select
                  value={propFilterKey}
                  onChange={(e) => {
                    setPropFilterKey(e.target.value)
                    setPropFilterVal('')
                  }}
                  className="rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1 text-[12px] text-fg-muted outline-none [color-scheme:dark]"
                >
                  <option value="">Propiedad…</option>
                  {propKeys.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
                {propFilterKey && (
                  <select
                    value={propFilterVal}
                    onChange={(e) => setPropFilterVal(e.target.value)}
                    className="rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1 text-[12px] text-fg-muted outline-none [color-scheme:dark]"
                  >
                    <option value="">(cualquiera)</option>
                    {filterValues.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                )}
              </>
            )}
            {view === 'board' && (
              <label className="flex items-center gap-2 text-[12px] text-fg-subtle">
                <span>Agrupar por</span>
                <select
                  value={effectiveGroupKey ?? ''}
                  onChange={(e) => setGroupKey(e.target.value || null)}
                  className="rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1 text-[12px] text-fg-muted outline-none [color-scheme:dark]"
                >
                  {groupableKeys.length === 0 && <option value="">(sin propiedades)</option>}
                  {groupableKeys.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
                {effectiveGroupKey && (
                  <span className="text-fg-faint">
                    {groupDraggable ? '· arrastrá para recategorizar' : '· solo lectura'}
                  </span>
                )}
              </label>
            )}
          </div>
        )}

        {/* Contenido */}
        <div className="min-h-0 flex-1 overflow-auto">
          {isLoading && (
            <p className="px-6 py-4 text-[13px] text-fg-subtle">Cargando base…</p>
          )}
          {isError && (
            <p className="px-6 py-4 text-[13px] text-danger">
              {error instanceof Error ? error.message : 'Error al cargar la colección'}
            </p>
          )}
          {!isLoading && !isError && items.length === 0 && <EmptyState dir={dir} />}

          {!isLoading && !isError && items.length > 0 && view === 'table' && (
            <TableView
              items={sorted}
              propKeys={propKeys}
              sort={sort}
              onSort={toggleSort}
              onOpen={openNote}
            />
          )}

          {!isLoading && !isError && items.length > 0 && view === 'board' && (
            <DndContext
              sensors={sensors}
              collisionDetection={pointerWithin}
              onDragStart={onDragStart}
              onDragEnd={onDragEnd}
            >
              <div className="flex gap-4 p-4">
                {boardGroups.map((g) => (
                  <BoardColumn
                    key={g.value}
                    id={g.value}
                    label={g.label}
                    count={g.items.length}
                    droppable={groupDraggable}
                  >
                    {g.items.map((it) => (
                      <BoardCard
                        key={it.path}
                        item={it}
                        propKeys={propKeys}
                        groupKey={effectiveGroupKey}
                        draggable={groupDraggable}
                        onOpen={() => openNote(it.path)}
                      />
                    ))}
                  </BoardColumn>
                ))}
              </div>
              <DragOverlay>
                {activeItem ? (
                  <BoardCard
                    item={activeItem}
                    propKeys={propKeys}
                    groupKey={effectiveGroupKey}
                    draggable={false}
                    overlay
                  />
                ) : null}
              </DragOverlay>
            </DndContext>
          )}

          {!isLoading && !isError && items.length > 0 && view === 'gallery' && (
            <GalleryView items={sorted} propKeys={propKeys} onOpen={openNote} />
          )}
        </div>
      </div>
    </div>
  )
}

// ---------- Tabla ----------
function TableView({
  items,
  propKeys,
  sort,
  onSort,
  onOpen,
}: {
  items: CollectionItem[]
  propKeys: string[]
  sort: { key: string; dir: 'asc' | 'desc' }
  onSort: (key: string) => void
  onOpen: (path: string) => void
}) {
  function SortIcon({ col }: { col: string }) {
    if (sort.key !== col)
      return <ArrowUpDown size={11} className="text-fg-faint opacity-0 group-hover:opacity-60" />
    return sort.dir === 'asc' ? (
      <ArrowUp size={11} className="text-accent" />
    ) : (
      <ArrowDown size={11} className="text-accent" />
    )
  }
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead className="sticky top-0 z-10 glass-strong">
        <tr className="border-b border-[var(--border-strong)]">
          <Th onClick={() => onSort('__title__')}>
            <span className="flex items-center gap-1">
              Título <SortIcon col="__title__" />
            </span>
          </Th>
          {propKeys.map((k) => (
            <Th key={k} onClick={() => onSort(k)}>
              <span className="flex items-center gap-1">
                {k} <SortIcon col={k} />
              </span>
            </Th>
          ))}
        </tr>
      </thead>
      <tbody>
        {items.map((it) => (
          <tr
            key={it.path}
            onClick={() => onOpen(it.path)}
            className="cursor-pointer border-b border-[var(--border-subtle)] transition hover:bg-surface-hover"
          >
            <td className="max-w-[280px] px-3 py-2">
              <div className="flex items-center gap-2">
                <FileText size={14} className="shrink-0 text-fg-faint" />
                <span className="truncate font-medium text-fg">{it.title}</span>
              </div>
            </td>
            {propKeys.map((k) => (
              <td key={k} className="px-3 py-2 align-top">
                <ValueCell value={it.properties[k]} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Th({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <th
      onClick={onClick}
      className="group cursor-pointer whitespace-nowrap px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider text-fg-subtle transition hover:text-fg-muted"
    >
      {children}
    </th>
  )
}

// ---------- Board ----------
function BoardColumn({
  id,
  label,
  count,
  droppable,
  children,
}: {
  id: string
  label: string
  count: number
  droppable: boolean
  children: ReactNode
}) {
  const { setNodeRef, isOver } = useDroppable({ id, disabled: !droppable })
  return (
    <div className="flex min-w-[260px] max-w-[300px] flex-1 flex-col">
      <div className="mb-2 flex items-center gap-2 px-1">
        <span className="truncate text-[11px] font-semibold uppercase tracking-wider text-fg-muted">
          {label}
        </span>
        <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">{count}</span>
      </div>
      <div
        ref={setNodeRef}
        className={`flex min-h-[120px] flex-1 flex-col gap-2 rounded-lg border p-2 transition-colors ${
          isOver ? 'border-accent/60 bg-accent/5' : 'border-[var(--border-subtle)] glass'
        }`}
      >
        {children}
      </div>
    </div>
  )
}

function BoardCard({
  item,
  propKeys,
  groupKey,
  draggable,
  overlay = false,
  onOpen,
}: {
  item: CollectionItem
  propKeys: string[]
  groupKey: string | null
  draggable: boolean
  overlay?: boolean
  onOpen?: () => void
}) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: item.path,
    disabled: !draggable,
  })
  // Mostrar hasta 3 propiedades (excluyendo la de agrupación).
  const shown = propKeys.filter((k) => k !== groupKey).slice(0, 3)
  return (
    <motion.div
      layout={!overlay}
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      onClick={onOpen}
      className={`rounded-lg border border-[var(--border-strong)] bg-surface p-3 text-left transition ${
        draggable ? 'cursor-grab active:cursor-grabbing' : 'cursor-pointer'
      } ${overlay ? 'rotate-2 shadow-2xl' : 'hover:border-[rgba(255,255,255,0.12)]'} ${
        isDragging && !overlay ? 'opacity-30' : ''
      }`}
    >
      <p className="text-[13px] font-medium leading-snug text-fg">{item.title}</p>
      {shown.length > 0 && (
        <div className="mt-2 space-y-1">
          {shown.map((k) => {
            const v = item.properties[k]
            if (v == null || v === '' || (Array.isArray(v) && v.length === 0)) return null
            return (
              <div key={k} className="flex items-center gap-1.5 text-[11px]">
                <span className="shrink-0 text-fg-faint">{k}</span>
                <span className="min-w-0 truncate">
                  <ValueCell value={v} />
                </span>
              </div>
            )
          })}
        </div>
      )}
    </motion.div>
  )
}

// ---------- Galería ----------
function GalleryView({
  items,
  propKeys,
  onOpen,
}: {
  items: CollectionItem[]
  propKeys: string[]
  onOpen: (path: string) => void
}) {
  const shownKeys = propKeys.slice(0, 5)
  return (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3 p-4">
      {items.map((it) => (
        <button
          key={it.path}
          onClick={() => onOpen(it.path)}
          className="flex flex-col gap-2 rounded-xl border border-[var(--border-strong)] bg-[rgba(255,255,255,0.02)] p-4 text-left transition hover:border-[rgba(255,255,255,0.12)] hover:bg-[rgba(255,255,255,0.03)]"
        >
          <div className="flex items-start gap-2">
            <FileText size={16} className="mt-0.5 shrink-0 text-accent" />
            <span className="text-[14px] font-semibold leading-snug text-fg">{it.title}</span>
          </div>
          <div className="space-y-1">
            {shownKeys.map((k) => {
              const v = it.properties[k]
              if (v == null || v === '' || (Array.isArray(v) && v.length === 0)) return null
              return (
                <div key={k} className="flex gap-2 text-[12px]">
                  <span className="w-20 shrink-0 truncate text-fg-faint">{k}</span>
                  <span className="min-w-0 flex-1">
                    <ValueCell value={v} />
                  </span>
                </div>
              )
            })}
          </div>
        </button>
      ))}
    </div>
  )
}
