import { FileText, CheckSquare, Database, BookOpen, Network, Hash, Star, Home, Columns3, X, Plus } from 'lucide-react'
import { motion } from 'framer-motion'
import { useShellStore, type Tab } from '../store'

function tabIcon(tab: Tab) {
  if (tab.kind === 'board') return CheckSquare
  if (tab.kind === 'note') return FileText
  if (tab.kind === 'home') return Home
  if (tab.kind === 'notes') return Columns3
  if (tab.kind === 'biblioteca') return BookOpen
  switch (tab.placeholderKey) {
    case 'bases':
      return Database
    case 'grafo':
      return Network
    case 'tags':
      return Hash
    case 'pinned':
      return Star
    default:
      return FileText
  }
}

export default function DocTabs() {
  const tabs = useShellStore((s) => s.tabs)
  const activeTabId = useShellStore((s) => s.activeTabId)
  const setActiveTab = useShellStore((s) => s.setActiveTab)
  const closeTab = useShellStore((s) => s.closeTab)
  const openPalette = useShellStore((s) => s.openPalette)

  if (tabs.length === 0) return null

  return (
    <div className="flex h-9 shrink-0 items-stretch border-b border-[var(--border-strong)] bg-panel">
      <div className="flex min-w-0 flex-1 items-stretch overflow-x-auto">
        {tabs.map((tab) => {
          const Icon = tabIcon(tab)
          const active = tab.id === activeTabId
          return (
            <motion.div
              key={tab.id}
              layout
              transition={{ type: 'spring', stiffness: 300, damping: 30 }}
              className={`group relative flex max-w-[220px] shrink-0 items-center gap-2 border-r border-[var(--border-subtle)] pl-3 pr-2 ${
                active ? 'glass-strong text-fg' : 'glass text-fg-subtle hover:text-fg-muted'
              }`}
            >
              {active && (
                <span className="absolute inset-x-0 bottom-0 h-0.5 bg-accent" />
              )}
              <button
                onClick={() => setActiveTab(tab.id)}
                className="flex min-w-0 items-center gap-2 py-0"
              >
                <Icon size={14} className={active ? 'text-accent' : 'text-fg-faint'} />
                <span className="truncate text-[13px]">{tab.title}</span>
              </button>
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  closeTab(tab.id)
                }}
                title="Cerrar"
                className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-fg-faint opacity-0 transition hover:bg-surface-hover hover:text-fg group-hover:opacity-100"
              >
                <X size={13} />
              </button>
            </motion.div>
          )
        })}
        <button
          onClick={() => openPalette('')}
          title="Abrir (⌘K)"
          className="flex w-9 shrink-0 items-center justify-center text-fg-faint transition hover:bg-surface-hover hover:text-fg"
        >
          <Plus size={15} />
        </button>
      </div>
    </div>
  )
}
