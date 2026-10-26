import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { fetchMe, type Me } from '../../lib/api'
import ChangePasswordForm from './ChangePasswordForm'

interface Props {
  open: boolean
  onClose: () => void
  /** Si arranca directo en el formulario de cambio de contraseña. */
  initialTab?: 'info' | 'password'
}

/** Etiqueta legible del rol. */
function roleLabel(role: string): string {
  if (role === 'admin') return 'Administrador'
  if (role === 'member') return 'Miembro'
  if (role === 'guest') return 'Invitado'
  return role
}

/** Descripción del acceso a directorios (scopes) para mostrar en Mi cuenta. */
function accessLabel(me: Me): string {
  if (me.role === 'admin' || me.emergency) return 'Acceso completo (admin)'
  const grants = me.scope_grants.filter((g) => g.path_prefix)
  if (grants.length === 0) return 'Sin accesos asignados'
  return grants
    .map((g) => `${g.path_prefix}${g.can_write ? ' (lectura/escritura)' : ' (lectura)'}`)
    .join(', ')
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="shrink-0 text-xs text-fg-subtle">{label}</span>
      <span className="truncate text-right text-[13px] text-fg">{value}</span>
    </div>
  )
}

export default function AccountDialog({ open, onClose, initialTab = 'info' }: Props) {
  const [tab, setTab] = useState<'info' | 'password'>(initialTab)
  const { data: me, isLoading } = useQuery({
    queryKey: ['me'],
    queryFn: fetchMe,
    enabled: open,
  })

  const tabBtn = (active: boolean) =>
    `rounded-md px-2.5 py-1 text-xs transition ${
      active
        ? 'bg-surface text-fg'
        : 'text-fg-subtle hover:bg-surface hover:text-fg'
    }`

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
            {/* Wrapper que centra: el centrado lo hace el grid, NO el transform.
                Asi la animacion de Framer (scale+opacity) no pisa el centrado. */}
            <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto p-3 sm:p-4">
              <Dialog.Content asChild forceMount>
                <motion.div
                  initial={{ opacity: 0, scale: 0.98 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.98 }}
                  transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                  className="glass-strong relative flex max-h-[90vh] w-full max-w-md flex-col overflow-y-auto rounded-lg border border-[var(--border-strong)] p-3 shadow-2xl sm:p-5"
                >
                <Dialog.Title className="text-sm font-semibold text-fg">
                  Mi cuenta
                </Dialog.Title>

                <div className="mt-3 flex gap-1">
                  <button className={tabBtn(tab === 'info')} onClick={() => setTab('info')}>
                    Información
                  </button>
                  <button
                    className={tabBtn(tab === 'password')}
                    onClick={() => setTab('password')}
                  >
                    Cambiar contraseña
                  </button>
                </div>

                <div className="mt-4">
                  {tab === 'info' ? (
                    isLoading || !me ? (
                      <p className="py-4 text-center text-xs text-fg-subtle">Cargando…</p>
                    ) : (
                      <div className="divide-y divide-[var(--border-strong)]">
                        <Field label="Usuario" value={me.name} />
                        <Field label="Rol" value={roleLabel(me.role)} />
                        {me.email && <Field label="Email" value={me.email} />}
                        <Field label="Accesos" value={accessLabel(me)} />
                      </div>
                    )
                  ) : (
                    <ChangePasswordForm onSuccess={onClose} onCancel={() => setTab('info')} />
                  )}
                </div>

                {tab === 'info' && (
                  <div className="mt-4 flex justify-end">
                    <Dialog.Close asChild>
                      <button className="rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg">
                        Cerrar
                      </button>
                    </Dialog.Close>
                  </div>
                )}
              </motion.div>
            </Dialog.Content>
            </div>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  )
}
