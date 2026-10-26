import { useState } from 'react'
import * as AlertDialog from '@radix-ui/react-alert-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { deleteDir, deleteFile, dirStats, ApiError, type TreeNode } from '../lib/api'
import { useToast } from './Toast'

interface Props {
  node: TreeNode | null
  onClose: () => void
  onDeleted: (path: string) => void
}

export default function DeleteDialog({ node, onClose, onDeleted }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  const open = node !== null
  const isDir = node?.type === 'dir'
  const stats = useQuery({
    queryKey: ['dir_stats', node?.path],
    queryFn: () => dirStats(node!.path),
    enabled: open && isDir,
    retry: false,
  })

  async function onConfirm() {
    if (!node || busy || (isDir && (!stats.data || stats.isError || stats.isFetching || !stats.data.allowed))) return
    setBusy(true)
    try {
      if (isDir) await deleteDir(node.path)
      else await deleteFile(node.path)
      await qc.invalidateQueries({ queryKey: ['tree'] })
      toast('Movido a .trash')
      onDeleted(node.path)
      onClose()
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'No se pudo eliminar'
      toast(msg, 'err')
    } finally {
      setBusy(false)
    }
  }

  return (
    <AlertDialog.Root open={open} onOpenChange={(o) => !o && !busy && onClose()}>
      <AnimatePresence>
        {open && (
          <AlertDialog.Portal forceMount>
            <AlertDialog.Overlay asChild forceMount>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                className="fixed inset-0 z-40 bg-black/50"
              />
            </AlertDialog.Overlay>
            <AlertDialog.Content asChild forceMount>
              <motion.div
                initial={{ opacity: 0, y: 8, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 8, scale: 0.98 }}
                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                className="glass-strong fixed left-1/2 top-1/2 z-50 w-full max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-[var(--border-strong)] p-5 shadow-2xl"
              >
                <AlertDialog.Title className="text-sm font-semibold text-fg">
                  Eliminar {isDir ? 'carpeta' : 'archivo'}
                </AlertDialog.Title>
                <AlertDialog.Description className="mt-2 text-xs leading-relaxed text-fg-subtle">
                  <span className="font-mono text-fg-muted">{node?.path}</span>
                  <br />
                  No es un borrado definitivo: se mueve a{' '}
                  <span className="font-mono text-fg-muted">.trash/</span> y se
                  puede recuperar. No se elimina de forma permanente.
                </AlertDialog.Description>

                {isDir && (
                  <div className="mt-3 text-xs text-fg-subtle" role="status">
                    {stats.isPending || stats.isFetching ? 'Contando archivos…' : stats.isError
                      ? (stats.error instanceof Error ? stats.error.message : 'No se pudo contar la carpeta')
                      : <>
                          {stats.data.allowed ? `Se van a mover ${stats.data.files} archivos a .trash.` : `La carpeta contiene ${stats.data.truncated ? `más de ${stats.data.files}` : stats.data.files} archivos.`}
                          {!stats.data.allowed && <span className="block text-red-300">La carpeta tiene {stats.data.truncated ? `más de ${stats.data.files}` : stats.data.files} archivos; el tope es {stats.data.max_files}. No se puede eliminar desde este menú.</span>}
                          {stats.data.symlinks > 0 && <span className="block text-amber-300">Advertencia: contiene {stats.data.symlinks} enlace(s) simbólico(s){stats.data.truncated ? ' en la parte contada' : ''}; los destinos no se moverán.</span>}
                        </>}
                  </div>
                )}
                <div className="mt-4 flex justify-end gap-2">
                  <AlertDialog.Cancel asChild>
                    <button
                      disabled={busy}
                      className="rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg disabled:opacity-50"
                    >
                      Cancelar
                    </button>
                  </AlertDialog.Cancel>
                  <button
                    onClick={onConfirm}
                    disabled={busy || (isDir && (!stats.data || stats.isError || !stats.data.allowed || stats.isFetching))}
                    className="rounded-md border border-red-500/40 bg-red-500/10 px-3 py-1.5 text-xs font-medium text-red-300 transition hover:bg-red-500/20 disabled:opacity-50"
                  >
                    {busy ? 'Eliminando…' : 'Mover a .trash'}
                  </button>
                </div>
              </motion.div>
            </AlertDialog.Content>
          </AlertDialog.Portal>
        )}
      </AnimatePresence>
    </AlertDialog.Root>
  )
}
