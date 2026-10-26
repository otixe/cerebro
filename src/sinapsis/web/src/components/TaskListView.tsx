import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { fetchTasks, fetchMe, type Task, type TaskStatus } from '../lib/api'
import TaskDialog from './TaskDialog'
import TaskFilterBar from './TaskFilterBar'
import { useAppStore, useShellStore } from '../store'
import {
  projectChip,
  orgChip,
  orgLabel,
  taskOrg,
  statusMeta,
  priorityMeta,
  PRIORITY_ORDER,
  STATUS_ORDER,
  STATUS_META,
  formatDueShort,
} from './taskShared'

type SortKey = 'title' | 'organization' | 'project' | 'status' | 'priority' | 'assignee' | 'due_date' | 'created_at'
type SortDir = 'asc' | 'desc'

const STATUS_FILTERS: TaskStatus[] = [
  // 'submitted' faltaba: el backend lo usa (8 tareas hoy) pero no habia forma de
  // filtrarlas en la UI. Mismo origen que el bug de la pantalla en negro.
  'todo', 'in_progress', 'submitted', 'in_review', 'blocked', 'deferred', 'done',
]

const COLUMNS: { key: SortKey; label: string; className?: string }[] = [
  { key: 'title', label: 'Título' },
  { key: 'organization', label: 'Organización' },
  { key: 'project', label: 'Proyecto' },
  { key: 'status', label: 'Estado' },
  { key: 'priority', label: 'Prioridad' },
  { key: 'assignee', label: 'Asignado' },
  { key: 'due_date', label: 'Vence' },
  { key: 'created_at', label: 'Creada' },
]

