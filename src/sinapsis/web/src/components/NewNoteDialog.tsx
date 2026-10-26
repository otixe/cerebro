import { useEffect, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createFile, fetchTree, ApiError } from '../lib/api'
import FolderPicker from './FolderPicker'
import { useToast } from './Toast'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (path: string) => void
  /** Ruta/carpeta prellenada al abrir (p.ej. "Carpeta/"). */
  initialPath?: string
}

export default function NewNoteDialog({
  open,
  onOpenChange,
  onCreated,
  initialPath = '',
}: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [path, setPath] = useState('')
  const [folder, setFolder] = useState('')
  const { data: tree, isError: treeError } = useQuery({
    queryKey: ['tree'], queryFn: fetchTree, enabled: open,
  })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // Al abrir, sembrar con la carpeta prellenada (si la hay).
  useEffect(() => {
    if (open) {
      setPath(initialPath)
      setFolder(initialPath.replace(/\/$/, ''))
      setErr(null)
      setBusy(false)
    }
  }, [open, initialPath])

  function reset() {
    setPath('')
    setFolder('')
    setErr(null)
    setBusy(false)
  }

  async function onCreate() {
    const p = path.trim()
    if (!p || busy) return
    setBusy(true)
    setErr(null)
    try {
      const res = await createFile(p)
      await qc.invalidateQueries({ queryKey: ['tree'] })
      toast('Nota creada')
      onOpenChange(false)
      reset()
      onCreated(res.path)
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr('Ya existe una nota en esa ruta.')
      } else {
        setErr(e instanceof Error ? e.message : 'No se pudo crear la nota')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o)
        if (!o) reset()
      }}
    >
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
                className="glass-strong fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[calc(100vw-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-[var(--border-strong)] p-5 shadow-2xl"
              >
                <Dialog.Title className="text-sm font-semibold text-fg">
                  Nueva nota
                </Dialog.Title>
                <Dialog.Description className="mt-1 text-xs text-fg-subtle">
                  Elegí una carpeta y escribí el nombre, o ingresá la ruta completa. Se agrega .md si falta.
                </Dialog.Description>

                <div className="mt-3">
                  <label className="mb-1 block text-xs text-fg-muted">Carpeta</label>
                  {tree ? (
                    <FolderPicker tree={tree} selected={folder} compact onSelect={(dir) => {
                      setFolder(dir)
                      const name = path.split('/').pop() ?? ''
                      setPath(dir ? `${dir}/${name}` : name)
                    }} />
                  ) : (
                    <div className="rounded-md border border-[var(--border-subtle)] p-3 text-xs text-fg-subtle">
                      {treeError ? 'No se pudo cargar el árbol; podés escribir la ruta completa.' : 'Cargando carpetas…'}
                    </div>
                  )}
                </div>
                <label htmlFor="new-note-path" className="mt-3 block text-xs text-fg-muted">
                  Nombre del archivo o ruta completa
                </label>
                <input
                  id="new-note-path"
                  autoFocus
                  value={path}
                  onChange={(e) => {
                    const next = e.target.value
                    setPath(next)
                    setFolder(next.includes('/') ? next.slice(0, next.lastIndexOf('/')) : '')
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') onCreate()
                  }}
                  placeholder="ej: 50 Agents/assistant-b/Notas/mi-nota.md"
                  className="mt-1 w-full rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent"
                />
                {err && <p className="mt-2 text-xs text-red-400">{err}</p>}

                <div className="mt-4 flex justify-end gap-2">
                  <Dialog.Close asChild>
                    <button className="rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg">
                      Cancelar
                    </button>
                  </Dialog.Close>
                  <button
                    onClick={onCreate}
                    disabled={busy || !path.trim()}
                    className="rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50"
                  >
                    {busy ? 'Creando…' : 'Crear'}
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
