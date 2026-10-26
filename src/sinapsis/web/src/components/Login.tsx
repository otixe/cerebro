import { useState, type FormEvent } from 'react'
import { motion } from 'framer-motion'
import { useAppStore } from '../store'
import { login } from '../lib/api'
import CharReveal from './login/CharReveal'
import ParticlesBackground, { type ParticlesStats } from './ParticlesBackground'

// ParticlesBackground ya se importa de forma estatica en AppShell (variant
// 'ambient'), asi que aqui se importa directo (no lazy): un import dinamico
// seria inefectivo, Vite ya lo deja en el chunk principal de todos modos.

export default function Login() {
  const setToken = useAppStore((s) => s.setToken)
  const setMustChangePassword = useAppStore((s) => s.setMustChangePassword)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [stats, setStats] = useState<ParticlesStats | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!username || !password || loading) return
    setLoading(true)
    setError(null)
    try {
      const { token, must_change_password } = await login(username, password)
      setMustChangePassword(must_change_password)
      setToken(token)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error desconocido')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="relative flex h-full w-full items-center justify-center overflow-hidden bg-bg px-4">
      <ParticlesBackground variant="login" onStats={setStats} />

      {/* Telemetria QA del campo de particulas (portada de PARTICLES-REFERENCE.html).
          aria-hidden + pointer-events-none: puramente informativo, no compite con
          el formulario ni con el foco de teclado/lectores de pantalla. */}
      {stats && (
        <div
          aria-hidden="true"
          className="particles-stats pointer-events-none fixed inset-x-0 bottom-[max(0.65rem,env(safe-area-inset-bottom))] z-[1] px-4 text-center sm:inset-x-auto sm:left-[max(0.9rem,env(safe-area-inset-left))] sm:text-left"
        >
          {'SINAPSIS · campo de particulas\n'}
          {'modo: '}
          <b>{stats.mode}</b>
          {`  dpr:${stats.dpr.toFixed(1)}  ${stats.deviceLabel}\n`}
          {'particulas: '}
          <b>{stats.count}</b>
          {` / objetivo ${stats.target}\n`}
          {`conexion max: ${Math.round(stats.maxDist)}px  frame: ${stats.frameMs.toFixed(2)}ms`}
        </div>
      )}

      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 w-full max-w-sm"
      >
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, delay: 0.05, ease: [0.16, 1, 0.3, 1] }}
          className="mb-10 text-center"
        >
          <CharReveal
            text="SINAPSIS"
            className="login-wordmark"
          />
        </motion.div>

        <form onSubmit={onSubmit} className="space-y-4">
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.1, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-1.5"
          >
            <label htmlFor="login-username" className="login-label">
              Usuario
            </label>
            <input
              id="login-username"
              type="text"
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="usuario"
              className="login-input w-full px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-faint"
            />
          </motion.div>
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-1.5"
          >
            <label htmlFor="login-password" className="login-label">
              Contraseña
            </label>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              className="login-input w-full px-3.5 py-2.5 text-sm text-fg placeholder:text-fg-faint"
            />
          </motion.div>
          {error && (
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-sm text-red-400"
            >
              {error}
            </motion.p>
          )}
          <motion.button
            type="submit"
            disabled={loading || !username || !password}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.19, ease: [0.16, 1, 0.3, 1] }}
            className="login-btn w-full px-3 py-2.5 text-sm"
          >
            {loading ? 'Entrando…' : 'Entrar'}
          </motion.button>
        </form>
      </motion.div>
    </div>
  )
}