export default function TaskListView() {
  const currentUser = useQuery({ queryKey: ['me'], queryFn: fetchMe }).data
  const { data, isLoading, isError } = useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
  })

  const [projectFilter, setProjectFilter] = useState('Todos')
  const [orgFilter, setOrgFilter] = useState('Todos')
  // 'Todos' | 'equipo:<lider>' | 'agente:<nombre>'
  const [whoFilter, setWhoFilter] = useState('Todos')
  const [q, setQ] = useState('')
  const [statusFilter, setStatusFilter] = useState<'Todos' | TaskStatus>('Todos')
  const [showCancelled, setShowCancelled] = useState(false)
  // Orden por DEFECTO: lo mas reciente primero (admin : "en la seccion de
  // listas que predeterminada muestre primero las mas recientes"). Antes ordenaba
  // por status asc, que dejaba arriba las 'todo' viejas.
  const [sortKey, setSortKey] = useState<SortKey>('created_at')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const openTaskTab = useShellStore((s) => s.openTaskPanel)

  // Jerarquia Organizacion -> Proyecto (cambio de implementación). `meta` puede no traer
  // `organizations`/`projects_v2` si el backend todavia es el viejo: los helpers
  // de taskShared degradan solos a `meta.projects`.
  const meta = useAppStore((s) => s.meta)
  const organizations = meta?.organizations
  const projectsV2 = meta?.projects_v2
  // Filtro por agente/equipo (pedido de admin : "en la seccion de lista de
  // las tareas agregar para filtrar por agentes titular y subagentes").
  const LEADS = meta?.leads ?? []
  const SUBS_BY_TEAM = meta?.subagents_by_team ?? {}

  const tasks = useMemo(() => data?.tasks ?? [], [data])

  const filtered = useMemo(
    () =>
      tasks.filter(
        (t) =>
          (showCancelled || t.status !== 'cancelled') &&
          (orgFilter === 'Todos' || taskOrg(t, projectsV2) === orgFilter) &&
          (projectFilter === 'Todos' || t.project === projectFilter) &&
          (statusFilter === 'Todos' || t.status === statusFilter) &&
          (q.trim() === '' ||
            t.id.toLowerCase().startsWith(q.trim().toLowerCase()) ||
            t.title.toLowerCase().includes(q.trim().toLowerCase())) &&
          (whoFilter === 'Todos' ||
            (whoFilter.startsWith('equipo:')
              ? [
                  whoFilter.slice(7),
                  ...(SUBS_BY_TEAM[whoFilter.slice(7)] ?? []),
                ].includes(t.assignee || '')
              : (t.assignee || '') === whoFilter.slice(7)))
      ),
    [tasks, orgFilter, projectFilter, statusFilter, whoFilter, q, showCancelled, projectsV2, SUBS_BY_TEAM]
  )

  const sorted = useMemo(() => {
    const arr = [...filtered]
    const dir = sortDir === 'asc' ? 1 : -1
    arr.sort((a, b) => {
      let cmp = 0
      switch (sortKey) {
        case 'priority':
          cmp =
            (PRIORITY_ORDER[a.priority || 'medium'] ?? 1) -
            (PRIORITY_ORDER[b.priority || 'medium'] ?? 1)
          break
        case 'status':
          cmp = (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9)
          break
        case 'due_date':
          // Sin fecha va al final en asc (usamos '~' como sentinel alto).
          cmp = (a.due_date || '~').localeCompare(b.due_date || '~')
          break
        case 'organization':
          // Ordena por el NOMBRE visible ('acme'), no por el slug ('acme'),
          // y resuelve la org efectiva (propia o derivada del proyecto).
          cmp = orgLabel(taskOrg(a, projectsV2), organizations).localeCompare(
            orgLabel(taskOrg(b, projectsV2), organizations),
            'es',
            { sensitivity: 'base' }
          )
          break
        default:
          cmp = String(a[sortKey] || '').localeCompare(
            String(b[sortKey] || ''),
            'es',
            { sensitivity: 'base' }
          )
      }
      if (cmp === 0) cmp = a.title.localeCompare(b.title, 'es')
      return cmp * dir
    })
    return arr
  }, [filtered, sortKey, sortDir, projectsV2, organizations])

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortKey(k)
      setSortDir('asc')
    }
  }

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
      <TaskFilterBar
        title="Lista"
        projectFilter={projectFilter}
        onProjectFilter={setProjectFilter}
        orgFilter={orgFilter}
        onOrgFilter={setOrgFilter}
        showCancelled={showCancelled}
        onToggleCancelled={() => setShowCancelled((v) => !v)}
        onNew={openCreate}
      >
        {/* BUSQUEDA (pedido de admin : "no hay una forma de buscar por el
            numero de la tarea en los menu"). Matchea por PREFIJO DE ID (los 8 chars
            que se muestran en el tablero y que usamos para hablar de una tarea) y
            tambien por titulo, para que sirva escribiendo cualquiera de los dos. */}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar por id (8 chars) o título…"
          /* text-[16px] en movil NO es estetico: por debajo de 16px Safari iOS
             hace zoom automatico al enfocar el input y descuadra la vista. En
             lg vuelve a 12px. min-h-[44px] para que sea tocable. */
          className="min-h-[44px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[16px] text-fg placeholder:text-fg-subtle lg:min-h-0 lg:w-56 lg:text-xs"
        />
        {/* Filtro por agente / equipo */}
        <select
          value={whoFilter}
          onChange={(e) => setWhoFilter(e.target.value)}
          className="min-h-[44px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[16px] text-fg lg:min-h-0 lg:w-auto lg:text-xs"
          title="Filtrar por equipo completo o por un agente"
        >
          <option value="Todos">Todos los agentes</option>
          {/* admin primero y destacado (pedido ): no aparecia porque el
              desplegable se armaba de `leads` + `subagents_by_team`, y admin no es
              lider ni subagente (team vacio, is_lead=false). Tenia 9 tareas abiertas
              INFILTRABLES desde la UI. Va arriba porque es el filtro que mas se usa:
              "que me falta decidir a mi". */}
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
        <div className="-mx-1 flex max-w-full items-center gap-1 overflow-x-auto rounded-md bg-surface p-0.5 px-1 lg:mx-0 lg:overflow-visible lg:px-0.5">
          {(['Todos', ...STATUS_FILTERS] as const).map((s) => {
            const label = s === 'Todos' ? 'Todos' : STATUS_META[s].label
            const active = statusFilter === s
            return (
              <button
                key={s}
                onClick={() => setStatusFilter(s)}
                className={`min-h-[44px] shrink-0 whitespace-nowrap rounded px-3 py-1 text-xs transition lg:min-h-0 lg:px-2.5 ${
                  active ? 'bg-accent-bg text-fg' : 'text-fg-subtle hover:text-fg'
                }`}
              >
                {label}
              </button>
            )
          })}
        </div>
      </TaskFilterBar>

      <div className="min-h-0 flex-1 overflow-auto">
        {isLoading && (
          <p className="p-5 text-xs text-fg-subtle">Cargando tareas…</p>
        )}
        {isError && (
          <p className="p-5 text-xs text-red-400">
            No se pudieron cargar las tareas.
          </p>
        )}
        {!isLoading && !isError && (
          /* `hidden lg:table`: la tabla de 8 columnas necesita ~720px y en 360px
             CSS no hay forma honesta de encogerla. Por debajo de lg la reemplaza
             la lista de tarjetas de mas abajo (MISMO `sorted`, MISMO onClick). */
          <table className="hidden w-full min-w-[720px] border-collapse text-sm lg:table">
            <thead className="sticky top-0 z-10 glass-strong">
              <tr className="border-b border-[var(--border-strong)]">
                {COLUMNS.map((c) => {
                  const active = sortKey === c.key
                  return (
                    <th
                      key={c.key}
                      onClick={() => toggleSort(c.key)}
                      className="cursor-pointer select-none px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-fg-muted transition hover:text-fg"
                    >
                      <span className="inline-flex items-center gap-1">
                        {c.label}
                        {active &&
                          (sortDir === 'asc' ? (
                            <ChevronUp size={12} className="text-accent" />
                          ) : (
                            <ChevronDown size={12} className="text-accent" />
                          ))}
                      </span>
                    </th>
                  )
                })}
              </tr>
            </thead>
            <tbody>
              {sorted.map((t) => {
                const sm = statusMeta(t.status)
                const pm = priorityMeta(t.priority)
                return (
                  <tr
                    key={t.id}
                    onClick={() => openEdit(t)}
                    className="cursor-pointer border-b border-[var(--border-subtle)] transition hover:bg-surface-hover"
                  >
                    <td className="max-w-[380px] px-4 py-2.5">
                      <span className="line-clamp-1 font-medium text-fg">
                        {t.title}
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      {(() => {
                        // Celda de ORGANIZACION, separada de la de proyecto.
                        // Chip pill con punto de color propio de la org, para
                        // distinguirlo a simple vista del badge plano de proyecto.
                        const oid = taskOrg(t, projectsV2)
                        if (!oid) return null
                        const ch = orgChip(oid, organizations)
                        return (
                          <span className={ch.className} style={ch.style}>
                            <span
                              className="h-1.5 w-1.5 rounded-full"
                              style={{ backgroundColor: ch.dotColor }}
                            />
                            {orgLabel(oid, organizations)}
                          </span>
                        )
                      })()}
                    </td>
                    <td className="px-4 py-2.5">
                      {t.project && (
                        <span
                          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${projectChip(
                            t.project
                          )}`}
                        >
                          {t.project}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                        <span className={`h-1.5 w-1.5 rounded-full ${sm.dot}`} />
                        <span className={`text-xs ${sm.text}`}>{sm.label}</span>
                      </span>
                    </td>
                    <td className="px-4 py-2.5">
                      <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                        <span className={`h-1.5 w-1.5 rounded-full ${pm.dot}`} />
                        <span className={`text-xs ${pm.text}`}>{pm.label}</span>
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs text-fg-subtle">
                      {t.subagent || (t.assignee ? `@${t.assignee}` : '—')}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-fg-subtle">
                      {formatDueShort(t.due_date) || '—'}
                    </td>
                    {/* Columna 'Creada': el orden por defecto de la lista usa este
                        campo (desc), asi que conviene que se vea. */}
                    <td className="whitespace-nowrap px-4 py-2.5 text-xs tabular-nums text-fg-subtle">
                      {t.created_at ? formatDueShort(t.created_at.slice(0, 10)) : '—'}
                    </td>
                  </tr>
                )
              })}
              {sorted.length === 0 && (
                <tr>
                  <td
                    colSpan={COLUMNS.length}
                    className="px-4 py-10 text-center text-xs text-fg-subtle/60"
                  >
                    No hay tareas con estos filtros.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}

        {/* MOVIL (<lg): MISMA lista (`sorted`), otro PATRON. Una tarea = una
            tarjeta; el titulo manda y los metadatos van como chips debajo con
            su etiqueta. No hay scroll horizontal porque no hay columnas.
            El tap llama al MISMO openEdit que la fila de la tabla. */}
        {!isLoading && !isError && (
          <div className="flex flex-col gap-2 p-3 lg:hidden">
            {sorted.map((t) => {
              const sm = statusMeta(t.status)
              const pm = priorityMeta(t.priority)
              const oid = taskOrg(t, projectsV2)
              const och = oid ? orgChip(oid, organizations) : null
              return (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => openEdit(t)}
                  /* min-h-[44px] + gap-2 entre tarjetas: con las filas pegadas
                     un tap acierta la tarea equivocada (skill ui L155). */
                  className="flex min-h-[44px] w-full flex-col gap-2 rounded-lg border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] p-3 text-left transition active:bg-surface-hover"
                >
                  {/* El titulo fluye en varias lineas: en 360px CSS un
                      line-clamp-1 dejaba el titulo cortado a media palabra y
                      es el dato principal de la tarjeta. */}
                  <span className="break-words text-[15px] font-medium leading-snug text-fg">
                    {t.title}
                  </span>

                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                      <span className={`h-1.5 w-1.5 rounded-full ${sm.dot}`} />
                      <span className={`text-[12px] ${sm.text}`}>{sm.label}</span>
                    </span>
                    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                      <span className={`h-1.5 w-1.5 rounded-full ${pm.dot}`} />
                      <span className={`text-[12px] ${pm.text}`}>{pm.label}</span>
                    </span>
                    {och && (
                      <span className={och.className} style={och.style}>
                        <span
                          className="h-1.5 w-1.5 rounded-full"
                          style={{ backgroundColor: och.dotColor }}
                        />
                        {orgLabel(oid, organizations)}
                      </span>
                    )}
                    {t.project && (
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${projectChip(
                          t.project
                        )}`}
                      >
                        {t.project}
                      </span>
                    )}
                  </div>

                  {/* Etiqueta + valor: sin cabecera de tabla, un '—' suelto no
                      dice de que campo es. */}
                  <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-fg-subtle">
                    <span>
                      <span className="text-fg-subtle/60">Asignado: </span>
                      {t.subagent || (t.assignee ? `@${t.assignee}` : '—')}
                    </span>
                    <span className="tabular-nums">
                      <span className="text-fg-subtle/60">Vence: </span>
                      {formatDueShort(t.due_date) || '—'}
                    </span>
                    <span className="tabular-nums">
                      <span className="text-fg-subtle/60">Creada: </span>
                      {t.created_at ? formatDueShort(t.created_at.slice(0, 10)) : '—'}
                    </span>
                    <span className="font-mono text-fg-subtle/60">
                      {t.id.slice(0, 8)}
                    </span>
                  </div>
                </button>
              )
            })}
            {sorted.length === 0 && (
              <p className="px-1 py-10 text-center text-xs text-fg-subtle/60">
                No hay tareas con estos filtros.
              </p>
            )}
          </div>
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
