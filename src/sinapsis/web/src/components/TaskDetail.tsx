/** TaskDetail — cuerpo REUSABLE del detalle de una tarea.
 *
 *  Extraido VERBATIM de TaskDialog.tsx (paso 1/3 del refactor, ): el
 *  cuerpo se movio tal cual, sin cambios de comportamiento. Lo unico que quedo
 *  en TaskDialog.tsx es el envoltorio Radix (Root/Portal/Overlay/Content) + las
 *  animaciones de Framer Motion; TaskDialog es ahora un wrapper delgado.
 *
 *  Lo Radix-especifico (Dialog.Title/Description/Close) NO puede vivir fuera de
 *  un Dialog.Root, asi que entra por SLOTS: renderTitle / renderDescription /
 *  renderClose. Sin slots (variant='page') se renderizan elementos equivalentes.
 *
 *  `busy` sigue viviendo aca y se espeja al envoltorio con onBusyChange, porque
 *  el modal lo usa para bloquear su propio cierre a mitad de un guardado.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Hand, Send, ShieldCheck, Lock, Unlock, ArrowRightLeft, PackageCheck, Copy, FileText } from 'lucide-react'
import { extractTaskDocs } from '../lib/taskDocs'
import {
  taskCreate,
  taskUpdate,
  taskCancel,
  taskClaim,
  taskSubmit,
  taskComment,
  taskHandoff,
  taskReview,
  taskBlock,
  taskUnblock,
  fetchTaskActivity,
  ApiError,
  type Task,
  type TaskStatus,
  type BlockReason,
  projectCreate,
  fetchTree,
} from '../lib/api'
import { useToast } from './Toast'
import { statusMeta, orgLabel, taskOrg, projectOptions } from './taskShared'
import { useAppStore, useShellStore } from '../store'
import { SubtasksSection, DependenciesSection } from './TaskChainSections'

/** Máquina de estados (espejo del SDD / validación del BFF). */
const TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  todo: ['in_progress', 'blocked', 'deferred', 'cancelled'],
  in_progress: ['submitted', 'in_review', 'done', 'blocked', 'deferred', 'cancelled'],
  // 'submitted' = entregada, esperando gate. Desde aca el auditor aprueba (done)
  // o pide cambios (in_progress). Faltaba en el Record y por eso el dialogo
  // crasheaba al abrir una tarea entregada (bug pantalla negra, ).
  submitted: ['done', 'in_progress', 'cancelled'],
  in_review: ['done', 'in_progress', 'cancelled'],
  blocked: ['in_progress', 'deferred', 'cancelled'],
  deferred: ['todo', 'in_progress', 'cancelled'],
  done: ['todo', 'in_progress'],
  cancelled: [],
}

const STATUS_LABELS: Record<TaskStatus, string> = {
  todo: 'Por hacer',
  in_progress: 'En progreso',
  submitted: 'Entregada',
  in_review: 'En revisión',
  blocked: 'Bloqueada',
  deferred: 'Pospuesta',
  done: 'Hecho',
  cancelled: 'Cancelada',
}

/** Los agentes para derivar (handoff) se obtienen dinamicamente del store
 *  (GET /api/meta). Ver uso de meta.agents mas abajo. */

const BLOCK_REASONS: BlockReason[] = ['dependency', 'external', 'info', 'resource']

const KIND_LABEL: Record<string, string> = {
  created: 'creó la tarea',
  comment: 'comentó',
  status_change: 'cambió estado',
  claim: 'tomó',
  handoff: 'derivó',
  assign: 'asignó',
  note: 'nota',
  result: 'resultado',
  review: 'revisó',
  block: 'bloqueó',
  unblock: 'desbloqueó',
}

/** Formatea un timestamptz::text de Postgres a la hora local del panel. */
function fmtActivityTs(ts: string): string {
  let s = ts.trim().replace(' ', 'T')
  const m = s.match(/([+-]\d{2})(\d{2})?$/)
  if (m) s = m[2] ? s.slice(0, -2) + ':' + m[2] : s + ':00'
  else if (!/[Zz]$/.test(s)) s = s + 'Z'
  const d = new Date(s)
  if (isNaN(d.getTime())) return ts
  return new Intl.DateTimeFormat('es-CL', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: (import.meta.env.VITE_TIMEZONE || Intl.DateTimeFormat().resolvedOptions().timeZone),
  }).format(d)
}

export const PRIORITIES = ['low', 'medium', 'high'] as const

// PROJECTS, ASSIGNEES y SUBAGENTS ya NO se hardcodean: vienen del backend via
// GET /api/meta (store: meta.projects / meta.agents / meta.subagents_by_team).
// El campo Asignado es un combobox de texto libre, asi que cualquier valor es valido.

