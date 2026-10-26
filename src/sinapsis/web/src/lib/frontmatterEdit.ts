/**
 * Parser/serializador de frontmatter YAML para EDICIÓN (Ola 2 — fusión Notion).
 *
 * Objetivo crítico: poder reescribir SOLO el bloque frontmatter (`---` inicial)
 * preservando el cuerpo de la nota intacto byte-a-byte, y round-tripear las
 * propiedades no tocadas EXACTAMENTE como estaban (para no corromper YAML que
 * este parser no entiende del todo: mapas anidados, comentarios, estilos raros).
 *
 * Estrategia:
 *  - Se separa el bloque frontmatter del cuerpo con la misma regex que el visor.
 *  - El bloque se parsea a una lista ORDENADA de entradas: campos editables
 *    (clave→valor tipado) o pasarelas "raw" (comentarios, líneas en blanco,
 *    bloques anidados que no exponemos como campos).
 *  - Al serializar, cada campo NO modificado se emite con su texto original
 *    (`raw`); los modificados se normalizan. Las pasarelas raw se emiten
 *    verbatim. Así lo que el usuario no tocó nunca cambia.
 */

export type FieldType = 'text' | 'number' | 'boolean' | 'date' | 'list' | 'select'

export interface FMField {
  kind: 'field'
  key: string
  type: FieldType
  /** Valor escalar (text/date/select) o número/booleano como texto. */
  text: string
  /** Valor de lista/tags. */
  list: string[]
  /** Valor booleano. */
  bool: boolean
  /** Texto original exacto del campo (una o varias líneas), sin salto final. */
  raw: string
  /** true si el usuario lo modificó (se debe re-serializar normalizado). */
  dirty: boolean
}

export interface FMRaw {
  kind: 'raw'
  /** Texto original exacto (comentarios / líneas en blanco / bloques anidados). */
  raw: string
}

export type FMEntry = FMField | FMRaw

export interface ParsedFM {
  hasFM: boolean
  entries: FMEntry[]
  /** Cuerpo posterior al bloque frontmatter. Se preserva intacto. */
  body: string
}

const FM_RE = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/

function unquote(v: string): string {
  const s = v.trim()
  if (
    (s.startsWith('"') && s.endsWith('"') && s.length >= 2) ||
    (s.startsWith("'") && s.endsWith("'") && s.length >= 2)
  ) {
    return s.slice(1, -1)
  }
  return s
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$/
const NUM_RE = /^-?\d+(\.\d+)?$/

function inferScalarType(v: string): FieldType {
  const s = v.trim()
  if (s === 'true' || s === 'false') return 'boolean'
  if (NUM_RE.test(s)) return 'number'
  if (DATE_RE.test(s)) return 'date'
  return 'text'
}

/** Divide una lista inline `[a, b, "c, d"]` respetando comillas. */
function splitInlineList(inner: string): string[] {
  const out: string[] = []
  let cur = ''
  let quote: string | null = null
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i]
    if (quote) {
      if (ch === quote) quote = null
      else cur += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === ',') {
      out.push(cur.trim())
      cur = ''
    } else {
      cur += ch
    }
  }
  if (cur.trim() !== '' || out.length > 0) out.push(cur.trim())
  return out.map((x) => unquote(x)).filter((x) => x !== '')
}

// clave de primer nivel: empieza sin espacio, `clave:` (con o sin valor).
const KEY_RE = /^([A-Za-z0-9_][\w .-]*?):(?:[ \t]+(.*))?$/
const BLOCK_ITEM_RE = /^[ \t]+-[ \t]*(.*)$/
const INDENTED_RE = /^[ \t]+\S/

