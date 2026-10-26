import { motion } from 'framer-motion'
import { useAppStore } from '../../store'
import ChangePasswordForm from './ChangePasswordForm'
import ParticlesBackground from '../ParticlesBackground'

/**
 * Gate de cambio de contraseña forzado (primer login con pass generada por el
 * sistema). Se muestra a pantalla completa, prominente y sin escape fácil, ANTES
 * de dejar usar la app. Al cambiar la contraseña, el flag pasa a false y el gate
 * desaparece. Reusa la estética del login (GridBackground implícito por el bg,
 * login-*). No bloquea el logout de emergencia porque el token sigue vivo, pero
 * la UI no ofrece otra acción que cambiar la contraseña.
 */
export default function ForcedPasswordChange() {
  const logout = useAppStore((s) => s.logout)

  return (
    <div className="relative flex min-h-full w-full items-center justify-center overflow-y-auto bg-bg px-4 py-8 sm:px-6">
      <ParticlesBackground variant="ambient" />
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 w-full max-w-sm"
      >
        <div className="mb-8 text-center">
          <h1 className="text-lg font-semibold text-fg">Cambiá tu contraseña</h1>
          <p className="mt-2 text-[13px] leading-relaxed text-fg-subtle">
            Tu contraseña actual fue generada por el sistema. Por seguridad,
            elegí una nueva antes de continuar.
          </p>
        </div>
        <ChangePasswordForm forced />
        <button
          type="button"
          onClick={logout}
          className="mt-6 w-full text-center text-xs text-fg-subtle transition hover:text-fg"
        >
          Cerrar sesión
        </button>
      </motion.div>
    </div>
  )
}
