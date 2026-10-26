import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Plus,
  Search,
  X,
  Circle,
  CheckCircle2,
  Link2,
  ArrowUpRight,
  ListChecks,
  GitBranch,
} from 'lucide-react'
import {
  fetchTasks,
  fetchSubtasks,
  fetchTaskDeps,
  taskCreate,
  taskUpdate,
  taskLink,
  taskUnlink,
  ApiError,
  type Task,
  type TaskStatus,
  type DepTask,
  type TasksResponse,
} from '../lib/api'
import { useToast } from './Toast'

// ---------- Chip de estado (Notion-badge tintado) ----------
const STATUS_META: Record<TaskStatus, { label: string; cls: string }> = {
  todo: { label: 'Por hacer', cls: 'bg-white/5 text-fg-subtle' },
  in_progress: { label: 'En progreso', cls: 'bg-[rgba(168,85,247,0.14)] text-accent' },
  submitted: { label: 'Entregada', cls: 'bg-[rgba(168,85,247,0.14)] text-accent' },
  in_review: { label: 'En revisión', cls: 'bg-[rgba(139,92,246,0.16)] text-violet-300' },
  blocked: { label: 'Bloqueado', cls: 'bg-[rgba(255,188,51,0.16)] text-[var(--warn)]' },
  deferred: { label: 'Pospuesta', cls: 'bg-white/5 text-zinc-400' },
  done: { label: 'Hecho', cls: 'bg-[rgba(16,185,129,0.15)] text-[var(--success)]' },
  cancelled: { label: 'Cancelada', cls: 'bg-white/5 text-fg-faint' },
}

export function StatusChip({ status }: { status: TaskStatus }) {
  const m = STATUS_META[status] ?? STATUS_META.todo
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${m.cls}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {m.label}
    </span>
  )
}

const sectionTitleCls =
  'flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-fg-muted'

// ---------- Picker de tareas (popover command-like) ----------
function TaskPicker({
  onSelect,
  excludeIds,
}: {
  onSelect: (t: Task) => void
  excludeIds: Set<string>
}) {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const { data } = useQuery({ queryKey: ['tasks'], queryFn: fetchTasks })

  const filtered = useMemo(() => {
    const all = (data?.tasks ?? []).filter(
      (t) => !excludeIds.has(t.id) && t.status !== 'cancelled'
    )
    const query = q.trim().toLowerCase()
    const list = query
      ? all.filter((t) => t.title.toLowerCase().includes(query))
      : all
    return list.slice(0, 60)
  }, [data, excludeIds, q])

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 rounded-md border border-[var(--border-strong)] bg-[rgba(255,255,255,0.03)] px-2.5 py-1.5 text-[12px] text-fg-subtle transition hover:bg-[rgba(255,255,255,0.06)] hover:text-fg"
      >
        <Plus size={13} /> Depende de…
      </button>
      <AnimatePresence>
        {open && (
          <>
            {/* Backdrop para cerrar al hacer click afuera */}
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, scale: 0.98, y: -4 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.98, y: -4 }}
              transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
              className="float-shadow absolute left-0 z-50 mt-1 w-72 overflow-hidden rounded-lg border border-[var(--border-strong)] bg-surface"
            >
              <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
                <Search size={14} className="shrink-0 text-fg-subtle" />
                <input
                  autoFocus
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Buscar tarea…"
                  spellCheck={false}
                  className="w-full bg-transparent text-[13px] text-fg placeholder:text-fg-subtle outline-none"
                />
              </div>
              <div className="max-h-56 overflow-y-auto py-1">
                {filtered.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[12px] text-fg-subtle">
                    Sin tareas para vincular
                  </div>
                ) : (
                  filtered.map((t) => (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => {
                        onSelect(t)
                        setOpen(false)
                        setQ('')
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left transition hover:bg-surface-hover"
                    >
                      <span className="min-w-0 flex-1 truncate text-[13px] text-fg-muted">
                        {t.title}
                      </span>
                      <StatusChip status={t.status} />
                    </button>
                  ))
                )}
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}

