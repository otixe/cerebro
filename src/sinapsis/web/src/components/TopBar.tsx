import { useState } from 'react'
import { Menu, Search, PanelRight, Sun, Moon, LogOut, ChevronRight, User, Fingerprint, ShieldCheck } from 'lucide-react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { useQuery } from '@tanstack/react-query'
import { useShellStore } from '../store'
import { useAppStore } from '../store'
import { fetchMe, logoutSession } from '../lib/api'
import AccountDialog from './account/AccountDialog'
import AdminDialog from './admin/AdminDialog'

// Clases del menú reusadas del patrón existente (FileTree) para estética idéntica.
const MENU_CONTENT =
  'menu-content z-50 min-w-[200px] rounded-md border border-[var(--border-strong)] glass p-1 text-[13px] shadow-xl'
const ITEM =
  'flex cursor-pointer select-none items-center gap-2 rounded px-2 py-1.5 text-fg-muted outline-none transition data-[highlighted]:bg-surface data-[highlighted]:text-fg'
const ITEM_DISABLED =
  'flex select-none items-center gap-2 rounded px-2 py-1.5 text-fg-faint outline-none data-[disabled]:opacity-60'

function breadcrumbs(
  tab:
    | { kind: 'note'; path?: string }
    | { kind: 'board' }
    | { kind: 'placeholder'; title: string }
    | undefined,
  title: string | undefined
): string[] {
  if (!tab) return ['El Cerebro']
  if (tab.kind === 'note' && tab.path) {
    const parts = tab.path.replace(/\.md$/, '').split('/')
    return ['Notas', ...parts]
  }
  if (tab.kind === 'board') return ['Tareas', 'Tablero']
  return [title ?? 'SINAPSIS']
}

