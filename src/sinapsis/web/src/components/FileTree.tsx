import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as ContextMenu from '@radix-ui/react-context-menu'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { fetchTree, type TreeNode } from '../lib/api'
import RenameDialog, { type RenameMode } from './RenameDialog'
import DeleteDialog from './DeleteDialog'

interface Props {
  selected: string | null
  onSelect: (path: string) => void
  /** Abrir el diálogo de nueva nota con la carpeta `dir` prellenada. */
  onNewNoteInDir: (dir: string) => void
  /** Notificar a la app que una ruta cambió (renombrar/mover) para remapear selección. */
  onPathRenamed: (oldPath: string, newPath: string) => void
  /** Notificar a la app que una ruta fue eliminada (soft) para limpiar selección. */
  onPathDeleted: (path: string) => void
}

interface Actions {
  onRename: (node: TreeNode) => void
  onMove: (node: TreeNode) => void
  onDelete: (node: TreeNode) => void
  onNewNote: (dir: string) => void
}

function parentDir(path: string): string {
  const i = path.lastIndexOf('/')
  return i === -1 ? '' : path.slice(0, i)
}

const MENU_CONTENT =
  'menu-content z-50 min-w-[180px] rounded-md border border-[var(--border-strong)] glass p-1 text-[13px] shadow-xl'
const ITEM =
  'flex cursor-pointer select-none items-center rounded px-2 py-1.5 text-fg-muted outline-none transition data-[highlighted]:bg-surface data-[highlighted]:text-fg'
const ITEM_DANGER =
  'flex cursor-pointer select-none items-center rounded px-2 py-1.5 text-red-400 outline-none transition data-[highlighted]:bg-red-500/10 data-[highlighted]:text-red-300'

/** Acciones del menú según tipo de nodo. Devuelve [ {label, danger, run} ]. */
function menuItemsFor(node: TreeNode, actions: Actions) {
  const dir = node.type === 'dir' ? node.path : parentDir(node.path)
  const items: { label: string; danger?: boolean; run: () => void }[] = [
    { label: 'Nueva nota aquí', run: () => actions.onNewNote(dir) },
    { label: 'Renombrar…', run: () => actions.onRename(node) },
    { label: 'Mover…', run: () => actions.onMove(node) },
    { label: 'Eliminar…', danger: true, run: () => actions.onDelete(node) },
  ]
  return items
}

export default function FileTree({
  selected,
  onSelect,
  onNewNoteInDir,
  onPathRenamed,
  onPathDeleted,
}: Props) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['tree'],
    queryFn: fetchTree,
  })

  const [renameTarget, setRenameTarget] = useState<{
    node: TreeNode
    mode: RenameMode
  } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<TreeNode | null>(null)

  const actions: Actions = {
    onRename: (node) => setRenameTarget({ node, mode: 'rename' }),
    onMove: (node) => setRenameTarget({ node, mode: 'move' }),
    onDelete: (node) => setDeleteTarget(node),
    onNewNote: (dir) => onNewNoteInDir(dir),
  }

  if (isLoading)
    return <div className="px-3 py-2 text-xs text-fg-subtle">Cargando árbol…</div>
  if (isError)
    return (
      <div className="px-3 py-2 text-xs text-red-400">
        {error instanceof Error ? error.message : 'Error al cargar el árbol'}
      </div>
    )
  if (!data) return null

  return (
    <>
      {/* --tree-indent: sangria por nivel del arbol. En movil 8px (a 360px CSS
          un arbol de 6 niveles con 12px comia 72px de ancho antes del nombre);
          desde lg vuelve a los 12px originales. Se declara aca y la heredan
          todos los TreeItem, que la consumen en su paddingLeft. */}
      <ul className="select-none [--tree-indent:8px] lg:[--tree-indent:12px]">
        {(data.children ?? []).map((child) => (
          <TreeItem
            key={child.path}
            node={child}
            depth={0}
            selected={selected}
            onSelect={onSelect}
            actions={actions}
          />
        ))}
      </ul>

      <RenameDialog
        target={renameTarget}
        onClose={() => setRenameTarget(null)}
        onRenamed={onPathRenamed}
      />
      <DeleteDialog
        node={deleteTarget}
        onClose={() => setDeleteTarget(null)}
        onDeleted={onPathDeleted}
      />
    </>
  )
}

