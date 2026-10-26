import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Sun,
  CloudSun,
  Cloud,
  CloudFog,
  CloudDrizzle,
  CloudRain,
  CloudSnow,
  CloudLightning,
  Clock,
  AlertTriangle,
  CalendarDays,
  LayoutGrid,
  History,
  FileText,
  Image as ImageIcon,
  FileType,
  File as FileIcon,
  ChevronRight,
  type LucideIcon,
} from 'lucide-react'
import {
  fetchTasks,
  fetchRecent,
  fetchWeather,
  WEATHER_ENABLED,
  WEATHER_LABEL,
  type Task,
  type TaskStatus,
  type RecentFile,
} from '../lib/api'
import { useShellStore, useAppStore } from '../store'
import { projectChip, orgChip, orgLabel, taskOrg } from './taskShared'
import TaskDialog from './TaskDialog'

const TZ: string = (import.meta.env.VITE_TIMEZONE || Intl.DateTimeFormat().resolvedOptions().timeZone)
// Zustand useSyncExternalStore requires a stable snapshot while meta has not loaded.
const EMPTY_PROJECTS: string[] = []
// PROJECTS viene del store (GET /api/meta). Ver useAppStore en el componente.
const ACTIVE_STATUSES: TaskStatus[] = ['todo', 'in_progress', 'blocked']

// El color del chip de proyecto usa el helper compartido projectChip() (hash-based,
// tolera cualquier proyecto). El 'glow' de hover usa una paleta ciclica local por hash.
const PROJECT_GLOW_PALETTE: string[] = [
  'hover:border-violet-400/50 hover:shadow-[0_0_0_1px_rgba(167,139,250,0.25)]',
  'hover:border-emerald-400/50 hover:shadow-[0_0_0_1px_rgba(52,211,153,0.25)]',
  'hover:border-sky-400/50 hover:shadow-[0_0_0_1px_rgba(56,189,248,0.25)]',
  'hover:border-pink-400/50 hover:shadow-[0_0_0_1px_rgba(244,114,182,0.25)]',
  'hover:border-amber-400/50 hover:shadow-[0_0_0_1px_rgba(251,191,36,0.25)]',
  'hover:border-teal-400/50 hover:shadow-[0_0_0_1px_rgba(45,212,191,0.25)]',
]
function projectGlow(p?: string | null): string {
  const name = (p || '').trim()
  if (!name) return ''
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return PROJECT_GLOW_PALETTE[h % PROJECT_GLOW_PALETTE.length]
}

/** Fecha de hoy en la zona del panel como 'YYYY-MM-DD' (comparable lexicográficamente). */
function panelToday(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: TZ,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
}

/** Mapa WMO weather_code → icono lucide + etiqueta. */
function weatherFor(code: number): { Icon: LucideIcon; label: string } {
  if (code === 0) return { Icon: Sun, label: 'Despejado' }
  if (code === 1 || code === 2) return { Icon: CloudSun, label: 'Parcial' }
  if (code === 3) return { Icon: Cloud, label: 'Nublado' }
  if (code === 45 || code === 48) return { Icon: CloudFog, label: 'Niebla' }
  if (code >= 51 && code <= 57) return { Icon: CloudDrizzle, label: 'Llovizna' }
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82))
    return { Icon: CloudRain, label: 'Lluvia' }
  if ((code >= 71 && code <= 77) || code === 85 || code === 86)
    return { Icon: CloudSnow, label: 'Nieve' }
  if (code >= 95) return { Icon: CloudLightning, label: 'Tormenta' }
  return { Icon: Cloud, label: '—' }
}