export default function TopBar() {
  const toggleSidebar = useShellStore((s) => s.toggleSidebar)
  const toggleRightPanel = useShellStore((s) => s.toggleRightPanel)
  const rightPanelOpen = useShellStore((s) => s.rightPanelOpen)
  const toggleMobileNav = useShellStore((s) => s.toggleMobileNav)
  const toggleMobilePanel = useShellStore((s) => s.toggleMobilePanel)
  const openPalette = useShellStore((s) => s.openPalette)
  const tabs = useShellStore((s) => s.tabs)
  const activeTabId = useShellStore((s) => s.activeTabId)
  const theme = useAppStore((s) => s.theme)
  const toggleTheme = useAppStore((s) => s.toggleTheme)
  const logout = useAppStore((s) => s.logout)

  const [accountOpen, setAccountOpen] = useState(false)
  const [accountTab, setAccountTab] = useState<'info' | 'password'>('info')
  const [adminOpen, setAdminOpen] = useState(false)
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe })
  const isAdmin = me?.role === 'admin' || me?.emergency === true

  const userName = me?.name ?? 'Cuenta'
  const initial = (me?.name?.[0] ?? '?').toUpperCase()

  async function handleLogout() {
    // Revoca la sesión en el backend (sessions.revoked_at); luego limpia el token
    // local. Si el revoke falla igual cerramos sesión local para no dejar colgado.
    try {
      await logoutSession()
    } catch {
      /* noop: cerramos sesión local igual */
    } finally {
      logout()
    }
  }

  function openAccount(tab: 'info' | 'password') {
    setAccountTab(tab)
    setAccountOpen(true)
  }

  const activeTab = tabs.find((t) => t.id === activeTabId)
  const crumbs = breadcrumbs(activeTab as never, activeTab?.title)

  return (
    <header className="glass flex min-h-[44px] shrink-0 items-center gap-2 border-b border-[var(--border-strong)] pl-[max(0.75rem,env(safe-area-inset-left))] pr-[max(0.75rem,env(safe-area-inset-right))] pt-[env(safe-area-inset-top)]">
      {/* Hamburguesa — solo móvil: abre el drawer de navegación */}
      <button
        onClick={toggleMobileNav}
        title="Abrir navegación"
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:hidden"
      >
        <Menu size={18} />
      </button>

      {/* Colapsar/expandir sidebar — solo desktop */}
      <button
        onClick={toggleSidebar}
        title="Alternar sidebar  (⌘\)"
        className="hidden h-7 w-7 shrink-0 items-center justify-center rounded-md text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:flex"
      >
        <Menu size={16} />
      </button>

      {/* Breadcrumbs */}
      <nav className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
        {crumbs.map((c, i) => {
          const last = i === crumbs.length - 1
          return (
            <span key={i} className="flex min-w-0 items-center gap-1">
              {i > 0 && <ChevronRight size={13} className="shrink-0 text-fg-faint" />}
              <span
                className={`truncate text-[13px] ${
                  last ? 'font-medium text-fg' : 'text-fg-subtle'
                }`}
              >
                {c}
              </span>
            </span>
          )
        })}
      </nav>

      {/* ⌘K trigger — en móvil solo el icono (el keycap ⌘ no aplica en touch) */}
      <button
        onClick={() => openPalette('')}
        title="Command palette"
        className="flex h-11 shrink-0 items-center gap-2 rounded-md border border-[var(--border-strong)] bg-[rgba(255,255,255,0.02)] px-2.5 text-fg-subtle transition hover:bg-surface-hover hover:text-fg-muted lg:h-7"
      >
        <Search size={14} />
        <span className="hidden text-[12px] sm:inline">Buscar…</span>
        <span className="keycap !hidden sm:!inline-flex">⌘K</span>
      </button>

      <button
        onClick={toggleTheme}
        title="Alternar tema  (⌘⇧L)"
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:h-7 lg:w-7"
      >
        {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
      </button>

      {/* Panel derecho — desktop (toggle de columna) */}
      <button
        onClick={toggleRightPanel}
        title="Alternar panel derecho  (⌘.)"
        className={`hidden h-7 w-7 shrink-0 items-center justify-center rounded-md transition hover:bg-surface-hover hover:text-fg lg:flex ${
          rightPanelOpen ? 'text-accent' : 'text-fg-subtle'
        }`}
      >
        <PanelRight size={16} />
      </button>

      {/* Panel derecho — móvil (abre el drawer de contexto) */}
      <button
        onClick={toggleMobilePanel}
        title="Abrir panel de contexto"
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-fg-subtle transition hover:bg-surface-hover hover:text-fg lg:hidden"
      >
        <PanelRight size={18} />
      </button>

      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            title={userName}
            className="flex h-11 shrink-0 items-center gap-2 rounded-md px-1.5 text-fg-subtle outline-none transition hover:bg-surface-hover hover:text-fg data-[state=open]:bg-surface-hover data-[state=open]:text-fg lg:h-7"
          >
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-surface text-[11px] font-medium text-fg-muted lg:h-6 lg:w-6">
              {initial}
            </span>
            <span className="hidden max-w-[120px] truncate text-[12px] sm:inline">
              {userName}
            </span>
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content align="end" sideOffset={4} className={MENU_CONTENT}>
            <DropdownMenu.Item className={ITEM} onSelect={() => openAccount('info')}>
              <User size={14} /> Mi cuenta
            </DropdownMenu.Item>
            <DropdownMenu.Item className={ITEM_DISABLED} disabled>
              <Fingerprint size={14} /> Passkey (próximamente)
            </DropdownMenu.Item>
            {isAdmin && (
              <>
                <DropdownMenu.Separator className="my-1 h-px bg-[var(--border-strong)]" />
                <DropdownMenu.Item className={ITEM} onSelect={() => setAdminOpen(true)}>
                  <ShieldCheck size={14} /> Administración
                </DropdownMenu.Item>
              </>
            )}
            <DropdownMenu.Separator className="my-1 h-px bg-[var(--border-strong)]" />
            <DropdownMenu.Item className={ITEM} onSelect={handleLogout}>
              <LogOut size={14} /> Cerrar sesión
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <AccountDialog
        open={accountOpen}
        onClose={() => setAccountOpen(false)}
        initialTab={accountTab}
      />
      {isAdmin && (
        <AdminDialog open={adminOpen} onClose={() => setAdminOpen(false)} />
      )}
    </header>
  )
}
