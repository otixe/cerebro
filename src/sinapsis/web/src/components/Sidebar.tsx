import { useQuery } from '@tanstack/react-query'
import {
  Search,
  FilePlus2,
  Zap,
  Home,
  FileText,
  Database,
  BookOpen,
  Sparkles,
  CheckSquare,
  Network,
  Hash,
  Radio,
  MessagesSquare,
  Inbox,
  Star,
  PanelLeftClose,
  PanelLeftOpen,
  Brain,
  X,
  type LucideIcon,
} from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import { useShellStore, type SectionKey } from '../store'
import { useIsDesktop } from '../hooks/useIsDesktop'
import { fetchMe } from '../lib/api'
import { useQuickNote } from '../hooks/useQuickNote'

interface SectionDef {
  key: SectionKey
  label: string
  icon: LucideIcon
}

const SECTIONS: SectionDef[] = [
  { key: 'inicio', label: 'Inicio', icon: Home },
  { key: 'pinned', label: 'Pinned', icon: Star },
  { key: 'notas', label: 'Notas', icon: FileText },
  { key: 'bases', label: 'Bases', icon: Database },
  { key: 'tareas', label: 'Tareas', icon: CheckSquare },
  { key: 'biblioteca', label: 'Biblioteca', icon: BookOpen },
  { key: 'skills', label: 'Skills', icon: Sparkles },
  { key: 'grafo', label: 'Grafo', icon: Network },
  { key: 'tags', label: 'Tags', icon: Hash },
]

/** Sección "Bus": se OFRECE solo al lector autorizado del bus (can_read_bus lo
 *  resuelve el backend con el mismo guard que protege /api/bus/*). Ocultarla es
 *  cosmético: el permiso REAL lo hace cumplir el BFF con 403. */
const BUS_SECTION: SectionDef = { key: 'bus', label: 'Bus', icon: Radio }
const CORRESPONDENCIA_SECTION: SectionDef = { key: 'correspondencia', label: 'Correspondencia', icon: Inbox }

/** Sección "Chat": se OFRECE solo al remitente autorizado (can_use_chat lo resuelve
 *  el backend con el mismo guard que protege /api/chat/*). Permiso SEPARADO del bus:
 *  leer el bus es una lectura, un send es EJECUCIÓN POR DELEGACIÓN en un proceso pi.
 *  Ocultarla es cosmético: el permiso REAL lo hace cumplir el BFF con 403. */
const CHAT_SECTION: SectionDef = { key: 'chat', label: 'Chat', icon: MessagesSquare }

interface SidebarProps {
  /** Drawer abierto (solo relevante en móvil). */
  mobileOpen?: boolean
  /** Cierra el drawer móvil (tras navegar o accionar). */
  onClose?: () => void
}

