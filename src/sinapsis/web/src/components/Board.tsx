import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueries, useQueryClient } from '@tanstack/react-query'
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
import { CSS } from '@dnd-kit/utilities'
import { AnimatePresence, motion } from 'framer-motion'
import { Lock, ListChecks, ChevronLeft, ChevronRight, Ban, Tag } from 'lucide-react'
import {
  fetchTasks,
  fetchMe,
  fetchTasksReady,
  fetchTaskDeps,
  taskUpdate,
  type Task,
  type TaskStatus,
  type TasksResponse,
} from '../lib/api'
import { useToast } from './Toast'
import TaskDialog from './TaskDialog'
import { useShellStore, useAppStore } from '../store'
import { projectChip, orgChip, orgLabel, taskOrg, projectOptions } from './taskShared'

// COLUMNAS DEL BOARD.
// ⚠️ : faltaban 'submitted' y 'in_review' => 19 tareas ENTREGADAS y 2
// pospuestas NO SE VEIAN EN NINGUNA COLUMNA. El filtro de abajo descarta todo
// status que no tenga columna, asi que una tarea entregada desaparecia del tablero
// sin ningun error: quedaba fuera del board Y fuera de la bandeja (que tampoco
// mostraba 'submitted'). O sea: invisible en las DOS vistas a la vez.
// Regla: si se agrega un status al backend, TIENE que tener columna aca, o se
// pierde en silencio. El descarte silencioso es el modo de falla de este archivo.
const COLUMNS: { key: TaskStatus; label: string; dim?: boolean }[] = [
  { key: 'todo', label: 'Por hacer' },
  { key: 'in_progress', label: 'En progreso' },
  { key: 'submitted', label: 'Entregadas' },
  { key: 'blocked', label: 'Bloqueadas' },
  { key: 'done', label: 'Terminadas' },
  { key: 'cancelled', label: 'Canceladas', dim: true },
]

// Stable fallback: a fresh [] returned by a Zustand selector loops in React 19 before meta loads.
const EMPTY_PROJECTS: string[] = []

// Preferencia de visibilidad de la columna 'Canceladas' (persistida).
const CANCELLED_PREF_KEY = 'sinapsis.board.showCancelled'
// Prefijo de tag que define un "proyecto" dentro de un ámbito.
const PROJECT_TAG_PREFIX = 'proyecto/'
// Filtro de proyecto por-ámbito (persistido): mapa { ambito: tag }.
const PROJECT_TAG_PREF_KEY = 'sinapsis.board.projectTag'

function loadProjectTagMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(PROJECT_TAG_PREF_KEY)
    return raw ? (JSON.parse(raw) as Record<string, string>) : {}
  } catch {
    return {}
  }
}

function loadProjectTag(ambito: string): string {
  return loadProjectTagMap()[ambito] ?? 'Todos'
}

function saveProjectTag(ambito: string, tag: string): void {
  try {
    const map = loadProjectTagMap()
    map[ambito] = tag
    localStorage.setItem(PROJECT_TAG_PREF_KEY, JSON.stringify(map))
  } catch {
    /* localStorage no disponible: se ignora la persistencia */
  }
}
// Estados que se pueden mover con drag & drop (drop válido). No incluye
// 'cancelled': cancelar es una acción SOFT con cascada (endpoint aparte),
// no un simple cambio de columna.
const BOARD_STATUSES: TaskStatus[] = ['todo', 'in_progress', 'blocked', 'done']
// Estados que el tablero renderiza como columnas (incluye 'cancelled', que
// ahora llega desde /api/tasks). El frontend decide qué mostrar por columna/mes.
// Debe coincidir con COLUMNS: lo que no este aca se FILTRA y desaparece del board.
const COLUMN_STATUSES: TaskStatus[] = ['todo', 'in_progress', 'submitted', 'blocked', 'done', 'cancelled']

