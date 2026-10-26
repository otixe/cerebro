import { buildDocument, parseFrontmatterEdit, type FMEntry, type FMField } from './frontmatterEdit'

export type FieldDelta = Map<string, FMField | null>
const fields = (entries: FMEntry[]) => new Map(entries
  .filter((e): e is FMField => e.kind === 'field' && e.key.trim() !== '')
  .map(e => [e.key, e]))
const value = (f: FMField) => JSON.stringify([f.type, f.text, f.list, f.bool])

export function collectFieldDelta(before: FMEntry[], after: FMEntry[], delta: FieldDelta) {
  const old = fields(before)
  const next = fields(after)
  for (const key of old.keys()) if (!next.has(key)) delta.set(key, null)
  for (const [key, field] of next) {
    if (!old.has(key) || value(old.get(key)!) !== value(field)) {
      delta.set(key, { ...field, list: [...field.list], dirty: true })
    }
  }
}

export function applyFieldDelta(content: string, delta: FieldDelta): string {
  const parsed = parseFrontmatterEdit(content)
  let entries = parsed.entries
  // Esto no es un merge de tres vías ni resolución de conflictos.
  // En el MISMO campo la última operación gana; campos no tocados vienen del fresco.
  for (const [key, field] of delta) {
    const idx = entries.findIndex(e => e.kind === 'field' && e.key === key)
    if (field === null) {
      entries = entries.filter(e => e.kind !== 'field' || e.key !== key)
    } else if (idx >= 0) {
      entries = entries.map((e, i) => i === idx ? field : e)
    } else {
      entries = [...entries, field]
    }
  }
  return buildDocument(entries, parsed.body)
}
