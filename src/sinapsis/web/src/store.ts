import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { fetchMeta, type Meta } from './lib/api'

type Theme = 'dark' | 'light'

interface AppState {
  token: string | null
  theme: Theme
  /** Metadata dinamica (roster de agentes/proyectos) provista por GET /api/meta.
  NADA hardcodeado: se carga tras el login. null mientras no se cargo. */
  meta: Meta | null
  /** El principal debe cambiar su contraseña antes de usar la app (primer login
  con pass generada por el sistema). No se persiste: se resuelve en el login y se
  refresca desde GET /me al montar la app. */
  mustChangePassword: boolean
  setToken: (token: string | null) => void
  setMustChangePassword: (v: boolean) => void
  loadMeta: () => Promise<void>
  logout: () => void
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      token: null,
      theme: 'dark',
      meta: null,
      mustChangePassword: false,
      setToken: (token) => set({ token }),
      setMustChangePassword: (v) => set({ mustChangePassword: v }),
      loadMeta: async () => {
        try {
          const meta = await fetchMeta()
          set({ meta })
        } catch {
          // silencioso: la UI usa fallback vacio mientras tanto
        }
      },
      logout: () => set({ token: null, mustChangePassword: false, meta: null }),
      setTheme: (theme) => set({ theme }),
      toggleTheme: () => set({ theme: get().theme === 'dark' ? 'light' : 'dark' }),
    }),
    {
      name: 'sinapsis-store',
      partialize: (s) => ({ token: s.token, theme: s.theme }),
    }
  )
)

interface EditorState {
  /** Hay cambios sin guardar en el editor abierto. */
  dirty: boolean
  setDirty: (dirty: boolean) => void
}

/** Estado efímero del editor (no persiste). Usado para el guard de cambios. */
export const useEditorStore = create<EditorState>((set) => ({
  dirty: false,
  setDirty: (dirty) => set({ dirty }),
}))

// ---------- Shell (Ola 1): tabs + secciones + panel + palette ----------

export type TabKind = 'note' | 'board' | 'placeholder' | 'notes' | 'home' | 'bases' | 'biblioteca' | 'skills' | 'bus' | 'correspondencia' | 'chat' | 'task'
/** Vista activa de la sección de tareas (segmented control). */
export type TaskView = 'board' | 'list' | 'calendar' | 'timeline' | 'inbox'

/**
 * Panel abierto al costado en la seccion de tareas.
 *
 * Pedido de admin (): "puedes ver como es el comportamiento de las notas
 * cuando se abre las carpetas algo asi quiero... que vaya como dividiendo la vista
 * actual". Es el patron de MillerColumns (columnas que se agregan a la derecha),
 * NO tabs: el primer intento uso tabs y NO era lo pedido, porque un tab REEMPLAZA
 * la vista en vez de dividirla.
 *
 * El stack es una CADENA: tarea -> documento -> documento. Abrir algo desde el
 * panel N descarta los paneles > N (igual que el `trail` de MillerColumns), asi
 * la navegacion no acumula basura a la derecha.
 */
export type TaskPanel =
  | { kind: 'task'; id: string; title: string }
  | { kind: 'approval'; id: string; title: string }
  | { kind: 'note'; path: string; title: string }
export type SectionKey =
  | 'inicio'
  | 'notas'
  | 'bases'
  | 'tareas'
  | 'biblioteca'
  | 'skills'
  | 'bus'
  | 'correspondencia'
  | 'chat'
  | 'grafo'
  | 'tags'
  | 'pinned'

export interface Tab {
  id: string
  kind: TabKind
  title: string
  /** Ruta del vault (solo tabs de nota). */
  path?: string
  /** Clave de sección (solo tabs placeholder). */
  placeholderKey?: SectionKey
  /** Id de la tarea (solo tabs de tarea). Pedido de admin : la tarea se
   *  abre COMO UNA NOTA, en el area de contenido, para poder tener a la vista los
   *  documentos relacionados al costado en vez de que un modal tape todo. */
  taskId?: string
}

/** Guard global de cambios sin guardar. Devuelve true si se puede continuar. */
function confirmDiscard(): boolean {
  if (useEditorStore.getState().dirty) {
    if (!window.confirm('Tenés cambios sin guardar. ¿Descartar y continuar?')) {
      return false
    }
    useEditorStore.getState().setDirty(false)
  }
  return true
}

function noteTitle(path: string): string {
  return (path.split('/').pop() ?? path).replace(/\.md$/, '')
}

