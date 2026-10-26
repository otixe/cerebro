import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { approvalDecision, approvalReview, type ApprovalReview } from '../lib/api'
import { approvalError, displayScope, isOutcomeUnknown, reconcile, verifiedScope, type Decision } from '../lib/approval'

const secondary = 'inline-flex min-h-[44px] items-center rounded-md border border-[var(--border-strong)] bg-surface px-3 py-1.5 text-xs font-medium text-fg hover:bg-surface-hover disabled:opacity-50 lg:min-h-0'
const primary = 'inline-flex min-h-[44px] items-center rounded-md bg-accent-bg px-3 py-1.5 text-xs font-medium text-fg hover:bg-accent-hover disabled:opacity-50 lg:min-h-0'
const statusLabels: Record<string, string> = {
  pending_approval: 'Pendiente de aprobación', aprobada: 'Aprobada', revocada: 'Revocada',
  changes_requested: 'Cambios solicitados', done: 'Finalizada', cancelled: 'Cancelada',
}
const labels: Record<Decision, string> = { approve: 'Aprobar', 'request-changes': 'Pedir cambios', revoke: 'Revocar' }

export default function ApprovalDetail({ id }: { id: string }) {
  const client = useQueryClient()
  const [review, setReview] = useState<ApprovalReview | null>(null)
  const [verified, setVerified] = useState(false)
  const [checking, setChecking] = useState(true)
  const [reason, setReason] = useState('')
  const [action, setAction] = useState<Decision | null>(null)
  const [busy, setBusy] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const confirmed = useRef<{ version: number; hash: string } | null>(null)

  function clearConfirmation() { setAction(null); setReason(''); confirmed.current = null }

  useEffect(() => {
    let active = true
    setReview(null); setVerified(false); setChecking(true); setUncertain(false)
    clearConfirmation(); setError(''); setMessage('')
    approvalReview(id).then(async detail => {
      const valid = await verifiedScope(detail)
      if (active) { setReview(detail); setVerified(valid); setChecking(false) }
    }).catch(e => { if (active) { setError(approvalError(e)); setChecking(false) } })
    return () => { active = false }
  }, [id])

  async function reload() {
    clearConfirmation(); setMessage(''); setError('')
    try {
      setChecking(true)
      const detail = await approvalReview(id)
      const valid = await verifiedScope(detail)
      setReview(detail); setVerified(valid); setUncertain(false); setError('')
    } catch (e) { setError(approvalError(e)) }
    finally { setChecking(false) }
  }

  async function refreshInbox() {
    await Promise.all([client.invalidateQueries({ queryKey: ['approval', 'pending'] }), client.invalidateQueries({ queryKey: ['approval', 'approved'] }), client.invalidateQueries({ queryKey: ['tasks'] })])
  }

  async function decide() {
    if (!review || !action || busy || checking || uncertain || !verified) return
    if (!confirmed.current || confirmed.current.version !== review.version || confirmed.current.hash !== review.scope_hash) { clearConfirmation(); setError('La solicitud cambió; volvé a confirmar.'); return }
    if (action !== 'approve' && !reason.trim()) { setError('Motivo obligatorio'); return }
    const pre = review
    setBusy(true); setError(''); setMessage('')
    try {
      const payload = {
        expected_version: pre.version, expected_hash: pre.scope_hash,
        ...(action !== 'approve' ? { reason: reason.trim() } : {}),
      }
      // Ruta de aprobación explícita; las otras decisiones mantienen su motivo obligatorio.
      if (action === 'approve') await approvalDecision(pre.task_id, 'approve', payload)
      else await approvalDecision(pre.task_id, action, payload)
    } catch (e) {
      if (isOutcomeUnknown(e)) {
        clearConfirmation()
        setUncertain(true)
        setError('resultado incierto: re-leyendo…')
        try {
          const post = await approvalReview(pre.task_id)
          const valid = await verifiedScope(post)
          clearConfirmation(); setReview(post); setVerified(valid)
          const result = reconcile(pre, post, action)
          setError(result === 'a-prime' ? "(a') transición aplicada; atribución a esta invocación NO probada" :
            result === 'b' ? '(b) no aplicado' : '(c) cambio concurrente: revisión manual')
          void refreshInbox().catch(() => {}) // la bandeja no cambia el resultado de la re-lectura
        } catch { setError('(c) cambio concurrente: revisión manual (falló re-lectura)') }
      } else setError(approvalError(e))
      return
    } finally { setBusy(false) }
    clearConfirmation()
    setMessage('Decisión recibida; recargando solicitud.')
    await reload()
    try { await refreshInbox() } catch (e) { setError(approvalError(e)) }
  }

  const blocked = !verified || !signedIdentity(review) || checking || busy || uncertain
  const signed = review && verified ? signedIdentity(review) : null
  return <div className="min-h-full space-y-4 p-3 text-sm text-fg lg:p-4">
    {checking && <p role="status" className="text-fg-subtle">Verificando huella…</p>}
    {error && <p role="alert" className="rounded-md bg-[rgba(255,188,51,0.16)] p-2 text-[var(--warn)]">{error}</p>}
    {message && !error && <p role="status" className="rounded-md bg-accent-bg p-2">{message}</p>}
    {!review && !checking && <button type="button" className={secondary} onClick={() => void reload()}>Reintentar</button>}
    {review && <>
      <header className="space-y-2 border-b border-[var(--border-subtle)] pb-4">
        <h2 className="break-words text-base font-semibold">{review.title}</h2>
        <span className="inline-flex rounded-full bg-surface px-2 py-0.5 text-[11px] text-fg-subtle">{statusLabels[review.status] ?? review.status}</span>
      </header>
      {review.description && <p className="whitespace-pre-wrap text-xs text-fg-subtle">{review.description}</p>}
      <section className="space-y-2 border-t border-[var(--border-subtle)] pt-4">
        <h3 className="text-xs font-semibold text-fg">Solicitud</h3>
        {review && !signed && <p role="alert" className="text-xs text-[var(--warn)]">No se pudo verificar la identidad del alcance firmado; decisiones deshabilitadas.</p>}
        {signed && (signed.action_type !== review.action_type || signed.resource_kind !== review.resource_kind || signed.resource_id !== review.resource_id) && <p role="alert" className="text-xs text-[var(--warn)]">Las columnas de la tarea difieren del alcance firmado. Se muestra el alcance firmado.</p>}
        {([['Creador', `${review.creator} (${review.creator_kind})`], ['Acción', signed?.action_type ?? 'No verificable'], ['Recurso', signed ? `${signed.resource_kind} / ${signed.resource_id}` : 'No verificable']] as const).map(([key, value]) =>
          <div key={key} className="grid grid-cols-[90px_minmax(0,1fr)] gap-2 text-xs"><span className="text-fg-subtle">{key}</span><span className="break-all">{value}</span></div>)}
      </section>
      <section className="border-t border-[var(--border-subtle)] pt-4">
        <details>
          <summary className="flex min-h-[44px] cursor-pointer items-center text-xs font-medium lg:min-h-0">Ver alcance técnico</summary>
          <div className="space-y-2 pt-2 text-xs">
            <pre className="overflow-auto whitespace-pre-wrap break-all rounded-md border border-[var(--border-subtle)] bg-surface p-3 font-mono">{displayScope(review.scope_canonical_text)}</pre>
            <p className="break-all font-mono text-fg-subtle">SHA-256: {review.scope_hash}</p>
            <p>Versión de alcance: {review.scope_version}</p>
          </div>
        </details>
        {!checking && <p role="status" className={`mt-2 text-xs ${verified ? 'text-accent' : 'text-[var(--warn)]'}`}>
          {verified ? 'Huella verificada' : 'ALERTA: la huella del alcance no coincide; decisiones deshabilitadas'}
        </p>}
      </section>
      {(review.status === 'pending_approval' || review.status === 'aprobada') && <section className="space-y-3 border-t border-[var(--border-subtle)] pt-4">
        <h3 className="text-xs font-semibold">Decisión</h3>
        {!action ? <div className="flex flex-wrap gap-2">
          {review.status === 'pending_approval' && <button type="button" className={primary} disabled={blocked} onClick={() => { confirmed.current = { version: review.version, hash: review.scope_hash }; setAction('approve') }}>Aprobar</button>}
          <button type="button" className={secondary} disabled={blocked} onClick={() => { confirmed.current = { version: review.version, hash: review.scope_hash }; setAction(review.status === 'aprobada' ? 'revoke' : 'request-changes') }}>
            {review.status === 'aprobada' ? 'Revocar' : 'Pedir cambios'}
          </button>
        </div> : <div className="space-y-3 rounded-md border border-[var(--border-strong)] bg-surface p-3">
          <p className="text-xs font-medium">¿Confirmás {labels[action].toLowerCase()} esta solicitud?</p>
          {action !== 'approve' && <label className="block text-xs">Motivo obligatorio
            <textarea className="mt-1 min-h-[88px] w-full rounded-md border border-[var(--border-strong)] bg-surface px-3 py-2 text-[16px] text-fg lg:text-sm" value={reason} onChange={e => setReason(e.target.value)} />
          </label>}
          <div className="flex flex-wrap gap-2">
            <button type="button" className={primary} disabled={blocked || (action !== 'approve' && !reason.trim())} onClick={() => void decide()}>
              {busy ? 'Enviando…' : action === 'approve' ? 'Confirmar aprobación' : `Confirmar ${labels[action].toLowerCase()}`}
            </button>
            <button type="button" className={secondary} disabled={busy} onClick={() => { clearConfirmation(); setError('') }}>Cancelar</button>
          </div>
        </div>}
      </section>}
      <button type="button" className={secondary} disabled={busy || checking} onClick={() => void reload()}>Recargar</button>
    </>}
  </div>
}

/** Values read from scope_canonical_text, the exact UTF-8 bytes checked by SHA-256. */
function signedIdentity(review: ApprovalReview | null): { action_type: string; resource_kind: string; resource_id: string } | null {
  if (!review) return null
  try {
    const value: unknown = JSON.parse(review.scope_canonical_text)
    if (!value || typeof value !== 'object') return null
    const scope = value as Record<string, unknown>
    if (typeof scope.action_type !== 'string' || typeof scope.resource_kind !== 'string' || typeof scope.resource_id !== 'string') return null
    return { action_type: scope.action_type, resource_kind: scope.resource_kind, resource_id: scope.resource_id }
  } catch { return null }
}