/** Tiempo relativo en español a partir de un epoch en segundos. */
function relativeTime(epochSec: number): string {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSec))
  if (s < 60) return 'recién'
  const m = Math.floor(s / 60)
  if (m < 60) return `hace ${m} min`
  const h = Math.floor(m / 60)
  if (h < 24) return `hace ${h} h`
  const d = Math.floor(h / 24)
  if (d < 30) return `hace ${d} d`
  const mo = Math.floor(d / 30)
  if (mo < 12) return `hace ${mo} mes${mo === 1 ? '' : 'es'}`
  const y = Math.floor(d / 365)
  return `hace ${y} año${y === 1 ? '' : 's'}`
}

const IMG_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'])

/** Icono lucide según el tipo de archivo. */
function iconForFile(f: RecentFile): LucideIcon {
  if (f.is_md) return FileText
  if (IMG_EXTS.has(f.ext)) return ImageIcon
  if (f.ext === '.pdf') return FileType
  return FileIcon
}

/** Nombre a mostrar: sin extensión para .md, nombre completo para adjuntos. */
function displayName(f: RecentFile): string {
  const base = f.path.split('/').pop() ?? f.path
  return f.is_md ? base.replace(/\.md$/, '') : base
}

/** Carpeta contenedora (para contexto sutil). '' si está en la raíz. */
function parentDir(path: string): string {
  const parts = path.split('/')
  parts.pop()
  return parts.join('/')
}

function ClockWidget() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const time = new Intl.DateTimeFormat('es-CL', {
    timeZone: TZ,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(now)
  const date = new Intl.DateTimeFormat('es-CL', {
    timeZone: TZ,
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  }).format(now)

  return (
    <div className="flex items-center gap-2.5">
      <Clock size={18} className="text-fg-faint" />
      <div>
        <div className="font-mono text-[22px] font-medium leading-none tracking-tight text-fg tabular-nums">
          {time}
        </div>
        <div className="mt-1 text-[12px] capitalize text-fg-subtle">{date}</div>
      </div>
    </div>
  )
}

function WeatherWidget() {
  const { data, isError } = useQuery({
    queryKey: ['weather'],
    enabled: WEATHER_ENABLED,
    queryFn: fetchWeather,
    staleTime: 10 * 60 * 1000,
    refetchInterval: 15 * 60 * 1000,
    retry: 1,
  })
  // Degradación elegante: si falla o aún no hay dato, no renderizar nada.
  if (isError || !data) return null
  const { Icon, label } = weatherFor(data.weatherCode)
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
      <Icon size={22} className="text-accent" />
      <div className="leading-tight">
        <div className="text-[18px] font-medium text-fg tabular-nums">
          {Math.round(data.temperature)}°C
        </div>
        <div className="text-[11px] text-fg-subtle">{WEATHER_LABEL ? `${WEATHER_LABEL} · ` : ''}{label}</div>
      </div>
    </div>
  )
}

