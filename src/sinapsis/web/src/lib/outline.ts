export interface Heading {
  level: number
  text: string
}

/**
 * Extrae los headings ATX (`#`..`######`) del cuerpo markdown en orden de
 * documento, ignorando los bloques de código cercados. El orden coincide 1:1
 * con los elementos `h1..h6` que renderiza react-markdown, de modo que el
 * Outline puede hacer scroll al heading por índice.
 */
export function extractHeadings(markdown: string): Heading[] {
  const noFences = markdown
    .replace(/```[\s\S]*?```/g, '')
    .replace(/~~~[\s\S]*?~~~/g, '')
  const out: Heading[] = []
  for (const line of noFences.split(/\r?\n/)) {
    const m = /^(#{1,6})\s+(.*\S)\s*#*\s*$/.exec(line)
    if (m) out.push({ level: m[1].length, text: m[2].replace(/\s+#+\s*$/, '').trim() })
  }
  return out
}
