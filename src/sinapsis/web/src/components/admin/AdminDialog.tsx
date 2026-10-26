import { useMemo, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AnimatePresence, motion } from 'framer-motion'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Copy,
  FolderPlus,
  Loader2,
  Plus,
  ShieldCheck,
  UserPlus,
  X,
} from 'lucide-react'
import {
  adminAssignAgent,
  adminCreateGrant,
  adminCreateUser,
  adminRevokeGrant,
  fetchAdminPrincipals,
  fetchTree,
  type AdminPrincipal,
  type CreatedUser,
  type TreeNode,
  ApiError,
} from '../../lib/api'
import { useToast } from '../Toast'

interface Props {
  open: boolean
  onClose: () => void
}

/** Etiqueta legible del rol (espejo de AccountDialog). */
function roleLabel(role: string): string {
  if (role === 'admin') return 'Administrador'
  if (role === 'member') return 'Miembro'
  if (role === 'guest') return 'Invitado'
  return role
}

/** Aplana el árbol del vault a rutas de carpeta (con trailing slash), ordenadas. */
function collectFolders(node: TreeNode | undefined): string[] {
  if (!node) return []
  const out: string[] = []
  const walk = (n: TreeNode) => {
    if (n.type === 'dir' && n.path) out.push(n.path + '/')
    n.children?.forEach(walk)
  }
  node.children?.forEach(walk)
  return out.sort((a, b) => a.localeCompare(b))
}

const INPUT =
  'w-full rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 text-sm text-fg placeholder:text-fg-subtle outline-none transition focus:border-accent'
const BTN_PRIMARY =
  'rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg transition hover:bg-accent-hover disabled:opacity-50'
const BTN_GHOST =
  'rounded-md px-3 py-1.5 text-xs text-fg-subtle transition hover:bg-surface hover:text-fg'

/** Chip de un scope_grant con botón de revocar. */
function GrantChip({
  prefix,
  canWrite,
  onRevoke,
  busy,
}: {
  prefix: string
  canWrite: boolean
  onRevoke: () => void
  busy: boolean
}) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[11px] text-fg-muted">
      <span className="truncate font-mono">{prefix}</span>
      <span className="shrink-0 text-fg-faint">{canWrite ? 'rw' : 'ro'}</span>
      <button
        onClick={onRevoke}
        disabled={busy}
        title="Revocar acceso"
        className="shrink-0 text-fg-faint transition hover:text-red-400 disabled:opacity-50"
      >
        <X size={12} />
      </button>
    </span>
  )
}

/** Fila para agregar un scope_grant a un principal (carpeta + can_write). */
function AddGrantRow({
  folders,
  onAdd,
  busy,
}: {
  folders: string[]
  onAdd: (prefix: string, canWrite: boolean) => void
  busy: boolean
}) {
  const [prefix, setPrefix] = useState('')
  const [canWrite, setCanWrite] = useState(false)
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <select
        value={prefix}
        onChange={(e) => setPrefix(e.target.value)}
        className="min-w-0 max-w-[240px] flex-1 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[12px] text-fg outline-none transition focus:border-accent"
      >
        <option value="">Elegir carpeta…</option>
        {folders.map((f) => (
          <option key={f} value={f}>
            {f}
          </option>
        ))}
      </select>
      <label className="flex items-center gap-1.5 text-[11px] text-fg-subtle">
        <input
          type="checkbox"
          checked={canWrite}
          onChange={(e) => setCanWrite(e.target.checked)}
          className="accent-[var(--accent)]"
        />
        Escritura
      </label>
      <button
        onClick={() => {
          if (prefix) onAdd(prefix, canWrite)
        }}
        disabled={busy || !prefix}
        className="inline-flex items-center gap-1 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[11px] text-fg-muted transition hover:bg-surface-hover disabled:opacity-50"
      >
        <FolderPlus size={12} /> Agregar
      </button>
    </div>
  )
}

