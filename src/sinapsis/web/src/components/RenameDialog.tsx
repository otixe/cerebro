import { useEffect, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import { useQueryClient } from '@tanstack/react-query'
import { renameFile, ApiError, type TreeNode } from '../lib/api'
import { useToast } from './Toast'

export type RenameMode = 'rename' | 'move'

interface Props {
  target: { node: TreeNode; mode: RenameMode } | null
  onClose: () => void
  onRenamed: (oldPath: string, newPath: string) => void
}

function parentDir(path: string): string {
  const i = path.lastIndexOf('/')
  return i === -1 ? '' : path.slice(0, i)
}

export default function RenameDialog({ target, onClose, onRenamed }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const open = target !== null
  const mode = target?.mode ?? 'rename'
  const isDir = target?.node.type === 'dir'

  // Sembrar el input al abrir: renombrar = nombre base; mover = ruta completa.
  useEffect(() => {
    if (!target) return
    setErr(null)
    setBusy(false)
    setValue(target.mode === 'rename' ? target.node.name : target.node.path)
  }, [target])

  function resolveNewPath(): string {
    if (!target) return ''
    const input = value.trim()
    if (mode === 'rename') {
      const dir = parentDir(target.node.path)
      return dir ? `${dir}/${input}` : input
    }
    return input
  }

  async function onSubmit() {
    if (!target || busy) return
    const input = value.trim()
    if (!input) return
    const newPath = resolveNewPath()
    if (newPath === target.node.path) {
      onClose()
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const res = await renameFile(target.node.path, newPath)
      await qc.invalidateQueries({ queryKey: ['tree'] })
      toast(mode === 'move' ? 'Movido' : 'Renombrado')
      onRenamed(res.from, res.to)
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr('El destino ya existe. Elegí otro nombre o ruta.')
      } else {
        setErr(e instanceof Error ? e.message : 'No se pudo completar la operación')
      }
    } finally {
      setBusy(false)
    }
  }

  const title = mode === 'move' ? 'Mover' : 'Renombrar'
  const label = isDir ? 'carpeta' : 'archivo'

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <AnimatePresence>
        {open && (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild forceMount>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                className="fixed inset-0 z-40 bg-black/50"
              />
            </Dialog.Overlay>
            <Dialog.Content asChild forceMount>
              <motion.div
                initial={{ opacity: 0, y: 8, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 8, scale: 0.98 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                className="glass-strong fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-[var(--border-strong)] p-5 shadow-2xl"
              >
                <Dialog.Title className="text-sm font-semibold text-fg">
                  {title} {label}
                </Dialog.Title>
                <Dialog.Description className="mt-1 text-xs text-fg-subtle">
                  {mode === 'move'
                    ? 'Ruta destino completa dentro del vault.'
                    : `Nuevo nombre del ${label} (se mantiene la carpeta).`}
                </Dialog.Description>

                <input
                  autoFocus
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') onSubmit()
                  }}
                  className="mt-3 w-full rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 font-mono text-[13px] text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent"
                />
                {err && <p className="mt-2 text-xs text-red-400">{err}</p>}

                <div className="mt-4 flex justify-end gap-2">
                  <Dialog.Close asChild>
                    <button className="rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg">
                      Cancelar
                    </button>
                  </Dialog.Close>
                  <button
                    onClick={onSubmit}
                    disabled={busy || !value.trim()}
                    className="rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50"
                  >
                    {busy ? 'Aplicando…' : title}
                  </button>
                </div>
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  )
}
