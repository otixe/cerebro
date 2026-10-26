import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as ContextMenu from '@radix-ui/react-context-menu'
import { ChevronRight, Folder, FolderOpen, FileText } from 'lucide-react'
import { fetchTree, type TreeNode } from '../lib/api'
import { useShellStore } from '../store'
import RenameDialog, { type RenameMode } from './RenameDialog'
import DeleteDialog from './DeleteDialog'

const MENU_CONTENT =
  'menu-content z-50 min-w-[180px] rounded-md border border-[var(--border-strong)] glass p-1 text-[13px] shadow-xl'
const ITEM =
  'flex cursor-pointer select-none items-center rounded px-2 py-1.5 text-fg-muted outline-none transition data-[highlighted]:bg-surface data-[highlighted]:text-fg'
const ITEM_DANGER =
  'flex cursor-pointer select-none items-center rounded px-2 py-1.5 text-red-400 outline-none transition data-[highlighted]:bg-red-500/10 data-[highlighted]:text-red-300'

interface MenuActions {
  onRename: (node: TreeNode) => void
  onMove: (node: TreeNode) => void
  onDelete: (node: TreeNode) => void
}

/** Carpetas: sólo borrado; renombrar/mover siguen limitados a .md. */
function menuItemsFor(node: TreeNode, actions: MenuActions) {
  if (node.type === 'dir')
    return [{ label: 'Eliminar carpeta…', danger: true, run: () => actions.onDelete(node) }]
  return [
    { label: 'Renombrar…', run: () => actions.onRename(node) },
    { label: 'Mover…', run: () => actions.onMove(node) },
    { label: 'Eliminar…', danger: true, run: () => actions.onDelete(node) },
  ]
}

function RowContextMenu({
  node,
  actions,
  children,
}: {
  node: TreeNode
  actions: MenuActions
  children: React.ReactNode
}) {
  const items = menuItemsFor(node, actions)
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>{children}</ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content className={MENU_CONTENT}>
          {items.map((it) => (
            <ContextMenu.Item
              key={it.label}
              onSelect={it.run}
              className={it.danger ? ITEM_DANGER : ITEM}
            >
              {it.label}
            </ContextMenu.Item>
          ))}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  )
}

/** Igualdad de ruta o descendiente, con límite de segmento (no confundir a con ab). */
function isAtOrBelow(path: string, ancestor: string): boolean {
  return path === ancestor || path.startsWith(`${ancestor}/`)
}

interface Props {
  /** Abrir un archivo en un tab (mecanismo de tabs existente). */
  onOpenFile: (path: string) => void
}

/** Carpetas primero, luego archivos; alfabético dentro de cada grupo. */
function sortNodes(nodes: TreeNode[]): TreeNode[] {
  return [...nodes].sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1
    return a.name.localeCompare(b.name, 'es')
  })
}

/** Camina el arbol desde la raiz siguiendo los segmentos de `targetPath`.
 *  Devuelve la cadena de carpetas (trail) hasta la carpeta contenedora y, si
 *  el path apunta a un archivo (nota .md), su ruta para resaltarlo. */
function buildTrailFor(
  root: TreeNode,
  targetPath: string
): { trail: TreeNode[]; file: string | null } {
  const segments = targetPath.split('/').filter(Boolean)
  const trail: TreeNode[] = []
  let children = root.children ?? []
  let file: string | null = null
  for (const seg of segments) {
    const node = children.find((n) => n.name === seg)
    if (!node) break
    if (node.type === 'dir') {
      trail.push(node)
      children = node.children ?? []
    } else {
      // Ultimo segmento: es una nota. No entra al trail; se resalta.
      file = node.path
      break
    }
  }
  return { trail, file }
}

