import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAppStore } from './store'
import { fetchMe } from './lib/api'
import Login from './components/Login'
import AppShell from './components/AppShell'
import ForcedPasswordChange from './components/account/ForcedPasswordChange'

export default function App() {
  const token = useAppStore((s) => s.token)
  const mustChangePassword = useAppStore((s) => s.mustChangePassword)
  const setMustChangePassword = useAppStore((s) => s.setMustChangePassword)
  const loadMeta = useAppStore((s) => s.loadMeta)

  // Con una sesión viva (token persistido) el flag local puede no reflejar el
  // estado real de la DB (p.ej. se marcó must_change_password después del login).
  // Refrescamos desde /me al haber token para que el gate aparezca igual.
  const { data: me } = useQuery({
    queryKey: ['me'],
    queryFn: fetchMe,
    enabled: !!token,
  })

  useEffect(() => {
    if (me) setMustChangePassword(me.must_change_password)
  }, [me, setMustChangePassword])

  // Carga la metadata dinamica (roster de agentes/proyectos) al haber sesión.
  useEffect(() => {
    if (token) loadMeta()
  }, [token, loadMeta])

  if (!token) return <Login />
  if (mustChangePassword) return <ForcedPasswordChange />
  return <AppShell />
}
