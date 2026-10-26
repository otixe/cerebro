import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAppStore, useShellStore } from '../store'
import { Inbox, Sparkles, ChevronDown, ChevronUp, ShieldQuestion } from 'lucide-react'
import {
  fetchTasksInbox,
  type InboxTask,
  type Task,
  type TaskStatus,
} from '../lib/api'
import TaskDialog from './TaskDialog'
import { statusMeta, priorityMeta, projectChip, orgChip, orgLabel, taskOrg } from './taskShared'

// El roster de agentes (cuyo inbox se puede inspeccionar) viene del store
// (GET /api/meta -> agents). Ver useAppStore mas abajo. NADA hardcodeado.

/** Estados que agrupa la bandeja (mismos que devuelve tasks_inbox).
 *  'submitted' va PRIMERO (igual que el ORDER BY del backend): es trabajo
 *  terminado que espera tu firma, no trabajo pendiente.
 *  Ojo: esta lista es un SEGUNDO filtro, independiente del SQL. El `grouped` de
 *  abajo descarta en silencio (`map[it.status]?.push`) cualquier estado ausente
 *  aca, asi que arreglar solo el backend dejaba el bug vivo. */
const GROUPS: { key: TaskStatus; label: string }[] = [
  { key: 'submitted', label: 'Espera tu firma' },
  { key: 'todo', label: 'Por hacer' },
  { key: 'in_progress', label: 'En progreso' },
  { key: 'in_review', label: 'En revisión' },
  { key: 'blocked', label: 'Bloqueadas' },
]

/**
 * Vista "Bandeja" (inbox): tareas activas asignadas a un agente, agrupadas por
 * estado, con marca `ready` (deps cumplidas). Un click abre el TaskDialog con
 * la tarea completa (buscada en /api/tasks) para operar el issue (claim, hilo…).
 */