export default function MillerColumns({ onOpenFile }: Props) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['tree'],
    queryFn: fetchTree,
  })

  // Cadena de carpetas abiertas (una por columna, a partir de la raíz).
  const [trail, setTrail] = useState<TreeNode[]>([])
  // Archivo seleccionado (resaltado), independiente de la cadena de carpetas.
  const [selectedFile, setSelectedFile] = useState<string | null>(null)
  const [renameTarget, setRenameTarget] = useState<{
    node: TreeNode
    mode: RenameMode
  } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TreeNode | null>(null)
  const renamePath = useShellStore((s) => s.renamePath)
  const deletePath = useShellStore((s) => s.deletePath)
  const scrollRef = useRef<HTMLDivElement>(null)
  const setNotesCurrentDir = useShellStore((s) => s.setNotesCurrentDir)

  useEffect(() => {
    setNotesCurrentDir(trail.at(-1)?.path ?? '')
  }, [trail, setNotesCurrentDir])

  const actions: MenuActions = {
    onRename: (node) => setRenameTarget({ node, mode: 'rename' }),
    onMove: (node) => setRenameTarget({ node, mode: 'move' }),
    onDelete: (node) => setDeleteTarget(node),
  }

  function trimTrail(path: string) {
    // Si cambia una carpeta del trail, cerrar desde ella: no conservar nodos con paths viejos.
    setTrail((prev) => {
      const firstAffected = prev.findIndex((dir) => isAtOrBelow(dir.path, path))
      return firstAffected < 0 ? prev : prev.slice(0, firstAffected)
    })
  }

  function onDeleted(path: string) {
    setSelectedFile((prev) => (prev === path ? null : prev))
    trimTrail(path)
    deletePath(path)
  }

  function onRenamed(oldPath: string, newPath: string) {
    setSelectedFile((prev) => (prev === oldPath ? newPath : prev))
    trimTrail(oldPath)
    renamePath(oldPath, newPath)
  }

  // Al abrir una carpeta nueva, desplazar hacia la derecha para ver la columna.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollLeft = el.scrollWidth
  }, [trail.length])

  const notesRevealPath = useShellStore((s) => s.notesRevealPath)
  const clearNotesReveal = useShellStore((s) => s.clearNotesReveal)

  // Reveal: al pedir revelar una ruta (desde el breadcrumb/boton de la nota),
  // armamos el trail caminando el arbol hasta su carpeta contenedora y
  // resaltamos el archivo. Espera a que el arbol (data) este cargado.
  useEffect(() => {
    if (!notesRevealPath || !data) return
    const { trail: built, file } = buildTrailFor(data, notesRevealPath)
    setTrail(built)
    setSelectedFile(file)
    clearNotesReveal()
  }, [notesRevealPath, data, clearNotesReveal])

  if (isLoading)
    return <div className="px-6 py-4 text-sm text-fg-subtle">Cargando árbol…</div>
  if (isError)
    return (
      <div className="px-6 py-4 text-sm text-danger">
        {error instanceof Error ? error.message : 'Error al cargar el árbol'}
      </div>
    )
  if (!data) return null

  // Columna 0 = raíz; columna i+1 = hijos de la carpeta trail[i].
  const columns: { nodes: TreeNode[]; key: string }[] = [
    { nodes: sortNodes(data.children ?? []), key: '__root__' },
    ...trail.map((dir) => ({ nodes: sortNodes(dir.children ?? []), key: dir.path })),
  ]

  function openDir(colIndex: number, node: TreeNode) {
    setTrail((prev) => [...prev.slice(0, colIndex), node])
    setSelectedFile(null)
  }

  function openFile(colIndex: number, node: TreeNode) {
    setTrail((prev) => prev.slice(0, colIndex))
    setSelectedFile(node.path)
    onOpenFile(node.path)
  }

  return (
    <>
    <div className="flex h-full flex-col">
      {/* Breadcrumb del path activo */}
      <div className="glass flex h-11 shrink-0 items-center gap-2 overflow-x-auto border-b border-[var(--border-subtle)] px-4 text-[13px] text-fg-subtle lg:h-10 lg:text-[12px]">
        <span className="shrink-0 text-fg-muted">El Cerebro</span>
        {trail.map((dir, i) => (
          <span key={dir.path} className="flex shrink-0 items-center gap-2">
            <ChevronRight size={13} className="text-fg-faint" />
            <span className={i === trail.length - 1 ? 'font-medium text-fg' : 'text-fg-muted'}>
              {dir.name}
            </span>
          </span>
        ))}
      </div>

      {/* Columnas (scroll horizontal).
          MOVIL (<lg): cada columna ocupa TODO el ancho y el contenedor es un
          carrusel con scroll-snap, asi que se ve UNA columna a la vez (efecto
          drill-down). El useEffect ya existente (`scrollLeft = scrollWidth`)
          deja visible la ULTIMA columna abierta; volver es un swipe, o sea el
          MISMO mecanismo de scroll que ya estaba.
          Por que snap y no `hidden`: este componente NO tiene handler de
          cerrar/volver (a diferencia de closeTaskPanel en TasksView). Ocultar
          las columnas previas dejaria al usuario atrapado en la mas profunda, y
          agregar ese handler seria logica nueva (fuera de alcance de esta ola).
          En lg: identico a antes (columnas de 240px lado a lado). */}
      <div
        ref={scrollRef}
        className="flex min-h-0 flex-1 snap-x snap-mandatory overflow-x-auto lg:snap-none"
      >
        {columns.map((col, colIndex) => {
          const activeDir = trail[colIndex]?.path ?? null
          return (
            <div
              key={col.key}
              /* w-full + shrink-0: en movil la columna es la pantalla entera
                 (a 360px CSS, 240px fijos dejaban 120px de columna cortada al
                 lado). snap-start la alinea al borde. Desde lg vuelve a 240px. */
              className="flex w-full shrink-0 snap-start flex-col overflow-y-auto border-r border-[var(--border-subtle)] py-2 lg:w-[240px]"
            >
              {col.nodes.length === 0 && (
                <div className="px-3 py-2 text-[12px] text-fg-subtle/60">Vacío</div>
              )}
              {col.nodes.map((node) => {
                const isDir = node.type === 'dir'
                const selected = isDir
                  ? activeDir === node.path
                  : selectedFile === node.path
                const Icon = isDir ? (selected ? FolderOpen : Folder) : FileText
                const row = (
                  <button
                    key={node.path}
                    onClick={() =>
                      isDir ? openDir(colIndex, node) : openFile(colIndex, node)
                    }
                    /* Fila tocable: 44px minimos y 14px de texto en movil;
                       en lg vuelve al alto por padding y 13px de antes. */
                    className={`group mx-2 flex min-h-[44px] items-center gap-2 rounded-md px-2 py-2 text-left text-[14px] outline-none transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] focus-visible:ring-2 focus-visible:ring-[var(--accent)] lg:min-h-0 lg:text-[13px] ${
                      selected
                        ? 'bg-[rgba(168,85,247,0.12)] text-fg shadow-[var(--glow-sm)]'
                        : 'text-fg-muted hover:bg-surface-hover hover:text-fg'
                    }`}
                  >
                    <Icon
                      size={15}
                      className={`shrink-0 ${
                        selected ? 'text-accent' : 'text-fg-faint'
                      }`}
                    />
                    <span className="min-w-0 flex-1 truncate">
                      {isDir ? node.name : node.name.replace(/\.md$/, '')}
                    </span>
                    {isDir && (
                      <ChevronRight
                        size={13}
                        className={`shrink-0 ${
                          selected ? 'text-accent' : 'text-fg-faint'
                        }`}
                      />
                    )}
                  </button>
                )
                return <RowContextMenu key={node.path} node={node} actions={actions}>{row}</RowContextMenu>
              })}
            </div>
          )
        })}
      </div>
    </div>
    <RenameDialog
      target={renameTarget}
      onClose={() => setRenameTarget(null)}
      onRenamed={onRenamed}
    />
    <DeleteDialog
      node={deleteTarget}
      onClose={() => setDeleteTarget(null)}
      onDeleted={onDeleted}
    />
    </>
  )
}
