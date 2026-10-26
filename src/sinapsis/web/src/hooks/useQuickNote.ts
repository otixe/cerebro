import { useCallback, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, createFile } from '../lib/api'
import { useShellStore } from '../store'
import { useToast } from '../components/Toast'

/** Carpeta fija en la raíz del vault; cambiar aquí si cambia el destino. */
const QUICK_NOTES_DIR = 'Notas rapidas'
const MAX_ATTEMPTS = 4
const pad = (n: number) => String(n).padStart(2, '0')

/** file_create devuelve detail='ya existe' únicamente para la colisión de nombre. */
function isNameCollision(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409 && e.detail === 'ya existe'
}

export function useQuickNote() {
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const qc = useQueryClient()
  const toast = useToast()
  const openNote = useShellStore((s) => s.openNote)

  const createQuickNote = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    try {
      // Crear antes del guard: si falla, el borrador permanece sucio y protegido.
      // openNote ejecuta el guard después de una creación exitosa.
      const now = new Date()
      // Hora local del navegador; segundos desde el primer intento y sufijo aleatorio
      // reducen la probabilidad de competir por el mismo path (no es exclusión).
      const stem = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
      const random = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b.toString(16).padStart(2, '0')).join('')
      for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
        const path = `${QUICK_NOTES_DIR}/${stem}-${random}${attempt ? `-${attempt}` : ''}.md`
        let created: Awaited<ReturnType<typeof createFile>>
        try {
          created = await createFile(path)
        } catch (e) {
          if (isNameCollision(e)) {
            if (attempt + 1 < MAX_ATTEMPTS) continue
            throw new Error(`No se pudo crear la nota rápida: nombre ocupado tras ${MAX_ATTEMPTS} intentos`)
          }
          throw e
        }
        // Jamás reintentar una creación después de recibir éxito del servidor.
        try {
          await qc.invalidateQueries({ queryKey: ['tree'] })
        } catch {
          // El editor todavía puede abrir el archivo aunque el árbol no se refresque.
        }
        openNote(created.path)
        const opened = useShellStore.getState().activeTabId === `note:${created.path}`
        if (opened) toast('Nota rápida creada y abierta')
        else toast(`Nota rápida creada en ${created.path}, pero no se abrió. Revisá tus cambios sin guardar.`, 'err')
        return
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : 'No se pudo crear la nota rápida', 'err')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [qc, openNote, toast])

  return { createQuickNote, quickNoteBusy: busy }
}