export function parseFrontmatterEdit(raw: string): ParsedFM {
  const m = FM_RE.exec(raw)
  if (!m) return { hasFM: false, entries: [], body: raw }

  const inner = m[1]
  const body = raw.slice(m[0].length)
  const lines = inner.split(/\r?\n/)
  const entries: FMEntry[] = []

  let rawBuf: string[] = []
  const flushRaw = () => {
    if (rawBuf.length) {
      entries.push({ kind: 'raw', raw: rawBuf.join('\n') })
      rawBuf = []
    }
  }

  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const km = KEY_RE.exec(line)
    if (!km) {
      rawBuf.push(line)
      i++
      continue
    }

    const key = km[1].trim()
    const rest = (km[2] ?? '').trim()

    // ¿bloque de lista debajo? (valor vacío + siguientes líneas `- item`)
    if (rest === '' && i + 1 < lines.length && BLOCK_ITEM_RE.test(lines[i + 1])) {
      const rawLines = [line]
      const items: string[] = []
      let j = i + 1
      while (j < lines.length && BLOCK_ITEM_RE.test(lines[j])) {
        rawLines.push(lines[j])
        items.push(unquote(BLOCK_ITEM_RE.exec(lines[j])![1].trim()))
        j++
      }
      flushRaw()
      entries.push({
        kind: 'field',
        key,
        type: 'list',
        text: '',
        list: items.filter((x) => x !== ''),
        bool: false,
        raw: rawLines.join('\n'),
        dirty: false,
      })
      i = j
      continue
    }

    // valor vacío + siguientes líneas indentadas que NO son items → mapa anidado.
    // No lo exponemos como campo editable; lo preservamos como raw.
    if (rest === '' && i + 1 < lines.length && INDENTED_RE.test(lines[i + 1])) {
      const rawLines = [line]
      let j = i + 1
      while (j < lines.length && (INDENTED_RE.test(lines[j]) || lines[j].trim() === '')) {
        if (lines[j].trim() === '' && j + 1 < lines.length && !INDENTED_RE.test(lines[j + 1])) break
        rawLines.push(lines[j])
        j++
      }
      rawBuf.push(...rawLines)
      i = j
      continue
    }

    // lista inline `[...]`
    if (rest.startsWith('[') && rest.endsWith(']')) {
      flushRaw()
      entries.push({
        kind: 'field',
        key,
        type: 'list',
        text: '',
        list: splitInlineList(rest.slice(1, -1)),
        bool: false,
        raw: line,
        dirty: false,
      })
      i++
      continue
    }

    // escalar
    const val = unquote(rest)
    const type = inferScalarType(rest)
    flushRaw()
    entries.push({
      kind: 'field',
      key,
      type,
      text: type === 'boolean' ? '' : val,
      list: [],
      bool: type === 'boolean' ? rest.trim() === 'true' : false,
      raw: line,
      dirty: false,
    })
    i++
  }
  flushRaw()

  return { hasFM: true, entries, body }
}

const NEEDS_QUOTE_RE = /[:#{}[\],&*!|>%@`]|^[\s'"?-]|[\s]$/
const RESERVED = new Set(['true', 'false', 'null', 'yes', 'no', '~', ''])

function quoteScalar(s: string, type: FieldType): string {
  if (s === '') return ''
  const looksSpecial =
    NEEDS_QUOTE_RE.test(s) ||
    RESERVED.has(s.toLowerCase()) ||
    // texto que parece número/fecha/bool pero se quiere como texto → citar
    (type === 'text' && (NUM_RE.test(s) || DATE_RE.test(s) || s === 'true' || s === 'false'))
  if (!looksSpecial) return s
  return '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
}

function quoteItem(s: string): string {
  if (s === '') return '""'
  if (/[,[\]"'#:]|^\s|\s$/.test(s)) {
    return '"' + s.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'
  }
  return s
}

function serializeField(f: FMField): string {
  switch (f.type) {
    case 'boolean':
      return `${f.key}: ${f.bool ? 'true' : 'false'}`
    case 'list':
      return `${f.key}: [${f.list.map(quoteItem).join(', ')}]`
    case 'number':
      return `${f.key}: ${f.text.trim()}`
    case 'date':
      return `${f.key}: ${f.text.trim()}`
    default: {
      const v = quoteScalar(f.text, f.type)
      return v === '' ? `${f.key}:` : `${f.key}: ${v}`
    }
  }
}

function serializeEntry(e: FMEntry): string {
  if (e.kind === 'raw') return e.raw
  return e.dirty ? serializeField(e) : e.raw
}

/**
 * Reconstruye el documento completo: bloque frontmatter (si hay entradas) +
 * cuerpo intacto. Si no quedan entradas, se omite el bloque por completo.
 */
export function buildDocument(entries: FMEntry[], body: string): string {
  const meaningful = entries.filter(
    (e) => e.kind === 'field' || (e.kind === 'raw' && e.raw.trim() !== '')
  )
  if (meaningful.length === 0) return body
  const inner = entries.map(serializeEntry).join('\n')
  return `---\n${inner}\n---\n${body}`
}

/** Solo los campos editables (para la UI). */
export function fieldsOf(parsed: ParsedFM): FMField[] {
  return parsed.entries.filter((e): e is FMField => e.kind === 'field')
}