interface ShellState {
  tabs: Tab[]
  activeTabId: string | null
  activeSection: SectionKey
  sidebarCollapsed: boolean
  rightPanelOpen: boolean
  /** Drawer del Sidebar abierto (solo móvil; efímero, no persiste). */
  mobileNavOpen: boolean
  /** Drawer del panel derecho abierto (solo móvil; efímero, no persiste). */
  mobilePanelOpen: boolean
  paletteOpen: boolean
  palettePrefix: string
  newNoteOpen: boolean
  newNoteDir: string
  notesCurrentDir: string
  basesCurrentDir: string
  /** Filtro de proyecto activo del Tablero. 'Todos' = sin filtro. */
  boardProjectFilter: string
  /** Vista activa de la sección de tareas (Tablero/Lista/Calendario/Timeline). */
  taskView: TaskView
  /** Paneles abiertos a la derecha en la seccion de tareas (cadena tipo Miller). */
  taskPanels: TaskPanel[]
  /** Ruta a "revelar" en el navegador de notas (Miller columns). Efímero, no persiste. */
  notesRevealPath: string | null

  openNote: (path: string) => void
  openTask: (taskId: string, title?: string) => void
  openBoard: () => void
  openHome: () => void
  openNotesBrowser: (revealPath?: string) => void
  /** Limpia la señal de reveal (la consume MillerColumns tras aplicarla). */
  clearNotesReveal: () => void
  openBases: () => void
  /** Abre la Biblioteca (skills del registro compartido). */
  openBiblioteca: () => void
  /** Abre Skills como sección de primer nivel, reutilizando la vista de Biblioteca. */
  openSkills: () => void
  /** Abre el Bus (hilos del bus de la flota; solo el lector autorizado). */
  openBus: () => void
  /** Abre Correspondencia (mismo permiso nominal de lectura del bus). */
  openCorrespondencia: () => void
  /** Abre el Chat con los agentes (solo el remitente autorizado: can_use_chat). */
  openChat: () => void
  openPlaceholder: (key: SectionKey, title: string) => void
  setActiveTab: (id: string) => void
  closeTab: (id: string) => void
  renamePath: (oldPath: string, newPath: string) => void
  deletePath: (path: string) => void
  setActiveSection: (s: SectionKey) => void
  toggleSidebar: () => void
  setSidebarCollapsed: (b: boolean) => void
  toggleRightPanel: () => void
  setRightPanelOpen: (b: boolean) => void
  /** Alterna el drawer de navegación móvil (cierra el panel derecho si estaba abierto). */
  toggleMobileNav: () => void
  /** Alterna el drawer de panel derecho móvil (cierra el de navegación si estaba abierto). */
  toggleMobilePanel: () => void
  /** Cierra ambos drawers móviles (tap en overlay / navegación). */
  closeMobileDrawers: () => void
  openPalette: (prefix?: string) => void
  closePalette: () => void
  openNewNote: (dir?: string) => void
  setNotesCurrentDir: (dir: string) => void
  setBasesCurrentDir: (dir: string) => void
  setNewNoteOpen: (b: boolean) => void
  /** Setea el filtro de proyecto del Tablero (usado desde Inicio → tarjetas). */
  setBoardProjectFilter: (project: string) => void
  /** Setea la vista activa de la sección de tareas (persistida). */
  setTaskView: (v: TaskView) => void
  /** Abre una tarea en el panel 0 (reemplaza la cadena entera). */
  openTaskPanel: (taskId: string, title?: string) => void
  openApprovalPanel: (id: string, title: string) => void
  /** Abre un documento DESPUES del panel `afterIndex`, descartando lo que hubiera a la derecha. */
  openDocPanel: (afterIndex: number, path: string, title?: string) => void
  /** Cierra el panel `index` y todo lo que este a su derecha. */
  closeTaskPanel: (index: number) => void
  closeAllTaskPanels: () => void
}

