import { useState, type FormEvent } from 'react'
import { motion } from 'framer-motion'
import { useAppStore } from '../../store'
import { changePassword, ApiError } from '../../lib/api'
import { useToast } from '../Toast'

interface Props {
  /** En modo forzado el mensaje explica por qué aparece y no hay "Cancelar". */
  forced?: boolean
  /** Callback tras un cambio exitoso (cerrar modal / levantar el gate). */
  onSuccess?: () => void
  /** Callback de cancelación (solo modo no forzado). */
  onCancel?: () => void
}

/**
 * Formulario de cambio de contraseña. Reusa los tokens/clases del login
 * (login-input, login-label, login-btn) para verse parte natural de la app.
 * Usado tanto en "Mi cuenta" como en el gate de cambio forzado (primer login).
 */
export default function ChangePasswordForm({ forced, onSuccess, onCancel }: Props) {
  const toast = useToast()
  const setMustChangePassword = useAppStore((s) => s.setMustChangePassword)
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const canSubmit =
    !!oldPw && newPw.length >= 8 && newPw === confirmPw && !loading

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!oldPw || !newPw) return
    if (newPw.length < 8) {
      setError('La nueva contraseña debe tener al menos 8 caracteres.')
      return
    }
    if (newPw !== confirmPw) {
      setError('La confirmación no coincide con la nueva contraseña.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      await changePassword(oldPw, newPw)
      setMustChangePassword(false)
      toast('Contraseña actualizada')
      setOldPw('')
      setNewPw('')
      setConfirmPw('')
      onSuccess?.()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('La contraseña actual es incorrecta.')
      } else {
        setError(err instanceof Error ? err.message : 'No se pudo cambiar la contraseña.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={onSubmit} className="space-y-4">
      <div className="space-y-1.5">
        <label htmlFor="cp-old" className="login-label">
          Contraseña actual
        </label>
        <input
          id="cp-old"
          type="password"
          autoComplete="current-password"
          value={oldPw}
          onChange={(e) => setOldPw(e.target.value)}
          placeholder="••••••••"
          className="login-input w-full px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-faint"
        />
      </div>
      <div className="space-y-1.5">
        <label htmlFor="cp-new" className="login-label">
          Nueva contraseña
        </label>
        <input
          id="cp-new"
          type="password"
          autoComplete="new-password"
          value={newPw}
          onChange={(e) => setNewPw(e.target.value)}
          placeholder="mínimo 8 caracteres"
          className="login-input w-full px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-faint"
        />
      </div>
      <div className="space-y-1.5">
        <label htmlFor="cp-confirm" className="login-label">
          Repetir nueva contraseña
        </label>
        <input
          id="cp-confirm"
          type="password"
          autoComplete="new-password"
          value={confirmPw}
          onChange={(e) => setConfirmPw(e.target.value)}
          placeholder="••••••••"
          className="login-input w-full px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-faint"
        />
      </div>
      {error && (
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="text-sm text-red-400"
        >
          {error}
        </motion.p>
      )}
      <div className={forced ? '' : 'flex justify-end gap-2'}>
        {!forced && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg"
          >
            Cancelar
          </button>
        )}
        <button
          type="submit"
          disabled={!canSubmit}
          className={
            forced
              ? 'login-btn w-full px-3 py-2.5 text-sm'
              : 'rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50'
          }
        >
          {loading ? 'Guardando…' : 'Cambiar contraseña'}
        </button>
      </div>
    </form>
  )
}