export default function Sidebar({ mobileOpen = false, onClose }: SidebarProps) {
  const collapsed = useShellStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useShellStore((s) => s.toggleSidebar)
  const setSidebarCollapsed = useShellStore((s) => s.setSidebarCollapsed)
  const activeSection = useShellStore((s) => s.activeSection)
  const openBoard = useShellStore((s) => s.openBoard)
  const openHome = useShellStore((s) => s.openHome)
  const openNotesBrowser = useShellStore((s) => s.openNotesBrowser)
  const openBases = useShellStore((s) => s.openBases)
  const openBiblioteca = useShellStore((s) => s.openBiblioteca)
  const openSkills = useShellStore((s) => s.openSkills)
  const openBus = useShellStore((s) => s.openBus)
  const openCorrespondencia = useShellStore((s) => s.openCorrespondencia)
  const openChat = useShellStore((s) => s.openChat)
  const openPlaceholder = useShellStore((s) => s.openPlaceholder)
  const openNewNote = useShellStore((s) => s.openNewNote)
  const openPalette = useShellStore((s) => s.openPalette)
  const isDesktop = useIsDesktop()
  const { createQuickNote, quickNoteBusy } = useQuickNote()

  // Reusa la query ['me'] que ya cachea TopBar/App: no agrega round-trips.
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe })
  // Dos permisos INDEPENDIENTES: cada entrada se suma por su propio flag, no por
  // el del otro. Encadenarlos haría que perder el acceso al bus escondiera el chat.
  const sections = [
    ...SECTIONS,
    ...(me?.can_read_bus ? [BUS_SECTION, CORRESPONDENCIA_SECTION] : []),
    ...(me?.can_use_chat ? [CHAT_SECTION] : []),
  ]

  function navigate(key: SectionKey, label: string) {
    if (key === 'inicio') {
      openHome()
    } else if (key === 'tareas') {
      openBoard()
    } else if (key === 'notas') {
      // Navegación de notas = menú cascada (Miller columns) en el área principal.
      openNotesBrowser()
    } else if (key === 'bases') {
      openBases()
    } else if (key === 'biblioteca') {
      openBiblioteca()
    } else if (key === 'skills') {
      openSkills()
    } else if (key === 'bus') {
      openBus()
    } else if (key === 'correspondencia') {
      openCorrespondencia()
    } else if (key === 'chat') {
      openChat()
    } else {
      openPlaceholder(key, label)
    }
  }

  function handleSection(key: SectionKey, label: string) {
    // El colapso a riel es un concepto de escritorio: solo se expande allí.
    if (isDesktop && collapsed) setSidebarCollapsed(false)
    navigate(key, label)
    if (!isDesktop) onClose?.()
  }

  // ==================== MÓVIL: drawer off-canvas ====================
  if (!isDesktop) {
    return (
      <aside
        className={`glass fixed inset-y-0 left-0 z-40 flex w-[264px] max-w-[85vw] flex-col border-r border-[var(--border-strong)] pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] pt-[env(safe-area-inset-top)] shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] ${
          mobileOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        {/* Header con marca + cerrar */}
        <div className="flex min-h-[44px] shrink-0 items-center gap-2 px-3 py-1">
          <div className="flex h-7 w-7 items-center justify-center rounded bg-accent-bg text-fg">
            <Brain size={15} />
          </div>
          <span className="flex-1 truncate text-[14px] font-semibold text-fg wordmark">
            El Cerebro
          </span>
          <button
            onClick={onClose}
            title="Cerrar"
            className="flex h-11 w-11 items-center justify-center rounded-md text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <X size={18} />
          </button>
        </div>

        {/* Acciones rápidas */}
        <div className="px-2 pb-2">
          <MobileAction
            icon={Search}
            label="Buscar…"
            onClick={() => {
              openPalette('')
              onClose?.()
            }}
          />
          <MobileAction
            icon={FilePlus2}
            label="Nueva nota"
            onClick={() => {
              openNewNote('')
              onClose?.()
            }}
          />
          <MobileAction icon={Zap} label={quickNoteBusy ? 'Creando…' : 'Nota rápida'} onClick={() => {
            if (!quickNoteBusy) void createQuickNote()
            onClose?.()
          }} />
        </div>

        <div className="mx-3 my-1 h-px bg-[var(--border-subtle)]" />

        {/* Secciones */}
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
          {sections.map((s) => (
            <MobileSection
              key={s.key}
              icon={s.icon}
              label={s.label}
              active={activeSection === s.key}
              onClick={() => handleSection(s.key, s.label)}
            />
          ))}
        </div>
      </aside>
    )
  }

  // ==================== ESCRITORIO: layout de columna (sin cambios) ====================
  return (
    <AnimatePresence mode="wait" initial={false}>
      {collapsed ? (
        <motion.aside
          key="sidebar-collapsed"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="glass flex w-14 shrink-0 flex-col items-center border-r border-[var(--border-strong)] py-2"
        >
        <button
          onClick={() => setSidebarCollapsed(false)}
          title="Expandir sidebar"
          className="mb-2 flex h-8 w-8 items-center justify-center rounded-md bg-accent-bg text-fg"
        >
          <Brain size={16} />
        </button>
        <IconBtn icon={Search} label="Buscar  ⌘K" onClick={() => openPalette('')} />
        <IconBtn icon={FilePlus2} label="Nueva nota  ⌘N" onClick={() => openNewNote('')} />
        <IconBtn icon={Zap} label={quickNoteBusy ? 'Creando…' : 'Nota rápida'} onClick={() => {
          if (!quickNoteBusy) void createQuickNote()
        }} />
        <div className="my-2 h-px w-6 bg-[var(--border-subtle)]" />
        {sections.map((s) => (
          <IconBtn
            key={s.key}
            icon={s.icon}
            label={s.label}
            active={activeSection === s.key}
            onClick={() => handleSection(s.key, s.label)}
          />
        ))}
        <div className="mt-auto">
          <IconBtn icon={PanelLeftOpen} label="Expandir" onClick={toggleSidebar} />
        </div>
        </motion.aside>
      ) : (
        <motion.aside
          key="sidebar-expanded"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="glass flex w-[260px] shrink-0 flex-col border-r border-[var(--border-strong)]"
        >
      {/* Workspace switcher */}
      <div className="flex h-11 shrink-0 items-center gap-2 px-3">
        <div className="flex h-6 w-6 items-center justify-center rounded bg-accent-bg text-fg">
          <Brain size={14} />
        </div>
        <span className="flex-1 truncate text-[13px] font-semibold text-fg wordmark">
          El Cerebro
        </span>
        <button
          onClick={toggleSidebar}
          title="Colapsar sidebar  (⌘\)"
          className="flex h-7 w-7 items-center justify-center rounded-md text-fg-subtle transition-colors duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:bg-surface-hover hover:text-fg"
        >
          <PanelLeftClose size={16} />
        </button>
      </div>

      {/* Quick actions */}
      <div className="px-2 pb-2">
        <QuickAction icon={Search} label="Buscar…" keycap="⌘K" onClick={() => openPalette('')} />
        <QuickAction icon={FilePlus2} label="Nueva nota" keycap="⌘N" onClick={() => openNewNote('')} />
        <QuickAction icon={Zap} label={quickNoteBusy ? 'Creando…' : 'Nota rápida'} onClick={() => {
          if (!quickNoteBusy) void createQuickNote()
        }} />
      </div>

      <div className="mx-3 my-2 h-px bg-[var(--border-subtle)]" />

      {/* Secciones (navegación por lente). "Notas" abre el menú cascada. */}
      <div className="min-h-0 flex-1 overflow-y-auto pb-4">
        {sections.map((s) => (
          <SectionRow
            key={s.key}
            icon={s.icon}
            label={s.label}
            active={activeSection === s.key}
            onClick={() => handleSection(s.key, s.label)}
          />
        ))}
      </div>
        </motion.aside>
      )}
    </AnimatePresence>
  )
}

function IconBtn({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: LucideIcon
  label: string
  active?: boolean
  onClick: () => void
}) {
  return (
    <button
      title={label}
      onClick={onClick}
      className={`mb-2 flex h-9 w-9 items-center justify-center rounded-md outline-none transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${
        active
          ? 'bg-[rgba(168,85,247,0.12)] text-accent shadow-[var(--glow-sm)]'
          : 'text-fg-subtle hover:bg-[var(--accent-bg)] hover:text-fg'
      }`}
    >
      <Icon size={18} />
    </button>
  )
}

function QuickAction({
  icon: Icon,
  label,
  keycap,
  onClick,
}: {
  icon: LucideIcon
  label: string
  keycap?: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="group flex h-8 w-full items-center gap-2 rounded-md px-2 text-fg-subtle transition-colors duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:bg-surface-hover hover:text-fg-muted"
    >
      <Icon size={16} className="shrink-0 text-fg-faint group-hover:text-fg-subtle" />
      <span className="flex-1 truncate text-left text-[13px]">{label}</span>
      {keycap && <span className="keycap">{keycap}</span>}
    </button>
  )
}

function SectionRow({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: LucideIcon
  label: string
  active?: boolean
  onClick: () => void
}) {
  return (
    <div className="relative px-2">
      {active && (
        <span className="absolute left-2 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded bg-accent" />
      )}
      <div
        className={`group flex h-[30px] items-center gap-2 rounded-md px-2 outline-none transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] focus-within:ring-2 focus-within:ring-[var(--accent)] ${
          active
            ? 'bg-[rgba(168,85,247,0.12)] text-fg shadow-[var(--glow-sm)]'
            : 'text-fg-subtle hover:bg-[var(--accent-bg)] hover:text-fg'
        }`}
      >
        <span className="w-4 shrink-0" />
        <button
          onClick={onClick}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <Icon size={16} className={active ? 'text-accent' : 'text-fg-faint'} />
          <span className="truncate text-[13px] font-medium">{label}</span>
        </button>
      </div>
    </div>
  )
}

// ---------- Variantes móviles (touch targets >=44px) ----------

function MobileAction({
  icon: Icon,
  label,
  onClick,
}: {
  icon: LucideIcon
  label: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="group flex min-h-[44px] w-full items-center gap-3 rounded-md px-2 text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg-muted active:bg-surface-hover"
    >
      <Icon size={18} className="shrink-0 text-fg-faint group-hover:text-fg-subtle" />
      <span className="flex-1 truncate text-left text-[14px]">{label}</span>
    </button>
  )
}

function MobileSection({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: LucideIcon
  label: string
  active?: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`mb-0.5 flex min-h-[44px] w-full items-center gap-3 rounded-md px-3 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${
        active
          ? 'bg-[rgba(168,85,247,0.12)] text-fg shadow-[var(--glow-sm)]'
          : 'text-fg-subtle hover:bg-[var(--accent-bg)] hover:text-fg active:bg-[var(--accent-bg)]'
      }`}
    >
      <Icon size={18} className={active ? 'text-accent' : 'text-fg-faint'} />
      <span className="truncate text-[14px] font-medium">{label}</span>
    </button>
  )
}