export const useShellStore = create<ShellState>()(
  persist(
    (set, get) => ({
      tabs: [],
      activeTabId: null,
      activeSection: 'notas',
      sidebarCollapsed: false,
      rightPanelOpen: true,
      mobileNavOpen: false,
      mobilePanelOpen: false,
      paletteOpen: false,
      palettePrefix: '',
      newNoteOpen: false,
      newNoteDir: '',
      notesCurrentDir: '',
      basesCurrentDir: '',
      boardProjectFilter: 'Todos',
      taskView: 'board',
      taskPanels: [],
      notesRevealPath: null,

      openNote: (path) => {
        const id = 'note:' + path
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'notas' })
          return
        }
        if (!confirmDiscard()) return
        if (tabs.some((t) => t.id === id)) {
          set({ activeTabId: id, activeSection: 'notas' })
        } else {
          set({
            tabs: [...tabs, { id, kind: 'note', path, title: noteTitle(path) }],
            activeTabId: id,
            activeSection: 'notas',
          })
        }
      },

      /** Abre una tarea como TAB (no como modal). Calcado de openNote: reusa el tab
       *  si ya existe, si no lo agrega, y deja la seccion en 'tareas'. */
      openTask: (taskId: string, title?: string) => {
        const id = 'task:' + taskId
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'tareas' })
          return
        }
        if (!confirmDiscard()) return
        const label = title && title.trim() ? title.trim() : taskId.slice(0, 8)
        if (tabs.some((t) => t.id === id)) {
          set({ activeTabId: id, activeSection: 'tareas' })
        } else {
          set({
            tabs: [...tabs, { id, kind: 'task', taskId, title: label }],
            activeTabId: id,
            activeSection: 'tareas',
          })
        }
      },

      openBoard: () => {
        const id = 'board'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'tareas' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'board', title: 'Tablero' }],
          activeTabId: id,
          activeSection: 'tareas',
        })
      },

      openHome: () => {
        const id = 'home'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'inicio' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'home', title: 'Inicio' }],
          activeTabId: id,
          activeSection: 'inicio',
        })
      },

      openNotesBrowser: (revealPath) => {
        const id = 'notes'
        const { tabs, activeTabId } = get()
        const reveal = revealPath ?? null
        // Al revelar (breadcrumb/botón) cerramos los drawers móviles si aplica.
        const drawers = reveal ? { mobileNavOpen: false, mobilePanelOpen: false } : {}
        if (activeTabId === id) {
          set({ activeSection: 'notas', notesRevealPath: reveal, ...drawers })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'notes', title: 'Notas' }],
          activeTabId: id,
          activeSection: 'notas',
          notesRevealPath: reveal,
          ...drawers,
        })
      },

      clearNotesReveal: () => set({ notesRevealPath: null }),

      openBases: () => {
        const id = 'bases'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'bases' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'bases', title: 'Bases' }],
          activeTabId: id,
          activeSection: 'bases',
        })
      },

      openBiblioteca: () => {
        const id = 'biblioteca'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'biblioteca' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'biblioteca', title: 'Biblioteca' }],
          activeTabId: id,
          activeSection: 'biblioteca',
        })
      },
      openSkills: () => {
        const id = 'skills'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'skills' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'skills', title: 'Skills' }],
          activeTabId: id,
          activeSection: 'skills',
        })
      },

      openBus: () => {
        const id = 'bus'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'bus' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'bus', title: 'Bus' }],
          activeTabId: id,
          activeSection: 'bus',
        })
      },

      openCorrespondencia: () => {
        const id = 'correspondencia'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'correspondencia' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'correspondencia', title: 'Correspondencia' }],
          activeTabId: id,
          activeSection: 'correspondencia',
        })
      },

      openChat: () => {
        const id = 'chat'
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: 'chat' })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'chat', title: 'Chat' }],
          activeTabId: id,
          activeSection: 'chat',
        })
      },

      openPlaceholder: (key, title) => {
        const id = 'ph:' + key
        const { tabs, activeTabId } = get()
        if (activeTabId === id) {
          set({ activeSection: key })
          return
        }
        if (!confirmDiscard()) return
        set({
          tabs: tabs.some((t) => t.id === id)
            ? tabs
            : [...tabs, { id, kind: 'placeholder', title, placeholderKey: key }],
          activeTabId: id,
          activeSection: key,
        })
      },

      setActiveTab: (id) => {
        if (get().activeTabId === id) return
        if (!confirmDiscard()) return
        const tab = get().tabs.find((t) => t.id === id)
        set({
          activeTabId: id,
          ...(tab?.kind === 'note' ? { activeSection: 'notas' as SectionKey } : {}),
          ...(tab?.kind === 'notes' ? { activeSection: 'notas' as SectionKey } : {}),
          ...(tab?.kind === 'board' ? { activeSection: 'tareas' as SectionKey } : {}),
          ...(tab?.kind === 'task' ? { activeSection: 'tareas' as SectionKey } : {}),
          ...(tab?.kind === 'bases' ? { activeSection: 'bases' as SectionKey } : {}),
          ...(tab?.kind === 'biblioteca'
            ? { activeSection: 'biblioteca' as SectionKey }
            : {}),
          ...(tab?.kind === 'skills' ? { activeSection: 'skills' as SectionKey } : {}),
          ...(tab?.kind === 'bus' ? { activeSection: 'bus' as SectionKey } : {}),
          ...(tab?.kind === 'correspondencia'
            ? { activeSection: 'correspondencia' as SectionKey }
            : {}),
          ...(tab?.kind === 'chat' ? { activeSection: 'chat' as SectionKey } : {}),
          ...(tab?.kind === 'home' ? { activeSection: 'inicio' as SectionKey } : {}),
        })
      },

      closeTab: (id) => {
        const { tabs, activeTabId } = get()
        const idx = tabs.findIndex((t) => t.id === id)
        if (idx === -1) return
        if (activeTabId === id && !confirmDiscard()) return
        const next = tabs.filter((t) => t.id !== id)
        let nextActive = activeTabId
        if (activeTabId === id) {
          const fallback = next[idx] ?? next[idx - 1] ?? null
          nextActive = fallback ? fallback.id : null
        }
        set({ tabs: next, activeTabId: nextActive })
      },

      renamePath: (oldPath, newPath) => {
        const remap = (p: string): string => {
          if (p === oldPath) return newPath
          if (p.startsWith(oldPath + '/')) return newPath + p.slice(oldPath.length)
          return p
        }
        set((s) => ({
          tabs: s.tabs.map((t) => {
            if (t.kind !== 'note' || !t.path) return t
            const np = remap(t.path)
            if (np === t.path) return t
            return { ...t, id: 'note:' + np, path: np, title: noteTitle(np) }
          }),
          activeTabId:
            s.activeTabId && s.activeTabId.startsWith('note:')
              ? 'note:' + remap(s.activeTabId.slice('note:'.length))
              : s.activeTabId,
        }))
      },

      deletePath: (path) => {
        set((s) => {
          const affected = (p?: string) =>
            !!p && (p === path || p.startsWith(path + '/'))
          const tabs = s.tabs.filter(
            (t) => !(t.kind === 'note' && affected(t.path))
          )
          let activeTabId = s.activeTabId
          if (activeTabId && activeTabId.startsWith('note:')) {
            const cur = activeTabId.slice('note:'.length)
            if (affected(cur)) {
              activeTabId = tabs.length ? tabs[tabs.length - 1].id : null
            }
          }
          return { tabs, activeTabId }
        })
      },

      setActiveSection: (s) => set({ activeSection: s }),
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (b) => set({ sidebarCollapsed: b }),
      toggleRightPanel: () => set((s) => ({ rightPanelOpen: !s.rightPanelOpen })),
      setRightPanelOpen: (b) => set({ rightPanelOpen: b }),
      toggleMobileNav: () =>
        set((s) => ({ mobileNavOpen: !s.mobileNavOpen, mobilePanelOpen: false })),
      toggleMobilePanel: () =>
        set((s) => ({ mobilePanelOpen: !s.mobilePanelOpen, mobileNavOpen: false })),
      closeMobileDrawers: () => set({ mobileNavOpen: false, mobilePanelOpen: false }),
      openPalette: (prefix = '') => set({ paletteOpen: true, palettePrefix: prefix }),
      closePalette: () => set({ paletteOpen: false }),
      openNewNote: (dir = '') => {
        const { tabs, activeTabId, notesCurrentDir, basesCurrentDir } = get()
        const active = tabs.find((t) => t.id === activeTabId)
        const currentDir = active?.kind === 'notes' ? notesCurrentDir
          : active?.kind === 'bases' ? basesCurrentDir : ''
        const selected = dir || currentDir
        set({
          newNoteOpen: true,
          newNoteDir: selected ? (selected.endsWith('/') ? selected : selected + '/') : '',
        })
      },
      setNotesCurrentDir: (dir) => set({ notesCurrentDir: dir }),
      setBasesCurrentDir: (dir) => set({ basesCurrentDir: dir }),
      setNewNoteOpen: (b) => set({ newNoteOpen: b }),
      setBoardProjectFilter: (project) => set({ boardProjectFilter: project }),
      setTaskView: (v) => set({ taskView: v }),

      openTaskPanel: (taskId, title) =>
        set({
          taskPanels: [
            { kind: 'task', id: taskId, title: title?.trim() || taskId.slice(0, 8) },
          ],
        }),

      openApprovalPanel: (id, title) =>
        set({ taskPanels: [{ kind: 'approval', id, title }] }),

      openDocPanel: (afterIndex, path, title) =>
        set((st) => {
          const label = title?.trim() || path.split('/').pop()?.replace(/\.md$/i, '') || path
          // Ya abierto justo despues => no duplicar (igual que reusar un tab existente).
          const next = st.taskPanels[afterIndex + 1]
          if (next && next.kind === 'note' && next.path === path) return st
          return {
            taskPanels: [
              ...st.taskPanels.slice(0, afterIndex + 1),
              { kind: 'note', path, title: label },
            ],
          }
        }),

      closeTaskPanel: (index) =>
        set((st) => ({ taskPanels: st.taskPanels.slice(0, index) })),

      closeAllTaskPanels: () => set({ taskPanels: [] }),
    }),
    {
      name: 'sinapsis-shell',
      // Solo persistimos preferencias de layout; los tabs arrancan limpios.
      // Los drawers móviles (mobileNavOpen/mobilePanelOpen) son efímeros: no
      // se persisten para que la app arranque siempre con los drawers cerrados.
      partialize: (s) => ({
        sidebarCollapsed: s.sidebarCollapsed,
        rightPanelOpen: s.rightPanelOpen,
        taskView: s.taskView,
      }),
    }
  )
)
