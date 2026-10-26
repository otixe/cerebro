import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { useAppStore, useShellStore } from '../store'
import { fetchTasks, fetchMe, type Task } from '../lib/api'
import TaskDialog from './TaskDialog'
import TaskFilterBar from './TaskFilterBar'
import {
  projectChip,
  statusMeta,
  priorityMeta,
  parseDueLocal,
  weekStart,
  weekLabel,
  toLocalISO,
  formatDueShort,
  todayISO,
  orgChip,
  orgLabel,
  taskOrg,
  STATUS_META,
  PRIORITY_META,
} from './taskShared'

interface WeekGroup {
  key: string // ISO del lunes
  label: string
  isCurrent: boolean
  tasks: Task[]
}

export default function TaskTimelineView() {
  const currentUser = useQuery({ queryKey: ['me'], queryFn: fetchMe }).data
  const { data, isLoading, isError } = useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
  })

  const meta = useAppStore((s) => s.meta)
  const projectsV2 = meta?.projects_v2
  const organizations = meta?.organizations
  // Roster para el filtro por agente/equipo (pedido de admin ).
  const LEADS = meta?.leads ?? []
  const SUBS_BY_TEAM = meta?.subagents_by_team ?? {}
  const [orgFilter, setOrgFilter] = useState('Todos')
  const [projectFilter, setProjectFilter] = useState('Todos')
  // 'Todos' | 'equipo:<lider>' | 'agente:<nombre>'
  const [whoFilter, setWhoFilter] = useState('Todos')
  const [showLeyenda, setShowLeyenda] = useState(false)
  const [showCancelled, setShowCancelled] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const openTaskTab = useShellStore((s) => s.openTaskPanel)

  const tasks = useMemo(() => data?.tasks ?? [], [data])

  const miembrosDe = (lider: string) => [lider, ...(SUBS_BY_TEAM[lider] ?? [])]

  const visible = useMemo(
    () =>
      tasks.filter((t) => {
        if (!showCancelled && t.status === 'cancelled') return false
        if (orgFilter !== 'Todos' && taskOrg(t, projectsV2) !== orgFilter) return false
        if (projectFilter !== 'Todos' && t.project !== projectFilter) return false
        if (whoFilter !== 'Todos') {
          const a = t.assignee || ''
          if (whoFilter.startsWith('equipo:')) {
            if (!miembrosDe(whoFilter.slice(7)).includes(a)) return false
          } else if (whoFilter.startsWith('agente:')) {
            if (a !== whoFilter.slice(7)) return false
          }
        }
        return true
      }),
    [tasks, orgFilter, projectFilter, whoFilter, showCancelled, projectsV2, SUBS_BY_TEAM]
  )

  // Semana actual (lunes) para resaltar.
  const currentWeekKey = useMemo(() => {
    const t = parseDueLocal(todayISO())
    return t ? toLocalISO(weekStart(t)) : ''
  }, [])

  // Agrupar por semana (lunes) ordenado asc; sin fecha al final.
  //
  // FIX  (admin: "aplicar en la seccion de timeline los cambios del
  // calendario, ya que hay varias que no tienen fecha y en ese menu es muy
  // importante que salgan en la fecha de creacion o en la otra").
  // MISMA CAUSA que en el calendario: agrupaba SOLO por `due_date`, que es la fecha
  // COMPROMETIDA y es manual y opcional. Medido: 108 de 114 tareas abiertas no la
  // tienen, pero las 114 tienen `created_at` => el timeline mandaba casi todo a la
  // bandeja "sin fecha" y quedaba practicamente vacio.
  // AHORA: due_date si existe (es el compromiso, tiene prioridad); si no,
  // created_at. Se marca cual se uso (`_dateSource`) para no confundir un
  // compromiso con una fecha de alta: el render lo distingue con borde punteado.
  const fechaDe = (t: Task): { iso: string | null; fuente: 'due' | 'created' } =>
    t.due_date
      ? { iso: t.due_date, fuente: 'due' }
      : { iso: t.created_at ? t.created_at.slice(0, 10) : null, fuente: 'created' }

  const { weeks, undated } = useMemo(() => {
    const map = new Map<string, WeekGroup>()
    const nofecha: Task[] = []
    for (const t of visible) {
      const { iso, fuente } = fechaDe(t)
      const d = parseDueLocal(iso)
      if (!d) {
        nofecha.push(t)
        continue
      }
      const monday = weekStart(d)
      const key = toLocalISO(monday)
      let g = map.get(key)
      if (!g) {
        g = {
          key,
          label: weekLabel(monday),
          isCurrent: key === currentWeekKey,
          tasks: [],
        }
        map.set(key, g)
      }
      // Anotacion no destructiva + `_sortDate` para ordenar por la fecha EFECTIVA,
      // no por due_date (que en la mayoria es null y colapsaba el orden interno).
      g.tasks.push({ ...t, _dateSource: fuente, _sortDate: iso } as Task)
    }
    // FIX  (admin: "el timeline que este ordenado desde el mas reciente al
    // mas antiguo, actualmente esta al reves"). Semanas DESCENDENTES: lo de esta
    // semana arriba, lo viejo abajo. Antes era ascendente y arrancaba en abril.
    const ordered = [...map.values()].sort((a, b) => b.key.localeCompare(a.key))
    for (const g of ordered) {
      // Dentro de cada semana, tambien lo mas reciente primero (mismo criterio).
      g.tasks.sort((a, b) => (b._sortDate || '').localeCompare(a._sortDate || ''))
    }
    return { weeks: ordered, undated: nofecha }
  }, [visible, currentWeekKey])

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

  const isEmpty = weeks.length === 0 && undated.length === 0

  return (
    <div className="flex h-full flex-col">
      <TaskFilterBar
        title="Timeline"
        projectFilter={projectFilter}
        orgFilter={orgFilter}
        onOrgFilter={setOrgFilter}
        onProjectFilter={setProjectFilter}
        showCancelled={showCancelled}
        onToggleCancelled={() => setShowCancelled((v) => !v)}
        onNew={openCreate}
      >
        {/* Filtro por agente / equipo (pedido de admin ) */}
        <select
          value={whoFilter}
          onChange={(e) => setWhoFilter(e.target.value)}
          className="rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-xs text-fg"
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
        <button
          onClick={() => setShowLeyenda((v) => !v)}
          aria-pressed={showLeyenda}
          title="Qué significan los colores"
          className={`flex items-center gap-1 rounded-md px-2 py-1.5 text-xs transition ${
            showLeyenda ? 'bg-accent-bg text-accent' : 'bg-surface text-fg-subtle hover:text-fg'
          }`}
        >
          <Info size={13} /> Leyenda
        </button>
      </TaskFilterBar>

      {/* LEYENDA. OJO: en el TIMELINE el punto es el ESTADO y el borde izquierdo la
          PRIORIDAD -- al reves que en el calendario, donde el punto es la prioridad.
          Se documenta explicitamente para que el mismo color no signifique dos cosas
          sin aviso. */}
      {showLeyenda && (
        <div className="shrink-0 border-b border-[var(--border-strong)] bg-surface/40 px-5 py-3">
          <div className="flex flex-wrap gap-x-8 gap-y-3 text-[11px]">
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Punto de color = Estado
              </div>
              <div className="flex flex-wrap gap-3">
                {Object.entries(STATUS_META).map(([k, v]) => (
                  <span key={k} className="inline-flex items-center gap-1.5 text-fg-subtle">
                    <span className={`h-2 w-2 rounded-full ${v.dot}`} />
                    {v.label}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Borde izquierdo = Prioridad
              </div>
              <div className="flex flex-wrap gap-3">
                {Object.entries(PRIORITY_META).map(([k, v]) => (
                  <span key={k} className="inline-flex items-center gap-1.5 text-fg-subtle">
                    <span className={`h-3 w-0 border-l-2 ${v.border}`} />
                    {v.label}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Fecha
              </div>
              <div className="flex flex-wrap gap-3 text-fg-subtle">
                <span>12 ago = fecha comprometida</span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-3 w-0 border-l-2 border-dashed border-[var(--border-subtle)]" />
                  ~12 ago = fecha de creación (sin compromiso)
                </span>
              </div>
            </div>
            {organizations && organizations.length > 0 && (
              <div>
                <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                  Organizaciones
                </div>
                <div className="flex flex-wrap gap-3">
                  {organizations.map((o) => (
                    <span key={o.id} className="inline-flex items-center gap-1.5 text-fg-subtle">
                      <span
                        className="h-2 w-2 rounded-full"
                        style={{ backgroundColor: orgChip(o.id, organizations).dotColor }}
                      />
                      {orgLabel(o.id, organizations)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto p-5">
        {isLoading && <p className="text-xs text-fg-subtle">Cargando tareas…</p>}
        {isError && (
          <p className="text-xs text-red-400">No se pudieron cargar las tareas.</p>
        )}
        {!isLoading && !isError && (
          <div className="mx-auto max-w-3xl">
            {isEmpty && (
              <p className="py-10 text-center text-xs text-fg-subtle/60">
                No hay tareas con estos filtros.
              </p>
            )}

            {/* Línea temporal por semana */}
            <div className="relative border-l border-[var(--border-strong)] pl-5">
              {weeks.map((g) => (
                <div key={g.key} className="relative mb-6">
                  {/* Nodo de la semana */}
                  <span
                    className={`absolute -left-[23px] top-1 h-2.5 w-2.5 rounded-full border-2 border-bg ${
                      g.isCurrent ? 'bg-accent' : 'bg-fg-faint'
                    }`}
                  />
                  <div className="mb-2 flex items-center gap-2">
                    <span
                      className={`text-xs font-semibold ${
                        g.isCurrent ? 'text-accent' : 'text-fg-muted'
                      }`}
                    >
                      {g.label}
                    </span>
                    {g.isCurrent && (
                      <span className="rounded bg-accent-bg px-1.5 py-0.5 text-[10px] font-medium text-fg">
                        Esta semana
                      </span>
                    )}
                    <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
                      {g.tasks.length}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {g.tasks.map((t) => (
                      <TimelineRow key={t.id} task={t} onClick={() => openEdit(t)} />
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {/* Sin fecha al final */}
            {undated.length > 0 && (
              <div className="mt-2">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-xs font-semibold text-fg-muted">
                    Sin fecha
                  </span>
                  <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
                    {undated.length}
                  </span>
                </div>
                <div className="flex flex-col gap-1.5">
                  {undated.map((t) => (
                    <TimelineRow key={t.id} task={t} onClick={() => openEdit(t)} />
                  ))}
                </div>
              </div>
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

function TimelineRow({ task, onClick }: { task: Task; onClick: () => void }) {
  // Jerarquia Organizacion -> Proyecto (cambio de implementación).
  const organizations = useAppStore((s) => s.meta?.organizations)
  const projectsV2 = useAppStore((s) => s.meta?.projects_v2)
  const sm = statusMeta(task.status)
  const pm = priorityMeta(task.priority)
  const due = formatDueShort(task.due_date)
  return (
    <button
      onClick={onClick}
      title={
        task._dateSource === 'created'
          ? `${task.title}\n(ubicada por fecha de CREACIÓN — sin fecha comprometida)`
          : task.due_date
            ? `${task.title}\n(fecha comprometida: ${task.due_date})`
            : task.title
      }
      className={`flex items-center gap-2 rounded-lg border border-[var(--border-strong)] border-l-2 ${pm.border} ${
        task._dateSource === 'created' ? 'border-l-dashed' : ''
      } bg-surface px-3 py-2 text-left transition hover:bg-surface-hover`}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${sm.dot}`} />
      <span className="line-clamp-1 flex-1 text-[13px] font-medium text-fg">
        {task.title}
      </span>
      {(() => {
        const oid = taskOrg(task, projectsV2)
        if (!oid) return null
        const ch = orgChip(oid, organizations)
        return (
          <span className={`shrink-0 ${ch.className}`} style={ch.style} title="Organización">
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
          className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${projectChip(
            task.project
          )}`}
        >
          {task.project}
        </span>
      )}
      {/* Fecha EFECTIVA: due_date si hay, si no la de creacion (con ~ para marcar
          que no es un compromiso). Antes solo mostraba due_date y el 95% de las
          tareas no mostraba ninguna fecha. */}
      {(due || task._sortDate) && (
        <span
          className="shrink-0 text-[11px] tabular-nums text-fg-subtle"
          title={task._dateSource === 'created' ? 'Fecha de creación' : 'Fecha comprometida'}
        >
          {due || `~${formatDueShort(task._sortDate)}`}
        </span>
      )}
    </button>
  )
}