function TaskRow({ task, onClick }: { task: Task; onClick: () => void }) {
  // Jerarquia Organizacion -> Proyecto (cambio de implementación).
  const organizations = useAppStore((s) => s.meta?.organizations)
  const projectsV2 = useAppStore((s) => s.meta?.projects_v2)
  return (
    <button
      onClick={onClick}
      className="group flex w-full items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-3 py-2 text-left transition hover:border-[rgba(255,255,255,0.12)] hover:bg-[rgba(255,255,255,0.03)]"
    >
      <span className="min-w-0 flex-1 truncate text-[13px] text-fg-muted group-hover:text-fg">
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
          className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
            projectChip(task.project)
          }`}
        >
          {task.project}
        </span>
      )}
      {task.due_date && (
        <span className="shrink-0 text-[10px] text-fg-subtle">{task.due_date}</span>
      )}
    </button>
  )
}

function Section({
  icon: Icon,
  title,
  count,
  accent,
  children,
}: {
  icon: LucideIcon
  title: string
  count: number
  accent?: boolean
  children: React.ReactNode
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Icon size={15} className={accent ? 'text-danger' : 'text-fg-faint'} />
        <h2
          className={`text-[12px] font-semibold uppercase tracking-wider ${
            accent ? 'text-danger/90' : 'text-fg-muted'
          }`}
        >
          {title}
        </h2>
        <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
          {count}
        </span>
      </div>
      <div className="flex flex-col gap-1.5">{children}</div>
    </section>
  )
}

/** Fila de un archivo reciente. .md abre en tab; adjuntos se muestran discretos. */
function RecentRow({ file, onOpen }: { file: RecentFile; onOpen: (path: string) => void }) {
  const Icon = iconForFile(file)
  const dir = parentDir(file.path)
  const when = relativeTime(file.mtime)

  const inner = (
    <>
      <Icon
        size={15}
        className={file.is_md ? 'shrink-0 text-accent' : 'shrink-0 text-fg-faint'}
      />
      <span className="min-w-0 flex-1 truncate">
        <span
          className={`text-[13px] ${
            file.is_md ? 'text-fg-muted group-hover:text-fg' : 'text-fg-subtle'
          }`}
        >
          {displayName(file)}
        </span>
        {dir && (
          <span className="ml-2 text-[11px] text-fg-subtle/60">{dir}</span>
        )}
      </span>
      <span className="shrink-0 text-[10px] tabular-nums text-fg-subtle">{when}</span>
    </>
  )

  if (file.is_md) {
    return (
      <button
        onClick={() => onOpen(file.path)}
        className="group flex w-full items-center gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-3 py-2 text-left transition hover:border-[rgba(255,255,255,0.12)] hover:bg-[rgba(255,255,255,0.03)]"
      >
        {inner}
      </button>
    )
  }
  // Adjuntos no-md: discretos, no abren tab.
  return (
    <div
      title={file.path}
      className="flex w-full items-center gap-2 rounded-md px-3 py-2 opacity-70"
    >
      {inner}
    </div>
  )
}

export default function HomeView() {
  const PROJECTS = useAppStore((s) => s.meta?.projects ?? EMPTY_PROJECTS)
  const { data } = useQuery({ queryKey: ['tasks'], queryFn: fetchTasks })
  const { data: recentData } = useQuery({
    queryKey: ['recent'],
    queryFn: () => fetchRecent(20),
    staleTime: 30 * 1000,
    refetchInterval: 60 * 1000,
  })
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)

  const openBoard = useShellStore((s) => s.openBoard)
  const openNote = useShellStore((s) => s.openNote)
  const setBoardProjectFilter = useShellStore((s) => s.setBoardProjectFilter)

  const today = panelToday()
  const tasks = useMemo(() => data?.tasks ?? [], [data])
  const recent = useMemo(() => recentData?.recent ?? [], [recentData])

  const active = useMemo(
    () => tasks.filter((t) => ACTIVE_STATUSES.includes(t.status)),
    [tasks]
  )
  const hoy = useMemo(
    () => active.filter((t) => t.due_date === today),
    [active, today]
  )
  const atrasadas = useMemo(
    () => active.filter((t) => t.due_date && t.due_date < today),
    [active, today]
  )
  const countByProject = useMemo(() => {
    const map: Record<string, number> = {}
    for (const p of PROJECTS) map[p] = 0
    for (const t of active) {
      const proj = t.project && map[t.project] !== undefined ? t.project : 'Personal'
      map[proj] += 1
    }
    return map
  }, [active])

  function openTask(t: Task) {
    setDetailTask(t)
    setDialogOpen(true)
  }

  /** Ir al Tablero filtrado por proyecto. */
  function goToProject(project: string) {
    setBoardProjectFilter(project)
    openBoard()
  }

  return (
    <div className="h-full overflow-y-auto">
      {/* Marcador: SINAPSIS_HOME_REDISEÑO_V1 — cards glass agrupadas (cabecera,
          proyectos, hoy/atrasadas, recientes) para jerarquia clara y para que
          el fondo ambient de particulas se note a traves del panel. Mismos
          datos/acciones/endpoints; solo composicion, spacing y superficies. */}
      <div className="mx-auto max-w-[1100px] px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        {/* Cabecera: reloj + clima */}
        <header className="glass-strong mb-5 flex flex-col gap-4 rounded-2xl border border-[var(--border-subtle)] p-4 sm:mb-6 sm:flex-row sm:items-center sm:justify-between sm:p-5 lg:mb-8">
          <ClockWidget />
          <WeatherWidget />
        </header>

        {/* Proyectos: tarjeta por proyecto → Tablero filtrado */}
        <section className="glass-strong mb-5 rounded-2xl border border-[var(--border-subtle)] p-4 sm:mb-6 sm:p-5 lg:mb-8">
          <div className="mb-3 flex items-center gap-2">
            <LayoutGrid size={15} className="text-fg-faint" />
            <h2 className="text-[12px] font-semibold uppercase tracking-wider text-fg-muted">
              Proyectos
            </h2>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
            {PROJECTS.map((p) => {
              const count = countByProject[p] ?? 0
              return (
                <button
                  key={p}
                  onClick={() => goToProject(p)}
                  title={`Ver ${p} en el Tablero`}
                  className={`group flex flex-col gap-2.5 rounded-xl border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.03)] p-3.5 text-left transition sm:gap-3 sm:p-4 ${
                    projectGlow(p)
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${projectChip(p)}`}
                    >
                      {p}
                    </span>
                    <ChevronRight
                      size={15}
                      className="text-fg-subtle/40 transition group-hover:translate-x-0.5 group-hover:text-fg-subtle"
                    />
                  </div>
                  <div>
                    <span className="text-[24px] font-semibold leading-none tabular-nums text-fg sm:text-[26px]">
                      {count}
                    </span>
                    <span className="ml-1.5 text-[12px] text-fg-subtle">
                      activa{count === 1 ? '' : 's'}
                    </span>
                  </div>
                </button>
              )
            })}
          </div>
        </section>

        <div className="mb-5 grid grid-cols-1 gap-4 sm:mb-6 lg:mb-8 lg:grid-cols-2 lg:gap-6">
          <section className="glass-strong rounded-2xl border border-[var(--border-subtle)] p-4 sm:p-5">
            <Section icon={CalendarDays} title="Hoy" count={hoy.length}>
              {hoy.length === 0 ? (
                <p className="text-[12px] text-fg-subtle">Nada vence hoy.</p>
              ) : (
                hoy.map((t) => (
                  <TaskRow key={t.id} task={t} onClick={() => openTask(t)} />
                ))
              )}
            </Section>
          </section>

          <section className="glass-strong rounded-2xl border border-[var(--border-subtle)] p-4 sm:p-5">
            <Section
              icon={AlertTriangle}
              title="Atrasadas"
              count={atrasadas.length}
              accent
            >
              {atrasadas.length === 0 ? (
                <p className="text-[12px] text-fg-subtle">Sin tareas atrasadas.</p>
              ) : (
                atrasadas.map((t) => (
                  <TaskRow key={t.id} task={t} onClick={() => openTask(t)} />
                ))
              )}
            </Section>
          </section>
        </div>

        {/* Recientes: últimas notas editadas / adjuntos subidos */}
        <section className="glass-strong rounded-2xl border border-[var(--border-subtle)] p-4 sm:p-5">
          <div className="mb-3 flex items-center gap-2">
            <History size={15} className="text-fg-faint" />
            <h2 className="text-[12px] font-semibold uppercase tracking-wider text-fg-muted">
              Recientes
            </h2>
            <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-fg-subtle">
              {recent.length}
            </span>
          </div>
          {recent.length === 0 ? (
            <p className="text-[12px] text-fg-subtle">Sin actividad reciente.</p>
          ) : (
            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {recent.map((f) => (
                <RecentRow key={f.path} file={f} onOpen={openNote} />
              ))}
            </div>
          )}
        </section>
      </div>

      <TaskDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        task={detailTask}
        onOpenTask={openTask}
      />
    </div>
  )
}
