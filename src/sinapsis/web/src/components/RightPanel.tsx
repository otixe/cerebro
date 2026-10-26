import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { SlidersHorizontal, Link2, ListTree, FileQuestion } from 'lucide-react'
import { fetchNode } from '../lib/api'
import { parseFrontmatter } from '../lib/frontmatter'
import { extractHeadings } from '../lib/outline'
import PropertiesPanel from './PropertiesPanel'
import BacklinksPanel from './BacklinksPanel'

type PanelTab = 'props' | 'links' | 'outline'

interface Props {
  activePath: string | null
  onNavigate: (path: string) => void
}

const READ_SCROLL_ID = 'note-scroll'

export default function RightPanel({ activePath, onNavigate }: Props) {
  const [tab, setTab] = useState<PanelTab>('outline')

  const { data } = useQuery({
    queryKey: ['node', activePath],
    queryFn: () => fetchNode(activePath as string),
    enabled: !!activePath,
  })

  const raw = data?.content ?? ''
  const { body } = useMemo(() => parseFrontmatter(raw), [raw])
  const headings = useMemo(() => extractHeadings(body), [body])

  const [activeHeading, setActiveHeading] = useState(0)

  // Scrollspy: resalta el heading visible más alto en el contenedor de lectura.
  useEffect(() => {
    if (tab !== 'outline' || !activePath) return
    const container = document.getElementById(READ_SCROLL_ID)
    if (!container) return
    const onScroll = () => {
      const hs = container.querySelectorAll<HTMLElement>('h1,h2,h3,h4,h5,h6')
      const top = container.getBoundingClientRect().top + 24
      let idx = 0
      for (let i = 0; i < hs.length; i++) {
        if (hs[i].getBoundingClientRect().top <= top) idx = i
        else break
      }
      setActiveHeading(idx)
    }
    onScroll()
    container.addEventListener('scroll', onScroll, { passive: true })
    return () => container.removeEventListener('scroll', onScroll)
  }, [tab, activePath, headings.length])

  function scrollToHeading(idx: number) {
    const container = document.getElementById(READ_SCROLL_ID)
    const hs = container?.querySelectorAll<HTMLElement>('h1,h2,h3,h4,h5,h6')
    hs?.[idx]?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    setActiveHeading(idx)
  }

  const TABS: { key: PanelTab; label: string; icon: typeof Link2 }[] = [
    { key: 'props', label: 'Propiedades', icon: SlidersHorizontal },
    { key: 'links', label: 'Backlinks', icon: Link2 },
    { key: 'outline', label: 'Outline', icon: ListTree },
  ]

  return (
    <aside className="glass flex w-[300px] shrink-0 flex-col border-l border-[var(--border-strong)]">
      {/* Tabs internas */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-2 shadow-[0_1px_8px_-4px_rgba(168,85,247,0.25)]">
        {TABS.map((t) => {
          const Icon = t.icon
          const active = tab === t.key
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              title={t.label}
              className={`relative flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] outline-none transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] focus-visible:ring-2 focus-visible:ring-[var(--accent)] ${
                active
                  ? 'bg-surface-hover text-fg shadow-[var(--glow-sm)]'
                  : 'text-fg-subtle hover:bg-surface-hover hover:text-fg-muted'
              }`}
            >
              <Icon size={14} className={active ? 'text-accent' : 'text-fg-faint'} />
              <span>{t.label}</span>
              {active && (
                <span className="pointer-events-none absolute inset-x-2 bottom-0.5 h-0.5 rounded-full bg-accent" />
              )}
            </button>
          )
        })}
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={tab}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          className="min-h-0 flex-1 overflow-y-auto p-3"
        >
          {!activePath ? (
            <EmptyPanel text="Abrí una nota para ver su contexto." />
          ) : tab === 'outline' ? (
            headings.length === 0 ? (
              <EmptyPanel text="Esta nota no tiene encabezados." />
            ) : (
              <ul className="space-y-0.5">
                {headings.map((h, i) => (
                  <li key={i}>
                    <button
                      onClick={() => scrollToHeading(i)}
                      style={{ paddingLeft: `${(h.level - 1) * 12 + 8}px` }}
                      className={`flex w-full items-center rounded-md py-2 pr-2 text-left text-[13px] transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] ${
                        i === activeHeading
                          ? 'bg-[rgba(168,85,247,0.12)] text-fg shadow-[var(--glow-sm)]'
                          : 'text-fg-subtle hover:bg-surface-hover hover:text-fg-muted'
                      }`}
                    >
                      <span className="truncate">{h.text}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )
          ) : tab === 'props' ? (
            <PropertiesPanel activePath={activePath} raw={raw} />
          ) : (
            <BacklinksPanel activePath={activePath} onNavigate={onNavigate} />
          )}
        </motion.div>
      </AnimatePresence>
    </aside>
  )
}

function EmptyPanel({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-center gap-3 px-4 py-10 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-surface">
        <FileQuestion size={22} className="text-fg-faint" />
      </div>
      <p className="max-w-[220px] text-[13px] text-fg-subtle">{text}</p>
    </div>
  )
}