// Color sutil por prioridad (borde izquierdo de la tarjeta).
const PRIORITY_ACCENT: Record<string, string> = {
  high: 'border-l-red-400/70',
  medium: 'border-l-amber-400/60',
  low: 'border-l-sky-400/50',
}

function priorityAccent(p?: string | null): string {
  return PRIORITY_ACCENT[p || 'medium'] || 'border-l-transparent'
}



// ---------- Navegación por mes (zona del panel) ----------
const TZ: string = (import.meta.env.VITE_TIMEZONE || Intl.DateTimeFormat().resolvedOptions().timeZone)
const MONTH_NAMES = [
  'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
  'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
]

interface YearMonth {
  year: number
  month: number // 1-12
}

/** Año-mes actual en la zona del panel. */
function currentYearMonth(): YearMonth {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
  }).formatToParts(new Date())
  const year = Number(parts.find((p) => p.type === 'year')?.value)
  const month = Number(parts.find((p) => p.type === 'month')?.value)
  return { year, month }
}

/** Parsea un timestamptz::text de Postgres (ej: " 02:07:50.972303+00"). */
function parsePgTs(ts: string): Date | null {
  let s = ts.trim().replace(' ', 'T')
  // Normalizar offset: "+00" -> "+00:00", "+0300" -> "+03:00".
  const m = s.match(/([+-]\d{2})(\d{2})?$/)
  if (m) {
    if (!m[2]) s = s + ':00'
    else s = s.slice(0, -2) + ':' + m[2]
  } else if (!/[Zz]$/.test(s)) {
    s = s + 'Z'
  }
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

/** Año-mes de un timestamp, evaluado en la zona del panel. */
function tsYearMonth(ts?: string | null): YearMonth | null {
  if (!ts) return null
  const d = parsePgTs(ts)
  if (!d) return null
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
  }).formatToParts(d)
  return {
    year: Number(parts.find((p) => p.type === 'year')?.value),
    month: Number(parts.find((p) => p.type === 'month')?.value),
  }
}

function sameYearMonth(a: YearMonth, b: YearMonth): boolean {
  return a.year === b.year && a.month === b.month
}

function addMonths(ym: YearMonth, delta: number): YearMonth {
  const zero = ym.year * 12 + (ym.month - 1) + delta
  return { year: Math.floor(zero / 12), month: (zero % 12) + 1 }
}

