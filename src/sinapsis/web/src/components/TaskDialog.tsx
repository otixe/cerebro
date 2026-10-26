/** TaskDialog — envoltorio MODAL de TaskDetail.
 *
 *  El cuerpo (estado, hooks, handlers, formulario, subtareas, dependencias,
 *  comentarios, issue, botones) vive en ./TaskDetail desde el refactor del
 *  . Aca solo queda el envoltorio Radix Dialog + Framer Motion.
 *
 *  Dos cosas que NO se pueden perder y por eso estan explicitas:
 *  1) onOpenChange={(o) => !busy && onOpenChange(o)} — bloquea el cierre
 *     (Escape / click en el overlay) mientras se esta guardando. `busy` ahora
 *     vive en TaskDetail, asi que lo recibimos por onBusyChange.
 *  2) PRIORITIES es API publica de ESTE modulo (otros archivos la importan de
 *     './TaskDialog'), asi que se re-exporta.
 */
import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import TaskDetail, { type Props as TaskDetailProps } from './TaskDetail'

// Re-export: PRIORITIES se definia en este archivo y varios modulos la importan
// desde aca. Se movio a TaskDetail con el resto del cuerpo; el nombre publico
// './TaskDialog' -> PRIORITIES se preserva intacto.
export { PRIORITIES } from './TaskDetail'

/** Exactamente las props que TaskDialog aceptaba antes del refactor: las de
 *  TaskDetail menos las que el envoltorio provee por su cuenta. */
type Props = Omit<
  TaskDetailProps,
  'variant' | 'renderTitle' | 'renderDescription' | 'renderClose' | 'onBusyChange'
>

export default function TaskDialog({
  open,
  onOpenChange,
  task = null,
  initialProject,
  onOpenTask,
}: Props) {
  // Espejo del `busy` de TaskDetail. Unico proposito: guardar el onOpenChange
  // del Dialog.Root para que un Escape a mitad de guardado no cierre el modal.
  const [busy, setBusy] = useState(false)

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <AnimatePresence>
        {open && (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild forceMount>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15 }}
                className="fixed inset-0 z-40 bg-black/60 backdrop-blur-[2px]"
              />
            </Dialog.Overlay>
            {/* Wrapper que centra: el centrado lo hace el grid, NO el transform.
                Así la animación de Framer (scale+opacity) no pisa el centrado. */}
            <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto p-4">
              <Dialog.Content asChild forceMount>
                <motion.div
                  initial={{ opacity: 0, scale: 0.98 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.98 }}
                  transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                  className="glass-strong relative max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-lg border border-[var(--border-strong)] p-5 shadow-2xl"
                >
                <TaskDetail
                  variant="modal"
                  open={open}
                  onOpenChange={onOpenChange}
                  task={task}
                  initialProject={initialProject}
                  onOpenTask={onOpenTask}
                  onBusyChange={setBusy}
                  renderTitle={(title) => (
                    <Dialog.Title className="text-sm font-semibold text-fg">
                      {title}
                    </Dialog.Title>
                  )}
                  renderDescription={(description) => (
                    <Dialog.Description className="mt-1 text-xs text-fg-subtle">
                      {description}
                    </Dialog.Description>
                  )}
                  renderClose={(children) => (
                    <Dialog.Close asChild>{children}</Dialog.Close>
                  )}
                />
              </motion.div>
            </Dialog.Content>
            </div>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  )
}