function Kebab({ node, actions }: { node: TreeNode; actions: Actions }) {
  const items = menuItemsFor(node, actions)
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          aria-label="Acciones"
          onClick={(e) => e.stopPropagation()}
          className="mr-1 flex h-11 w-11 shrink-0 items-center justify-center rounded text-fg-subtle opacity-60 outline-none transition hover:bg-[var(--border-strong)] hover:text-fg focus:opacity-100 data-[state=open]:opacity-100 lg:h-auto lg:w-auto lg:p-0.5 lg:opacity-0 lg:group-hover:opacity-100"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="12" cy="5" r="1.6" />
            <circle cx="12" cy="12" r="1.6" />
            <circle cx="12" cy="19" r="1.6" />
          </svg>
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" sideOffset={4} className={MENU_CONTENT}>
          {items.map((it) => (
            <DropdownMenu.Item
              key={it.label}
              onSelect={it.run}
              className={it.danger ? ITEM_DANGER : ITEM}
            >
              {it.label}
            </DropdownMenu.Item>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}

function RowContextMenu({
  node,
  actions,
  children,
}: {
  node: TreeNode
  actions: Actions
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

function TreeItem({
  node,
  depth,
  selected,
  onSelect,
  actions,
}: {
  node: TreeNode
  depth: number
  selected: string | null
  onSelect: (path: string) => void
  actions: Actions
}) {
  const [open, setOpen] = useState(depth < 1)
  // calc + var: mismo resultado que `8 + depth * 12` en escritorio, y en movil
  // usa 8px por nivel. Sigue siendo layout, no logica.
  const pad = { paddingLeft: `calc(8px + ${depth} * var(--tree-indent, 12px))` }

  if (node.type === 'dir') {
    return (
      <li>
        <RowContextMenu node={node} actions={actions}>
          <div className="group flex items-center rounded transition hover:bg-surface">
            <button
              onClick={() => setOpen((o) => !o)}
              style={pad}
              className="flex min-h-[44px] min-w-0 flex-1 items-center gap-1.5 py-1 pr-1 text-left text-[14px] text-fg-muted lg:min-h-0 lg:text-[13px]"
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                className={`shrink-0 text-fg-subtle transition-transform ${open ? 'rotate-90' : ''}`}
              >
                <path
                  d="M9 6l6 6-6 6"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              <span className="truncate font-medium">{node.name}</span>
            </button>
            <Kebab node={node} actions={actions} />
          </div>
        </RowContextMenu>
        {open && node.children && (
          <ul>
            {node.children.map((child) => (
              <TreeItem
                key={child.path}
                node={child}
                depth={depth + 1}
                selected={selected}
                onSelect={onSelect}
                actions={actions}
              />
            ))}
          </ul>
        )}
      </li>
    )
  }

  const isSelected = selected === node.path
  return (
    <li>
      <RowContextMenu node={node} actions={actions}>
        <div
          className={`group flex items-center rounded transition ${
            isSelected ? 'bg-surface' : 'hover:bg-surface'
          }`}
        >
          <button
            onClick={() => onSelect(node.path)}
            style={pad}
            className={`flex min-h-[44px] min-w-0 flex-1 items-center gap-1.5 py-1 pr-1 text-left text-[14px] transition lg:min-h-0 lg:text-[13px] ${
              isSelected ? 'text-fg' : 'text-fg-subtle group-hover:text-fg-muted'
            }`}
          >
            <span className="ml-[12px] truncate">
              {node.name.replace(/\.md$/, '')}
            </span>
          </button>
          <Kebab node={node} actions={actions} />
        </div>
      </RowContextMenu>
    </li>
  )
}
