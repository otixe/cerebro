import { useEffect, lazy, Suspense } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Database, Network, Hash, Star, Command, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { fetchMe, fetchTasks } from '../lib/api'
import { useAppStore, useEditorStore, useShellStore, type SectionKey } from '../store'
import { useIsDesktop } from '../hooks/useIsDesktop'
import TopBar from './TopBar'
import Sidebar from './Sidebar'
import DocTabs from './DocTabs'
import RightPanel from './RightPanel'
import CommandPalette from './CommandPalette'
import NewNoteDialog from './NewNoteDialog'
import ParticlesBackground from './ParticlesBackground'

// Code-splitting (Fase D): las vistas pesadas se cargan on-demand, cada una en
// su propio chunk. GraphView arrastra react-force-graph-2d y FileViewer arrastra
// CodeMirror + react-markdown: son los más pesados y sólo se descargan al abrir
// su sección, bajando el chunk inicial.
const FileViewer = lazy(() => import('./FileViewer'))
const TasksView = lazy(() => import('./TasksView'))
// La tarea como PAGINA (tab), no como modal (pedido de admin ).
const TaskDetail = lazy(() => import('./TaskDetail'))
const BasesView = lazy(() => import('./BasesView'))
const HomeView = lazy(() => import('./HomeView'))
const SkillsView = lazy(() => import('./SkillsView'))
// Bus de la flota: solo lo ve el lector autorizado (gate real en el BFF).
const BusView = lazy(() => import('./BusView'))
// Correspondencia: vista nueva; sólo se monta cuando /me confirma el permiso de lectura.
const CorrespondenciaView = lazy(() => import('./CorrespondenciaView'))
// Chat con los agentes: solo lo ve el remitente autorizado (gate real en el BFF).
const ChatView = lazy(() => import('./ChatView'))
const MillerColumns = lazy(() => import('./MillerColumns'))
const GraphView = lazy(() => import('./GraphView'))

const PLACEHOLDER_META: Record<
  string,
  { icon: typeof Database; title: string; desc: string }
> = {
  bases: {
    icon: Database,
    title: 'Bases',
    desc: 'Tratá conjuntos de notas con frontmatter homogéneo como bases de datos (tabla/board/galería). Llega en la Ola 3.',
  },
  grafo: {
    icon: Network,
    title: 'Grafo',
    desc: 'Vista de grafo global de notas enlazadas por wikilinks. Llega en la Ola 4.',
  },
  tags: {
    icon: Hash,
    title: 'Tags',
    desc: 'Navegación transversal por #etiqueta a través de notas, bases y tareas. Llega en la Ola 4.',
  },
  pinned: {
    icon: Star,
    title: 'Pinned',
    desc: 'Fijá notas y vistas favoritas para acceso rápido. Llega en una próxima ola.',
  },
}