// ---------- Tarjeta ----------
function TaskCard({
  task,
  onClick,
  overlay = false,
  progress,
  blocked,
  ready = false,
}: {
  task: Task
  onClick?: () => void
  overlay?: boolean
  /** Progreso de subtareas (done/total) si tiene. */
  progress?: { done: number; total: number }
  /** Info de bloqueo: cantidad de deps no-done (null = aún no cargó el conteo)
   *  y los títulos de las tareas de las que depende y aún no están done. */
  blocked?: { count: number | null; titles: string[] }
  /** Lista para empezar (resalte sutil). */
  ready?: boolean
}) {
  // Meta para el chip de organizacion (cambio de implementación). Si el backend todavia no
  // manda organizations/projects_v2, taskOrg() devuelve '' y el chip no se pinta.
  const organizations = useAppStore((s) => s.meta?.organizations)
  const projectsV2 = useAppStore((s) => s.meta?.projects_v2)
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({ id: task.id, data: { status: task.status } })

  const style = {
    transform: CSS.Translate.toString(transform),
  }

  const base = `group cursor-grab rounded-lg border border-[var(--border-strong)] border-l-2 ${priorityAccent(
    task.priority
  )} bg-surface p-3 text-left shadow-sm active:cursor-grabbing`

  const readyRing = ready && !overlay ? 'ring-1 ring-accent/40' : ''

  return (
    <motion.div
      layout={!overlay}
      ref={setNodeRef}
      style={style}
      {...listeners}
      {...attributes}
      onClick={onClick}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className={`${base} ${readyRing} ${overlay ? 'rotate-2 shadow-2xl' : ''} ${
        isDragging && !overlay ? 'opacity-30' : 'opacity-100'
      }`}
    >
      <div className="flex items-start gap-1.5">
        {blocked && (
          <span
            className="mt-0.5 shrink-0 text-[var(--warn)]"
            title={
              blocked.count && blocked.count > 0
                ? `Bloqueada por ${blocked.count} tarea(s)`
                : 'Bloqueada'
            }
          >
            <Lock size={13} />
          </span>
        )}
        <p className="text-[13px] font-medium leading-snug text-fg">{task.title}</p>
      </div>
      {blocked && blocked.titles.length > 0 && (
        <p
          className="mt-1 line-clamp-2 text-[11px] leading-snug text-[var(--warn)]/80"
          title={'Depende de: ' + blocked.titles.join(', ')}
        >
          Depende de: {blocked.titles.join(', ')}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {/* Chip de ORGANIZACION en la tarjeta, ANTES del de proyecto (cambio de implementación):
            asi se lee "acme · example-suite" de un vistazo sin abrir la tarea. */}
        {(() => {
          const oid = taskOrg(task, projectsV2)
          if (!oid) return null
          const ch = orgChip(oid, organizations)
          return (
            <span className={ch.className} style={ch.style} title="Organización">
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ backgroundColor: ch.dotColor }}
              />
              {orgLabel(oid, organizations)}
            </span>
          )
        })()}
        {task.project && (
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${projectChip(
              task.project
            )}`}
          >
            {task.project}
          </span>
        )}
        {progress && progress.total > 0 && (
          <span
            className="inline-flex items-center gap-1 rounded bg-surface-hover px-1.5 py-0.5 text-[10px] tabular-nums text-fg-subtle"
            title="Subtareas completadas"
          >
            <ListChecks size={11} />
            {progress.done}/{progress.total}
          </span>
        )}
        {task.assignee && (
          <span className="text-[10px] text-fg-subtle">@{task.assignee}</span>
        )}
        {task.subagent && (
          <span
            className="rounded bg-surface-hover px-1.5 py-0.5 text-[10px] font-medium text-fg-subtle"
            title="Asignado (subagente)"
          >
            {task.subagent}
          </span>
        )}
        {task.due_date && (
          <span className="ml-auto text-[10px] text-fg-subtle">{task.due_date}</span>
        )}
      </div>
    </motion.div>
  )
}

// ---------- Columna ----------
function Column({
  status,
  label,
  tasks,
  onCardClick,
  cardMeta,
  dim = false,
}: {
  status: TaskStatus
  label: string
  tasks: Task[]
  onCardClick: (t: Task) => void
  cardMeta: (t: Task) => {
    progress?: { done: number; total: number }
    blocked?: { count: number | null; titles: string[] }
    ready?: boolean
  }
  /** Columna atenuada (ej: Canceladas, tareas descartadas). */
  dim?: boolean
}) {
  const { setNodeRef, isOver } = useDroppable({ id: status })
  return (
    /* MOVIL: `w-full` y SIN `min-w-[260px]`/`flex-1` — el padre pasa a
       `flex-col`, asi que cada columna es una seccion a lo ancho completo. El
       `min-w-[260px]` original es lo que forzaba 6x260px=1560px de fila y
       aplastaba las tarjetas en 360px CSS. Desde lg vuelve identico. */
    <div className={`flex w-full min-w-0 flex-col lg:min-w-[260px] lg:flex-1 ${dim ? 'opacity-60' : ''}`}>
      <div className="mb-2 flex items-center gap-2 px-1">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">
          {label}
        </span>
        <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
          {tasks.length}
        </span>
      </div>
      <div
        ref={setNodeRef}
        /* min-h chico en movil: apilado en vertical, 6 columnas con 120px de
           minimo obligan a scrollear por huecos vacios antes de llegar al
           contenido. En lg vuelve a 120px (las columnas son hermanas). */
        className={`flex min-h-[64px] flex-1 flex-col gap-2 rounded-lg border p-2 transition-colors lg:min-h-[120px] ${
          isOver
            ? 'border-accent/60 bg-accent/5'
            : 'border-[var(--border-subtle)] glass'
        }`}
      >
        <AnimatePresence initial={false}>
          {tasks.map((t) => {
            const meta = cardMeta(t)
            return (
              <TaskCard
                key={t.id}
                task={t}
                onClick={() => onCardClick(t)}
                progress={meta.progress}
                blocked={meta.blocked}
                ready={meta.ready}
              />
            )
          })}
        </AnimatePresence>
        {tasks.length === 0 && (
          <div className="flex flex-1 items-center justify-center py-6 text-[11px] text-fg-subtle/60">
            —
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- Tablero ----------
export default function Board() {
  const currentUser = useQuery({ queryKey: ['me'], queryFn: fetchMe }).data
  const qc = useQueryClient()
  const toast = useToast()
  const PROJECTS = useAppStore((s) => s.meta?.projects ?? EMPTY_PROJECTS)
  // Jerarquia Organizacion -> Proyecto (cambio de implementación).
  const ORGS = useAppStore((s) => s.meta?.organizations)
  const PROJECTS_V2 = useAppStore((s) => s.meta?.projects_v2)
  // Filtro por agente/equipo, con admin primero (pedido ). El Board es la
  // vista donde se ve el FLUJO, asi que aca importa poder aislar "que me toca a mi".
  const LEADS = useAppStore((s) => s.meta?.leads) ?? []
  const SUBS_BY_TEAM = useAppStore((s) => s.meta?.subagents_by_team) ?? {}
  const [whoFilter, setWhoFilter] = useState('Todos')
  const [orgFilter, setOrgFilter] = useState('Todos')
  // Proyectos del selector, ACOTADOS a la organizacion elegida. Con 49 proyectos
  // y varias variantes del mismo nombre, listarlos todos juntos es el desorden.
  const projectChoices = useMemo(
    () => projectOptions(orgFilter === 'Todos' ? '' : orgFilter, PROJECTS_V2, PROJECTS),
    [orgFilter, PROJECTS_V2, PROJECTS]
  )
  // Filtro de proyecto: estado compartido en el shell store, para que Inicio
  // pueda navegar al Tablero con un proyecto ya filtrado.
  const projectFilter = useShellStore((s) => s.boardProjectFilter)
  const setProjectFilter = useShellStore((s) => s.setBoardProjectFilter)
  const [monthCursor, setMonthCursor] = useState<YearMonth>(currentYearMonth)
  const [showCancelled, setShowCancelled] = useState<boolean>(() => {
    try {
      return localStorage.getItem(CANCELLED_PREF_KEY) === '1'
    } catch {
      return false
    }
  })
  // Filtro de proyecto (tag `proyecto/<n>`), recuperado por-ámbito.
  const [projectTag, setProjectTag] = useState<string>(() =>
    loadProjectTag(projectFilter)
  )
  // Al cambiar de ámbito, recuperar el proyecto guardado para ese ámbito.
  useEffect(() => {
    setProjectTag(loadProjectTag(projectFilter))
  }, [projectFilter])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const openTaskTab = useShellStore((s) => s.openTaskPanel)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
  })

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } })
  )

  const tasks = useMemo(() => data?.tasks ?? [], [data])

  // Proyectos presentes en el ámbito actual = tags distintos `proyecto/*`.
  const availableProjects = useMemo(() => {
    const set = new Set<string>()
    for (const t of tasks) {
      if (projectFilter !== 'Todos' && t.project !== projectFilter) continue
      for (const tag of t.tags ?? []) {
        if (tag.startsWith(PROJECT_TAG_PREFIX)) set.add(tag)
      }
    }
    return [...set].sort()
  }, [tasks, projectFilter])

  // Si el proyecto guardado ya no existe en el ámbito, no se aplica filtro.
  const effectiveTag =
    projectTag !== 'Todos' && availableProjects.includes(projectTag)
      ? projectTag
      : 'Todos'

  // IDs (del ámbito) que llevan el tag de proyecto, para incluir subtareas.
  const taggedIds = useMemo(() => {
    if (effectiveTag === 'Todos') return null
    const s = new Set<string>()
    for (const t of tasks) {
      if (projectFilter !== 'Todos' && t.project !== projectFilter) continue
      if ((t.tags ?? []).includes(effectiveTag)) s.add(t.id)
    }
    return s
  }, [tasks, projectFilter, effectiveTag])

  const filtered = useMemo(
    () =>
      tasks.filter(
        (t) =>
          COLUMN_STATUSES.includes(t.status) &&
          (orgFilter === 'Todos' || taskOrg(t, PROJECTS_V2) === orgFilter) &&
          (projectFilter === 'Todos' || t.project === projectFilter) &&
          (whoFilter === 'Todos' ||
            (whoFilter.startsWith('equipo:')
              ? [whoFilter.slice(7), ...(SUBS_BY_TEAM[whoFilter.slice(7)] ?? [])].includes(
                  t.assignee || ''
                )
              : (t.assignee || '') === whoFilter.slice(7))) &&
          (taggedIds === null ||
            taggedIds.has(t.id) ||
            (t.parent_id !== null && taggedIds.has(t.parent_id)))
      ),
    [tasks, orgFilter, projectFilter, whoFilter, taggedIds, PROJECTS_V2, SUBS_BY_TEAM]
  )

  const byStatus = useMemo(() => {
    const map: Record<string, Task[]> = {
      todo: [],
      in_progress: [],
      blocked: [],
      done: [],
      cancelled: [],
    }
    for (const t of filtered) map[t.status]?.push(t)
    return map
  }, [filtered])

  // "Terminadas" y "Canceladas" se acotan al mes seleccionado (por updated_at,
  // zona del panel). todo / in_progress / blocked se muestran siempre.
  const doneThisMonth = useMemo(
    () =>
      (byStatus.done ?? []).filter((t) => {
        const ym = tsYearMonth(t.updated_at)
        return ym !== null && sameYearMonth(ym, monthCursor)
      }),
    [byStatus, monthCursor]
  )

  const cancelledThisMonth = useMemo(
    () =>
      (byStatus.cancelled ?? []).filter((t) => {
        const ym = tsYearMonth(t.updated_at)
        return ym !== null && sameYearMonth(ym, monthCursor)
      }),
    [byStatus, monthCursor]
  )

  const isCurrentMonth = sameYearMonth(monthCursor, currentYearMonth())

  // Progreso de subtareas: se computa client-side desde la lista de tareas
  // (parent_id ya viene en /api/tasks). /api/tasks ahora incluye cancelled,
  // así que las canceladas se excluyen aquí para no inflar el total.
  const progressByParent = useMemo(() => {
    const map = new Map<string, { done: number; total: number }>()
    for (const t of tasks) {
      if (!t.parent_id) continue
      if (t.status === 'cancelled') continue
      const cur = map.get(t.parent_id) ?? { done: 0, total: 0 }
      cur.total += 1
      if (t.status === 'done') cur.done += 1
      map.set(t.parent_id, cur)
    }
    return map
  }, [tasks])

  // "Listas para empezar" (todo con todas sus deps done): 1 sola query.
  const { data: readyData } = useQuery({
    queryKey: ['tasks_ready', projectFilter],
    queryFn: () =>
      fetchTasksReady(projectFilter !== 'Todos' ? projectFilter : undefined),
  })
  const readySet = useMemo(
    () => new Set((readyData?.tasks_ready ?? []).map((t) => t.id)),
    [readyData]
  )

  // Tareas para las que pedimos task_deps y así mostrar de qué dependen:
  //  - status 'blocked': siempre (mostrar el/los título(s) de la dependencia).
  //  - status 'todo' que NO está lista: tiene ≥1 dep no-done (candado + conteo).
  const blockedIds = useMemo(() => {
    const ids = new Set<string>()
    for (const t of filtered) {
      if (t.status === 'blocked') ids.add(t.id)
      else if (t.status === 'todo' && readyData && !readySet.has(t.id))
        ids.add(t.id)
    }
    return [...ids]
  }, [filtered, readySet, readyData])

  const depQueries = useQueries({
    queries: blockedIds.map((id) => ({
      queryKey: ['task_deps', id],
      queryFn: () => fetchTaskDeps(id),
      staleTime: 30_000,
    })),
  })

  const blockedInfo = useMemo(() => {
    const map = new Map<string, { count: number | null; titles: string[] }>()
    blockedIds.forEach((id, i) => {
      const d = depQueries[i]?.data
      if (d) {
        const pending = d.blocked_by.filter((b) => b.status !== 'done')
        map.set(id, {
          count: pending.length,
          titles: pending.map((b) => b.title),
        })
      } else {
        map.set(id, { count: null, titles: [] })
      }
    })
    return map
  }, [blockedIds, depQueries])

  const cardMeta = useMemo(
    () => (t: Task) => ({
      progress: progressByParent.get(t.id),
      blocked: blockedInfo.has(t.id) ? blockedInfo.get(t.id)! : undefined,
      ready: t.status === 'todo' && readySet.has(t.id),
    }),
    [progressByParent, blockedInfo, readySet]
  )

  const activeTask = activeId ? tasks.find((t) => t.id === activeId) ?? null : null

  function onDragStart(e: DragStartEvent) {
    setActiveId(String(e.active.id))
  }

  async function onDragEnd(e: DragEndEvent) {
    setActiveId(null)
    const { active, over } = e
    if (!over) return
    const id = String(active.id)
    const newStatus = String(over.id) as TaskStatus
    const task = tasks.find((t) => t.id === id)
    if (!task || task.status === newStatus) return
    if (!BOARD_STATUSES.includes(newStatus)) return

    // Optimista: mover la tarjeta ya en el cache.
    qc.setQueryData<TasksResponse>(['tasks'], (prev) =>
      prev
        ? {
            tasks: prev.tasks.map((t) =>
              t.id === id
                ? {
                    ...t,
                    status: newStatus,
                    ...(newStatus === 'done'
                      ? { updated_at: new Date().toISOString() }
                      : {}),
                  }
                : t
            ),
          }
        : prev
    )
    try {
      const res = await taskUpdate({ id, status: newStatus })
      if (newStatus === 'done' && res.unblocked && res.unblocked.length) {
        toast('Desbloqueaste: ' + res.unblocked.map((u) => u.title).join(', '))
      }
      // El cambio de estado altera qué está listo y los conteos de bloqueo.
      qc.invalidateQueries({ queryKey: ['tasks_ready'] })
      qc.invalidateQueries({ queryKey: ['task_deps'] })
    } catch {
      toast('No se pudo mover la tarea', 'err')
      qc.invalidateQueries({ queryKey: ['tasks'] })
    }
  }

  function selectProjectTag(tag: string) {
    setProjectTag(tag)
    saveProjectTag(projectFilter, tag)
  }

  function toggleCancelled() {
    setShowCancelled((v) => {
      const next = !v
      try {
        localStorage.setItem(CANCELLED_PREF_KEY, next ? '1' : '0')
      } catch {
        /* localStorage no disponible: se ignora la persistencia */
      }
      return next
    })
  }

  // Columnas visibles: 'Canceladas' solo si el toggle esta activo.
  const visibleColumns = showCancelled
    ? COLUMNS
    : COLUMNS.filter((c) => c.key !== 'cancelled')

  function openCreate() {
    setEditTask(null)
    setDialogOpen(true)
  }
  // ⚠️ : abrir una tarea EXISTENTE ya no usa el modal, abre un TAB
  // (pedido de admin: "pincho la tarea se abre al lado derecho... y si tiene un
  // archivo al pincharlo se abre a la derecha de este"). Con el modal, abrir un
  // documento relacionado TAPABA la tarea. El modal se conserva SOLO para "nueva
  // tarea", donde no hay nada al costado que valga la pena mirar en paralelo.

  function openEdit(t: Task) {

    openTaskTab(t.id, t.title)

  }

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      {/* Toolbar: ya venia con `flex-wrap`, lo que faltaba era que los
          controles no se pasen de 360px y sean tocables. */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-strong)] px-3 py-2 lg:gap-3 lg:px-5 lg:py-3">
        <h1 className="text-sm font-semibold text-fg">Tablero</h1>
        {/* Filtro de ORGANIZACION (cambio de implementación). Al cambiar de org se resetea el
            proyecto, porque el elegido puede no pertenecer a la org nueva. */}
        {ORGS && ORGS.length > 0 && (
          <div className="flex max-w-full items-center gap-1 overflow-x-auto rounded-md bg-surface p-0.5 lg:overflow-visible">
            {['Todos', ...ORGS.map((o) => o.id)].map((oid) => (
              <button
                key={oid}
                onClick={() => {
                  setOrgFilter(oid)
                  setProjectFilter('Todos')
                }}
                className={`min-h-[44px] shrink-0 whitespace-nowrap rounded px-3 py-1 text-xs font-medium transition lg:min-h-0 lg:px-2.5 ${
                  orgFilter === oid
                    ? 'bg-accent-bg text-fg'
                    : 'text-fg-subtle hover:text-fg'
                }`}
              >
                {oid === 'Todos' ? 'Todas' : orgLabel(oid, ORGS)}
              </button>
            ))}
          </div>
        )}
        {/* PROYECTO: desplegable en vez de fila de botones (admin ).
            Con 22 proyectos activos la fila se desbordaba. Mismo criterio que en
            TaskFilterBar, que es el que usan Lista/Calendario/Timeline. */}
        <select
          value={whoFilter}
          onChange={(e) => setWhoFilter(e.target.value)}
          /* text-[16px]: por debajo de 16px Safari iOS hace zoom al enfocar el
             control y descuadra toda la vista. En lg vuelve a 12px. */
          className="min-h-[44px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[16px] text-fg lg:min-h-0 lg:w-auto lg:text-xs"
          title="Filtrar por equipo completo o por un agente"
        >
          <option value="Todos">Todos los agentes</option>
          {currentUser?.name && <option value={`agente:${currentUser.name}`}>★ Mis tareas ({currentUser.name})</option>}
          {LEADS.map((l) => (
            <optgroup key={l} label={l}>
              <option value={`equipo:${l}`}>▸ Equipo {l} (con subagentes)</option>
              <option value={`agente:${l}`}>{l} (solo el líder)</option>
              {(SUBS_BY_TEAM[l] ?? []).map((sub) => (
                <option key={sub} value={`agente:${sub}`}>
                  {sub.startsWith(`${l}-`) ? sub.slice(l.length + 1) : sub}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <select
          value={projectFilter}
          onChange={(e) => setProjectFilter(e.target.value)}
          className="min-h-[44px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[16px] text-fg lg:min-h-0 lg:w-auto lg:max-w-[260px] lg:text-xs"
          title={
            orgFilter === 'Todos'
              ? 'Proyecto (todos los de la flota)'
              : `Proyecto — acotado a ${orgLabel(orgFilter, ORGS)}`
          }
        >
          <option value="Todos">{`Todos los proyectos (${projectChoices.length})`}</option>
          {projectChoices.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        {availableProjects.length > 0 && (
          <div className="flex max-w-full items-center gap-1 overflow-x-auto rounded-md bg-surface p-0.5 lg:overflow-visible">
            <Tag size={12} className="ml-1 mr-0.5 text-fg-subtle" />
            {['Todos', ...availableProjects].map((tag) => {
              const label =
                tag === 'Todos' ? 'Todos' : tag.slice(PROJECT_TAG_PREFIX.length)
              const active = effectiveTag === tag
              return (
                <button
                  key={tag}
                  onClick={() => selectProjectTag(tag)}
                  className={`min-h-[44px] shrink-0 whitespace-nowrap rounded px-3 py-1 text-xs transition lg:min-h-0 lg:px-2.5 ${
                    active
                      ? 'bg-accent-bg text-fg'
                      : 'text-fg-subtle hover:text-fg'
                  }`}
                >
                  {label}
                </button>
              )
            })}
          </div>
        )}
        <div className="flex items-center gap-1 rounded-md bg-surface p-0.5 lg:ml-auto">
          <button
            onClick={() => setMonthCursor((m) => addMonths(m, -1))}
            title="Mes anterior"
            aria-label="Mes anterior"
            className="flex h-11 w-11 items-center justify-center rounded text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:h-7 lg:w-7"
          >
            <ChevronLeft size={15} />
          </button>
          <button
            onClick={() => setMonthCursor(currentYearMonth())}
            title={isCurrentMonth ? 'Mes actual' : 'Volver al mes actual'}
            className={`min-h-[44px] min-w-[112px] rounded px-2 py-1 text-center text-xs font-medium tabular-nums transition lg:min-h-0 ${
              isCurrentMonth
                ? 'text-fg'
                : 'text-accent hover:bg-surface-hover'
            }`}
          >
            {MONTH_NAMES[monthCursor.month - 1]} {monthCursor.year}
          </button>
          <button
            onClick={() => setMonthCursor((m) => addMonths(m, 1))}
            title="Mes siguiente"
            aria-label="Mes siguiente"
            className="flex h-11 w-11 items-center justify-center rounded text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:h-7 lg:w-7"
          >
            <ChevronRight size={15} />
          </button>
        </div>
        <button
          onClick={toggleCancelled}
          aria-pressed={showCancelled}
          title={showCancelled ? 'Ocultar canceladas' : 'Mostrar canceladas'}
          className={`flex min-h-[44px] items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition lg:min-h-0 lg:px-2.5 ${
            showCancelled
              ? 'bg-accent-bg text-accent'
              : 'bg-surface text-fg-subtle hover:text-fg'
          }`}
        >
          <Ban size={14} />
          Canceladas
        </button>
        <button
          onClick={openCreate}
          className="min-h-[44px] rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover lg:min-h-0"
        >
          + Nueva tarea
        </button>
      </div>

      {/* Board */}
      <div className="min-h-0 flex-1 overflow-auto p-3 lg:p-5">
        {isLoading && (
          <p className="text-xs text-fg-subtle">Cargando tareas…</p>
        )}
        {isError && (
          <p className="text-xs text-red-400">No se pudieron cargar las tareas.</p>
        )}
        {!isLoading && !isError && (
          <DndContext
            sensors={sensors}
            collisionDetection={pointerWithin}
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
          >
            {/* `flex-col` en movil = una sola columna vertical scrolleable;
                `lg:flex-row` restituye el kanban lado a lado. */}
            <div className="flex flex-col gap-4 lg:flex-row">
              {visibleColumns.map((c) => (
                <Column
                  key={c.key}
                  status={c.key}
                  label={c.label}
                  dim={c.dim}
                  tasks={
                    c.key === 'done'
                      ? doneThisMonth
                      : c.key === 'cancelled'
                        ? cancelledThisMonth
                        : byStatus[c.key] ?? []
                  }
                  onCardClick={openEdit}
                  cardMeta={cardMeta}
                />
              ))}
            </div>
            <DragOverlay>
              {activeTask ? <TaskCard task={activeTask} overlay /> : null}
            </DragOverlay>
          </DndContext>
        )}
      </div>

      <TaskDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        task={editTask}
        initialProject={projectFilter !== 'Todos' ? projectFilter : undefined}
        onOpenTask={openEdit}
      />
    </div>
  )
}
