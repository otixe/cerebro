export type CorrespondenciaContentInput = {
  contenido?: string | null
  contenido_preview?: string | null
  contenido_len?: number | null
  contenido_truncado?: boolean | null
}

export type ContentPresentation =
  | { kind: 'full' | 'empty'; text: string; length: number }
  | { kind: 'preview' | 'unloaded'; text: string; length: number }

/**
 * Decide cómo representar un mensaje que puede venir completo o como metadata.
 * `contenido === ''` es un cuerpo cargado y vacío; no es ausencia de contenido.
 */
export function contentPresentation(message: CorrespondenciaContentInput): ContentPresentation {
  if (typeof message.contenido === 'string') {
    return message.contenido === ''
      ? { kind: 'empty', text: 'Mensaje vacío.', length: 0 }
      : { kind: 'full', text: message.contenido, length: message.contenido.length }
  }
  const preview = message.contenido_preview ?? ''
  // Metadata explícita de longitud cero prueba que el cuerpo es vacío aunque
  // `contenido` no viaje. No inferirlo cuando falta `contenido_len`: payloads
  // legacy sin esa metadata siguen requiriendo carga explícita.
  if (message.contenido_len === 0) {
    return { kind: 'empty', text: 'Mensaje vacío.', length: 0 }
  }
  const length = message.contenido_len ?? 0
  return {
    kind: preview ? 'preview' : 'unloaded',
    text: preview + (message.contenido_truncado ? '…' : ''),
    length,
  }
}
