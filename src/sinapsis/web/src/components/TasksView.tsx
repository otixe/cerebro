import { lazy, Suspense, useMemo } from 'react'
import { LayoutGrid, List, CalendarDays, GanttChartSquare, Inbox, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { fetchTasks, fetchNode, fetchTree, fetchMe, approvalInbox, approvalReview, type ApprovalReview, type ApprovalInboxStatus } from '../lib/api'
import { buildWikiIndex } from '../lib/wikilink'
import { useShellStore, type TaskView } from '../store'
import MarkdownView from './MarkdownView'
import { approvalError } from '../lib/approval'

// El detalle de la tarea, MISMO componente que usa el modal (variant='page').
const TaskDetail = lazy(() => import('./TaskDetail'))
import Board from './Board'
import TaskListView from './TaskListView'
import TaskCalendarView from './TaskCalendarView'
import TaskTimelineView from './TaskTimelineView'
import TaskInboxView from './TaskInboxView'
const ApprovalDetail = lazy(() => import('./ApprovalDetail'))

const VIEWS: {
  key: TaskView
  label: string
  icon: typeof LayoutGrid
}[] = [
  { key: 'board', label: 'Tablero', icon: LayoutGrid },
  { key: 'list', label: 'Lista', icon: List },
  { key: 'inbox', label: 'Bandeja', icon: Inbox },
  { key: 'calendar', label: 'Calendario', icon: CalendarDays },
  { key: 'timeline', label: 'Timeline', icon: GanttChartSquare },
]

/**
 * Contenedor de la sección de tareas. Ofrece un selector de vista (segmented
 * control) — Tablero | Lista | Calendario | Timeline — y monta la vista activa.
 * Todas operan sobre las mismas tareas (fetchTasks + TanStack Query) y usan
 * TaskDialog para editar. El Board (Kanban + dnd) queda intacto como una vista.
 * La vista elegida se persiste en el shell store (sobrevive navegación).
 */
export default function TasksView() {
  const view = useShellStore((s) => s.taskView)
  const setView = useShellStore((s) => s.setTaskView)
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe, enabled: false })
  const canApprove = me?.is_admin === true
  const panels = useShellStore((s) => s.taskPanels)
  const closePanel = useShellStore((s) => s.closeTaskPanel)

  return (
    <div className="flex h-full flex-col">
      {/* Selector de vista */}
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-strong)] px-3 py-2">
        <div className="flex items-center gap-0.5 overflow-x-auto rounded-md bg-surface p-0.5">
          {VIEWS.map((v) => {
            const Icon = v.icon
            const active = view === v.key
            return (
              <button
                key={v.key}
                onClick={() => setView(v.key)}
                aria-pressed={active}
                className={`flex min-h-[44px] shrink-0 items-center gap-1.5 rounded px-3 py-1 text-xs font-medium transition lg:min-h-0 lg:px-2.5 ${
                  active
                    ? 'bg-accent-bg text-fg'
                    : 'text-fg-subtle hover:text-fg'
                }`}
              >
                <Icon size={13} />
                <span>{v.label}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* VISTA DIVIDIDA (patron MillerColumns, pedido de admin ):
          la vista de tareas queda a la izquierda y cada cosa que se abre agrega
          un panel A LA DERECHA, sin tapar lo anterior. Scroll horizontal cuando
          la cadena es larga, igual que el navegador de carpetas. */}
      {/* h-full en la fila + min-h-0 para que el flex pueda encogerse y el scroll
          quede DENTRO de cada panel (si no, el scroll se va al contenedor y los
          headers de los paneles se desplazan). */}
      {/* MOVIL (<lg): la vista dividida NO cabe. Se cambia el PATRON DE
          INTERACCION a drill-down: se ve UNA columna a la vez (la ultima
          abierta) y el boton de cerrar del panel funciona como "volver",
          porque closeTaskPanel(i) ya cierra ese panel y los de su derecha.
          Cero logica nueva: solo visibilidad por breakpoint.
          En lg: identico a antes (Miller con scroll horizontal). */}
      <div className="flex h-full min-h-0 flex-1 lg:overflow-x-auto">
        {/* Panel base: la vista de tareas. Se ENCOGE cuando hay paneles abiertos
            (de flex-1 a un ancho fijo) para dejar aire al detalle, pero NO
            desaparece: eso es lo que diferencia "dividir" de "reemplazar". */}
        <div
          className={
            panels.length > 0
              ? // Con paneles abiertos: en movil se OCULTA (el detalle toma la
                // pantalla completa); en lg se encoge a 380px como siempre.
                'hidden h-full flex-col overflow-hidden border-r border-[var(--border-strong)] lg:flex lg:w-[380px] lg:shrink-0'
              : 'flex h-full min-w-0 flex-1 flex-col'
          }
        >
          {canApprove && <ApprovalSections />}
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            {view === 'board' ? (
            <Board />
          ) : view === 'list' ? (
            <TaskListView />
          ) : view === 'inbox' ? (
            <TaskInboxView />
          ) : view === 'calendar' ? (
            <TaskCalendarView />
          ) : (
            <TaskTimelineView />
            )}
          </div>
        </div>

        {panels.map((p, i) => (
          <div
            key={p.kind === 'note' ? 'note:' + p.path + ':' + i : p.kind + ':' + p.id + ':' + i}
            /* ALTO: h-full para que el panel llegue al fondo de la fila. Sin esto
               el alto lo definia el contenido y quedaba un hueco abajo.
               ANCHO: el ULTIMO panel toma el espacio RESTANTE (flex-1 + min-w),
               los intermedios quedan fijos porque son la pila de navegacion.
               Antes el ultimo tambien era fijo (620px) y en una pantalla ancha
               dejaba espacio MUERTO a la derecha, con el borde cortado a mitad
               de la pantalla — ese era el "gap" que se veia feo (medido sobre un
               screenshot de 1915px: sobraban ~475px sin usar).
               min-w-0 permite que el truncado del header funcione dentro del flex. */
            className={`h-full flex-col overflow-hidden ${
              i === panels.length - 1
                ? // MOVIL: el min-w-[560px] es justo lo que aplastaba el detalle
                  // en una pantalla de 360 CSS. Aca solo aplica desde lg.
                  'flex w-full min-w-0 flex-1 lg:min-w-[560px]'
                : // Panel intermedio: en movil no se muestra (esta "detras" en
                  // la pila); en lg sigue siendo la columna fija de 420px.
                  'hidden min-w-0 border-r border-[var(--border-strong)] lg:flex lg:w-[420px] lg:shrink-0'
            }`}
          >
            <div className="glass flex h-11 shrink-0 items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 lg:h-9">
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg lg:text-[12px]" title={p.title}>
                {p.title}
              </span>
              {/* En movil este boton ES el "volver" de la pila, asi que tiene que
                  ser tocable (44x44). En lg vuelve al icono chico de antes. */}
              <button
                onClick={() => closePanel(i)}
                title="Cerrar este panel y los de la derecha"
                aria-label="Cerrar este panel y los de la derecha"
                className="-mr-1 flex h-11 w-11 shrink-0 items-center justify-center rounded text-fg-subtle transition hover:bg-surface hover:text-fg lg:mr-0 lg:h-auto lg:w-auto lg:p-0.5"
              >
                <X size={18} className="lg:h-[13px] lg:w-[13px]" />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {p.kind === 'task' ? (
                <TaskPanelBody taskId={p.id} index={i} />
              ) : p.kind === 'approval' ? (
                <Suspense fallback={<div className="p-4 text-xs text-fg-subtle">Cargando…</div>}>
                  <ApprovalDetail id={p.id} />
                </Suspense>
              ) : (
                <NotePanelBody path={p.path} index={i} />
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/** Each status loads independently: a failed page/review preserves successful cards and
 * warns the user. Limits guard against a backend returning a cyclic cursor. */
async function loadApprovals(status: ApprovalInboxStatus): Promise<{ reviews: ApprovalReview[]; warning: string }> {
  const items: Awaited<ReturnType<typeof approvalInbox>>['items'] = []
  const seen = new Set<string>()
  let cursor: string | null = null
  let warning = ''
  for (let pageNumber = 0; pageNumber < 20; pageNumber++) {
    try {
      const page = await approvalInbox(status, cursor ?? undefined)
      items.push(...page.items)
      cursor = page.next_cursor
      if (!cursor) break
      if (seen.has(cursor)) { warning = 'Paginación repetida; lista incompleta.'; break }
      seen.add(cursor)
      if (pageNumber === 19) warning = 'Se alcanzó el límite de 20 páginas; lista incompleta.'
    } catch (e) { warning = `Lista incompleta: ${approvalError(e)}`; break }
  }
  const results = await Promise.allSettled(items.map(item => approvalReview(item.id)))
  const reviews: ApprovalReview[] = []
  for (const result of results) {
    if (result.status === 'fulfilled' && result.value.status === status) reviews.push(result.value)
    else if (result.status === 'rejected') warning = [warning, `No se pudo cargar un detalle: ${approvalError(result.reason)}`].filter(Boolean).join(' ')
  }
  return { reviews, warning }
}

function ApprovalSections() {
  const openApproval = useShellStore((s) => s.openApprovalPanel)
  const pending = useQuery({ queryKey: ['approval', 'pending'], queryFn: () => loadApprovals('pending_approval'), refetchOnWindowFocus: true })
  const approved = useQuery({ queryKey: ['approval', 'approved'], queryFn: () => loadApprovals('aprobada'), refetchOnWindowFocus: true })
  const pendingItems = pending.data?.reviews ?? []
  const approvedItems = approved.data?.reviews ?? []
  const pendingWarning = pending.data?.warning || (pending.error ? approvalError(pending.error) : '')
  const approvedWarning = approved.data?.warning || (approved.error ? approvalError(approved.error) : '')
  if (!pendingItems.length && !approvedItems.length && !pendingWarning && !approvedWarning) return null
  function cards(items: ApprovalReview[], highlighted: boolean) {
    return <div className="flex max-h-48 flex-col gap-2 overflow-y-auto">
      {items.map(item => <button key={item.task_id} type="button" onClick={() => openApproval(item.task_id, item.title)}
        className={`group min-h-[44px] w-full rounded-lg border border-[var(--border-strong)] ${highlighted ? 'border-l-2 border-l-[var(--warn)]' : ''} bg-surface p-3 text-left shadow-sm transition hover:bg-surface-hover`}>
        <span className="block truncate text-xs font-medium text-fg">{item.title}</span>
        <span className="block break-words text-[11px] text-fg-subtle">{item.creator} · {item.action_type} · {item.resource_kind} / {item.resource_id}</span>
        <span className="mt-1 inline-flex rounded bg-[rgba(255,188,51,0.16)] px-1.5 py-0.5 text-[10px] text-[var(--warn)]">{highlighted ? 'Espera tu aprobación' : 'Aprobada · se puede revocar'}</span>
      </button>)}
    </div>
  }
  return <section aria-label="Solicitudes de aprobación" className="shrink-0 border-b border-[var(--border-strong)] px-3 py-3">
    <div className="flex items-center justify-between gap-2">
      <h2 className="text-sm font-semibold text-fg">{pendingItems.length ? `Esperan tu aprobación (${pendingItems.length})` : 'Aprobaciones'}</h2>
      <button type="button" className="rounded border border-[var(--border-strong)] px-2 py-1 text-xs text-fg hover:bg-surface-hover" onClick={() => { void pending.refetch(); void approved.refetch() }}>Actualizar</button>
    </div>
    {pendingWarning && <p role="alert" className="py-1 text-xs text-[var(--warn)]">Pendientes: {pendingWarning}</p>}
    {pendingItems.length > 0 && <div className="mt-2">{cards(pendingItems, true)}</div>}
    {approvedWarning && <p role="alert" className="py-1 text-xs text-[var(--warn)]">Aprobadas: {approvedWarning}</p>}
    {approvedItems.length > 0 && <details className="mt-2"><summary className="min-h-[44px] cursor-pointer py-2 text-xs font-medium text-fg lg:min-h-0">Aprobadas · {approvedItems.length}</summary>{cards(approvedItems, false)}</details>}
  </section>
}

/**
 * Detalle de la tarea dentro de un panel. Reusa TaskDetail (el MISMO componente
 * del modal) con variant='page' — cero duplicacion de la logica del form.
 *
 * Resuelve la tarea contra el cache de ['tasks'] porque el backend NO expone
 * GET /api/tasks/:id (verificado en lib/api.ts).
 */
function TaskPanelBody({ taskId, index }: { taskId: string; index: number }) {
  const { data } = useQuery({ queryKey: ['tasks'], queryFn: fetchTasks })
  const openDoc = useShellStore((st) => st.openDocPanel)
  const openTaskPanel = useShellStore((st) => st.openTaskPanel)
  const task = data?.tasks.find((t) => t.id === taskId) ?? null
  if (!task) {
    return (
      <div className="p-4 text-xs text-fg-subtle">Cargando {taskId.slice(0, 8)}…</div>
    )
  }
  return (
    /* min-h-full (no h-full): si el contenido es corto el panel se llena igual, y
       si es largo puede crecer y scrollear. Con h-full a secas, un detalle largo
       quedaba recortado. */
    <div className="min-h-full p-3 lg:p-4">
      <Suspense fallback={<div className="text-xs text-fg-subtle">Cargando…</div>}>
        <TaskDetail
          variant="page"
          open
          onOpenChange={() => {}}
          task={task}
          onOpenTask={(t) => openTaskPanel(t.id, t.title)}
          /* Un documento relacionado se abre EN EL PANEL SIGUIENTE, no en un tab:
             es literalmente lo que se pidio ("si tiene un archivo al pincharlo se
             abre a la derecha de este"). */
          onOpenDoc={(path) => openDoc(index, path)}
        />
      </Suspense>
    </div>
  )
}

/**
 * Nota del vault en modo lectura dentro de un panel.
 *
 * `index` importa: un [[wikilink]] DENTRO de la nota abre el destino en el panel
 * SIGUIENTE, encadenando igual que las columnas de carpetas. Sin eso, navegar
 * desde una nota abierta en panel habria vuelto a la logica de tabs y se rompia
 * la metafora a mitad de camino.
 */
function NotePanelBody({ path, index }: { path: string; index: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['node', path],
    queryFn: () => fetchNode(path),
  })
  const { data: tree } = useQuery({ queryKey: ['tree'], queryFn: fetchTree })
  const wikiIndex = useMemo(() => buildWikiIndex(tree), [tree])
  const openDoc = useShellStore((st) => st.openDocPanel)
  if (isLoading) return <div className="p-4 text-xs text-fg-subtle">Cargando…</div>
  if (error || !data) {
    return (
      <div className="p-4 text-xs text-[var(--warn)]">
        No se pudo abrir {path}
      </div>
    )
  }
  return (
    <div className="min-h-full p-3 lg:p-4">
      <MarkdownView
        content={data.content}
        notePath={path}
        wikiIndex={wikiIndex}
        onNavigate={(p) => openDoc(index, p)}
      />
    </div>
  )
}
