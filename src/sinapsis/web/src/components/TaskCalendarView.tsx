import { useMemo, useState } from 'react'
import { useAppStore, useShellStore } from '../store'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Info } from 'lucide-react'
import { fetchTasks, fetchMe, type Task } from '../lib/api'
import TaskDialog from './TaskDialog'
import TaskFilterBar from './TaskFilterBar'
import {
  priorityMeta,
  currentYearMonth,
  addMonths,
  sameYearMonth,
  todayISO,
  MONTH_NAMES,
  WEEKDAY_SHORT,
  PRIORITY_META,
  STATUS_META,
  type YearMonth,
  orgChip,
  taskOrg,
  orgLabel,
} from './taskShared'

/** Granularidad de la vista (pedido de admin ). */
type Zoom = 'dia' | 'semana' | 'mes' | 'anio'

/** Cantidad de días del mes. */
function daysInMonth(ym: YearMonth): number {
  return new Date(ym.year, ym.month, 0).getDate()
}

/** Índice (Lun=0 … Dom=6) del primer día del mes. */
function firstWeekdayIndex(ym: YearMonth): number {
  const dow = new Date(ym.year, ym.month - 1, 1).getDay() // Dom=0 … Sáb=6
  return (dow + 6) % 7
}

/** ISO local (YYYY-MM-DD) de un Date, sin corrimiento de zona. */
function isoOf(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** Lunes de la semana que contiene a `iso`. */
function mondayOf(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number)
  const dt = new Date(y, m - 1, d, 12, 0, 0)
  const dow = (dt.getDay() + 6) % 7 // Lun=0
  return new Date(y, m - 1, d - dow, 12, 0, 0)
}

/** Los 7 ISO de la semana que arranca en `monday`. */
function weekDays(monday: Date): string[] {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i, 12, 0, 0)
    return isoOf(d)
  })
}

function fmtDiaLargo(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Intl.DateTimeFormat('es-CL', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  }).format(new Date(y, m - 1, d, 12, 0, 0))
}

