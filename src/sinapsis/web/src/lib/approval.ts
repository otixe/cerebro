import { ApiError, type ApprovalReview } from './api'

export type Decision = 'approve' | 'revoke' | 'request-changes'
export type Reconciliation = 'a-prime' | 'b' | 'c'

export async function verifiedScope(review: ApprovalReview): Promise<boolean> {
  if (!/^[0-9a-f]{64}$/.test(review.scope_hash) || typeof review.scope_canonical_text !== 'string') return false
  try {
    const bytes = new TextEncoder().encode(review.scope_canonical_text)
    const digest = await crypto.subtle.digest('SHA-256', bytes)
    const hash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
    return hash === review.scope_hash
  } catch { return false }
}

export function displayScope(text: string): string {
  try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return text }
}

export function reconcile(pre: ApprovalReview, post: ApprovalReview, action: Decision): Reconciliation {
  const valid = (x: ApprovalReview) =>
    Number.isSafeInteger(x.version) && Number.isSafeInteger(x.event_count) && Number.isSafeInteger(x.effect_count) &&
    typeof x.scope_hash === 'string' && typeof x.status === 'string' && x.task_id === pre.task_id
  if (!valid(pre) || !valid(post)) return 'c'
  const same = post.version === pre.version && post.status === pre.status &&
    post.scope_hash === pre.scope_hash && post.event_count === pre.event_count && post.effect_count === pre.effect_count
  if (same) return 'b'
  const target = action === 'approve' ? 'aprobada' : action === 'revoke' ? 'revocada' : 'changes_requested'
  if (post.version === pre.version + 1 && post.status === target && post.scope_hash === pre.scope_hash &&
    post.event_count === pre.event_count + 1 && post.effect_count === pre.effect_count) return 'a-prime'
  return 'c'
}

export function isOutcomeUnknown(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 503) return false
  const d = error.detail
  if (typeof d === 'string') return d.startsWith('outcome_unknown;')
  if (d && typeof d === 'object') {
    const fields = d as Record<string, unknown>
    return [fields.error, fields.code, fields.detail, fields.reason].some(x =>
      typeof x === 'string' && /OutcomeUnknown|resultado incierto/i.test(x))
  }
  return /OutcomeUnknown|resultado incierto/i.test(error.message)
}

export function approvalError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return 'alguien cambió la solicitud, recargá'
    if (error.status === 403) return 'no autorizado'
    if (error.status === 503) return isOutcomeUnknown(error) ? 'resultado incierto: re-leyendo…' :
      /approval_disabled/.test(JSON.stringify(error.detail)) ? 'aprobaciones apagadas' : `Servicio no disponible: ${error.message}`
    if (error.status === 422) return `Error de validación: ${typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail ?? error.message)}`
  }
  return error instanceof Error ? error.message : 'Error al consultar aprobaciones'
}