// ---------- Subtareas ----------
export function SubtasksSection({
  taskId,
  project,
}: {
  taskId: string
  project: string
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const { data } = useQuery({
    queryKey: ['subtasks', taskId],
    queryFn: () => fetchSubtasks(taskId),
  })
  const subs = data?.subtasks ?? []
  const done = subs.filter((s) => s.status === 'done').length
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)

  async function invalidate() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['subtasks', taskId] }),
      qc.invalidateQueries({ queryKey: ['tasks'] }),
      qc.invalidateQueries({ queryKey: ['tasks_ready'] }),
    ])
  }

  async function add() {
    const t = title.trim()
    if (!t || busy) return
    setBusy(true)
    try {
      await taskCreate({ title: t, parent_id: taskId, project })
      setTitle('')
      await invalidate()
    } catch {
      toast('No se pudo crear la subtarea', 'err')
    } finally {
      setBusy(false)
    }
  }

  async function toggle(s: { id: string; status: TaskStatus }) {
    try {
      const next: TaskStatus = s.status === 'done' ? 'todo' : 'done'
      const res = await taskUpdate({ id: s.id, status: next })
      if (next === 'done' && res.unblocked && res.unblocked.length) {
        toast('Desbloqueaste: ' + res.unblocked.map((u) => u.title).join(', '))
      }
      await invalidate()
    } catch {
      toast('No se pudo actualizar la subtarea', 'err')
    }
  }

  async function detach(s: { id: string }) {
    try {
      await taskUpdate({ id: s.id, parent_id: '' })
      await invalidate()
    } catch {
      toast('No se pudo quitar la subtarea', 'err')
    }
  }

  return (
    <section className="space-y-2">
      <div className={sectionTitleCls}>
        <ListChecks size={14} /> Subtareas
        {subs.length > 0 && (
          <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] tabular-nums text-fg-subtle">
            {done}/{subs.length}
          </span>
        )}
      </div>

      {subs.length > 0 && (
        <div className="flex flex-col gap-1">
          {subs.map((s) => (
            <div
              key={s.id}
              className="group flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1.5 transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:shadow-[var(--glow-md)]"
            >
              <button
                type="button"
                onClick={() => toggle(s)}
                className="shrink-0 text-fg-subtle transition hover:text-accent"
                title={s.status === 'done' ? 'Marcar pendiente' : 'Marcar completa'}
              >
                {s.status === 'done' ? (
                  <CheckCircle2 size={16} className="text-[var(--success)]" />
                ) : (
                  <Circle size={16} />
                )}
              </button>
              <span
                className={`min-w-0 flex-1 truncate text-[13px] ${
                  s.status === 'done'
                    ? 'text-fg-subtle line-through'
                    : 'text-fg-muted'
                }`}
              >
                {s.title}
              </span>
              <StatusChip status={s.status} />
              <button
                type="button"
                onClick={() => detach(s)}
                className="shrink-0 rounded p-0.5 text-fg-faint opacity-0 transition hover:bg-surface-hover hover:text-fg group-hover:opacity-100"
                title="Quitar del padre"
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
          placeholder="Nueva subtarea…"
          className="w-full rounded-md border border-[var(--border-strong)] bg-surface px-2.5 py-1.5 text-[13px] text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent"
        />
        <button
          type="button"
          onClick={add}
          disabled={busy || !title.trim()}
          className="shrink-0 rounded-md bg-accent-bg px-2.5 py-1.5 text-[12px] font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50"
        >
          <Plus size={14} />
        </button>
      </div>
    </section>
  )
}

// ---------- Dependencias / cadenas ----------
export function DependenciesSection({
  task,
  onOpenTask,
}: {
  task: Task
  onOpenTask?: (t: Task) => void
}) {
  const qc = useQueryClient()
  const toast = useToast()
  const { data } = useQuery({
    queryKey: ['task_deps', task.id],
    queryFn: () => fetchTaskDeps(task.id),
  })
  const blockedBy = data?.blocked_by ?? []
  const blocks = data?.blocks ?? []

  const excludeIds = useMemo(
    () => new Set<string>([task.id, ...blockedBy.map((b) => b.id)]),
    [task.id, blockedBy]
  )

  async function invalidate() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['task_deps'] }),
      qc.invalidateQueries({ queryKey: ['tasks_ready'] }),
    ])
  }

  async function link(chosen: Task) {
    try {
      await taskLink(task.id, chosen.id)
      await invalidate()
    } catch (e) {
      const m =
        e instanceof ApiError
          ? e.status === 409
            ? 'Crearía un ciclo de dependencias'
            : e.status === 400
              ? 'Dependencia no válida'
              : e.message
          : 'No se pudo enlazar'
      toast(m, 'err')
    }
  }

  async function unlink(dep: DepTask) {
    try {
      await taskUnlink(task.id, dep.id)
      await invalidate()
    } catch {
      toast('No se pudo quitar la dependencia', 'err')
    }
  }

  function open(dep: DepTask) {
    if (!onOpenTask) return
    const all = qc.getQueryData<TasksResponse>(['tasks'])
    const full = all?.tasks.find((t) => t.id === dep.id)
    if (full) onOpenTask(full)
  }

  return (
    <section className="space-y-3">
      <div className="space-y-2">
        <div className={sectionTitleCls}>
          <Link2 size={14} /> Depende de
          {blockedBy.length > 0 && (
            <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] tabular-nums text-fg-subtle">
              {blockedBy.length}
            </span>
          )}
        </div>
        {blockedBy.length > 0 && (
          <div className="flex flex-col gap-1">
            {blockedBy.map((d) => (
              <div
                key={d.id}
                className="group flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1.5 transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:shadow-[var(--glow-md)]"
              >
                <button
                  type="button"
                  onClick={() => open(d)}
                  className="min-w-0 flex-1 truncate text-left text-[13px] text-fg-muted transition hover:text-fg"
                  title="Abrir tarea"
                >
                  {d.title}
                </button>
                <StatusChip status={d.status} />
                <button
                  type="button"
                  onClick={() => unlink(d)}
                  className="shrink-0 rounded p-0.5 text-fg-faint opacity-0 transition hover:bg-surface-hover hover:text-fg group-hover:opacity-100"
                  title="Quitar dependencia"
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}
        <TaskPicker onSelect={link} excludeIds={excludeIds} />
      </div>

      {blocks.length > 0 && (
        <div className="space-y-2">
          <div className={sectionTitleCls}>
            <GitBranch size={14} /> Bloquea a
            <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] tabular-nums text-fg-subtle">
              {blocks.length}
            </span>
          </div>
          <div className="flex flex-col gap-1">
            {blocks.map((d) => (
              <button
                key={d.id}
                type="button"
                onClick={() => open(d)}
                className="group flex items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-2 py-1.5 text-left transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:border-[rgba(255,255,255,0.12)] hover:shadow-[var(--glow-md)]"
                title="Abrir tarea"
              >
                <span className="min-w-0 flex-1 truncate text-[13px] text-fg-muted group-hover:text-fg">
                  {d.title}
                </span>
                <StatusChip status={d.status} />
                <ArrowUpRight
                  size={13}
                  className="shrink-0 text-fg-faint transition group-hover:text-fg"
                />
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  )
}
