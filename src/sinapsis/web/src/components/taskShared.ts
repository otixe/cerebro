import type { Organization, ProjectRef, TaskStatus } from '../lib/api'

// Los ambitos de proyecto YA NO se hardcodean: se obtienen dinamicamente del backend
// (GET /api/meta -> projects) via el store. Aca solo vive la logica de color del chip,
// que tolera cualquier proyecto arbitrario (paleta ciclica por hash del nombre).

// Paleta de colores para chips de proyecto (neutra, sin nombres fijos).
const PROJECT_PALETTE: string[] = [
  'bg-violet-500/15 text-violet-300',
  'bg-emerald-500/15 text-emerald-300',
  'bg-sky-500/15 text-sky-300',
  'bg-pink-500/15 text-pink-300',
  'bg-amber-500/15 text-amber-300',
  'bg-teal-500/15 text-teal-300',
  'bg-rose-500/15 text-rose-300',
  'bg-indigo-500/15 text-indigo-300',
]

export function projectChip(p?: string | null): string {
  const name = (p || '').trim()
  if (!name) return 'bg-surface text-fg-subtle'
  // hash estable del nombre -> indice de paleta (color consistente por proyecto,
  // sin necesidad de conocer los proyectos de antemano).
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return PROJECT_PALETTE[h % PROJECT_PALETTE.length]
}

// ---------- Organizacion (nivel superior sobre el proyecto, ) ----------
// El color de la organizacion viene de la BD (`organizations.color`, hex) via
// GET /api/meta. NO se hardcodea aca ni se construyen clases de Tailwind
// dinamicas (la purga JIT las eliminaria): se aplica con estilo inline.

/** Color hex neutro cuando la organizacion no declara `color`. */
const ORG_FALLBACK_COLOR = '#64748b'

/** Estilo inline del chip de organizacion a partir del color hex de la org.
 *  Visualmente distinto del chip de proyecto: borde + punto solido, no relleno
 *  plano. Devuelve tambien las clases estaticas que acompanan. */
export interface OrgChipStyle {
  /** Clases Tailwind estaticas (sobreviven la purga JIT). */
  className: string
  /** Estilo inline con el color real de la organizacion. */
  style: { color: string; backgroundColor: string; borderColor: string }
  /** Color solido para el punto indicador. */
  dotColor: string
}

/** Normaliza un color a hex valido (#rgb/#rrggbb); si no lo es, cae al neutro. */
function safeHex(c?: string | null): string {
  const v = (c || '').trim()
  return /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(v) ? v : ORG_FALLBACK_COLOR
}

/**
 * Chip de organizacion. `orgId` es el slug ('acme'); el color y el nombre
 * legible salen de `organizations` (meta). Si la org no esta en la lista, se
 * usa el color neutro y el propio id como etiqueta.
 */
export function orgChip(
  orgId?: string | null,
  organizations?: Organization[]
): OrgChipStyle {
  const org = organizations?.find((o) => o.id === orgId)
  const hex = safeHex(org?.color)
  return {
    className:
      'inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold',
    style: {
      color: hex,
      // assistant-e en hex de 8 digitos: relleno tenue del mismo tono.
      backgroundColor: hex + '1f',
      borderColor: hex + '59',
    },
    dotColor: hex,
  }
}

/** Nombre legible de una organizacion ('acme'), no el slug ('acme').
 *  Si la org no esta en `organizations`, devuelve el id capitalizado. */
export function orgLabel(
  orgId?: string | null,
  organizations?: Organization[]
): string {
  const id = (orgId || '').trim()
  if (!id) return ''
  const org = organizations?.find((o) => o.id === id)
  if (org?.name) return org.name
  return id.charAt(0).toUpperCase() + id.slice(1)
}

// ---------- Resolucion Proyecto <-> Organizacion ----------
// Todo tolera que `projects_v2` NO exista (backend previo al deploy): en ese caso
// se degrada al comportamiento historico basado en `meta.projects: string[]`.

/** Opcion de proyecto para un <select>: id real + etiqueta legible. */
export interface ProjectOption {
  id: string
  label: string
  org_id: string
}

