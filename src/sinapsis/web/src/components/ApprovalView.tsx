import React, { useEffect, useRef, useState } from 'react'
// React import is used by the minimal Node/tsx JSX test harness (classic transform).
void React
import { useQuery } from '@tanstack/react-query'
import { approvalInbox, approvalReview, approvalDecision, fetchMe, type ApprovalItem, type ApprovalReview, type ApprovalInboxStatus } from '../lib/api'
import { approvalError, displayScope, isOutcomeUnknown, reconcile, verifiedScope, type Decision } from '../lib/approval'

export default function ApprovalView() {
  const { data: me } = useQuery({ queryKey: ['me'], queryFn: fetchMe })
  if (!me?.is_admin) return null
  return <AuthorizedApprovals />
}

function AuthorizedApprovals() {
  const [items, setItems] = useState<ApprovalItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [tab, setTab] = useState<ApprovalInboxStatus>('pending_approval')
  const inboxRequest = useRef(0)
  const [id, setId] = useState<string | null>(null)
  const [review, setReview] = useState<ApprovalReview | null>(null)
  const [verified, setVerified] = useState(false)
  const [checking, setChecking] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [loading, setLoading] = useState(false)

  async function loadInbox(next?: string, quiet = false, status: ApprovalInboxStatus = tab) {
    const request = ++inboxRequest.current
    try {
      setLoading(true)
      const page = await approvalInbox(status, next)
      if (request !== inboxRequest.current) return
      setItems(prev => next ? [...prev, ...page.items] : page.items)
      setCursor(page.next_cursor)
    } catch (e) { if (request === inboxRequest.current && !quiet) setError(approvalError(e)) }
    finally { if (request === inboxRequest.current) setLoading(false) }
  }
  function selectTab(status: ApprovalInboxStatus) {
    if (status === tab) return
    setTab(status)
    setId(null)
    setItems([])
    setCursor(null)
    setError('')
    void loadInbox(undefined, false, status)
  }
  useEffect(() => { void loadInbox() }, [])
  useEffect(() => {
    let active = true
    setReview(null)
    setVerified(false)
    setChecking(!!id)
    setError('')
    setMessage('')
    setUncertain(false)
    if (id) approvalReview(id).then(async detail => {
      const valid = await verifiedScope(detail)
      if (active) { setReview(detail); setVerified(valid); setChecking(false) }
    }).catch(e => { if (active) { setError(approvalError(e)); setChecking(false) } })
    return () => { active = false }
  }, [id])

  async function reload() {
    if (!id) return
    try {
      setChecking(true)
      const detail = await approvalReview(id)
      const valid = await verifiedScope(detail)
      setReview(detail)
      setVerified(valid)
      setUncertain(false)
      setError('')
    } catch (e) { setError(approvalError(e)) }
    finally { setChecking(false) }
  }

  async function decide(action: Decision) {
    if (!review || busy || uncertain || checking || !verified) return
    if (action !== 'approve' && !reason.trim()) { setError('Motivo obligatorio'); return }
    const label = action === 'approve' ? 'Aprobar' : action === 'revoke' ? 'Revocar' : 'Pedir cambios'
    if (!window.confirm(`${label} la solicitud «${review.title}» de ${review.creator}?`)) return
    const pre = review
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await approvalDecision(pre.task_id, action, {
        expected_version: pre.version, expected_hash: pre.scope_hash,
        ...(action !== 'approve' ? { reason: reason.trim() } : {}),
      })
    } catch (e) {
      if (isOutcomeUnknown(e)) {
        setUncertain(true)
        setError('resultado incierto: re-leyendo…')
        try {
          const post = await approvalReview(pre.task_id)
          const valid = await verifiedScope(post)
          setReview(post)
          setVerified(valid)
          const result = reconcile(pre, post, action)
          setError(result === 'a-prime' ? "(a') transición aplicada; atribución a esta invocación NO probada" :
            result === 'b' ? '(b) no aplicado' : '(c) cambio concurrente: revisión manual')
          await loadInbox(undefined, true)
        } catch { setError('(c) cambio concurrente: revisión manual (falló re-lectura)') }
      } else { setError(approvalError(e)) }
      return
    } finally { setBusy(false) }
    setReason('')
    setMessage('Decisión recibida; recargando solicitud.')
    await reload()
    await loadInbox()
  }

  return <div className="flex h-full flex-col gap-3 overflow-auto p-4 text-sm">
    <h2 className="text-lg font-semibold">Aprobaciones</h2>
    {error && <p role="alert" className="text-red-500">{error}</p>}
    {message && !error && <p role="status">{message}</p>}
    <button type="button" onClick={() => { void reload(); void loadInbox() }} disabled={busy || checking}>Recargar</button>
    <div className="flex flex-col gap-4 lg:flex-row">
      <section aria-label="Solicitudes de aprobación" className="min-w-0 lg:w-1/3">
        <div role="tablist" aria-label="Filtrar solicitudes">
          {([['pending_approval', 'Pendientes'], ['aprobada', 'Aprobadas'], ['all', 'Todas']] as const).map(([status, label]) =>
            <button key={status} type="button" role="tab" aria-selected={tab === status} onClick={() => selectTab(status)}>{label}</button>)}
        </div>
        {items.length === 0 && !loading && <p>Sin solicitudes</p>}
        {items.map(item => <button type="button" key={item.id} className="block w-full border-b p-2 text-left" onClick={() => { setReview(null); setVerified(false); setChecking(true); setId(item.id) }}>
          <span className="block">{item.title}</span><span className="block text-xs">Creador: {item.creator}</span>
        </button>)}
        {cursor && <button type="button" disabled={loading} onClick={() => void loadInbox(cursor)}>Más solicitudes</button>}
      </section>
      <section aria-label="Detalle de aprobación" className="min-w-0 flex-1">
        {checking && <p>Verificando huella…</p>}
        {review && <>
          <h3 className="text-base font-semibold">{review.title}</h3>
          <p>Estado: {review.status} · Creador: {review.creator} ({review.creator_kind})</p>
          <p>Descripción: {review.description ?? 'Sin descripción'}</p>
          <p>Acción: {review.action_type} · Recurso: {review.resource_kind} / {review.resource_id} · Versión de alcance: {review.scope_version}</p>
          <h4>Alcance solicitado</h4>
          <pre className="overflow-auto whitespace-pre-wrap break-all border p-3">{displayScope(review.scope_canonical_text)}</pre>
          <p>SHA-256: {review.scope_hash}</p>
          {!checking && <p role="status" className={verified ? '' : 'text-red-500'}>
            {verified ? 'huella verificada' : 'ALERTA: la huella del alcance no coincide; Aprobar deshabilitado'}
          </p>}
          {(review.status === 'pending_approval' || review.status === 'aprobada') && <div className="space-y-2">
            {review.status === 'pending_approval' && <button type="button" disabled={!verified || checking || busy || uncertain} onClick={() => void decide('approve')}>Aprobar</button>}
            <label className="block">Motivo para cambios o revocación
              <textarea value={reason} onChange={e => setReason(e.target.value)} className="block w-full border p-2" />
            </label>
            {review.status === 'pending_approval' && <button type="button" disabled={!verified || checking || busy || uncertain || !reason.trim()} onClick={() => void decide('request-changes')}>Pedir cambios</button>}
            {review.status === 'aprobada' && <button type="button" disabled={!verified || checking || busy || uncertain || !reason.trim()} onClick={() => void decide('revoke')}>Revocar</button>}
          </div>}
        </>}
      </section>
    </div>
  </div>
}