export default function AppShell() {
  const theme = useAppStore((s) => s.theme)

  const tabs = useShellStore((s) => s.tabs)
  const activeTabId = useShellStore((s) => s.activeTabId)
  const rightPanelOpen = useShellStore((s) => s.rightPanelOpen)
  const mobileNavOpen = useShellStore((s) => s.mobileNavOpen)
  const mobilePanelOpen = useShellStore((s) => s.mobilePanelOpen)
  const closeMobileDrawers = useShellStore((s) => s.closeMobileDrawers)
  const openNote = useShellStore((s) => s.openNote)
  const openHome = useShellStore((s) => s.openHome)
  const openPalette = useShellStore((s) => s.openPalette)
  const closePalette = useShellStore((s) => s.closePalette)
  const toggleSidebar = useShellStore((s) => s.toggleSidebar)
  const toggleRightPanel = useShellStore((s) => s.toggleRightPanel)
  const openNewNote = useShellStore((s) => s.openNewNote)
  const newNoteOpen = useShellStore((s) => s.newNoteOpen)
  const newNoteDir = useShellStore((s) => s.newNoteDir)
  const setNewNoteOpen = useShellStore((s) => s.setNewNoteOpen)
  const toggleTheme = useAppStore((s) => s.toggleTheme)

  const isDesktop = useIsDesktop()
  // Observador de la query que App ya carga. enabled:false garantiza cero GET nuevos.
  const me = useQuery({ queryKey: ['me'], queryFn: fetchMe, enabled: false })

  const activeTab = tabs.find((t) => t.id === activeTabId)
  const activeNotePath = activeTab?.kind === 'note' ? (activeTab.path ?? null) : null

  // Sincronizar el tema con la clase del <html>.
  useEffect(() => {
    const root = document.documentElement
    root.classList.remove('light', 'dark')
    root.classList.add(theme)
  }, [theme])

  // Landing por defecto: si no hay tabs abiertos, arrancar en Inicio.
  useEffect(() => {
    if (useShellStore.getState().tabs.length === 0) openHome()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Aviso del navegador al cerrar/recargar con cambios pendientes.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (useEditorStore.getState().dirty) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [])

  // Scroll-lock del body mientras haya un drawer móvil abierto.
  useEffect(() => {
    const open = !isDesktop && (mobileNavOpen || mobilePanelOpen)
    const prev = document.body.style.overflow
    document.body.style.overflow = open ? 'hidden' : ''
    return () => {
      document.body.style.overflow = prev
    }
  }, [isDesktop, mobileNavOpen, mobilePanelOpen])

  // Al pasar a escritorio, garantizar que no queden drawers móviles "abiertos".
  useEffect(() => {
    if (isDesktop && (mobileNavOpen || mobilePanelOpen)) closeMobileDrawers()
  }, [isDesktop, mobileNavOpen, mobilePanelOpen, closeMobileDrawers])

  // Atajos globales.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey
      if (!mod) return
      const k = e.key.toLowerCase()
      if (k === 'k') {
        e.preventDefault()
        if (useShellStore.getState().paletteOpen) closePalette()
        else openPalette('')
      } else if (k === '\\') {
        e.preventDefault()
        toggleSidebar()
      } else if (k === '.') {
        e.preventDefault()
        toggleRightPanel()
      } else if (k === 'n' && !e.shiftKey) {
        e.preventDefault()
        openNewNote('')
      } else if (k === 'l' && e.shiftKey) {
        e.preventDefault()
        toggleTheme()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openPalette, closePalette, toggleSidebar, toggleRightPanel, openNewNote, toggleTheme])

  return (
    <div className="relative isolate flex h-full w-full flex-col bg-bg text-fg">
      <ParticlesBackground variant="ambient" />
      <TopBar />

      <div className="flex min-h-0 flex-1">
        <Sidebar mobileOpen={mobileNavOpen} onClose={closeMobileDrawers} />

        <main className="flex min-w-0 flex-1 flex-col glass-strong">
          <DocTabs />
          <div className="min-h-0 flex-1">
            <Suspense fallback={<ViewFallback />}>
              {!activeTab ? (
                <WelcomeState onOpen={() => openPalette('')} />
              ) : activeTab.kind === 'home' ? (
                <HomeView />
              ) : activeTab.kind === 'notes' ? (
                <MillerColumns onOpenFile={openNote} />
              ) : activeTab.kind === 'note' && activeTab.path ? (
                <FileViewer key={activeTab.path} path={activeTab.path} onNavigate={openNote} />
              ) : activeTab.kind === 'task' && activeTab.taskId ? (
                <TaskDetailPage taskId={activeTab.taskId} />
              ) : activeTab.kind === 'board' ? (
                <TasksView />
              ) : activeTab.kind === 'bases' ? (
                <BasesView />
              ) : activeTab.kind === 'biblioteca' || activeTab.kind === 'skills' ? (
                <SkillsView />
              ) : activeTab.kind === 'bus' ? (
                <BusView />
              ) : activeTab.kind === 'correspondencia' && me.data?.can_read_bus === true ? (
                <CorrespondenciaView me={me.data} />
              ) : activeTab.kind === 'chat' ? (
                <ChatView />
              ) : activeTab.kind === 'placeholder' && activeTab.placeholderKey === 'grafo' ? (
                <GraphView onOpenNote={openNote} />
              ) : (
                <PlaceholderView sectionKey={activeTab.placeholderKey} />
              )}
            </Suspense>
          </div>
        </main>

        {/* Panel derecho: columna en escritorio, drawer off-canvas en móvil. */}
        {isDesktop ? (
          <AnimatePresence initial={false}>
            {rightPanelOpen && (
              <motion.div
                initial={{ opacity: 0, x: 16 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 16 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                className="flex"
              >
                <RightPanel activePath={activeNotePath} onNavigate={openNote} />
              </motion.div>
            )}
          </AnimatePresence>
        ) : (
          <div
            className={`glass fixed inset-y-0 right-0 z-40 flex pb-[env(safe-area-inset-bottom)] pr-[env(safe-area-inset-right)] pt-[env(safe-area-inset-top)] shadow-2xl transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] ${
              mobilePanelOpen ? 'translate-x-0' : 'translate-x-full'
            }`}
          >
            <RightPanel activePath={activeNotePath} onNavigate={openNote} />
          </div>
        )}

        {/* Overlay para drawers móviles: tap fuera cierra. */}
        {!isDesktop && (
          <AnimatePresence>
            {(mobileNavOpen || mobilePanelOpen) && (
              <motion.div
                key="drawer-overlay"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.2 }}
                onClick={closeMobileDrawers}
                className="fixed inset-0 z-30 bg-black/80 backdrop-blur-sm"
                aria-hidden="true"
              />
            )}
          </AnimatePresence>
        )}
      </div>

      <CommandPalette />

      <NewNoteDialog
        open={newNoteOpen}
        onOpenChange={setNewNoteOpen}
        onCreated={openNote}
        initialPath={newNoteDir}
      />
    </div>
  )
}

