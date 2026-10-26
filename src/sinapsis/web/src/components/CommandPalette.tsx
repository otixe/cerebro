import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Search,
  FileText,
  CornerDownLeft,
  FilePlus2,
  Zap,
  CheckSquare,
  Home,
  PanelLeft,
  PanelRight,
  Sun,
  Moon,
  Hash,
  AtSign,
  ArrowUpDown,
  Database,
  BookOpen,
  Network,
  FolderTree,
  FileDown,
  Clock,
  type LucideIcon,
  ListChecks,
} from 'lucide-react'
import {
  fetchTree,
  fetchTasks,
  searchVault,
  fetchRecent,
  fetchNode,
  type TreeNode,
} from '../lib/api'
import { useShellStore } from '../store'
import { STATUS_META } from './taskShared'
import { exportNoteHtml } from '../lib/exportHtml'
import { exportMarkdownZip } from '../lib/exportMarkdownZip'
import { buildWikiIndex } from '../lib/wikilink'
import { useAppStore } from '../store'
import { useQuickNote } from '../hooks/useQuickNote'

interface PaletteItem {
  id: string
  icon: LucideIcon
  title: string
  meta?: string
  group: string
  keycap?: string
  run: () => void
}

// ---------- Descarga de blobs (export md/html de la nota actual) ----------
function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function baseName(path: string): string {
  return path.split('/').pop() ?? path
}

// ---------- Fuzzy match (subsecuencia con bonus por inicio de palabra) ----------
function fuzzyScore(query: string, target: string): number | null {
  const q = query.toLowerCase()
  const t = target.toLowerCase()
  if (!q) return 0
  let qi = 0
  let score = 0
  let last = -2
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      let bonus = 1
      if (last === ti - 1) bonus += 3
      if (ti === 0 || /[\s/\-_.]/.test(t[ti - 1])) bonus += 4
      score += bonus
      last = ti
      qi++
    }
  }
  if (qi < q.length) return null
  return score - t.length * 0.04
}

function flattenFiles(tree: TreeNode | undefined): { path: string; name: string }[] {
  const files: { path: string; name: string }[] = []
  const walk = (n: TreeNode) => {
    if (n.type === 'file' && n.path.endsWith('.md')) {
      files.push({ path: n.path, name: n.name.replace(/\.md$/, '') })
    }
    n.children?.forEach(walk)
  }
  if (tree) walk(tree)
  return files
}

function parseInput(raw: string): { prefix: '' | '>' | '?' | '#' | '@' | '!'; query: string } {
  const first = raw[0]
  // '!' = solo TAREAS (por codigo de 8 chars o por titulo). Agregado :
  // el caso de uso es "me pasaron f0383550 y quiero abrirla". Sin prefijo tambien
  // busca tareas (modo mixto), asi que '!' es el atajo para no competir con notas.
  if (first === '>' || first === '?' || first === '#' || first === '@' || first === '!') {
    return { prefix: first, query: raw.slice(1).trimStart() }
  }
  return { prefix: '', query: raw }
}

