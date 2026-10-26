import { useState } from 'react'
import { ChevronRight, Database, Folder, FolderOpen } from 'lucide-react'
import type { TreeNode } from '../lib/api'

// ---------- Folder picker (dirs del árbol) ----------
function dirsOnly(node: TreeNode): TreeNode[] {
  return (node.children ?? []).filter((c) => c.type === 'dir')
}

function FolderRow({
  node,
  depth,
  selected,
  onSelect,
}: {
  node: TreeNode
  depth: number
  selected: string
  onSelect: (path: string) => void
}) {
  const [open, setOpen] = useState(false)
  const subDirs = dirsOnly(node)
  const isSel = selected === node.path
  return (
    <div>
      <div
        className={`group flex h-[28px] items-center gap-1 rounded-md pr-2 transition ${
          isSel
            ? 'bg-[rgba(168, 85, 247,0.12)] text-fg'
            : 'text-fg-subtle hover:bg-surface-hover hover:text-fg-muted'
        }`}
        style={{ paddingLeft: 6 + depth * 12 }}
      >
        <button
          onClick={() => setOpen((o) => !o)}
          className={`flex h-4 w-4 shrink-0 items-center justify-center rounded ${
            subDirs.length ? 'text-fg-faint hover:text-fg' : 'opacity-0'
          }`}
          tabIndex={subDirs.length ? 0 : -1}
        >
          <ChevronRight
            size={12}
            className={`transition-transform ${open ? 'rotate-90' : ''}`}
          />
        </button>
        <button
          onClick={() => onSelect(node.path)}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
        >
          {open && subDirs.length ? (
            <FolderOpen size={14} className={isSel ? 'text-accent' : 'text-fg-faint'} />
          ) : (
            <Folder size={14} className={isSel ? 'text-accent' : 'text-fg-faint'} />
          )}
          <span className="truncate text-[13px]">{node.name}</span>
        </button>
      </div>
      {open &&
        subDirs.map((c) => (
          <FolderRow
            key={c.path}
            node={c}
            depth={depth + 1}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
    </div>
  )
}

export default function FolderPicker({
  tree,
  selected,
  onSelect,
  compact = false,
}: {
  tree: TreeNode
  selected: string
  onSelect: (path: string) => void
  compact?: boolean
}) {
  return (
    <div className={compact
      ? "glass flex h-48 min-h-0 w-full flex-col overflow-hidden rounded-md border border-[var(--border-subtle)]"
      : "glass flex w-[240px] shrink-0 flex-col border-r border-[var(--border-subtle)]"}>
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-3 text-[11px] font-semibold uppercase tracking-wider text-fg-subtle">
        <Database size={13} className="text-fg-faint" /> Carpetas
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1 py-1">
        <div
          className={`flex h-[28px] cursor-pointer items-center gap-1.5 rounded-md px-2 text-[13px] transition ${
            selected === ''
              ? 'bg-[rgba(168, 85, 247,0.12)] text-fg'
              : 'text-fg-subtle hover:bg-surface-hover hover:text-fg-muted'
          }`}
          onClick={() => onSelect('')}
        >
          <Folder size={14} className={selected === '' ? 'text-accent' : 'text-fg-faint'} />
          Raíz del vault
        </div>
        {dirsOnly(tree).map((c) => (
          <FolderRow
            key={c.path}
            node={c}
            depth={0}
            selected={selected}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  )
}