// Fallback de carga para las vistas lazy (code-splitting). Spinner sobrio
// centrado, con tokens del tema (funciona en dark y claro).
function ViewFallback() {
  return (
    <div className="flex h-full items-center justify-center">
      <Loader2 size={20} className="animate-spin text-fg-faint" />
    </div>
  )
}

function WelcomeState({ onOpen }: { onOpen: () => void }) {
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-surface">
          <Command size={26} className="text-accent" />
        </div>
        <div>
          <h2 className="text-[15px] font-medium text-fg">Bienvenido a SINAPSIS</h2>
          <p className="mt-1 max-w-[340px] text-[13px] text-fg-subtle">
            Elegí una nota del árbol, abrí el tablero, o empezá con el buscador de comandos.
          </p>
        </div>
        <button
          onClick={onOpen}
          className="flex items-center gap-2 rounded-md border border-[var(--border-strong)] bg-[rgba(255,255,255,0.02)] px-3 py-2 text-[13px] text-fg-muted transition hover:bg-surface-hover"
        >
          Abrir command palette <span className="keycap">⌘K</span>
        </button>
      </div>
    </div>
  )
}

function PlaceholderView({ sectionKey }: { sectionKey?: SectionKey }) {
  const meta = (sectionKey && PLACEHOLDER_META[sectionKey]) || PLACEHOLDER_META.bases
  const Icon = meta.icon
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
          <Icon size={24} className="text-fg-faint" />
        </div>
        <h2 className="text-[15px] font-medium text-fg-muted">{meta.title}</h2>
        <p className="max-w-[360px] text-[13px] text-fg-subtle">{meta.desc}</p>
      </div>
    </div>
  )
}


/**
 * La tarea abierta COMO PAGINA en el area de contenido.
 *
 * Pedido de admin (): "ideal que cambiemos la forma de ver las tareas
 * como vemos las notas: pincho la tarea se abre al lado derecho de la pantalla y
 * si tiene un archivo al pincharlo se abre a la derecha de este". Con la tarea en
 * un tab, el documento relacionado se abre en OTRO tab y los dos quedan a la vista;
 * con el modal, abrir el documento tapaba la tarea.
 *
 * Resuelve la tarea contra el cache de ['tasks'] porque el backend NO expone
 * GET /api/tasks/:id (verificado). Si el cache todavia no llego, muestra un
 * placeholder en vez de romper: es el estado normal en un refresh directo sobre
 * la URL de un tab de tarea.
 */
function TaskDetailPage({ taskId }: { taskId: string }) {
  const { data: tasks } = useQuery({ queryKey: ['tasks'], queryFn: fetchTasks })
  const task = tasks?.tasks.find((t) => t.id === taskId) ?? null
  if (!task) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-xs text-fg-subtle">
        Cargando la tarea {taskId.slice(0, 8)}…
      </div>
    )
  }
  return (
    <div className="h-full overflow-y-auto p-5">
      <div className="mx-auto max-w-3xl">
        <Suspense fallback={<div className="p-4 text-xs text-fg-subtle">Cargando…</div>}>
          <TaskDetail
            variant="page"
            open
            onOpenChange={() => {}}
            task={task}
            onOpenTask={(t) => useShellStore.getState().openTask(t.id, t.title)}
          />
        </Suspense>
      </div>
    </div>
  )
}