export default function TaskCalendarView() {
  const currentUser = useQuery({ queryKey: ['me'], queryFn: fetchMe }).data
  const { data, isLoading, isError } = useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
  })

  const meta = useAppStore((s) => s.meta)
  const organizations = meta?.organizations
  const projectsV2 = meta?.projects_v2
  // Roster para el filtro por agente/equipo (pedido de admin ).
  const LEADS = meta?.leads ?? []
  const SUBS_BY_TEAM = meta?.subagents_by_team ?? {}

  const [orgFilter, setOrgFilter] = useState('Todos')
  const [projectFilter, setProjectFilter] = useState('Todos')
  // Filtro por responsable: 'Todos' | 'equipo:<lider>' | 'agente:<nombre>'
  const [whoFilter, setWhoFilter] = useState('Todos')
  const [showCancelled, setShowCancelled] = useState(false)
  const [zoom, setZoom] = useState<Zoom>('mes')
  const [monthCursor, setMonthCursor] = useState<YearMonth>(currentYearMonth)
  const [dayCursor, setDayCursor] = useState<string>(todayISO)
  const [yearCursor, setYearCursor] = useState<number>(() => currentYearMonth().year)
  const [showLeyenda, setShowLeyenda] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const openTaskTab = useShellStore((s) => s.openTaskPanel)

  const today = todayISO()
  const isCurrentMonth = sameYearMonth(monthCursor, currentYearMonth())

  const tasks = useMemo(() => data?.tasks ?? [], [data])

  // Miembros del equipo elegido (para 'equipo:<lider>' incluye al líder).
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

  // ---------------------------------------------------------------------------
  // Fecha con la que se UBICA una tarea en el calendario.
  // FIX  (admin: "aún veo muchas sin fechas, ya que estas deberían tener
  // la fecha de creación; ¿por qué no la está tomando el calendario?").
  // CAUSA: usaba SOLO `due_date`, que es la fecha COMPROMETIDA y es manual y
  // opcional. Medido: 108 de 114 tareas abiertas no tienen due_date, pero las 114
  // tienen created_at => el calendario descartaba casi todo a "sin fecha".
  // AHORA: due_date si existe (es el compromiso, tiene prioridad); si no,
  // created_at. Se marca cuál se usó para no confundir compromiso con fecha de alta.
  // ---------------------------------------------------------------------------
  const fechaDe = (t: Task): { iso: string | null; fuente: 'due' | 'created' } =>
    t.due_date
      ? { iso: t.due_date, fuente: 'due' }
      : { iso: t.created_at ? t.created_at.slice(0, 10) : null, fuente: 'created' }

  /** Tareas indexadas por ISO, más las que no tienen ninguna fecha. */
  const { porDia, sinFecha } = useMemo(() => {
    const map = new Map<string, Task[]>()
    const nada: Task[] = []
    for (const t of visible) {
      const { iso, fuente } = fechaDe(t)
      if (!iso) {
        nada.push(t)
        continue
      }
      const arr = map.get(iso) ?? []
      arr.push({ ...t, _dateSource: fuente } as Task)
      map.set(iso, arr)
    }
    return { porDia: map, sinFecha: nada }
  }, [visible])

  const totalDias = daysInMonth(monthCursor)
  const lead = firstWeekdayIndex(monthCursor)
  const cells: (number | null)[] = [
    ...Array.from({ length: lead }, () => null),
    ...Array.from({ length: totalDias }, (_, i) => i + 1),
  ]
  // ⚠️ : abrir una tarea EXISTENTE ya no usa el modal, abre un TAB
  // (pedido de admin: "pincho la tarea se abre al lado derecho... y si tiene un
  // archivo al pincharlo se abre a la derecha de este"). Con el modal, abrir un
  // documento relacionado TAPABA la tarea. El modal se conserva SOLO para "nueva
  // tarea", donde no hay nada al costado que valga la pena mirar en paralelo.


  function openEdit(t: Task) {


    openTaskTab(t.id, t.title)


  }

  const isoDelMes = (day: number) =>
    `${monthCursor.year}-${String(monthCursor.month).padStart(2, '0')}-${String(day).padStart(2, '0')}`

  /** Chip compacto de tarea, reutilizado por las 4 vistas. */
  const ChipTarea = ({ t, compacto = false }: { t: Task; compacto?: boolean }) => {
    const pm = priorityMeta(t.priority)
    const creada = t._dateSource === 'created'
    return (
      <button
        onClick={() => openEdit(t)}
        title={`${t.title}\n${creada ? '(ubicada por fecha de CREACIÓN — sin fecha comprometida)' : '(fecha comprometida)'}\nPrioridad: ${pm.label} · Estado: ${STATUS_META[t.status]?.label ?? t.status}${t.assignee ? ` · ${t.assignee}` : ''}`}
        className={`flex w-full items-center gap-1 rounded border-l-2 bg-surface px-1.5 py-1 text-left transition hover:bg-surface-hover ${
          creada ? 'border-dashed border-[var(--border-subtle)]' : 'border-[var(--border-strong)]'
        }`}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${pm.dot}`} />
        <span className={`line-clamp-1 text-fg ${compacto ? 'text-[10px]' : 'text-[11px]'}`}>
          {t.title}
        </span>
      </button>
    )
  }

  /** Navegación: adapta el paso al zoom activo. */
  const navegar = (delta: number) => {
    if (zoom === 'mes') setMonthCursor((m) => addMonths(m, delta))
    else if (zoom === 'anio') setYearCursor((y) => y + delta)
    else {
      const [y, m, d] = dayCursor.split('-').map(Number)
      const paso = zoom === 'semana' ? 7 * delta : delta
      setDayCursor(isoOf(new Date(y, m - 1, d + paso, 12, 0, 0)))
    }
  }

  const tituloPeriodo = () => {
    if (zoom === 'mes') return `${MONTH_NAMES[monthCursor.month - 1]} ${monthCursor.year}`
    if (zoom === 'anio') return String(yearCursor)
    if (zoom === 'dia') return fmtDiaLargo(dayCursor)
    const lun = mondayOf(dayCursor)
    const dom = new Date(lun.getFullYear(), lun.getMonth(), lun.getDate() + 6, 12, 0, 0)
    const f = (d: Date) =>
      new Intl.DateTimeFormat('es-CL', { day: 'numeric', month: 'short' }).format(d)
    return `${f(lun)} – ${f(dom)} ${dom.getFullYear()}`
  }

  const irHoy = () => {
    setMonthCursor(currentYearMonth())
    setDayCursor(todayISO())
    setYearCursor(currentYearMonth().year)
  }

  const ZOOMS: { k: Zoom; label: string }[] = [
    { k: 'dia', label: 'Día' },
    { k: 'semana', label: 'Semana' },
    { k: 'mes', label: 'Mes' },
    { k: 'anio', label: 'Año' },
  ]

  return (
    <div className="flex h-full flex-col">
      <TaskFilterBar
        title="Calendario"
        projectFilter={projectFilter}
        onProjectFilter={setProjectFilter}
        orgFilter={orgFilter}
        onOrgFilter={setOrgFilter}
        showCancelled={showCancelled}
        onToggleCancelled={() => setShowCancelled((v) => !v)}
        onNew={() => {
          setEditTask(null)
          setDialogOpen(true)
        }}
      >
        {/* Selector de granularidad */}
        <div className="flex items-center gap-0.5 rounded-md bg-surface p-0.5">
          {ZOOMS.map((z) => (
            <button
              key={z.k}
              onClick={() => setZoom(z.k)}
              className={`rounded px-2 py-1 text-xs transition ${
                zoom === z.k ? 'bg-accent-bg text-fg' : 'text-fg-subtle hover:text-fg'
              }`}
            >
              {z.label}
            </button>
          ))}
        </div>
        {/* Filtro por agente / equipo */}
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
              {(SUBS_BY_TEAM[l] ?? []).map((s) => (
                <option key={s} value={`agente:${s}`}>
                  {s.startsWith(`${l}-`) ? s.slice(l.length + 1) : s}
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
        <div className="flex items-center gap-1">
          <button
            onClick={() => navegar(-1)}
            className="rounded-md bg-surface p-1.5 text-fg-subtle transition hover:text-fg"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="min-w-[190px] text-center text-xs font-medium capitalize text-fg">
            {tituloPeriodo()}
          </span>
          <button
            onClick={() => navegar(1)}
            className="rounded-md bg-surface p-1.5 text-fg-subtle transition hover:text-fg"
          >
            <ChevronRight size={14} />
          </button>
          {!(zoom === 'mes' && isCurrentMonth) && (
            <button
              onClick={irHoy}
              className="ml-1 rounded-md bg-surface px-2 py-1 text-xs text-fg-subtle transition hover:text-fg"
            >
              Hoy
            </button>
          )}
        </div>
      </TaskFilterBar>

      {/* LEYENDA (pedido de admin: "poner una parte que significan los colores de
          los puntos que tienen cada tarea"). El punto es la PRIORIDAD. */}
      {showLeyenda && (
        <div className="shrink-0 border-b border-[var(--border-strong)] bg-surface/40 px-5 py-3">
          <div className="flex flex-wrap gap-x-8 gap-y-3 text-[11px]">
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Punto de color = Prioridad
              </div>
              <div className="flex flex-wrap gap-3">
                {Object.entries(PRIORITY_META).map(([k, v]) => (
                  <span key={k} className="inline-flex items-center gap-1.5 text-fg-subtle">
                    <span className={`h-2 w-2 rounded-full ${v.dot}`} />
                    {v.label}
                  </span>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Borde izquierdo = Origen de la fecha
              </div>
              <div className="flex flex-wrap gap-3 text-fg-subtle">
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-3 w-0 border-l-2 border-[var(--border-strong)]" />
                  Fecha comprometida (due date)
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span className="h-3 w-0 border-l-2 border-dashed border-[var(--border-subtle)]" />
                  Fecha de creación (sin compromiso)
                </span>
              </div>
            </div>
            <div>
              <div className="mb-1.5 font-semibold uppercase tracking-wider text-fg-muted">
                Estados (en el detalle de la tarea)
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
        {isLoading && <p className="text-xs text-fg-subtle">Cargando calendario…</p>}
        {isError && <p className="text-xs text-red-400">No se pudo cargar el calendario.</p>}

        {!isLoading && !isError && (
          <>
            {/* ---------------- VISTA MES ---------------- */}
            {zoom === 'mes' && (
              <>
                <div className="mb-2 grid grid-cols-7 gap-2">
                  {WEEKDAY_SHORT.map((w) => (
                    <div
                      key={w}
                      className="text-center text-[10px] font-semibold uppercase tracking-wider text-fg-subtle"
                    >
                      {w}
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-7 gap-2">
                  {cells.map((day, i) => {
                    if (day === null)
                      return <div key={`h${i}`} className="min-h-[92px] rounded-lg" />
                    const iso = isoDelMes(day)
                    const dayTasks = porDia.get(iso) ?? []
                    const esHoy = iso === today
                    return (
                      <div
                        key={iso}
                        className={`min-h-[92px] rounded-lg border p-1.5 ${
                          esHoy
                            ? 'border-accent/60 bg-accent-bg/30'
                            : 'border-[var(--border-subtle)] glass'
                        }`}
                      >
                        <div className="mb-1 flex items-center justify-between">
                          <span
                            className={`text-[11px] font-semibold ${esHoy ? 'text-accent' : 'text-fg-muted'}`}
                          >
                            {day}
                          </span>
                          {dayTasks.length > 0 && (
                            <span className="rounded bg-surface px-1 text-[9px] tabular-nums text-fg-subtle">
                              {dayTasks.length}
                            </span>
                          )}
                        </div>
                        <div className="flex flex-col gap-1">
                          {dayTasks.slice(0, 4).map((t) => (
                            <ChipTarea key={t.id} t={t} compacto />
                          ))}
                          {dayTasks.length > 4 && (
                            <button
                              onClick={() => {
                                setDayCursor(iso)
                                setZoom('dia')
                              }}
                              className="px-1 text-left text-[10px] text-fg-subtle/70 hover:text-fg"
                            >
                              +{dayTasks.length - 4} más
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </>
            )}

            {/* ---------------- VISTA SEMANA ---------------- */}
            {zoom === 'semana' && (
              <div className="grid grid-cols-7 gap-2">
                {weekDays(mondayOf(dayCursor)).map((iso, idx) => {
                  const dayTasks = porDia.get(iso) ?? []
                  const esHoy = iso === today
                  return (
                    <div
                      key={iso}
                      className={`min-h-[260px] rounded-lg border p-2 ${
                        esHoy
                          ? 'border-accent/60 bg-accent-bg/30'
                          : 'border-[var(--border-subtle)] glass'
                      }`}
                    >
                      <div className="mb-2 flex items-center justify-between">
                        <span className={`text-[11px] font-semibold ${esHoy ? 'text-accent' : 'text-fg-muted'}`}>
                          {WEEKDAY_SHORT[idx]} {Number(iso.slice(8, 10))}
                        </span>
                        {dayTasks.length > 0 && (
                          <span className="rounded bg-surface px-1 text-[9px] tabular-nums text-fg-subtle">
                            {dayTasks.length}
                          </span>
                        )}
                      </div>
                      <div className="flex flex-col gap-1">
                        {dayTasks.map((t) => (
                          <ChipTarea key={t.id} t={t} />
                        ))}
                      </div>
                    </div>
                  )
                })}
              </div>
            )}

            {/* ---------------- VISTA DÍA ---------------- */}
            {zoom === 'dia' && (
              <div className="mx-auto max-w-3xl">
                {(() => {
                  const dayTasks = porDia.get(dayCursor) ?? []
                  if (dayTasks.length === 0)
                    return (
                      <p className="rounded-lg border border-[var(--border-subtle)] glass p-6 text-center text-xs text-fg-subtle">
                        Sin tareas este día.
                      </p>
                    )
                  return (
                    <div className="flex flex-col gap-1.5">
                      {dayTasks.map((t) => {
                        const pm = priorityMeta(t.priority)
                        const sm = STATUS_META[t.status]
                        const oid = taskOrg(t, projectsV2)
                        return (
                          <button
                            key={t.id}
                            onClick={() => openEdit(t)}
                            className={`flex items-center gap-2 rounded-lg border-l-2 border border-[var(--border-subtle)] glass px-3 py-2 text-left transition hover:bg-surface-hover ${
                              t._dateSource === 'created' ? 'border-l-dashed' : ''
                            }`}
                          >
                            <span className={`h-2 w-2 shrink-0 rounded-full ${pm.dot}`} />
                            <span className="min-w-0 flex-1 truncate text-[13px] text-fg">
                              {t.title}
                            </span>
                            {oid && (
                              <span
                                className="shrink-0 rounded-full px-1.5 py-0.5 text-[10px]"
                                style={{
                                  color: orgChip(oid, organizations).dotColor,
                                  backgroundColor: orgChip(oid, organizations).dotColor + '1f',
                                }}
                              >
                                {orgLabel(oid, organizations)}
                              </span>
                            )}
                            {t.assignee && (
                              <span className="shrink-0 text-[10px] text-fg-subtle">{t.assignee}</span>
                            )}
                            <span className={`shrink-0 text-[10px] ${sm?.text ?? ''}`}>
                              {sm?.label ?? t.status}
                            </span>
                          </button>
                        )
                      })}
                    </div>
                  )
                })()}
              </div>
            )}

            {/* ---------------- VISTA AÑO ---------------- */}
            {zoom === 'anio' && (
              <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
                {MONTH_NAMES.map((nombre, mi) => {
                  // Conteo del mes: se recorre `porDia` filtrando por prefijo YYYY-MM.
                  const pref = `${yearCursor}-${String(mi + 1).padStart(2, '0')}`
                  let n = 0
                  let altas = 0
                  for (const [iso, arr] of porDia) {
                    if (!iso.startsWith(pref)) continue
                    n += arr.length
                    altas += arr.filter((t) => t.priority === 'high').length
                  }
                  const esMesActual =
                    yearCursor === currentYearMonth().year && mi + 1 === currentYearMonth().month
                  return (
                    <button
                      key={nombre}
                      onClick={() => {
                        setMonthCursor({ year: yearCursor, month: mi + 1 })
                        setZoom('mes')
                      }}
                      className={`rounded-lg border p-3 text-left transition hover:bg-surface-hover ${
                        esMesActual
                          ? 'border-accent/60 bg-accent-bg/30'
                          : 'border-[var(--border-subtle)] glass'
                      }`}
                    >
                      <div className="mb-1 flex items-center justify-between">
                        <span
                          className={`text-xs font-semibold capitalize ${esMesActual ? 'text-accent' : 'text-fg'}`}
                        >
                          {nombre}
                        </span>
                        <span className="text-[11px] tabular-nums text-fg-subtle">{n}</span>
                      </div>
                      {altas > 0 && (
                        <span className="inline-flex items-center gap-1 text-[10px] text-red-300">
                          <span className="h-1.5 w-1.5 rounded-full bg-red-400" />
                          {altas} alta{altas === 1 ? '' : 's'}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            )}

            {/* Bandeja de tareas SIN NINGUNA fecha (ni due ni created) */}
            {sinFecha.length > 0 && (
              <div className="mt-5 rounded-lg border border-[var(--border-subtle)] glass p-3">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">
                    Sin fecha
                  </span>
                  <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
                    {sinFecha.length}
                  </span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {sinFecha.map((t) => {
                    const pm = priorityMeta(t.priority)
                    return (
                      <button
                        key={t.id}
                        onClick={() => openEdit(t)}
                        title={t.title}
                        className="flex max-w-[240px] items-center gap-1.5 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-left transition hover:bg-surface-hover"
                      >
                        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${pm.dot}`} />
                        <span className="line-clamp-1 text-[11px] text-fg">{t.title}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <TaskDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        task={editTask}
        initialProject={projectFilter !== 'Todos' ? projectFilter : undefined}
      />
    </div>
  )
}
