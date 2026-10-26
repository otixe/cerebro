import type { ReactNode } from 'react'
import { Ban } from 'lucide-react'
import { useAppStore } from '../store'
import { orgLabel, projectOptions } from './taskShared'

interface Props {
  title: string
  projectFilter: string
  onProjectFilter: (p: string) => void
  showCancelled: boolean
  onToggleCancelled: () => void
  onNew: () => void
  /** Filtro de ORGANIZACION (cambio de implementación). OPCIONALES a proposito: esta
   *  toolbar la comparten Lista, Calendario y Timeline; si una vista no los
   *  pasa, la fila de organizacion simplemente no se renderiza. */
  orgFilter?: string
  onOrgFilter?: (o: string) => void
  /** Controles extra (nav de mes, filtro de estado) alineados a la derecha. */
  children?: ReactNode
}

/**
 * Toolbar común de las vistas de tareas (Lista / Calendario / Timeline):
 * filtro de organización + proyecto, toggle de canceladas, botón + Nueva tarea.
 * El filtro de PROYECTO queda ACOTADO a la organización elegida: con 49 proyectos
 * y varias variantes del mismo nombre (acme/acme/ExampleOrg/example-suite...), listarlos
 * todos juntos es justamente el desorden que este cambio viene a resolver.
 * Consistente con la toolbar del Board. Es scrollable en móvil (flex-wrap).
 */
export default function TaskFilterBar({
  title,
  projectFilter,
  onProjectFilter,
  showCancelled,
  onToggleCancelled,
  onNew,
  orgFilter,
  onOrgFilter,
  children,
}: Props) {
  const meta = useAppStore((s) => s.meta)
  const ORGS = meta?.organizations ?? []
  const orgActive = orgFilter ?? 'Todos'
  // Proyectos visibles: acotados a la org elegida cuando hay projects_v2.
  // Sin projects_v2 (backend viejo) cae a meta.projects sin filtrar.
  const PROJECTS = projectOptions(
    orgActive === 'Todos' ? '' : orgActive,
    meta?.projects_v2,
    meta?.projects
  )
  const showOrgRow = Boolean(onOrgFilter) && ORGS.length > 0
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-[var(--border-strong)] px-5 py-3">
      <h1 className="text-sm font-semibold text-fg">{title}</h1>
      {showOrgRow && (
        <div className="flex items-center gap-1 rounded-md bg-surface p-0.5">
          {['Todos', ...ORGS.map((o) => o.id)].map((oid) => (
            <button
              key={oid}
              onClick={() => {
                onOrgFilter?.(oid)
                // Cambiar de organizacion resetea el proyecto: el que estaba
                // elegido puede no pertenecer a la org nueva.
                onProjectFilter('Todos')
              }}
              className={`rounded px-2.5 py-1 text-xs font-medium transition ${
                orgActive === oid
                  ? 'bg-accent-bg text-fg'
                  : 'text-fg-subtle hover:text-fg'
              }`}
            >
              {oid === 'Todos' ? 'Todas' : orgLabel(oid, ORGS)}
            </button>
          ))}
        </div>
      )}
      {/* PROYECTO: <select> desplegable, no una fila de botones.
          admin : "abajo salen los proyectos y es una lista enorme, podria
          ser una lista desplegable". Con 22 proyectos activos la fila de botones se
          desbordaba y tapaba el resto de la barra.
          Se muestra el CONTEO de opciones para que se sepa si el filtro de
          organizacion ya acoto la lista (ej. acme deja 2 de 22). */}
      <select
        value={projectFilter}
        onChange={(e) => onProjectFilter(e.target.value)}
        className="max-w-[260px] rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-xs text-fg"
        title={
          orgActive === 'Todos'
            ? 'Proyecto (todos los de la flota)'
            : `Proyecto — acotado a ${orgLabel(orgActive, ORGS)}`
        }
      >
        <option value="Todos">
          {`Todos los proyectos (${PROJECTS.length})`}
        </option>
        {PROJECTS.map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </select>


      <div className="ml-auto flex flex-wrap items-center gap-3">
        {children}
        <button
          onClick={onToggleCancelled}
          aria-pressed={showCancelled}
          title={showCancelled ? 'Ocultar canceladas' : 'Mostrar canceladas'}
          className={`flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition ${
            showCancelled
              ? 'bg-accent-bg text-accent'
              : 'bg-surface text-fg-subtle hover:text-fg'
          }`}
        >
          <Ban size={14} />
          Canceladas
        </button>
        <button
          onClick={onNew}
          className="rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover"
        >
          + Nueva tarea
        </button>
      </div>
    </div>
  )
}