export default function CommandPalette() {
  const open = useShellStore((s) => s.paletteOpen)
  const palettePrefix = useShellStore((s) => s.palettePrefix)
  const closePalette = useShellStore((s) => s.closePalette)
  const openNote = useShellStore((s) => s.openNote)
  const openBoard = useShellStore((s) => s.openBoard)
  const openTaskPanel = useShellStore((st) => st.openTaskPanel)
  const openHome = useShellStore((s) => s.openHome)
  const openNotesBrowser = useShellStore((s) => s.openNotesBrowser)
  const openBases = useShellStore((s) => s.openBases)
  const openBiblioteca = useShellStore((s) => s.openBiblioteca)
  const openPlaceholder = useShellStore((s) => s.openPlaceholder)
  const openNewNote = useShellStore((s) => s.openNewNote)
  const { createQuickNote, quickNoteBusy } = useQuickNote()
  const toggleSidebar = useShellStore((s) => s.toggleSidebar)
  const toggleRightPanel = useShellStore((s) => s.toggleRightPanel)
  const tabs = useShellStore((s) => s.tabs)
  const activeTabId = useShellStore((s) => s.activeTabId)
  const theme = useAppStore((s) => s.theme)
  const meta = useAppStore((s) => s.meta)
  const toggleTheme = useAppStore((s) => s.toggleTheme)

  const activeTab = tabs.find((t) => t.id === activeTabId)
  const activeNotePath =
    activeTab?.kind === 'note' ? (activeTab.path ?? null) : null

  const [input, setInput] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)

  // Al abrir: sembrar el prefijo, foco y reset de índice.
  useEffect(() => {
    if (open) {
      setInput(palettePrefix)
      setActiveIndex(0)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [open, palettePrefix])

  const { prefix, query } = parseInput(input)

  // Debounce para búsqueda semántica (?).
  const [debounced, setDebounced] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setDebounced(query.trim()), 220)
    return () => clearTimeout(id)
  }, [query])

  const { data: tree } = useQuery({
    queryKey: ['tree'],
    queryFn: fetchTree,
    enabled: open,
  })
  const { data: recentData } = useQuery({
    queryKey: ['recent'],
    queryFn: () => fetchRecent(12),
    enabled: open,
  })
  // Antes: enabled solo con prefijo '@' (proyectos). Ahora tambien se usa para
  // BUSCAR TAREAS por codigo o titulo (pedido de admin : "cuando me pasan
  // el codigo, buscarlas en sinapsis"), asi que hace falta en el modo mixto y en '!'.
  const { data: tasksData } = useQuery({
    queryKey: ['tasks'],
    queryFn: fetchTasks,
    enabled: open,
  })
  const { data: searchData, isFetching: searching } = useQuery({
    queryKey: ['search', debounced],
    queryFn: () => searchVault(debounced),
    enabled: open && prefix === '?' && debounced.length >= 2,
    staleTime: 60_000,
  })

  const files = useMemo(() => flattenFiles(tree), [tree])

  const actions: PaletteItem[] = useMemo(() => {
    const close = closePalette
    const list: PaletteItem[] = [
      {
        id: 'nav-home',
        icon: Home,
        title: 'Ir a Inicio',
        group: 'Navegación',
        run: () => {
          close()
          openHome()
        },
      },
      {
        id: 'nav-notes',
        icon: FolderTree,
        title: 'Ir a Notas',
        group: 'Navegación',
        run: () => {
          close()
          openNotesBrowser()
        },
      },
      {
        id: 'nav-board',
        icon: CheckSquare,
        title: 'Ir a Tareas',
        group: 'Navegación',
        run: () => {
          close()
          openBoard()
        },
      },
      {
        id: 'nav-bases',
        icon: Database,
        title: 'Ir a Bases',
        group: 'Navegación',
        run: () => {
          close()
          openBases()
        },
      },
      {
        id: 'nav-biblioteca',
        icon: BookOpen,
        title: 'Ir a Biblioteca',
        group: 'Navegación',
        run: () => {
          close()
          openBiblioteca()
        },
      },
      {
        id: 'nav-graph',
        icon: Network,
        title: 'Ir al Grafo',
        group: 'Navegación',
        run: () => {
          close()
          openPlaceholder('grafo', 'Grafo')
        },
      },
      {
        id: 'act-new-note',
        icon: FilePlus2,
        title: 'Nueva nota',
        group: 'Acciones',
        keycap: '⌘N',
        run: () => {
          close()
          openNewNote('')
        },
      },
      {
        id: 'act-quick-note',
        icon: Zap,
        title: 'Nota rápida',
        group: 'Acciones',
        run: () => {
          close()
          if (!quickNoteBusy) void createQuickNote()
        },
      },
      {
        id: 'act-theme',
        icon: theme === 'dark' ? Sun : Moon,
        title: 'Alternar tema',
        group: 'Acciones',
        keycap: '⌘⇧L',
        run: () => {
          close()
          toggleTheme()
        },
      },
      {
        id: 'act-sidebar',
        icon: PanelLeft,
        title: 'Colapsar / expandir sidebar',
        group: 'Acciones',
        keycap: '⌘\\',
        run: () => {
          close()
          toggleSidebar()
        },
      },
      {
        id: 'act-rpanel',
        icon: PanelRight,
        title: 'Alternar panel derecho',
        group: 'Acciones',
        run: () => {
          close()
          toggleRightPanel()
        },
      },
    ]

    // Exportar la nota actual (solo si el tab activo es una nota).
    if (activeNotePath) {
      const name = baseName(activeNotePath)
      const stem = name.replace(/\.md$/, '')
      list.push(
        {
          id: 'exp-md',
          icon: FileDown,
          title: 'Descargar nota actual (ZIP .md + imágenes)',
          meta: name,
          group: 'Nota actual',
          run: () => {
            close()
            fetchNode(activeNotePath)
              .then(async n => triggerDownload(
                await exportMarkdownZip(activeNotePath, n.content, n.lossy), stem + '.zip'))
              .catch(e => window.alert(e instanceof Error ? e.message : 'No se pudo descargar Markdown con imágenes'))
          },
        },
        {
          id: 'exp-html',
          icon: FileDown,
          title: 'Exportar nota actual (HTML)',
          meta: name,
          group: 'Nota actual',
          run: () => {
            close()
            Promise.all([fetchNode(activeNotePath), fetchTree()])
              .then(async ([n, tree]) => triggerDownload(
                await exportNoteHtml(activeNotePath, n.content, buildWikiIndex(tree)), stem + '.html'))
              .catch(() => {})
          },
        }
      )
    }

    return list
  }, [
    closePalette,
    openHome,
    openNotesBrowser,
    openBoard,
    openBases,
    openBiblioteca,
    openPlaceholder,
    openNewNote,
    createQuickNote,
    quickNoteBusy,
    toggleSidebar,
    toggleRightPanel,
    toggleTheme,
    theme,
    activeNotePath,
  ])

  // Notas recientes (últimas editadas): sólo .md, para el estado vacío.
  const recentItems = useMemo<PaletteItem[]>(() => {
    const recent = (recentData?.recent ?? []).filter((r) => r.is_md).slice(0, 6)
    return recent.map((r) => ({
      id: 'recent:' + r.path,
      icon: Clock,
      title: baseName(r.path).replace(/\.md$/, ''),
      meta: r.path,
      group: 'Recientes',
      run: () => {
        closePalette()
        openNote(r.path)
      },
    }))
  }, [recentData, closePalette, openNote])

  const items = useMemo<PaletteItem[]>(() => {
    const noteItem = (f: { path: string; name: string }): PaletteItem => ({
      id: 'note:' + f.path,
      icon: FileText,
      title: f.name,
      meta: f.path,
      group: 'Notas',
      run: () => {
        closePalette()
        openNote(f.path)
      },
    })

    if (prefix === '?') {
      const results = searchData?.results ?? []
      return results.map((r) => ({
        id: 'search:' + r.path,
        icon: FileText,
        title: r.title || (r.path.split('/').pop() ?? r.path),
        meta: r.path,
        group: 'Búsqueda semántica',
        run: () => {
          closePalette()
          openNote(r.path)
        },
      }))
    }

    // ---- TAREAS: por CODIGO (prefijo de 8 del uuid) o por titulo ----
    // El caso de uso real es "me pasaron f0383550, quiero abrirla". Un uuid no se
    // encuentra con fuzzy sobre el titulo, asi que el match por id va PRIMERO y es
    // por PREFIJO del uuid — la misma unidad de 8 chars que se dicta y que aceptan
    // todas las tools del Cerebro.
    const matchTasks = (q: string) => {
      const all = tasksData?.tasks ?? []
      const qq = q.trim().toLowerCase()
      if (!qq) return []
      const scored: { t: (typeof all)[number]; s: number; why: 'id' | 'title' }[] = []
      for (const t of all) {
        // (a) por id: prefijo del uuid. Se prueba tanto que la query sea prefijo del
        //     id como al revés, asi funciona con 4, 8 o los 36 chars completos.
        const id = t.id.toLowerCase()
        if (id.startsWith(qq) || qq.startsWith(id.slice(0, 8))) {
          scored.push({ t, s: 10_000, why: 'id' })
          continue
        }
        // (b) por titulo, fuzzy (el mismo scorer que usan notas y acciones)
        const fs = fuzzyScore(q, t.title)
        if (fs !== null) scored.push({ t, s: fs, why: 'title' })
      }
      return scored
        .sort((a, b) => b.s - a.s)
        .slice(0, 8)
        .map(({ t, why }) => ({
          id: 'task:' + t.id,
          icon: ListChecks,
          title: t.title,
          // El codigo SIEMPRE visible en el meta: es como se habla de las tareas.
          meta: `${t.id.slice(0, 8)} · ${STATUS_META[t.status]?.label ?? t.status}${
            t.project ? ' · ' + t.project : ''
          }${why === 'id' ? ' · coincide por código' : ''}`,
          group: 'Tareas',
          run: () => {
            closePalette()
            // Abre el panel lateral (no un tab): es la vista dividida de tareas.
            openTaskPanel(t.id, t.title)
            openBoard()
          },
        }))
    }

    // '!' = solo tareas. Atajo para cuando ya sabes que buscas una tarea y no
    // queres que compitan notas ni comandos en el ranking.
    if (prefix === '!') {
      return matchTasks(query)
    }

    if (prefix === '#') {
      return []
    }

    if (prefix === '@') {
      const projects = Array.from(
        new Set((tasksData?.tasks ?? []).map((t) => t.project).filter(Boolean))
      ) as string[]
      const base = projects.length ? projects : (meta?.projects ?? [])
      return base
        .map((p) => ({ p, s: fuzzyScore(query, p) }))
        .filter((x) => x.s !== null)
        .sort((a, b) => (b.s as number) - (a.s as number))
        .map(({ p }) => ({
          id: 'proj:' + p,
          icon: AtSign,
          title: p,
          meta: 'Proyecto · abre el tablero',
          group: 'Proyectos',
          run: () => {
            closePalette()
            openBoard()
          },
        }))
    }

    if (prefix === '>') {
      return actions
        .map((a) => ({ a, s: fuzzyScore(query, a.title) }))
        .filter((x) => x.s !== null)
        .sort((a, b) => (b.s as number) - (a.s as number))
        .map((x) => x.a)
    }

    // Modo mixto (sin prefijo)
    if (!query.trim()) {
      return [...actions, ...recentItems]
    }
    const scoredNotes = files
      .map((f) => ({ f, s: fuzzyScore(query, f.name) ?? fuzzyScore(query, f.path) }))
      .filter((x) => x.s !== null)
      .sort((a, b) => (b.s as number) - (a.s as number))
      .slice(0, 8)
      .map((x) => noteItem(x.f))
    const scoredActions = actions
      .map((a) => ({ a, s: fuzzyScore(query, a.title) }))
      .filter((x) => x.s !== null)
      .sort((a, b) => (b.s as number) - (a.s as number))
      .map((x) => x.a)
    // Las TAREAS van primero en el modo mixto: si alguien pega un codigo, lo que
    // quiere es la tarea, no una nota cuyo nombre se parezca al uuid.
    const scoredTasks = matchTasks(query)
    return [...scoredTasks, ...scoredNotes, ...scoredActions]
  }, [prefix, query, files, actions, recentItems, searchData, tasksData, closePalette, openNote, openBoard, openTaskPanel])

  // Mantener el índice activo dentro de rango.
  useEffect(() => {
    setActiveIndex((i) => (items.length === 0 ? 0 : Math.min(i, items.length - 1)))
  }, [items])

  // Scroll del item activo a la vista.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${activeIndex}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, items])

  if (!open) return null

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIndex((i) => (items.length ? (i + 1) % items.length : 0))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => (items.length ? (i - 1 + items.length) % items.length : 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      items[activeIndex]?.run()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      closePalette()
    }
  }

  const emptyLabel =
    prefix === '!'
      ? query.trim()
        ? `Ninguna tarea coincide con «${query.trim()}»`
        : 'Escribí el código (8 chars) o parte del título de una tarea…'
      : prefix === '?'
      ? debounced.length < 2
        ? 'Escribí al menos 2 caracteres para buscar en el contenido…'
        : searching
          ? 'Buscando…'
          : `Sin resultados para «${debounced}»`
      : prefix === '#'
        ? 'Etiquetas — próximamente (Ola 4)'
        : prefix === '@'
          ? 'Sin proyectos'
          : query.trim()
            ? `Sin resultados para «${query.trim()}»`
            : 'Escribí para buscar notas o ejecutá un comando'

  const placeholder =
    prefix === '!'
      ? 'Buscar una tarea por código o título…'
      : prefix === '?'
      ? 'Buscar en el contenido (semántica)…'
      : prefix === '>'
        ? 'Ejecutar un comando…'
        : prefix === '#'
          ? 'Saltar a una etiqueta…'
          : prefix === '@'
            ? 'Saltar a un proyecto…'
            : 'Buscar tareas (o su código), notas, proyectos o ejecutar un comando…'

  // Render con headers de grupo.
  let lastGroup = ''

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed inset-0 z-[100] flex items-start justify-center bg-black/60 backdrop-blur-[2px]"
          style={{ paddingTop: '18vh' }}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) closePalette()
          }}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.98, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 8 }}
            transition={{ duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
            className="palette-surface w-full max-w-[640px] overflow-hidden"
            role="dialog"
            aria-modal="true"
          >
            {/* Input */}
            <div className="flex items-center gap-3 px-[18px] py-4">
              <Search size={18} className="shrink-0 text-fg-subtle" />
              <input
                ref={inputRef}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value)
                  setActiveIndex(0)
                }}
                onKeyDown={onKeyDown}
                placeholder={placeholder}
                className="w-full bg-transparent text-[16px] text-fg placeholder:text-fg-subtle outline-none"
                spellCheck={false}
                autoComplete="off"
              />
            </div>

            <div className="h-px bg-[var(--border-subtle)]" />

            {/* Lista */}
            <div ref={listRef} className="max-h-[52vh] overflow-y-auto py-1.5">
              {items.length === 0 ? (
                <div className="px-4 py-8 text-center text-[13px] text-fg-subtle">
                  {emptyLabel}
                </div>
              ) : (
                items.map((it, idx) => {
                  const showHeader = it.group !== lastGroup
                  lastGroup = it.group
                  const Icon = it.icon
                  const active = idx === activeIndex
                  return (
                    <div key={it.id}>
                      {showHeader && (
                        <div className="px-3 pb-1 pt-2 text-[11px] font-medium uppercase tracking-[0.05em] text-fg-subtle">
                          {it.group}
                        </div>
                      )}
                      <button
                        data-idx={idx}
                        onMouseMove={() => setActiveIndex(idx)}
                        onClick={() => it.run()}
                        className={`relative flex h-9 w-full items-center gap-2.5 px-3 text-left outline-none ${
                          active ? 'bg-surface-hover' : ''
                        }`}
                      >
                        {active && (
                          <span className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded bg-accent" />
                        )}
                        <Icon
                          size={16}
                          className={`shrink-0 ${active ? 'text-accent' : 'text-fg-subtle'}`}
                        />
                        <span
                          className={`truncate text-[14px] font-medium ${
                            active ? 'text-fg' : 'text-fg-muted'
                          }`}
                        >
                          {it.title}
                        </span>
                        {it.meta && (
                          <span className="ml-auto shrink-0 truncate pl-3 text-[12px] text-fg-faint">
                            {it.meta}
                          </span>
                        )}
                        {it.keycap && (
                          <span className={`keycap ${it.meta ? 'ml-2' : 'ml-auto'}`}>
                            {it.keycap}
                          </span>
                        )}
                      </button>
                    </div>
                  )
                })
              )}
            </div>

            {/* Footer */}
            <div className="h-px bg-[var(--border-subtle)]" />
            <div className="flex items-center gap-3 px-3 py-2 text-[11px] text-fg-subtle">
              <span className="flex items-center gap-1">
                <ArrowUpDown size={12} /> navegar
              </span>
              <span className="flex items-center gap-1">
                <CornerDownLeft size={12} /> abrir
              </span>
              <span className="flex items-center gap-1">
                <Hash size={12} /> tags
                <AtSign size={12} className="ml-1" /> proyectos
              </span>
              <span className="ml-auto">{files.length} notas</span>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
