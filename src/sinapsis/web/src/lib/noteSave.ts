import { ApiError, saveFile, type NodeContent } from './api'

// Shared across all three UI doors and remounts. Reloading the page clears conflicts.
const notes = new Map<string, { busy: boolean; generation: number; conflict: boolean; waiters: Set<() => void> }>()
function state(path: string) {
  let value = notes.get(path)
  if (!value) {
    value = { busy: false, generation: 0, conflict: false, waiters: new Set() }
    notes.set(path, value)
  }
  return value
}
type Toast = (message: string, kind?: 'err') => void

export function canStartNoteSave(path: string, toast: Toast): boolean {
  const s = state(path)
  if (s.conflict) {
    toast('La nota cambió. Recarga la página antes de guardar; conserva tu borrador.', 'err')
    return false
  }
  if (s.busy) {
    // Discard, do not queue: a queued local edit could outlive a conflict.
    // Explicit feedback lets the user repeat it after the active operation ends.
    toast('Hay un guardado en curso. Este cambio no se guardó: repítelo cuando termine.', 'err')
    return false
  }
  return true
}

export function noteSaveConflicted(path: string): boolean {
  return state(path).conflict
}

// Only autosave deltas opt into waiting. A 409 wakes waiters but NEVER admits them.
// Cancellation removes a waiting panel without flushing edits on navigation/unmount.
async function acquireQueued(path: string, signal: AbortSignal, toast: Toast): Promise<boolean> {
  const s = state(path)
  while (s.busy && !s.conflict && !signal.aborted) {
    await new Promise<void>(resolve => {
      const wake = () => {
        s.waiters.delete(wake)
        signal.removeEventListener('abort', wake)
        resolve()
      }
      s.waiters.add(wake)
      signal.addEventListener('abort', wake, { once: true })
    })
  }
  if (signal.aborted || !canStartNoteSave(path, toast)) return false
  s.busy = true // Admission and busy check are synchronous, including among waiters.
  return true
}

export async function saveNoteOperation(
  path: string,
  load: () => Promise<NodeContent>,
  transform: (fresh: NodeContent) => string,
  onSuccess: (saved: NodeContent) => void,
  toast: Toast,
  queued?: { signal: AbortSignal },
): Promise<boolean> {
  const s = state(path)
  if (queued) {
    if (!await acquireQueued(path, queued.signal, toast)) return false
  } else {
    if (!canStartNoteSave(path, toast)) return false
    s.busy = true
  }
  const generation = s.generation
  // después de un 409 sobre una nota, NINGUNA operación ya iniciada sobre esa nota puede escribir.
  // Check after EVERY await (including rejection) and immediately before POST.
  const valid = () => !s.conflict && s.generation === generation
  try {
    if (queued?.signal.aborted) return false
    const fresh = await load()
    if (!valid() || queued?.signal.aborted) return false
    const content = transform(fresh)
    if (!valid()) return false
    const saved = await saveFile(path, content, fresh.version)
    if (!valid()) return false
    s.generation++
    // Synchronous callback only: no continuation may announce success after a conflict.
    onSuccess({ path, content, version: saved.version, bytes: saved.bytes })
    return true
  } catch (e) {
    if (!valid()) return false
    if (e instanceof ApiError && e.status === 409) {
      s.generation++
      s.conflict = true
      toast(`${e.message} Recarga la página antes de guardar; conserva tu borrador.`, 'err')
    } else {
      toast(e instanceof Error ? e.message : 'No se pudo guardar la nota', 'err')
    }
    return false
  } finally {
    s.busy = false
    for (const wake of [...s.waiters]) wake()
  }
}