export interface Props {
  /** Abre un documento relacionado. Si NO viene, cae a abrirlo como tab (modal).
   *  En la vista DIVIDIDA se pasa para que el doc se abra EN EL PANEL SIGUIENTE:
   *  es el pedido de admin ("si tiene un archivo al pincharlo se abre a la derecha
   *  de este"). El prop es opcional a proposito, asi el modal sigue funcionando
   *  sin cambios y no hay dos comportamientos que mantener sincronizados. */
  onOpenDoc?: (path: string) => void
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Tarea a editar; null → modo crear. */
  task?: Task | null
  /** Proyecto preseleccionado al crear. */
  initialProject?: string
  /** Abre otra tarea en detalle (navegación entre dependencias). */
  onOpenTask?: (t: Task) => void
  /** Diferencias mínimas de layout entre el modal y una página dedicada. */
  variant?: 'modal' | 'page'
  /** Slot del título. Radix exige que Dialog.Title viva dentro de un Dialog.Root,
   *  así que el envoltorio lo inyecta. Sin slot se renderiza un heading normal. */
  renderTitle?: (title: string) => ReactNode
  /** Slot de la descripción (Dialog.Description en el envoltorio modal). */
  renderDescription?: (description: string) => ReactNode
  /** Slot del botón de cierre: recibe el <button> ya armado y lo envuelve
   *  (Dialog.Close asChild en el modal). Sin slot es un <button> común. */
  renderClose?: (children: ReactNode) => ReactNode
  /** Espeja el estado `busy` hacia el envoltorio. El modal lo necesita para
   *  BLOQUEAR el cierre mientras se guarda (onOpenChange guardado por !busy). */
  onBusyChange?: (busy: boolean) => void
}

interface FormState {
  title: string
  project: string
  organization: string
  priority: string
  assignee: string
  subagent: string
  due_date: string
  description: string
  status: TaskStatus
}

const EMPTY: FormState = {
  title: '',
  project: 'Personal',
  organization: '',
  priority: 'medium',
  assignee: '',
  subagent: '',
  due_date: '',
  description: '',
  status: 'todo',
}

const inputCls =
  // text-[16px] en movil NO es una eleccion tipografica: por debajo de 16px
  // Safari iOS hace zoom al enfocar el input y descuadra la vista entera. En lg
  // vuelve a text-sm (14px), que es como se ve hoy en escritorio.
  'min-h-[44px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 text-[16px] text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent lg:min-h-0 lg:text-sm'
const labelCls = 'text-[11px] font-medium uppercase tracking-wider text-fg-subtle'
const actionBtnCls =
  'inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-[var(--border-strong)] bg-surface px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-surface-hover disabled:opacity-50 lg:min-h-0 lg:px-2.5'
const actionBtnOkCls =
  'inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition hover:bg-emerald-500/20 disabled:opacity-50 lg:min-h-0 lg:px-2.5'
/** Clase del boton Guardar/Crear. Compartida por el boton de arriba (header)
 *  y el de abajo (footer) para que sean identicos y no divergan. */
const saveBtnCls =
  'min-h-[44px] rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50 lg:min-h-0'

/** Auto-size de un <textarea>: ajusta la altura al contenido entre minH y maxH.
 *  Al tocar el techo el textarea scrollea solo, asi no empuja el dialogo (que ya
 *  tiene max-h-[85vh] + overflow-y-auto).
 *
 *  Dos detalles no obvios:
 *  1) El reset a 'auto' es necesario para que scrollHeight pueda ENCOGER, no solo
 *     crecer (scrollHeight nunca baja si la altura fija actual es mayor).
 *  2) El techo se aplica en JS, NO como max-height CSS: asi el auto-crecimiento
 *     se detiene en maxH, pero el usuario todavia puede arrastrar el handle de
 *     resize mas alla si quiere. Si detectamos que arrastro (el alto inline ya
 *     no es el que fijamos nosotros, marcado en data-auto-h), dejamos de
 *     auto-ajustar ese textarea: gana la eleccion manual. */
function fitTextarea(el: HTMLTextAreaElement | null, minH: number, maxH: number) {
  if (!el) return
  const ours = el.dataset.autoH
  if (ours && el.style.height && el.style.height !== ours) return
  el.style.height = 'auto'
  const content = el.scrollHeight
  // scrollHeight 0 => el nodo todavia no esta laid-out (dialogo abriendose).
  // No fijamos altura: el caller reintenta en el frame siguiente.
  if (content === 0) {
    if (ours) el.style.height = ours
    return
  }
  const h = `${Math.min(Math.max(content, minH), maxH)}px`
  el.style.height = h
  el.dataset.autoH = h
  el.style.overflowY = content > maxH ? 'auto' : 'hidden'
}

// Limites de alto (px). Descripcion: min ~4 filas de text-sm (4*20 + py-2*2 + borde).
const DESC_MIN_H = 98
const DESC_MAX_H = 420
// Comentario: min ~2 filas (era rows={2}), techo mas bajo que la descripcion.
const COMMENT_MIN_H = 58
const COMMENT_MAX_H = 220