/**
 * Organizacion a la que pertenece un proyecto, segun `projects_v2`.
 * '' si no se puede determinar (proyecto desconocido o backend viejo).
 * Se usa como respaldo cuando la tarea no trae `organization` propia.
 */
export function orgOfProject(
  projectId?: string | null,
  projects?: ProjectRef[]
): string {
  const id = (projectId || '').trim()
  if (!id) return ''
  return projects?.find((p) => p.id === id)?.org_id ?? ''
}

/**
 * Organizacion efectiva de una tarea: la propia `organization` si el backend la
 * manda; si no, la del proyecto via `projects_v2`. '' si no hay forma de saberlo.
 */
export function taskOrg(
  t: { organization?: string | null; project?: string | null },
  projects?: ProjectRef[]
): string {
  return (t.organization || '').trim() || orgOfProject(t.project, projects)
}

/**
 * Proyectos elegibles, opcionalmente ACOTADOS a una organizacion.
 * - Con `projects_v2`: filtra por `org_id` y usa el `name` como etiqueta.
 * - Sin `projects_v2` (backend viejo): devuelve `projects` (string[]) tal cual,
 *   sin filtrar, porque no hay informacion de organizacion. La UI sigue usable.
 */
export function projectOptions(
  orgId: string,
  projectsV2?: ProjectRef[],
  projectsLegacy?: string[]
): ProjectOption[] {
  if (projectsV2 && projectsV2.length) {
    return projectsV2
      .filter((p) => !orgId || p.org_id === orgId)
      .map((p) => ({ id: p.id, label: p.name || p.id, org_id: p.org_id }))
  }
  return (projectsLegacy ?? []).map((p) => ({ id: p, label: p, org_id: '' }))
}

/** Etiqueta legible de un proyecto ('example-suite'), cayendo al id si no se conoce. */
export function projectLabel(
  projectId?: string | null,
  projects?: ProjectRef[]
): string {
  const id = (projectId || '').trim()
  if (!id) return ''
  return projects?.find((p) => p.id === id)?.name || id
}

// Estado → etiqueta + color de punto/pill (dark-native, sutil).
export const STATUS_META: Record<
  TaskStatus,
  { label: string; dot: string; text: string }
> = {
  todo: { label: 'Por hacer', dot: 'bg-fg-subtle', text: 'text-fg-subtle' },
  in_progress: { label: 'En progreso', dot: 'bg-sky-400', text: 'text-sky-300' },
  // 'submitted' = entregada por el executor, esperando gate del auditor. El tipo
  // NO lo tenia y el backend SI lo usa (8 tareas hoy) => cualquier vista que
  // indexara STATUS_META[status] con una tarea submitted daba undefined y
  // crasheaba el render. Ese era el bug de la pantalla en negro ().
  submitted: { label: 'Entregada', dot: 'bg-teal-400', text: 'text-teal-300' },
  in_review: { label: 'En revisión', dot: 'bg-violet-400', text: 'text-violet-300' },
  blocked: { label: 'Bloqueada', dot: 'bg-[var(--warn)]', text: 'text-[var(--warn)]' },
  deferred: { label: 'Pospuesta', dot: 'bg-zinc-400', text: 'text-zinc-400' },
  done: { label: 'Terminada', dot: 'bg-emerald-400', text: 'text-emerald-300' },
  cancelled: { label: 'Cancelada', dot: 'bg-fg-faint', text: 'text-fg-faint' },
}

export function statusMeta(s: TaskStatus) {
  return STATUS_META[s] ?? STATUS_META.todo
}

// Prioridad → etiqueta + color sutil (texto + punto + borde izq).
// `border` se declara como literal estático para que sobreviva la purga JIT
// de Tailwind (no se construye dinámicamente).
export const PRIORITY_META: Record<
  string,
  { label: string; dot: string; text: string; border: string }
> = {
  high: {
    label: 'Alta',
    dot: 'bg-red-400',
    text: 'text-red-300',
    border: 'border-l-red-400/70',
  },
  medium: {
    label: 'Media',
    dot: 'bg-amber-400',
    text: 'text-amber-300',
    border: 'border-l-amber-400/60',
  },
  low: {
    label: 'Baja',
    dot: 'bg-sky-400',
    text: 'text-sky-300',
    border: 'border-l-sky-400/50',
  },
}

