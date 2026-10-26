import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react'
import { AnimatePresence, motion } from 'framer-motion'

type Kind = 'ok' | 'err'

interface ToastItem {
  id: number
  msg: string
  kind: Kind
}

type PushToast = (msg: string, kind?: Kind) => void

const ToastCtx = createContext<PushToast>(() => {})

// eslint-disable-next-line react-refresh/only-export-components
export function useToast(): PushToast {
  return useContext(ToastCtx)
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const push = useCallback<PushToast>((msg, kind = 'ok') => {
    const id = Date.now() + Math.random()
    setItems((xs) => [...xs, { id, msg, kind }])
    setTimeout(() => {
      setItems((xs) => xs.filter((i) => i.id !== id))
    }, 2600)
  }, [])

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        <AnimatePresence>
          {items.map((i) => (
            <motion.div
              key={i.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
              className={`pointer-events-auto min-w-[180px] rounded-md border px-3 py-2 text-sm shadow-lg ${
                i.kind === 'ok'
                  ? 'border-[var(--border-strong)] bg-surface text-fg'
                  : 'border-red-500/40 bg-surface text-red-300'
              }`}
            >
              {i.msg}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  )
}
