// The BFF returns attachments as octet-stream/adjunto.bin. Never restore an
// agent-declared active extension when saving that blob to the user's disk.
const INERT_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf', 'txt', 'csv', 'json', 'md', 'zip'])
const MAX_LENGTH = 180

export function safeDownloadName(declared: string): string {
  const basename = declared.split(/[\\/]/).at(-1) ?? ''
  const clean = Array.from(basename).filter(char => {
    const code = char.charCodeAt(0)
    return code > 31 && (code < 127 || code > 159) && !/\p{Cf}/u.test(char)
  }).join('').trim().replace(/[.\s]+$/g, '') || 'adjunto'
  const extension = /\.([^.]+)$/.exec(clean)?.[1].toLowerCase()
  const inert = extension !== undefined && INERT_EXTENSIONS.has(extension)
  const suffix = inert ? clean.slice(clean.lastIndexOf('.')) : '.bin'
  const stem = inert ? clean.slice(0, -suffix.length) : clean
  return `${stem.slice(0, MAX_LENGTH - suffix.length).replace(/[.\s]+$/g, '') || 'adjunto'}${suffix}`
}