export function priorityMeta(p?: string | null) {
  return PRIORITY_META[p || 'medium'] || PRIORITY_META.medium
}

// Orden para ordenar tablas/timeline. Menor = primero.
export const PRIORITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 }
export const STATUS_ORDER: Record<string, number> = {
  in_progress: 0,
  // 'submitted' FALTABA y devolvia undefined => cualquier sort que lo use se
  // comporta de forma indefinida. Va junto a in_review: las dos son "esperando
  // gate", y lo entregado se mira antes que lo bloqueado.
  submitted: 1,
  in_review: 2,
  blocked: 3,
  todo: 4,
  deferred: 5,
  done: 6,
  cancelled: 7,
}

// ---------- Fechas (zona horaria del panel: VITE_TIMEZONE o la del navegador) ----------
export const TZ: string = (import.meta.env.VITE_TIMEZONE || Intl.DateTimeFormat().resolvedOptions().timeZone)

export const MONTH_NAMES = [
  'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
  'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
]

// Semana con inicio en Lunes.
export const WEEKDAY_SHORT = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

export interface YearMonth {
  year: number
  month: number // 1-12
}

/** Año-mes actual en la zona del panel. */
export function currentYearMonth(): YearMonth {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
  }).formatToParts(new Date())
  return {
    year: Number(parts.find((p) => p.type === 'year')?.value),
    month: Number(parts.find((p) => p.type === 'month')?.value),
  }
}

/** Fecha de hoy como 'YYYY-MM-DD' en la zona del panel. */
export function todayISO(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

export function sameYearMonth(a: YearMonth, b: YearMonth): boolean {
  return a.year === b.year && a.month === b.month
}

export function addMonths(ym: YearMonth, delta: number): YearMonth {
  const zero = ym.year * 12 + (ym.month - 1) + delta
  return { year: Math.floor(zero / 12), month: (zero % 12) + 1 }
}

/**
 * due_date llega como 'YYYY-MM-DD'. Se parsea a un Date LOCAL (mediodía) para
 * evitar el corrimiento de zona horaria que produce `new Date('YYYY-MM-DD')`
 * (que lo interpreta como UTC medianoche).
 */
export function parseDueLocal(s?: string | null): Date | null {
  if (!s) return null
  const m = s.trim().match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12, 0, 0)
}

/** Componente año-mes de una due_date 'YYYY-MM-DD'. */
export function dueYearMonth(s?: string | null): YearMonth | null {
  const m = s?.trim().match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  return { year: Number(m[1]), month: Number(m[2]) }
}

/** Día del mes (1-31) de una due_date, o null. */
export function dueDay(s?: string | null): number | null {
  const m = s?.trim().match(/^(\d{4})-(\d{2})-(\d{2})/)
  return m ? Number(m[3]) : null
}

/** Formato corto legible: '6 jul'. Vacío si no hay fecha. */
export function formatDueShort(s?: string | null): string {
  const d = parseDueLocal(s)
  if (!d) return ''
  return new Intl.DateTimeFormat('es-CL', {
    day: 'numeric',
    month: 'short',
    timeZone: TZ,
  }).format(d)
}

/** Fecha de un Date como 'YYYY-MM-DD' local. */
export function toLocalISO(d: Date): string {
  const y = d.getFullYear()
  const mo = String(d.getMonth() + 1).padStart(2, '0')
  const da = String(d.getDate()).padStart(2, '0')
  return `${y}-${mo}-${da}`
}

/** Lunes de la semana que contiene a `d` (Date local). */
export function weekStart(d: Date): Date {
  const dow = (d.getDay() + 6) % 7 // Lun=0 … Dom=6
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - dow, 12, 0, 0)
}

/** Etiqueta de rango de semana: '30 jun – 6 jul'. */
export function weekLabel(monday: Date): string {
  const sunday = new Date(
    monday.getFullYear(),
    monday.getMonth(),
    monday.getDate() + 6,
    12,
    0,
    0
  )
  const fmt = (d: Date) =>
    new Intl.DateTimeFormat('es-CL', { day: 'numeric', month: 'short', timeZone: TZ }).format(d)
  return `${fmt(monday)} – ${fmt(sunday)}`
}
