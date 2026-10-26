import type { ChatImage, ChatImageCapability } from './api'

export const IMAGE_MIMES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
export const MAX_IMAGE_B64 = 8 * 1024 * 1024
export const MAX_IMAGES = 8

export type SendDraft = { agent: string; draft: string; files: File[] }

export function shouldClearSentDraft(sent: SendDraft, current: SendDraft): boolean {
  return sent.agent === current.agent && sent.draft === current.draft && sent.files === current.files
}

export function shouldInterceptImagePaste(cap: ChatImageCapability | undefined, files: File[]): boolean {
  return cap?.supported === true && files.length > 0
}

export function validateImageFiles(files: File[], cap: ChatImageCapability | undefined): string | null {
  if (!cap?.supported) return cap?.reason || 'No se comprobó soporte de imágenes del agente'
  if (files.length > MAX_IMAGES) return 'Máximo 8 imágenes por mensaje'
  let total = 0
  for (const file of files) {
    if (!IMAGE_MIMES.includes(file.type) || !cap.mimes.includes(file.type)) return 'Sólo PNG, JPEG, GIF o WebP admitidos por el agente'
    // Base64 encoded length (without data URL); preflight before reading image bytes.
    total += Math.ceil(file.size / 3) * 4
    if (total > Math.min(cap.max_b64, MAX_IMAGE_B64)) return 'Las imágenes superan el límite de 8 MB base64 del puente'
  }
  return null
}

export async function encodeImageFiles(files: File[]): Promise<ChatImage[]> {
  return Promise.all(files.map(async file => ({
    mimeType: file.type,
    data: btoa(Array.from(new Uint8Array(await file.arrayBuffer()), b => String.fromCharCode(b)).join('')),
  })))
}