export default function TaskInboxView() {
  const meta = useAppStore((s) => s.meta)
  // Jerarquia Organizacion -> Proyecto (cambio de implementación).
  const organizations = meta?.organizations
  const projectsV2 = meta?.projects_v2
  const AGENTS = meta?.agents ?? []
  // Roster JERARQUICO: titulares (lideres) en la primera linea y sus subagentes
  // en una segunda linea desplegable. Antes se listaban los ~33 agentes planos,
  // lideres y subagentes mezclados, y no se veia de quien dependia cada uno.
  // Los datos ya venian en GET /api/meta (`leads` + `subagents_by_team`): esto es
  // solo UI, no requiere ningun cambio en el Cerebro.
  const LEADS = meta?.leads ?? []
  const SUBS_BY_TEAM = meta?.subagents_by_team ?? {}
  // Agentes sin equipo declarado (ej. admin): se muestran como titulares sueltos
  // para que ninguno quede inalcanzable en el selector.
  const ORPHANS = AGENTS.filter(
    (a) => !LEADS.includes(a) && !Object.values(SUBS_BY_TEAM).some((l) => l.includes(a))
  )
  const [agent, setAgent] = useState<string>('')
  // Titular cuyo submenu de subagentes esta abierto ('' = ninguno).
  const [openTeam, setOpenTeam] = useState<string>('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editTask, setEditTask] = useState<Task | null>(null)
  const openTaskTab = useShellStore((s) => s.openTaskPanel)

  // Seleccion inicial: el primer TITULAR disponible (antes era el primer agente
  // de la lista plana, que podia ser un subagente cualquiera).
  useEffect(() => {
    if (!agent) {
      if (LEADS.length) setAgent(LEADS[0])
      else if (AGENTS.length) setAgent(AGENTS[0])
    }
  }, [agent, LEADS, AGENTS])

  // Si el agente elegido es un subagente, mantener abierto el submenu de SU
  // titular: evita que la segunda linea se cierre y se pierda el contexto de
  // a quien pertenece el agente que estas viendo.
  useEffect(() => {
    if (!agent) return
    const owner = Object.entries(SUBS_BY_TEAM).find(([, subs]) =>
      subs.includes(agent)
    )?.[0]
    if (owner) setOpenTeam(owner)
  }, [agent, SUBS_BY_TEAM])

  const inboxQ = useQuery({
    queryKey: ['tasks_inbox', agent],
    queryFn: () => fetchTasksInbox(agent),
    enabled: !!agent,
  })

  // (Se elimino el query de tareas completas: existia solo para alimentar el
  // fallback que reconstruia un Task a mano para el modal. Con el tab, TaskDetailPage
  // resuelve la tarea contra ['tasks'] por su cuenta y aca basta el id.)

  const grouped = useMemo(() => {
    const map: Record<string, InboxTask[]> = {
      submitted: [], todo: [], in_progress: [], in_review: [], blocked: [],
    }
    for (const it of inboxQ.data?.inbox ?? []) map[it.status]?.push(it)
    return map
  }, [inboxQ.data])

  // ⚠️ : abre la tarea como TAB, no como modal (pedido de admin: "pincho
  // la tarea se abre al lado derecho... y si tiene un archivo al pincharlo se abre a
  // la derecha de este"). Con el modal, abrir un documento relacionado TAPABA la tarea.
  //
  // Y desaparece el fallback que reconstruia un Task a mano cuando la tarea no
  // estaba en el cache: el tab solo necesita el id, y TaskDetailPage resuelve la
  // tarea completa contra ['tasks']. Ese objeto sintetico (con description:null y
  // tags:[]) era ademas un riesgo real: si el usuario guardaba desde ahi, escribia
  // los campos vacios sobre los de verdad. Menos codigo Y un defecto menos.
  function openItem(it: InboxTask) {
    openTaskTab(it.id, it.title)
  }

  const total = inboxQ.data?.inbox.length ?? 0

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar: selector JERARQUICO de agente (titular -> subagentes) */}
      <div className="shrink-0 border-b border-[var(--border-strong)] px-5 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="flex items-center gap-2 text-sm font-semibold text-fg">
            <Inbox size={15} /> Bandeja
          </h1>
          {/* LINEA 1: titulares (lideres). El chevron indica que tiene equipo. */}
          <div className="flex items-center gap-1 overflow-x-auto rounded-md bg-surface p-0.5">
            {[...LEADS, ...ORPHANS].map((lead) => {
              const subs = SUBS_BY_TEAM[lead] ?? []
              // Un titular se marca activo tambien cuando el agente elegido es
              // uno de SUS subagentes: asi nunca se pierde de vista el titular.
              const isOwnTeam = agent === lead || subs.includes(agent)
              const isOpen = openTeam === lead
              return (
                <button
                  key={lead}
                  onClick={() => {
                    setAgent(lead)
                    // Toggle del submenu; si no tiene equipo, no abre nada.
                    setOpenTeam(subs.length && !isOpen ? lead : '')
                  }}
                  className={`flex shrink-0 items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition ${
                    isOwnTeam ? 'bg-accent-bg text-fg' : 'text-fg-subtle hover:text-fg'
                  }`}
                  title={subs.length ? `${subs.length} subagente(s)` : 'Sin equipo'}
                >
                  {lead}
                  {subs.length > 0 && (
                    <>
                      <span className="tabular-nums opacity-60">{subs.length}</span>
                      {isOpen ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
                    </>
                  )}
                </button>
              )
            })}
          </div>
          <span className="ml-auto text-xs text-fg-subtle">
            {total} tarea{total === 1 ? '' : 's'}
          </span>
        </div>

        {/* LINEA 2: subagentes del titular abierto. Solo se renderiza cuando hay
            un submenu desplegado, para no ocupar alto de mas. */}
        {openTeam && (SUBS_BY_TEAM[openTeam] ?? []).length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 pl-1">
            <span className="text-[10px] uppercase tracking-wide text-fg-subtle">
              {openTeam} ›
            </span>
            <button
              onClick={() => setAgent(openTeam)}
              className={`rounded px-2 py-0.5 text-[11px] transition ${
                agent === openTeam
                  ? 'bg-accent-bg text-fg'
                  : 'text-fg-subtle hover:text-fg'
              }`}
            >
              {openTeam} (titular)
            </button>
            {(SUBS_BY_TEAM[openTeam] ?? []).map((s) => (
              <button
                key={s}
                onClick={() => setAgent(s)}
                className={`rounded px-2 py-0.5 text-[11px] transition ${
                  agent === s
                    ? 'bg-accent-bg text-fg'
                    : 'text-fg-subtle hover:bg-surface-hover hover:text-fg'
                }`}
              >
                {/* El prefijo del equipo es redundante bajo su propio titular:
                    'assistant-b-lurtz' se muestra como 'lurtz'. */}
                {s.startsWith(`${openTeam}-`) ? s.slice(openTeam.length + 1) : s}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Contenido: columnas por estado */}
      <div className="min-h-0 flex-1 overflow-auto p-5">
        {inboxQ.isLoading && <p className="text-xs text-fg-subtle">Cargando bandeja…</p>}
        {inboxQ.isError && (
          <p className="text-xs text-red-400">No se pudo cargar la bandeja.</p>
        )}
        {!inboxQ.isLoading && !inboxQ.isError && total === 0 && (
          <p className="text-xs text-fg-subtle/60">
            Sin tareas activas para <span className="text-fg">{agent}</span>.
          </p>
        )}
        {!inboxQ.isLoading && !inboxQ.isError && total > 0 && (
          <div className="flex flex-wrap gap-4">
            {GROUPS.map((g) => {
              const items = grouped[g.key] ?? []
              const sm = statusMeta(g.key)
              return (
                <div key={g.key} className="flex min-w-[260px] flex-1 flex-col">
                  <div className="mb-2 flex items-center gap-2 px-1">
                    <span className={`h-1.5 w-1.5 rounded-full ${sm.dot}`} />
                    <span className="text-[11px] font-semibold uppercase tracking-wider text-fg-muted">
                      {g.label}
                    </span>
                    <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
                      {items.length}
                    </span>
                  </div>
                  <div className="flex flex-1 flex-col gap-2 rounded-lg border border-[var(--border-subtle)] glass p-2">
                    {items.map((it) => {
                      const pm = priorityMeta(it.priority)
                      return (
                        <button
                          key={it.id}
                          onClick={() => openItem(it)}
                          className={`group rounded-lg border border-[var(--border-strong)] border-l-2 ${pm.border} bg-surface p-3 text-left shadow-sm transition hover:bg-surface-hover`}
                        >
                          <div className="flex items-start gap-1.5">
                            <p className="flex-1 text-[13px] font-medium leading-snug text-fg">
                              {it.title}
                            </p>
                            {it.status === 'submitted' && (
                              <span
                                className="mt-0.5 inline-flex shrink-0 items-center gap-0.5 rounded bg-teal-500/15 px-1 py-0.5 text-[9px] font-medium text-teal-300"
                                title="Entregada por el executor: espera tu firma (aprobar o pedir cambios)"
                              >
                                <ShieldQuestion size={9} /> espera tu firma
                              </span>
                            )}
                            {/* `ready` = lista para EMPEZAR. No aplica a una tarea ya
                                entregada, y mostrar ambos badges se contradice. */}
                            {it.ready && it.status !== 'submitted' && (
                              <span
                                className="mt-0.5 inline-flex shrink-0 items-center gap-0.5 rounded bg-accent-bg px-1 py-0.5 text-[9px] font-medium text-accent"
                                title="Lista para empezar (dependencias cumplidas)"
                              >
                                <Sparkles size={9} /> lista
                              </span>
                            )}
                          </div>
                          <div className="mt-2 flex flex-wrap items-center gap-1.5">
                            {/* Chip de ORGANIZACION antes del de proyecto (cambio de implementación) */}
                            {(() => {
                              const oid = taskOrg(it, projectsV2)
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
                            {it.project && (
                              <span
                                className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${projectChip(
                                  it.project
                                )}`}
                              >
                                {it.project}
                              </span>
                            )}
                            {it.claimed_by && (
                              <span className="rounded bg-surface-hover px-1.5 py-0.5 text-[10px] text-fg-subtle">
                                {it.claimed_by}
                              </span>
                            )}
                            {it.status === 'blocked' && it.blocked_reason && (
                              <span className="text-[10px] text-[var(--warn)]">
                                {it.blocked_reason}
                              </span>
                            )}
                          </div>
                        </button>
                      )
                    })}
                    {items.length === 0 && (
                      <div className="flex flex-1 items-center justify-center py-6 text-[11px] text-fg-subtle/60">
                        —
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <TaskDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        task={editTask}
        onOpenTask={(t) => {
          setEditTask(t)
          setDialogOpen(true)
        }}
      />
    </div>
  )
}