export default function TaskDetail({
  onOpenDoc,
  open,
  onOpenChange,
  task = null,
  initialProject,
  onOpenTask,
  variant = 'modal',
  renderTitle,
  renderDescription,
  renderClose,
  onBusyChange,
}: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const meta = useAppStore((s) => s.meta)
  const PROJECTS = meta?.projects ?? []
  // Jerarquia Organizacion -> Proyecto (cambio de implementación).
  const ORGS = meta?.organizations ?? []
  const PROJECTS_V2 = meta?.projects_v2
  // Org elegida en el dialogo. Al abrir para editar, se deriva de la tarea.
  const [orgSel, setOrgSel] = useState('')
  const [newProjName, setNewProjName] = useState('')
  const [creatingProj, setCreatingProj] = useState(false)
  const HANDOFF_AGENTS = meta?.agents ?? []
  const SUBAGENTS = meta?.subagents_by_team ?? {}
  const ASSIGNEES: { value: string; label: string }[] = [
    { value: '', label: 'Sin asignar' },
    ...(meta?.leads ?? []).map((n) => ({ value: n, label: n })),
  ]
  const isEdit = task !== null
  const [f, setF] = useState<FormState>(EMPTY)

  // DOCUMENTOS RELACIONADOS (pedido de admin ). Se derivan de la
  // descripcion: 312 de 1019 descripciones del tablero traen rutas .md sueltas y
  // 128 traen [[wikilinks]] => se soportan las dos, y solo se linkea lo que EXISTE
  // en el arbol del vault (un link roto manda a buscar algo que no esta).
  const { data: vaultTree } = useQuery({ queryKey: ['tree'], queryFn: fetchTree })
  const openNoteTab = useShellStore((s) => s.openNote)
  const relatedDocs = useMemo(
    () => extractTaskDocs(f.description || '', vaultTree ? [vaultTree] : []),
    [f.description, vaultTree],
  )
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [confirmCancel, setConfirmCancel] = useState(false)

  // ---- Estado de issues (F2) ----
  const [claimedBy, setClaimedBy] = useState('')
  const [blockedReason, setBlockedReason] = useState('')
  const [commentBody, setCommentBody] = useState('')
  const [handoffTo, setHandoffTo] = useState('')
  const [handoffNote, setHandoffNote] = useState('')
  const [reviewNote, setReviewNote] = useState('')
  const [showBlock, setShowBlock] = useState(false)
  const [blockReason, setBlockReason] = useState<BlockReason>('dependency')

  // Hilo de actividad cronológico (solo en edición).
  const activityQ = useQuery({
    queryKey: ['task_activity', task?.id ?? ''],
    queryFn: () => fetchTaskActivity(task!.id),
    enabled: open && isEdit && !!task,
  })

  useEffect(() => {
    if (!open) return
    setErr(null)
    setBusy(false)
    setConfirmCancel(false)
    setCommentBody('')
    setHandoffTo('')
    setHandoffNote('')
    setReviewNote('')
    setShowBlock(false)
    setBlockReason('dependency')
    setClaimedBy(task?.claimed_by ?? '')
    setBlockedReason(task?.blocked_reason ?? '')
    if (task) {
      setF({
        title: task.title ?? '',
        project: task.project || 'Personal',
        organization: taskOrg(task, PROJECTS_V2),
        priority: task.priority || 'medium',
        assignee: task.assignee ?? '',
        subagent: task.subagent ?? '',
        due_date: task.due_date ?? '',
        description: task.description ?? '',
        status: task.status,
      })
      // Precargar la organizacion de la tarea: propia si el backend la manda,
      // o derivada de su proyecto via projects_v2.
      setOrgSel(taskOrg(task, PROJECTS_V2))
    } else {
      setF({ ...EMPTY, project: initialProject || 'Personal' })
      setOrgSel(initialProject ? taskOrg({ project: initialProject }, PROJECTS_V2) : '')
    }
    setNewProjName('')
  }, [open, task, initialProject, PROJECTS_V2])

  function set<K extends keyof FormState>(k: K, v: FormState[K]) {
    setF((prev) => ({ ...prev, [k]: v }))
  }

  // ---- Auto-crecimiento de los textareas (descripcion y comentario) ----
  const descRef = useRef<HTMLTextAreaElement | null>(null)
  const commentRef = useRef<HTMLTextAreaElement | null>(null)
  const autosizeDesc = useCallback(
    () => fitTextarea(descRef.current, DESC_MIN_H, DESC_MAX_H),
    [],
  )
  const autosizeComment = useCallback(
    () => fitTextarea(commentRef.current, COMMENT_MIN_H, COMMENT_MAX_H),
    [],
  )

  // Ajusta al ABRIR con contenido ya cargado (descripcion larga de una tarea
  // existente) y en cada cambio del valor. La segunda pasada en el frame
  // siguiente cubre el caso en que el nodo aun no esta laid-out al montar el
  // dialogo (scrollHeight = 0) y por eso el auto-size no aplicaria.
  useEffect(() => {
    if (!open) return
    autosizeDesc()
    const raf = requestAnimationFrame(autosizeDesc)
    return () => cancelAnimationFrame(raf)
  }, [open, f.description, autosizeDesc])

  useEffect(() => {
    if (!open) return
    autosizeComment()
    const raf = requestAnimationFrame(autosizeComment)
    return () => cancelAnimationFrame(raf)
  }, [open, commentBody, autosizeComment])

  // Sugerencias de subagente segun el equipo elegido (texto libre siempre permitido).
  const subagentOptions = SUBAGENTS[f.assignee] ?? []

  async function onSave() {
    const title = f.title.trim()
    if (!title || busy) {
      if (!title) setErr('El título es obligatorio.')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      if (isEdit && task) {
        const res = await taskUpdate({
          id: task.id,
          title,
          project: f.project,
          organization: orgSel || undefined,
          priority: f.priority,
          assignee: f.assignee,
          subagent: f.subagent,
          due_date: f.due_date,
          description: f.description,
          status: f.status,
        })
        if (f.status === 'done' && res.unblocked && res.unblocked.length) {
          toast('Desbloqueaste: ' + res.unblocked.map((u) => u.title).join(', '))
        } else {
          toast('Tarea actualizada')
        }
        await qc.invalidateQueries({ queryKey: ['task_deps'] })
        await qc.invalidateQueries({ queryKey: ['tasks_ready'] })
      } else {
        await taskCreate({
          title,
          project: f.project,
          organization: orgSel || undefined,
          priority: f.priority,
          assignee: f.assignee,
          subagent: f.subagent,
          due_date: f.due_date,
          description: f.description,
        })
        toast('Tarea creada')
      }
      await qc.invalidateQueries({ queryKey: ['tasks'] })
      // + la bandeja: queryKey propia que nadie invalidaba (ver runAction)
      await qc.invalidateQueries({ queryKey: ['tasks_inbox'] })
      onOpenChange(false)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'No se pudo guardar la tarea')
    } finally {
      setBusy(false)
    }
  }

  async function onCancelTask() {
    if (!task || busy) return
    setBusy(true)
    setErr(null)
    try {
      await taskCancel(task.id)
      await qc.invalidateQueries({ queryKey: ['tasks'] })
      // + la bandeja: queryKey propia que nadie invalidaba (ver runAction)
      await qc.invalidateQueries({ queryKey: ['tasks_inbox'] })
      toast('Tarea cancelada')
      onOpenChange(false)
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'No se pudo cancelar la tarea')
    } finally {
      setBusy(false)
    }
  }

  // ---- Acciones de issues (endpoints dedicados, aplican y refrescan) ----
  async function runAction(fn: () => Promise<void>, okMsg: string) {
    if (!task || busy) return
    setBusy(true)
    setErr(null)
    try {
      await fn()
      await qc.invalidateQueries({ queryKey: ['tasks'] })
      // ⚠️ ['tasks_inbox'] TAMBIEN. La bandeja tiene su propia queryKey
      // (['tasks_inbox', agent]) y NADIE la invalidaba en todo el codigo => al
      // aprobar o entregar, la tarea SEGUIA en la bandeja con el estado viejo
      // (reportado por admin : "tiene el boton autorizar pero siguen
      // ahi, no se van ni cambian de estado"). Invalidar ['tasks'] no alcanza:
      // son dos caches distintas del MISMO dato, y esa es la trampa.
      await qc.invalidateQueries({ queryKey: ['tasks_inbox'] })
      await qc.invalidateQueries({ queryKey: ['tasks_ready'] })
      await qc.invalidateQueries({ queryKey: ['task_deps'] })
      await qc.invalidateQueries({ queryKey: ['task_activity', task.id] })
      toast(okMsg)
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'La acción falló'
      setErr(msg)
      toast(msg, 'err')
    } finally {
      setBusy(false)
    }
  }

  function onClaim() {
    if (!task) return
    void runAction(async () => {
      const r = await taskClaim(task.id)
      set('status', r.task?.status ?? 'in_progress')
      setClaimedBy(r.task?.claimed_by ?? '')
      setBlockedReason('')
    }, 'Tarea tomada')
  }

  function onBlock() {
    if (!task) return
    void runAction(async () => {
      const r = await taskBlock(task.id, blockReason)
      set('status', r.task?.status ?? 'blocked')
      setBlockedReason(r.task?.blocked_reason || blockReason)
      setShowBlock(false)
    }, 'Tarea bloqueada')
  }

  function onUnblock() {
    if (!task) return
    void runAction(async () => {
      const r = await taskUnblock(task.id)
      set('status', r.task?.status ?? 'in_progress')
      setBlockedReason('')
    }, 'Tarea desbloqueada')
  }

  /** Entrega para auditoria del gate. La transicion valida la decide TRANSITIONS
   *  (mapa de arriba), no un hardcode: el boton solo se muestra si es legal. */
  function onSubmitForReview() {
    if (!task) return
    void runAction(async () => {
      const r = await taskSubmit(task.id, reviewNote.trim())
      set('status', r.task?.status ?? 'submitted')
      setReviewNote('')
    }, 'Tarea entregada para revisión')
  }

  function onReview(verdict: 'pass' | 'needs_changes') {
    if (!task) return
    void runAction(async () => {
      const r = await taskReview(task.id, verdict, reviewNote.trim())
      set('status', r.task?.status ?? (verdict === 'pass' ? 'done' : 'in_progress'))
      setReviewNote('')
    }, verdict === 'pass' ? 'Tarea terminada' : 'Cambios solicitados')
  }

  function onComment() {
    const body = commentBody.trim()
    if (!task || !body) return
    void runAction(async () => {
      await taskComment(task.id, body)
      setCommentBody('')
    }, 'Comentario agregado')
  }

  function onHandoff() {
    if (!task || !handoffTo) return
    void runAction(async () => {
      await taskHandoff(task.id, handoffTo, handoffNote.trim())
      set('assignee', handoffTo)
      setClaimedBy('')
      setHandoffTo('')
      setHandoffNote('')
    }, 'Tarea derivada')
  }

  // Opciones de estado válidas (máquina de estados). Excluye blocked/cancelled
  // del select (tienen botones dedicados con su propia semántica).
  // El humano dueño puede Terminar (done) directo desde cualquier estado
  // NO-terminal; blocked/cancelled siguen fuera del select (botones dedicados).
  const isTerminal = f.status === 'done' || f.status === 'cancelled'
  const statusChoices = Array.from(
    new Set<TaskStatus>([
      f.status,
      ...TRANSITIONS[f.status],
      ...(isTerminal ? [] : (['done'] as TaskStatus[])),
    ])
  ).filter((s) => s === f.status || (s !== 'blocked' && s !== 'cancelled'))

  // Boton Guardar/Crear: UNA sola definicion de handler, disabled y label,
  // usada por el boton del header (arriba a la derecha) y el del footer.
  const renderSaveButton = (extraCls = '') => (
    <button
      onClick={onSave}
      disabled={busy || !f.title.trim()}
      className={`${extraCls} ${saveBtnCls}`.trim()}
    >
      {busy ? 'Guardando…' : isEdit ? 'Guardar' : 'Crear'}
    </button>
  )

  // Espeja `busy` al envoltorio. Aditivo: NO toca ningun setBusy del cuerpo, asi
  // el modal puede seguir bloqueando su cierre mientras se guarda.
  useEffect(() => {
    onBusyChange?.(busy)
  }, [busy, onBusyChange])

  // ---- Slots del envoltorio ----
  // Dialog.Title / Dialog.Description / Dialog.Close NO pueden renderizarse fuera
  // de un Dialog.Root, por eso los provee el envoltorio. Los fallbacks (modo
  // 'page') son elementos normales con las MISMAS clases.
  const titleText = isEdit ? 'Editar tarea' : 'Nueva tarea'
  const descriptionText = isEdit
    ? 'Modificá los campos y guardá los cambios.'
    : 'Creá una tarea nueva en el tablero.'

  const titleNode = renderTitle ? (
    renderTitle(titleText)
  ) : variant === 'page' ? (
    <h1 className="text-base font-semibold text-fg">{titleText}</h1>
  ) : (
    <h2 className="text-sm font-semibold text-fg">{titleText}</h2>
  )

  const descriptionNode = renderDescription ? (
    renderDescription(descriptionText)
  ) : (
    <p className="mt-1 text-xs text-fg-subtle">{descriptionText}</p>
  )

  // El <button> se pasa SIN onClick: Dialog.Close asChild le inyecta el suyo.
  // Sin slot hay que cablearlo a mano para que el boton siga cerrando.
  const closeNode = renderClose ? (
    renderClose(
      <button
        disabled={busy}
        className="min-h-[44px] rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg disabled:opacity-50 lg:min-h-0"
      >
        Cerrar
      </button>
    )
  ) : (
    <button
      type="button"
      onClick={() => onOpenChange(false)}
      disabled={busy}
      className="min-h-[44px] rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg disabled:opacity-50 lg:min-h-0"
    >
      Cerrar
    </button>
  )

  return (
    <>
      {/* Header: titulo/descripcion a la izquierda y Guardar arriba a la
          derecha, para no tener que scrollear al fondo en tareas largas.
          min-w-0 en la columna de texto evita que el titulo empuje al
          boton; shrink-0 en el boton evita que se comprima. */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          {titleNode}
          {descriptionNode}
          {/* ID de la tarea, visible y copiable (pedido de admin ):
              "no sale por ningun lado el numero o el codigo de la tarea, que
              es mas facil pasarte el codigo que el titulo".
              Se muestra el PREFIJO de 8 chars, que es la unidad con la que se
              habla y la que aceptan todas las tools del Cerebro; el uuid completo
              (36 chars) arruina el layout y nadie lo dicta en voz alta. */}
          {isEdit && task && (
            <button
              type="button"
              onClick={() => {
                const short = task.id.slice(0, 8)
                navigator.clipboard
                  ?.writeText(short)
                  .then(() => toast('ID copiado: ' + short))
                  .catch(() => toast('No se pudo copiar (¿sin permiso de portapapeles?)'))
              }}
              title="Click para copiar el ID"
              className="mt-1.5 inline-flex min-h-[44px] items-center gap-1 rounded border border-[var(--border-subtle)] bg-surface px-2 py-0.5 font-mono text-[11px] text-fg-subtle transition hover:border-[var(--border-strong)] hover:text-fg lg:min-h-0 lg:px-1.5 lg:text-[10px]"
            >
              <Copy size={9} /> {task.id.slice(0, 8)}
            </button>
          )}
        </div>
        {renderSaveButton('shrink-0')}
      </div>

      <div className="mt-4 space-y-3">
        <div>
          <label className={labelCls}>Título</label>
          <input
            autoFocus
            value={f.title}
            onChange={(e) => set('title', e.target.value)}
            placeholder="¿Qué hay que hacer?"
            className={`mt-1 ${inputCls}`}
          />
        </div>

        {/* Organizacion -> Proyecto (cambio de implementación). El select de proyecto
            queda ACOTADO a la organizacion elegida, y se puede crear un
            proyecto nuevo sin salir del dialogo (antes el select era
            cerrado y no habia forma de agregar uno). */}
        {ORGS.length > 0 && (
          <div>
            <label className={labelCls}>Organización</label>
            <select
              value={orgSel}
              onChange={(e) => {
                setOrgSel(e.target.value)
                set('organization', e.target.value)
                // El proyecto elegido puede no pertenecer a la org nueva.
                const opts = projectOptions(e.target.value, PROJECTS_V2, PROJECTS)
                if (!opts.some((o) => o.id === f.project)) set('project', '')
              }}
              className={`mt-1 ${inputCls}`}
            >
              <option value="">Sin organización</option>
              {ORGS.map((o) => (
                <option key={o.id} value={o.id}>
                  {orgLabel(o.id, ORGS)}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* 1 columna en movil: con 2 columnas cada campo queda en ~166px a
            360px CSS y los nombres de proyecto/equipo se cortan. En lg vuelve
            a la grilla de 2 que ya se usa en escritorio. */}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div>
            <label className={labelCls}>Proyecto</label>
            <select
              value={f.project}
              onChange={(e) => set('project', e.target.value)}
              className={`mt-1 ${inputCls}`}
            >
              <option value="">Sin proyecto</option>
              {projectOptions(orgSel, PROJECTS_V2, PROJECTS).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
            {orgSel && PROJECTS_V2 && (
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <input
                  value={newProjName}
                  onChange={(e) => setNewProjName(e.target.value)}
                  placeholder="Nuevo proyecto…"
                  className="min-h-[44px] flex-1 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[16px] text-fg lg:min-h-0 lg:text-xs"
                />
                <button
                  type="button"
                  disabled={!newProjName.trim() || creatingProj}
                  onClick={async () => {
                    const name = newProjName.trim()
                    if (!name) return
                    setCreatingProj(true)
                    try {
                      await projectCreate({ id: name, org_id: orgSel, name })
                      await qc.invalidateQueries({ queryKey: ['meta'] })
                      set('project', name)
                      setNewProjName('')
                      toast(`Proyecto “${name}” creado en ${orgLabel(orgSel, ORGS)}`)
                    } catch (err) {
                      toast(
                        err instanceof Error ? err.message : 'No se pudo crear el proyecto'
                      )
                    } finally {
                      setCreatingProj(false)
                    }
                  }}
                  className="min-h-[44px] rounded-md bg-accent-bg px-3 py-1 text-xs font-medium text-fg disabled:opacity-40 lg:min-h-0 lg:px-2"
                >
                  {creatingProj ? '…' : '+ Crear'}
                </button>
              </div>
            )}
          </div>
          <div>
            <label className={labelCls}>Prioridad</label>
            <select
              value={f.priority}
              onChange={(e) => set('priority', e.target.value)}
              className={`mt-1 ${inputCls}`}
            >
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 1 columna en movil: con 2 columnas cada campo queda en ~166px a
            360px CSS y los nombres de proyecto/equipo se cortan. En lg vuelve
            a la grilla de 2 que ya se usa en escritorio. */}
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <div>
            <label className={labelCls}>Equipo</label>
            <select
              value={f.assignee}
              onChange={(e) => set('assignee', e.target.value)}
              className={`mt-1 ${inputCls}`}
            >
              {f.assignee &&
                !ASSIGNEES.some((a) => a.value === f.assignee) && (
                  <option value={f.assignee}>{f.assignee}</option>
                )}
              {ASSIGNEES.map((a) => (
                <option key={a.value} value={a.value}>
                  {a.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Vence</label>
            <input
              type="date"
              value={f.due_date}
              onChange={(e) => set('due_date', e.target.value)}
              className={`mt-1 ${inputCls}`}
            />
          </div>
        </div>

        <div>
          <label className={labelCls}>Asignado</label>
          <input
            list="subagent-suggestions"
            value={f.subagent}
            onChange={(e) => set('subagent', e.target.value)}
            placeholder={
              subagentOptions.length
                ? 'Elegí un subagente o escribí uno'
                : 'Escribí un nombre (opcional)'
            }
            className={`mt-1 ${inputCls}`}
          />
          <datalist id="subagent-suggestions">
            {subagentOptions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </div>

        {isEdit && (
          <div>
            <label className={labelCls}>Estado</label>
            <select
              value={f.status}
              onChange={(e) => set('status', e.target.value as TaskStatus)}
              className={`mt-1 ${inputCls}`}
            >
              {statusChoices.map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
            <p className="mt-1 text-[10px] text-fg-subtle/70">
              Solo se ofrecen transiciones válidas. Bloquear/Cancelar tienen
              acciones dedicadas abajo.
            </p>
          </div>
        )}

        {/* Los documentos van ARRIBA de la descripcion a proposito: son la
            razon por la que se abre la tarea cuando el pedido es "revisa
            esta documentacion". Si no hay ninguno NO se renderiza nada:
            un bloque vacio es ruido. */}
        {relatedDocs.length > 0 && (
          <div>
            <label className={labelCls}>
              Documentos relacionados ({relatedDocs.length})
            </label>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {relatedDocs.map((d) => (
                <button
                  key={d.path}
                  type="button"
                  onClick={() => {
                    // Vista dividida: el doc va al panel de al lado y la tarea QUEDA
                    // visible. Sin el callback (modal) cae al tab y cierra el dialogo,
                    // porque ahi el doc taparia la tarea igual.
                    if (onOpenDoc) {
                      onOpenDoc(d.path)
                    } else {
                      openNoteTab(d.path)
                      onOpenChange(false)
                    }
                  }}
                  title={d.path}
                  className="inline-flex min-h-[44px] max-w-full items-center gap-1 rounded-md border border-[var(--border-subtle)] bg-surface px-2 py-1 text-xs text-fg-subtle transition hover:border-[var(--border-strong)] hover:text-fg lg:min-h-0"
                >
                  <FileText size={11} className="shrink-0" />
                  <span className="truncate">{d.label}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        <div>
          <label className={labelCls}>Descripción</label>
          <textarea
            ref={descRef}
            value={f.description}
            onChange={(e) => {
              set('description', e.target.value)
              autosizeDesc()
            }}
            rows={4}
            placeholder="Detalles (opcional)"
            style={{ minHeight: DESC_MIN_H }}
            className={`mt-1 resize-y ${inputCls}`}
          />
        </div>

        {isEdit && task && (
          <div className="space-y-4 border-t border-[var(--border-subtle)] pt-4">
            <SubtasksSection taskId={task.id} project={f.project} />
            <DependenciesSection task={task} onOpenTask={onOpenTask} />
          </div>
        )}

        {isEdit && task && (
          <div className="space-y-3 border-t border-[var(--border-subtle)] pt-4">
            <div className={labelCls}>Issue</div>

            {/* Chips: estado, equipo, claim, motivo de bloqueo */}
            <div className="flex flex-wrap items-center gap-2">
              {(() => {
                const sm = statusMeta(f.status)
                return (
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-surface px-2 py-0.5 text-[11px]">
                    <span className={`h-1.5 w-1.5 rounded-full ${sm.dot}`} />
                    <span className={sm.text}>{sm.label}</span>
                  </span>
                )
              })()}
              <span className="max-w-full break-words rounded-full bg-surface px-2 py-0.5 text-[11px] text-fg-subtle">
                Equipo: {f.assignee || '—'}
              </span>
              <span className="rounded-full bg-surface px-2 py-0.5 text-[11px] text-fg-subtle">
                {claimedBy ? `Tomada por ${claimedBy}` : 'Sin tomar'}
              </span>
              {f.status === 'blocked' && blockedReason && (
                <span className="rounded-full bg-[rgba(255,188,51,0.16)] px-2 py-0.5 text-[11px] text-[var(--warn)]">
                  Motivo: {blockedReason}
                </span>
              )}
            </div>

            {/* Acciones según estado */}
            <div className="flex flex-wrap gap-2">
              {(['todo', 'submitted', 'in_review', 'blocked'] as TaskStatus[]).includes(
                f.status
              ) && (
                <button onClick={onClaim} disabled={busy} className={actionBtnCls}>
                  <Hand size={13} /> Tomar
                </button>
              )}
              {TRANSITIONS[f.status].includes('submitted') && (
                <button
                  onClick={onSubmitForReview}
                  disabled={busy}
                  className={actionBtnCls}
                >
                  <PackageCheck size={13} /> Entregar
                </button>
              )}
              {(f.status === 'in_review' || f.status === 'submitted') && (
                <>
                  <button
                    onClick={() => onReview('pass')}
                    disabled={busy}
                    className={actionBtnOkCls}
                  >
                    <ShieldCheck size={13} /> Terminar
                  </button>
                  <button
                    onClick={() => onReview('needs_changes')}
                    disabled={busy}
                    className={actionBtnCls}
                  >
                    Pedir cambios
                  </button>
                </>
              )}
              {(['todo', 'in_progress'] as TaskStatus[]).includes(f.status) && (
                <button
                  onClick={() => setShowBlock((v) => !v)}
                  disabled={busy}
                  className={actionBtnCls}
                >
                  <Lock size={13} /> Bloquear
                </button>
              )}
              {f.status === 'blocked' && (
                <button onClick={onUnblock} disabled={busy} className={actionBtnCls}>
                  <Unlock size={13} /> Desbloquear
                </button>
              )}
            </div>

            {/* Motivo de bloqueo */}
            {showBlock && (
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={blockReason}
                  onChange={(e) => setBlockReason(e.target.value as BlockReason)}
                  className={inputCls}
                >
                  {BLOCK_REASONS.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <button onClick={onBlock} disabled={busy} className={actionBtnCls}>
                  Confirmar
                </button>
              </div>
            )}

            {/* Nota (opcional): de ENTREGA si la tarea todavia se puede
                entregar, de REVISION si ya esta en el gate. Es el mismo
                campo (reviewNote) que consumen onSubmitForReview y onReview;
                solo cambia el placeholder para no mentir sobre su uso. */}
            {(f.status === 'in_review' ||
              f.status === 'submitted' ||
              TRANSITIONS[f.status].includes('submitted')) && (
              <input
                value={reviewNote}
                onChange={(e) => setReviewNote(e.target.value)}
                placeholder={
                  f.status === 'in_review' || f.status === 'submitted'
                    ? 'Nota de revisión (opcional)'
                    : 'Nota de entrega (opcional)'
                }
                className={inputCls}
              />
            )}

            {/* Derivar (handoff) */}
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={handoffTo}
                onChange={(e) => setHandoffTo(e.target.value)}
                className={`${inputCls} w-full lg:max-w-[160px]`}
              >
                <option value="">Derivar a…</option>
                {HANDOFF_AGENTS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
              <input
                value={handoffNote}
                onChange={(e) => setHandoffNote(e.target.value)}
                placeholder="Nota (opcional)"
                /* min-w-[120px] solo desde lg: en movil el input toma la fila
                   completa (el flex-wrap del padre lo baja) en vez de competir
                   con el select por 360px. */
                className={`${inputCls} w-full lg:min-w-[120px] lg:flex-1`}
              />
              <button
                onClick={onHandoff}
                disabled={busy || !handoffTo}
                className={actionBtnCls}
              >
                <ArrowRightLeft size={13} /> Derivar
              </button>
            </div>

            {/* Comentar */}
            <div className="space-y-2">
              <textarea
                ref={commentRef}
                value={commentBody}
                onChange={(e) => {
                  setCommentBody(e.target.value)
                  autosizeComment()
                }}
                rows={2}
                placeholder="Escribí un comentario…"
                style={{ minHeight: COMMENT_MIN_H }}
                className={`resize-y ${inputCls}`}
              />
              <div className="flex justify-end">
                <button
                  onClick={onComment}
                  disabled={busy || !commentBody.trim()}
                  className={actionBtnCls}
                >
                  <Send size={13} /> Comentar
                </button>
              </div>
            </div>

            {/* Hilo de actividad */}
            <div>
              <div className={labelCls}>Actividad</div>
              <div className="mt-2 max-h-56 space-y-2 overflow-y-auto pr-1">
                {activityQ.isLoading && (
                  <p className="text-xs text-fg-subtle">Cargando actividad…</p>
                )}
                {activityQ.isError && (
                  <p className="text-xs text-red-400">
                    No se pudo cargar la actividad.
                  </p>
                )}
                {activityQ.data && activityQ.data.activity.length === 0 && (
                  <p className="text-xs text-fg-subtle/60">
                    Sin actividad todavía.
                  </p>
                )}
                {activityQ.data?.activity.map((ev) => (
                  <div
                    key={ev.id}
                    className="rounded-md border border-[var(--border-subtle)] bg-surface/50 px-2.5 py-2 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-fg">
                        {ev.agent}{' '}
                        <span className="font-normal text-fg-subtle">
                          {KIND_LABEL[ev.kind] ?? ev.kind}
                        </span>
                      </span>
                      <span className="shrink-0 tabular-nums text-[10px] text-fg-subtle">
                        {fmtActivityTs(ev.created_at)}
                      </span>
                    </div>
                    {ev.from_status && ev.to_status && (
                      <div className="mt-0.5 text-[11px] text-fg-subtle">
                        {STATUS_LABELS[ev.from_status as TaskStatus] ??
                          ev.from_status}{' '}
                        →{' '}
                        {STATUS_LABELS[ev.to_status as TaskStatus] ??
                          ev.to_status}
                      </div>
                    )}
                    {ev.body && (
                      <div className="mt-0.5 whitespace-pre-wrap text-fg-muted">
                        {ev.body}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>

      {err && <p className="mt-3 text-xs text-red-400">{err}</p>}

      <div className="mt-5 flex flex-wrap items-center justify-between gap-2">
        <div>
          {isEdit && !isTerminal &&
            (confirmCancel ? (
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-fg-subtle">¿Cancelar tarea?</span>
                <button
                  onClick={onCancelTask}
                  disabled={busy}
                  className="min-h-[44px] rounded-md border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-300 transition hover:bg-red-500/20 disabled:opacity-50 lg:min-h-0 lg:px-2.5"
                >
                  Sí, cancelar
                </button>
                <button
                  onClick={() => setConfirmCancel(false)}
                  disabled={busy}
                  className="min-h-[44px] min-w-[44px] rounded-md px-2 py-1.5 text-xs text-fg-subtle transition hover:text-fg lg:min-h-0 lg:min-w-0"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirmCancel(true)}
                disabled={busy}
                className="min-h-[44px] rounded-md px-3 py-1.5 text-xs text-red-300/80 transition hover:bg-red-500/10 hover:text-red-300 disabled:opacity-50 lg:min-h-0 lg:px-2.5"
              >
                Cancelar tarea
              </button>
            ))}
        </div>
        <div className="flex gap-2">
          {closeNode}
          {renderSaveButton()}
        </div>
      </div>
    </>
  )
}