/** Tarjeta de un principal (humano o agente) con su gestión de scopes. */
function PrincipalCard({
  p,
  humans,
  folders,
  onGrantAdd,
  onGrantRevoke,
  onAssign,
  busy,
}: {
  p: AdminPrincipal
  humans: AdminPrincipal[]
  folders: string[]
  onGrantAdd: (id: number, prefix: string, canWrite: boolean) => void
  onGrantRevoke: (grantId: number) => void
  onAssign: (agentId: number, ownerId: number | null) => void
  busy: boolean
}) {
  return (
    <div className="glass rounded-lg border border-[var(--border-strong)] p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface text-[11px] font-medium text-fg-muted">
            {(p.name[0] ?? '?').toUpperCase()}
          </span>
          <span className="truncate text-[13px] font-medium text-fg">{p.name}</span>
          <span className="rounded border border-[var(--border-strong)] px-1.5 py-0.5 text-[10px] text-fg-subtle">
            {roleLabel(p.role)}
          </span>
          {!p.active && (
            <span className="rounded border border-[var(--border-strong)] px-1.5 py-0.5 text-[10px] text-red-400">
              inactivo
            </span>
          )}
        </div>
        {p.kind === 'agent' && (
          <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-fg-subtle">
            <span className="shrink-0">Dueño:</span>
            <select
              value={p.owner_principal_id ?? ''}
              onChange={(e) =>
                onAssign(p.id, e.target.value ? Number(e.target.value) : null)
              }
              disabled={busy}
              className="min-w-0 max-w-[160px] flex-1 rounded-md border border-[var(--border-strong)] bg-surface px-2 py-1 text-[11px] text-fg outline-none transition focus:border-accent disabled:opacity-50"
            >
              <option value="">— sin dueño —</option>
              {humans.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      <div className="mt-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {p.role === 'admin' ? (
            <span className="text-[11px] text-fg-subtle">Acceso completo (admin)</span>
          ) : p.scope_grants.length === 0 ? (
            <span className="text-[11px] text-fg-faint">Sin accesos asignados</span>
          ) : (
            p.scope_grants.map((g) => (
              <GrantChip
                key={g.id}
                prefix={g.path_prefix}
                canWrite={g.can_write}
                onRevoke={() => onGrantRevoke(g.id)}
                busy={busy}
              />
            ))
          )}
        </div>
        {p.role !== 'admin' && (
          <AddGrantRow
            folders={folders}
            onAdd={(prefix, canWrite) => onGrantAdd(p.id, prefix, canWrite)}
            busy={busy}
          />
        )}
      </div>
    </div>
  )
}

/** Formulario de creación de usuario + revelado de la contraseña temporal (una vez). */
function CreateUserPanel({
  onCreated,
  busy,
  created,
  onDismiss,
}: {
  onCreated: (username: string, role: string, email: string) => void
  busy: boolean
  created: CreatedUser | null
  onDismiss: () => void
}) {
  const toast = useToast()
  const [username, setUsername] = useState('')
  const [role, setRole] = useState('member')
  const [email, setEmail] = useState('')

  if (created) {
    return (
      <div className="glass rounded-lg border border-[var(--border-strong)] p-4">
        <p className="text-[13px] font-medium text-fg">
          Usuario «{created.name}» creado
        </p>
        <p className="mt-1 text-xs text-fg-subtle">
          Copiá esta contraseña temporal y entregásela al usuario. No se vuelve a
          mostrar. La cambiará en su primer inicio de sesión.
        </p>
        <div className="mt-3 flex items-center gap-2">
          <code className="flex-1 select-all rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 font-mono text-sm text-fg">
            {created.temp_password}
          </code>
          <button
            onClick={() => {
              navigator.clipboard
                ?.writeText(created.temp_password)
                .then(() => toast('Contraseña copiada'))
                .catch(() => toast('No se pudo copiar'))
            }}
            title="Copiar"
            className="inline-flex items-center gap-1 rounded-md border border-[var(--border-strong)] bg-surface px-2.5 py-2 text-xs text-fg-muted transition hover:bg-surface-hover"
          >
            <Copy size={14} />
          </button>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={onDismiss} className={BTN_PRIMARY}>
            Listo
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="glass rounded-lg border border-[var(--border-strong)] p-4">
      <p className="text-[13px] font-medium text-fg">Crear usuario</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="usuario"
          className={INPUT}
        />
        <select
          value={role}
          onChange={(e) => setRole(e.target.value)}
          className={INPUT}
        >
          <option value="member">Miembro</option>
          <option value="guest">Invitado</option>
          <option value="admin">Administrador</option>
        </select>
        <input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="email (opcional)"
          className={INPUT}
        />
      </div>
      <div className="mt-3 flex justify-end">
        <button
          onClick={() => onCreated(username.trim(), role, email.trim())}
          disabled={busy || !username.trim()}
          className={`${BTN_PRIMARY} inline-flex items-center gap-1.5`}
        >
          <UserPlus size={14} /> {busy ? 'Creando…' : 'Crear usuario'}
        </button>
      </div>
    </div>
  )
}

export default function AdminDialog({ open, onClose }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [showCreate, setShowCreate] = useState(false)
  const [created, setCreated] = useState<CreatedUser | null>(null)

  const { data: principalsData, isLoading } = useQuery({
    queryKey: ['admin-principals'],
    queryFn: fetchAdminPrincipals,
    enabled: open,
  })
  const { data: tree } = useQuery({
    queryKey: ['tree'],
    queryFn: fetchTree,
    enabled: open,
  })

  const principals = principalsData?.principals ?? []
  const humans = useMemo(() => principals.filter((p) => p.kind === 'human'), [principals])
  const agents = useMemo(() => principals.filter((p) => p.kind === 'agent'), [principals])
  const folders = useMemo(() => collectFolders(tree), [tree])

  // Agentes agrupados por su dueño (owner_name). Los sin dueño van a "Sin dueño".
  const agentsByOwner = useMemo(() => {
    const groups = new Map<string, AdminPrincipal[]>()
    for (const a of agents) {
      const key = a.owner_name ?? '— Sin dueño —'
      const arr = groups.get(key) ?? []
      arr.push(a)
      groups.set(key, arr)
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [agents])

  async function refresh() {
    await qc.invalidateQueries({ queryKey: ['admin-principals'] })
  }

  async function handleCreate(username: string, role: string, email: string) {
    if (!username || busy) return
    setBusy(true)
    try {
      const res = await adminCreateUser(username, role, email)
      setCreated(res)
      await refresh()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'No se pudo crear el usuario')
    } finally {
      setBusy(false)
    }
  }

  async function handleGrantAdd(id: number, prefix: string, canWrite: boolean) {
    setBusy(true)
    try {
      await adminCreateGrant(id, prefix, canWrite)
      toast('Acceso agregado')
      await refresh()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'No se pudo agregar el acceso')
    } finally {
      setBusy(false)
    }
  }

  async function handleGrantRevoke(grantId: number) {
    setBusy(true)
    try {
      await adminRevokeGrant(grantId)
      toast('Acceso revocado')
      await refresh()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'No se pudo revocar el acceso')
    } finally {
      setBusy(false)
    }
  }

  async function handleAssign(agentId: number, ownerId: number | null) {
    setBusy(true)
    try {
      await adminAssignAgent(agentId, ownerId)
      toast('Agente asociado')
      await refresh()
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'No se pudo asociar el agente')
    } finally {
      setBusy(false)
    }
  }

  function dismissCreate() {
    setCreated(null)
    setShowCreate(false)
  }

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
                  className="glass-strong relative flex max-h-[90vh] w-full max-w-3xl flex-col rounded-lg border border-[var(--border-strong)] p-3 shadow-2xl sm:p-5"
                >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Dialog.Title className="flex items-center gap-2 text-sm font-semibold text-fg">
                    <ShieldCheck size={16} className="text-accent" />
                    Administración
                  </Dialog.Title>
                  {!showCreate && (
                    <button
                      onClick={() => {
                        setCreated(null)
                        setShowCreate(true)
                      }}
                      className={`${BTN_PRIMARY} inline-flex items-center gap-1.5`}
                    >
                      <Plus size={14} /> Crear usuario
                    </button>
                  )}
                </div>
                <Dialog.Description className="mt-1 text-xs text-fg-subtle">
                  Usuarios, agentes y accesos a carpetas del vault. Los accesos se
                  configuran acá; su aplicación efectiva llega en una fase posterior.
                </Dialog.Description>

                <div className="mt-4 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
                  {showCreate && (
                    <CreateUserPanel
                      onCreated={handleCreate}
                      busy={busy}
                      created={created}
                      onDismiss={dismissCreate}
                    />
                  )}

                  {isLoading ? (
                    <div className="flex justify-center py-8">
                      <Loader2 size={20} className="animate-spin text-fg-faint" />
                    </div>
                  ) : (
                    <>
                      <section>
                        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-fg-subtle">
                          Usuarios ({humans.length})
                        </h3>
                        <div className="space-y-2">
                          {humans.map((p) => (
                            <PrincipalCard
                              key={p.id}
                              p={p}
                              humans={humans}
                              folders={folders}
                              onGrantAdd={handleGrantAdd}
                              onGrantRevoke={handleGrantRevoke}
                              onAssign={handleAssign}
                              busy={busy}
                            />
                          ))}
                          {humans.length === 0 && (
                            <p className="text-xs text-fg-faint">Sin usuarios.</p>
                          )}
                        </div>
                      </section>

                      <section>
                        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-fg-subtle">
                          Agentes ({agents.length})
                        </h3>
                        <div className="space-y-3">
                          {agentsByOwner.map(([owner, list]) => (
                            <div key={owner}>
                              <p className="mb-1.5 text-[11px] text-fg-faint">
                                {owner}
                              </p>
                              <div className="space-y-2">
                                {list.map((p) => (
                                  <PrincipalCard
                                    key={p.id}
                                    p={p}
                                    humans={humans}
                                    folders={folders}
                                    onGrantAdd={handleGrantAdd}
                                    onGrantRevoke={handleGrantRevoke}
                                    onAssign={handleAssign}
                                    busy={busy}
                                  />
                                ))}
                              </div>
                            </div>
                          ))}
                          {agents.length === 0 && (
                            <p className="text-xs text-fg-faint">Sin agentes.</p>
                          )}
                        </div>
                      </section>
                    </>
                  )}
                </div>

                <div className="mt-4 flex justify-end border-t border-[var(--border-strong)] pt-3">
                  <Dialog.Close asChild>
                    <button className={BTN_GHOST}>Cerrar</button>
                  </Dialog.Close>
                </div>
              </motion.div>
            </Dialog.Content>
            </div>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  )
}
