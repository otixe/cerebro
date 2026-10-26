export interface Frontmatter {
  data: Record<string, string>
  body: string
}

/**
 * Parser mínimo de frontmatter YAML (bloque `---` inicial).
 * No es un parser YAML completo: extrae pares `clave: valor` de primer nivel
 * para mostrarlos como cabecera sutil. Devuelve el cuerpo sin el bloque.
 */
export function parseFrontmatter(raw: string): Frontmatter {
  const m = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(raw)
  if (!m) return { data: {}, body: raw }

  const data: Record<string, string> = {}
  for (const line of m[1].split(/\r?\n/)) {
    const idx = line.indexOf(':')
    if (idx === -1) continue
    const key = line.slice(0, idx).trim()
    if (!key || key.startsWith('#')) continue
    let val = line.slice(idx + 1).trim()
    // limpiar comillas envolventes
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1)
    }
    data[key] = val
  }
  return { data, body: raw.slice(m[0].length) }
}
