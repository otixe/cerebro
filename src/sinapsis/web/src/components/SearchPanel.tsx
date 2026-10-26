import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import { searchVault } from '../lib/api'

interface Props {
  onSelect: (path: string) => void
}

export default function SearchPanel({ onSelect }: Props) {
  const [term, setTerm] = useState('')
  const [debounced, setDebounced] = useState('')

  useEffect(() => {
    const id = setTimeout(() => setDebounced(term.trim()), 280)
    return () => clearTimeout(id)
  }, [term])

  const enabled = debounced.length >= 2
  const { data, isFetching, isError } = useQuery({
    queryKey: ['search', debounced],
    queryFn: () => searchVault(debounced),
    enabled,
    staleTime: 60_000,
  })

  const results = data?.results ?? []

  return (
    <div className="border-b border-[var(--border-subtle)] px-2 py-2">
      <div className="relative">
        <input
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder="Buscar en el vault…"
          className="w-full rounded-md border border-[var(--border-strong)] bg-surface px-2.5 py-1.5 text-[13px] text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent"
        />
        {isFetching && enabled && (
          <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-fg-subtle">
            …
          </span>
        )}
      </div>

      <AnimatePresence initial={false}>
        {enabled && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="mt-2 max-h-72 overflow-y-auto">
              {isError && (
                <p className="px-1 py-1 text-xs text-red-400">Error al buscar</p>
              )}
              {!isError && results.length === 0 && !isFetching && (
                <p className="px-1 py-1 text-xs text-fg-subtle">Sin resultados</p>
              )}
              <ul className="space-y-1">
                {results.map((r, i) => (
                  <li key={`${r.path}-${i}`}>
                    <button
                      onClick={() => onSelect(r.path)}
                      className="w-full rounded-md px-2 py-1.5 text-left transition hover:bg-surface"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-[13px] font-medium text-fg">
                          {r.title || r.path.split('/').pop()}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-fg-subtle">
                          {r.score.toFixed(2)}
                        </span>
                      </div>
                      <div className="truncate text-[11px] text-fg-subtle">
                        {r.path}
                      </div>
                      {r.snippet && (
                        <div className="mt-0.5 line-clamp-2 text-[11px] text-fg-muted/70">
                          {r.snippet}
                        </div>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
