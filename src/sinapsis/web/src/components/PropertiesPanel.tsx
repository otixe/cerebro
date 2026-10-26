import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import {
  Type,
  Hash,
  Calendar,
  ToggleLeft,
  ToggleRight,
  Tags,
  ChevronDownCircle,
  Plus,
  X,
  MoreHorizontal,
  Lock,
} from 'lucide-react'
import { fetchNode, type NodeContent } from '../lib/api'
import { canStartNoteSave, noteSaveConflicted, saveNoteOperation } from '../lib/noteSave'
import { applyFieldDelta, collectFieldDelta, type FieldDelta } from '../lib/frontmatterDelta'
import {
  parseFrontmatterEdit,
  type FMEntry,
  type FMField,
  type FieldType,
} from '../lib/frontmatterEdit'
import { useEditorStore } from '../store'
import { useToast } from './Toast'

interface Props {
  activePath: string
  raw: string
}

const TYPE_ICON: Record<FieldType, typeof Type> = {
  text: Type,
  number: Hash,
  date: Calendar,
  boolean: ToggleLeft,
  list: Tags,
  select: ChevronDownCircle,
}

const TYPE_LABEL: Record<FieldType, string> = {
  text: 'Texto',
  number: 'Número',
  date: 'Fecha',
  boolean: 'Casilla',
  list: 'Tags / lista',
  select: 'Selección',
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

function isField(e: FMEntry): e is FMField {
  return e.kind === 'field'
}

export default function PropertiesPanel({ activePath, raw }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const noteDirty = useEditorStore((s) => s.dirty)

  const [entries, setEntries] = useState<FMEntry[]>([])
  const lastSynced = useRef<string>('\u0000')
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // `pendingDelta` no vacío ⟺ hay cambios sin guardar, y la UI tiene que MOSTRARLO.
  const pendingDelta = useRef<FieldDelta>(new Map())
  const [saveStatus, setSaveStatus] = useState<'pending' | 'saving' | 'error' | 'conflict'>('pending')
  const visiblePath = useRef(activePath)
  visiblePath.current = activePath
  const session = useRef({ controller: new AbortController(), draining: false })

  useEffect(() => {
    const current = { controller: new AbortController(), draining: false }
    session.current = current
    lastSynced.current = '\u0000'
    setEntries(parseFrontmatterEdit(raw).entries)
    setSaveStatus('pending')
    return () => {
      current.controller.abort()
      if (saveTimer.current) clearTimeout(saveTimer.current)
      if (pendingDelta.current.size > 0) {
        toast(`${activePath}: cambios sin confirmar en ${[...pendingDelta.current.keys()].join(', ')}. No se guardarán los pendientes al salir; un envío ya iniciado puede terminar.`, 'err')
      }
      pendingDelta.current = new Map()
    }
    // Lifecycle belongs to the path, not refetches or toast function identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePath])

  // Sincroniza estado local con el contenido del servidor cuando cambia
  // externamente (cambio de nota o guardado ajeno). No pisa ediciones en curso.
  useEffect(() => {
    if (raw === lastSynced.current || pendingDelta.current.size > 0) return
    const parsed = parseFrontmatterEdit(raw)
    setEntries(parsed.entries)
    lastSynced.current = raw
  }, [raw, activePath])

  const fields = useMemo(
    () => entries.map((e, i) => ({ e, i })).filter((x) => isField(x.e)),
    [entries]
  ) as { e: FMField; i: number }[]

  async function persist() {
    const path = activePath
    const current = session.current
    const stillVisible = () => visiblePath.current === path && session.current === current && !current.controller.signal.aborted
    if (!path || current.draining || !stillVisible()) return
    current.draining = true
    try {
      while (stillVisible() && pendingDelta.current.size > 0) {
        if (noteSaveConflicted(path)) {
          setSaveStatus('conflict')
          canStartNoteSave(path, toast)
          break // Sticky 409: never reload a fresh hash and retry automatically.
        }
        setSaveStatus('saving')
        let sent: FieldDelta = new Map()
        const ok = await saveNoteOperation(path, () => fetchNode(path),
          fresh => {
            // Snapshot at admission/read completion: coalesce edits made while waiting.
            sent = new Map(pendingDelta.current)
            return applyFieldDelta(fresh.content, sent)
          },
          saved => {
            // Cache of A may advance even when B is visible. Local state must not.
            qc.setQueryData<NodeContent>(['node', path], saved)
            void qc.invalidateQueries({ queryKey: ['tree'] })
            if (!stillVisible()) return
            // Confirm only values actually sent; preserve newer edits during the POST.
            for (const [key, value] of sent) {
              if (pendingDelta.current.get(key) === value) pendingDelta.current.delete(key)
            }
            lastSynced.current = saved.content
            const local = applyFieldDelta(saved.content, pendingDelta.current)
            setEntries(parseFrontmatterEdit(local).entries)
            setSaveStatus('pending')
            if (pendingDelta.current.size === 0) toast('Propiedades guardadas')
          }, toast, { signal: current.controller.signal })
        if (!stillVisible()) return
        if (!ok) {
          setSaveStatus(noteSaveConflicted(path) ? 'conflict' : 'error')
          break // Network/422 errors also require explicit retry; no spinning drain.
        }
      }
    } finally {
      current.draining = false
    }
  }

  function scheduleSave() {
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => { void persist() }, 700)
  }

  function commit(next: FMEntry[]) {
    if (noteSaveConflicted(activePath)) {
      setSaveStatus('conflict')
      canStartNoteSave(activePath, toast)
      return
    }
    collectFieldDelta(entries, next, pendingDelta.current)
    setEntries(next)
    setSaveStatus('pending')
    scheduleSave()
  }

  function patchField(idx: number, patch: Partial<FMField>) {
    commit(
      entries.map((e, i) =>
        i === idx && e.kind === 'field' ? { ...e, ...patch, dirty: true } : e
      )
    )
  }

  function changeType(idx: number, type: FieldType) {
    const e = entries[idx]
    if (e.kind !== 'field') return
    let text = e.text
    let list = e.list
    let bool = e.bool
    // Conversión de valor entre tipos.
    if (type === 'list' && e.type !== 'list') {
      list = e.text ? e.text.split(',').map((s) => s.trim()).filter(Boolean) : []
    } else if (e.type === 'list' && type !== 'list') {
      text = e.list.join(', ')
    }
    if (type === 'boolean') bool = e.type === 'boolean' ? e.bool : /^(true|1|si|sí|yes)$/i.test(e.text)
    if (e.type === 'boolean' && type !== 'boolean') text = e.bool ? 'true' : 'false'
    patchField(idx, { type, text, list, bool })
  }

  function removeField(idx: number) {
    commit(entries.filter((_, i) => i !== idx))
  }

  function addField() {
    const newField: FMField = {
      kind: 'field',
      key: '',
      type: 'text',
      text: '',
      list: [],
      bool: false,
      raw: '',
      dirty: true,
    }
    setEntries((prev) => [...prev, newField])
    // No se guarda hasta que tenga clave.
  }

  const unsavedNotice = pendingDelta.current.size > 0 && (
    <div role="status" className="mb-2 rounded border border-amber-500 px-2 py-1 text-[12px] text-amber-400">
      Sin guardar: {[...pendingDelta.current.keys()].join(', ')}.
      {saveStatus === 'conflict' ? ' Conflicto: recarga la página; conserva estos valores primero.'
        : saveStatus === 'error' ? ' Error de guardado.' : ' Pendiente de confirmación.'}
      {saveStatus === 'error' && <button onClick={() => { void persist() }}>Reintentar guardado</button>}
    </div>
  )

  return (
    <AnimatePresence mode="wait" initial={false}>
      {noteDirty ? (
        <motion.div
          key="props-locked"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="space-y-3"
        >
        {unsavedNotice}
        {fields.length > 0 && <ReadOnlyList fields={fields.map((f) => f.e)} />}
        <div className="flex items-start gap-2 rounded-md border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-3 py-2 text-[11px] text-fg-subtle">
          <Lock size={13} className="mt-0.5 shrink-0 text-fg-faint" />
          <span>Guardá los cambios del editor para editar las propiedades sin conflictos.</span>
        </div>
        </motion.div>
      ) : (
        <motion.div
          key="props-editor"
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="space-y-1"
        >
      {unsavedNotice}
      {fields.length === 0 && (
        <p className="px-1 pb-2 text-[12px] text-fg-subtle">
          Sin propiedades. Agregá la primera para volver la nota una fila de base de datos.
        </p>
      )}

      {fields.map(({ e, i }) => (
        <PropRow
          key={i}
          field={e}
          unsaved={pendingDelta.current.has(e.key)}
          onKey={(key) => patchField(i, { key })}
          onKeyCommit={() => {
            if (e.key.trim() && pendingDelta.current.size > 0) scheduleSave()
          }}
          onText={(text) => patchField(i, { text })}
          onList={(list) => patchField(i, { list })}
          onBool={(bool) => patchField(i, { bool })}
          onType={(t) => changeType(i, t)}
          onRemove={() => removeField(i)}
        />
      ))}

      <button
        onClick={addField}
        className="mt-2 flex w-full items-center gap-2 rounded-md px-2 py-2 text-[12px] text-fg-subtle transition hover:bg-surface-hover hover:text-fg"
      >
        <Plus size={14} /> Agregar propiedad
      </button>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

interface RowProps {
  field: FMField
  unsaved: boolean
  onKey: (k: string) => void
  onKeyCommit: () => void
  onText: (t: string) => void
  onList: (l: string[]) => void
  onBool: (b: boolean) => void
  onType: (t: FieldType) => void
  onRemove: () => void
}

function PropRow({
  field,
  unsaved,
  onKey,
  onKeyCommit,
  onText,
  onList,
  onBool,
  onType,
  onRemove,
}: RowProps) {
  const Icon = TYPE_ICON[field.type]
  const [tagDraft, setTagDraft] = useState('')

  return (
    <div data-unsaved={unsaved || undefined} title={unsaved ? 'Sin guardar' : undefined} className={`group grid grid-cols-[auto_88px_1fr_auto] items-start gap-2 rounded-md px-1 py-1 transition hover:bg-[rgba(255,255,255,0.02)] ${unsaved ? 'border-l-2 border-amber-500' : ''}`}>
      {/* Menú de tipo */}
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            title={TYPE_LABEL[field.type]}
            className="mt-1 flex h-5 w-5 items-center justify-center rounded text-fg-faint transition hover:bg-surface-hover hover:text-fg-muted"
          >
            <Icon size={13} />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            className="menu-content float-shadow z-50 min-w-[168px] rounded-lg border border-[var(--border-strong)] bg-surface p-1"
            sideOffset={4}
            align="start"
          >
            {(Object.keys(TYPE_LABEL) as FieldType[]).map((t) => {
              const TI = TYPE_ICON[t]
              return (
                <DropdownMenu.Item
                  key={t}
                  onSelect={() => onType(t)}
                  className="flex h-7 cursor-pointer items-center gap-2 rounded px-2 text-[13px] text-fg-muted outline-none data-[highlighted]:bg-surface-hover data-[highlighted]:text-fg"
                >
                  <TI size={13} className="text-fg-faint" /> {TYPE_LABEL[t]}
                </DropdownMenu.Item>
              )
            })}
            <DropdownMenu.Separator className="my-1 h-px bg-[var(--border-subtle)]" />
            <DropdownMenu.Item
              onSelect={onRemove}
              className="flex h-7 cursor-pointer items-center gap-2 rounded px-2 text-[13px] text-[var(--danger)] outline-none data-[highlighted]:bg-[rgba(255,99,99,0.1)]"
            >
              <X size={13} /> Eliminar
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      {/* Clave */}
      <input
        value={field.key}
        placeholder="clave"
        onChange={(ev) => onKey(ev.target.value)}
        onBlur={onKeyCommit}
        className="mt-0.5 w-full bg-transparent text-[12px] text-fg-subtle outline-none placeholder:text-fg-faint"
      />

      {/* Valor por tipo */}
      <div className="min-w-0">
        {field.type === 'boolean' ? (
          <button
            onClick={() => onBool(!field.bool)}
            className="flex items-center gap-2 text-[13px] text-fg-muted"
          >
            {field.bool ? (
              <ToggleRight size={18} className="text-accent" />
            ) : (
              <ToggleLeft size={18} className="text-fg-faint" />
            )}
            {field.bool ? 'true' : 'false'}
          </button>
        ) : field.type === 'list' ? (
          <div className="flex flex-wrap items-center gap-2">
            {field.list.map((tag, ti) => (
              <span
                key={`${tag}-${ti}`}
                className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[rgba(168,85,247,0.1)] px-2 py-0.5 text-[11px] text-fg-muted"
              >
                {tag}
                <button
                  onClick={() => onList(field.list.filter((_, x) => x !== ti))}
                  className="text-fg-faint hover:text-[var(--danger)]"
                >
                  <X size={11} />
                </button>
              </span>
            ))}
            <input
              value={tagDraft}
              placeholder="+ tag"
              onChange={(ev) => setTagDraft(ev.target.value)}
              onKeyDown={(ev) => {
                if ((ev.key === 'Enter' || ev.key === ',') && tagDraft.trim()) {
                  ev.preventDefault()
                  onList([...field.list, tagDraft.trim()])
                  setTagDraft('')
                } else if (ev.key === 'Backspace' && !tagDraft && field.list.length) {
                  onList(field.list.slice(0, -1))
                }
              }}
              onBlur={() => {
                if (tagDraft.trim()) {
                  onList([...field.list, tagDraft.trim()])
                  setTagDraft('')
                }
              }}
              className="w-16 flex-1 bg-transparent text-[12px] text-fg-muted outline-none placeholder:text-fg-faint"
            />
          </div>
        ) : field.type === 'date' && ISO_DATE.test(field.text) ? (
          <input
            type="date"
            value={field.text}
            onChange={(ev) => onText(ev.target.value)}
            className="w-full bg-transparent text-[13px] text-fg-muted outline-none [color-scheme:dark]"
          />
        ) : field.type === 'number' ? (
          <input
            inputMode="decimal"
            value={field.text}
            onChange={(ev) => onText(ev.target.value)}
            className="w-full bg-transparent text-[13px] text-fg-muted outline-none"
          />
        ) : (
          <input
            value={field.text}
            placeholder="—"
            onChange={(ev) => onText(ev.target.value)}
            className="w-full bg-transparent text-[13px] text-fg-muted outline-none placeholder:text-fg-faint"
          />
        )}
      </div>

      <MoreHorizontal
        size={13}
        className="mt-1 text-fg-faint opacity-0 transition group-hover:opacity-60"
      />
    </div>
  )
}

function ReadOnlyList({ fields }: { fields: FMField[] }) {
  return (
    <dl className="space-y-1.5">
      {fields.map((f, i) => (
        <div key={i} className="flex gap-2 text-[12px]">
          <dt className="w-24 shrink-0 truncate text-fg-subtle">{f.key}</dt>
          <dd className="min-w-0 flex-1 break-words text-fg-muted">
            {f.type === 'list' ? f.list.join(', ') : f.type === 'boolean' ? String(f.bool) : f.text}
          </dd>
        </div>
      ))}
    </dl>
  )
}
