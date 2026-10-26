import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from 'react'
import { createPortal } from 'react-dom'
import { EditorView, basicSetup } from 'codemirror'
import { Annotation, EditorState, Prec, Transaction } from '@codemirror/state'
import { keymap } from '@codemirror/view'
import { markdown } from '@codemirror/lang-markdown'
import { oneDark } from '@codemirror/theme-one-dark'
import {
  Heading1,
  Heading2,
  Heading3,
  List,
  ListChecks,
  Quote,
  Code,
  Table,
  Info,
  Calendar,
  Link,
  Image,
  type LucideIcon,
} from 'lucide-react'
import {
  filterCommands,
  type SlashCommand,
} from '../lib/slashCommands'

export interface EditorChangeMeta {
  origin: 'user' | 'programmatic'
}

// Every dispatch owned here declares its source. User commands are programmatic
// transactions carrying explicit user intent, unlike external value restoration.
const dispatchOrigin = Annotation.define<'sync' | 'user-command'>()

interface Props {
  value: string
  onChange: (value: string, meta: EditorChangeMeta) => void
  /** Dispara el uploader de imagen (reusa adjuntos) para el comando "/imagen". */
  onRequestImageUpload?: () => void
}

/** Handle imperativo para insertar texto en la posición del cursor. */
export interface EditorHandle {
  insertText: (text: string) => void
  focus: () => void
}

const ICONS: Record<string, LucideIcon> = {
  Heading1,
  Heading2,
  Heading3,
  List,
  ListChecks,
  Quote,
  Code,
  Table,
  Info,
  Calendar,
  Link,
  Image,
}

interface SlashState {
  query: string
  from: number
  to: number
  left: number
  top: number
}

// Detecta `/` al inicio de línea o tras un espacio, seguido de la query (sin espacios).
const SLASH_RE = /(^|\s)\/(\S*)$/

const sinapsisTheme = EditorView.theme(
  {
    '&': {
      height: '100%',
      fontSize: '13px',
      backgroundColor: 'var(--bg)',
      color: 'var(--fg-muted)',
    },
    '.cm-scroller': {
      fontFamily: '"JetBrains Mono", ui-monospace, monospace',
      lineHeight: '1.6',
    },
    '.cm-gutters': {
      backgroundColor: 'var(--bg)',
      color: 'var(--fg-subtle)',
      border: 'none',
    },
    '.cm-activeLine': { backgroundColor: 'rgba(255,255,255,0.03)' },
    '.cm-activeLineGutter': { backgroundColor: 'rgba(255,255,255,0.03)' },
    '.cm-content': { caretColor: 'var(--accent)' },
    '&.cm-focused .cm-cursor': { borderLeftColor: 'var(--accent)' },
    '&.cm-focused': { outline: 'none' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
      backgroundColor: 'rgba(168, 85, 247,0.25)',
    },
  },
  { dark: true }
)

const CodeMirrorEditor = forwardRef<EditorHandle, Props>(
  function CodeMirrorEditor({ value, onChange, onRequestImageUpload }, ref) {
    const hostRef = useRef<HTMLDivElement | null>(null)
    const viewRef = useRef<EditorView | null>(null)
    const onChangeRef = useRef(onChange)
    onChangeRef.current = onChange

    const [slash, setSlash] = useState<SlashState | null>(null)
    const [selIndex, setSelIndex] = useState(0)

    const filtered = slash ? filterCommands(slash.query) : []

    // Ref sincrónico para que el keymap de CM lea el estado más reciente.
    const navRef = useRef({
      active: false,
      count: 0,
      index: 0,
      move: (_d: number) => {},
      choose: () => {},
      close: () => {},
    })
    navRef.current.active = !!slash && filtered.length > 0
    navRef.current.count = filtered.length
    navRef.current.index = selIndex

    useImperativeHandle(ref, () => ({
      insertText(text: string) {
        const view = viewRef.current
        if (!view) return
        const pos = view.state.selection.main.head
        view.dispatch({
          annotations: [dispatchOrigin.of('user-command'), Transaction.userEvent.of('input.attach')],
          changes: { from: pos, insert: text },
          selection: { anchor: pos + text.length },
          scrollIntoView: true,
        })
        view.focus()
      },
      focus() {
        viewRef.current?.focus()
      },
    }))

    // Recalcula el contexto del slash-menu tras cada transacción.
    const computeRef = useRef<(view: EditorView) => void>(() => {})
    computeRef.current = (view: EditorView) => {
      const sel = view.state.selection.main
      if (!sel.empty) {
        setSlash(null)
        return
      }
      const pos = sel.head
      const line = view.state.doc.lineAt(pos)
      const before = line.text.slice(0, pos - line.from)
      const m = SLASH_RE.exec(before)
      if (!m) {
        setSlash(null)
        return
      }
      const slashCol = m.index + m[1].length
      const from = line.from + slashCol
      const coords = view.coordsAtPos(from)
      if (!coords) {
        setSlash(null)
        return
      }
      setSlash((prev) => {
        if (!prev || prev.from !== from || prev.query !== m[2]) setSelIndex(0)
        return {
          query: m[2],
          from,
          to: pos,
          left: coords.left,
          top: coords.bottom + 4,
        }
      })
    }

    function applyCommand(cmd: SlashCommand) {
      const view = viewRef.current
      const s = slash
      if (!view || !s) return
      if (cmd.upload) {
        view.dispatch({
          annotations: [dispatchOrigin.of('user-command'), Transaction.userEvent.of('input.slash')],
          changes: { from: s.from, to: s.to, insert: '' },
        })
        setSlash(null)
        view.focus()
        onRequestImageUpload?.()
        return
      }
      const ins = cmd.insert!()
      const cursor = s.from + (ins.cursor ?? ins.text.length)
      view.dispatch({
        annotations: [dispatchOrigin.of('user-command'), Transaction.userEvent.of('input.slash')],
        changes: { from: s.from, to: s.to, insert: ins.text },
        selection: { anchor: cursor },
        scrollIntoView: true,
      })
      setSlash(null)
      view.focus()
    }

    // Mantiene los callbacks del keymap apuntando a la lógica actual.
    navRef.current.move = (d) =>
      setSelIndex((i) => {
        const n = filterCommands(slash?.query ?? '').length
        if (n === 0) return 0
        return (i + d + n) % n
      })
    navRef.current.choose = () => {
      const cmds = filterCommands(slash?.query ?? '')
      const cmd = cmds[navRef.current.index]
      if (cmd) applyCommand(cmd)
    }
    navRef.current.close = () => setSlash(null)

    // Crear la vista una sola vez.
    useEffect(() => {
      if (!hostRef.current) return
      const slashKeymap = Prec.highest(
        keymap.of([
          {
            key: 'ArrowDown',
            run: () => {
              if (!navRef.current.active) return false
              navRef.current.move(1)
              return true
            },
          },
          {
            key: 'ArrowUp',
            run: () => {
              if (!navRef.current.active) return false
              navRef.current.move(-1)
              return true
            },
          },
          {
            key: 'Enter',
            run: () => {
              if (!navRef.current.active) return false
              navRef.current.choose()
              return true
            },
          },
          {
            key: 'Tab',
            run: () => {
              if (!navRef.current.active) return false
              navRef.current.choose()
              return true
            },
          },
          {
            key: 'Escape',
            run: () => {
              if (!navRef.current.active) return false
              navRef.current.close()
              return true
            },
          },
        ])
      )

      const state = EditorState.create({
        doc: value,
        extensions: [
          slashKeymap,
          basicSetup,
          markdown(),
          oneDark,
          sinapsisTheme,
          EditorView.lineWrapping,
          EditorView.updateListener.of((u) => {
            if (u.docChanged) {
              // Only our explicit sync annotation is programmatic; all other document
              // changes are user edits. A false user costs a recoverable banner/409
              // (the draft keeps its original base); a false programmatic silently
              // loses user work. When in doubt, preserve the user edit, even in a mixed update.
              const user = u.transactions.some((tr) =>
                tr.docChanged && tr.annotation(dispatchOrigin) !== 'sync'
              )
              onChangeRef.current(u.state.doc.toString(), { origin: user ? 'user' : 'programmatic' })
            }
            if (u.docChanged || u.selectionSet || u.focusChanged) {
              computeRef.current(u.view)
            }
          }),
        ],
      })
      const view = new EditorView({ state, parent: hostRef.current })
      viewRef.current = view
      return () => {
        view.destroy()
        viewRef.current = null
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    // Sincronizar cambios externos de `value` (p.ej. al cargar otra nota).
    useEffect(() => {
      const view = viewRef.current
      if (!view) return
      const current = view.state.doc.toString()
      if (value !== current) {
        view.dispatch({
          annotations: dispatchOrigin.of('sync'),
          changes: { from: 0, to: current.length, insert: value },
        })
        setSlash(null)
      }
    }, [value])

    return (
      <div ref={hostRef} className="h-full overflow-hidden">
        {slash &&
          filtered.length > 0 &&
          createPortal(
            <div
              className="palette-surface fixed z-[60] w-64 overflow-hidden py-1"
              style={{
                left: Math.min(slash.left, window.innerWidth - 268),
                top: Math.min(slash.top, window.innerHeight - 260),
              }}
              onMouseDown={(e) => e.preventDefault()}
            >
              <div className="px-3 pb-1 pt-1 text-[10px] font-medium uppercase tracking-wide text-fg-faint">
                Insertar
              </div>
              <ul className="max-h-56 overflow-y-auto">
                {filtered.map((cmd, i) => {
                  const Icon = ICONS[cmd.icon] ?? List
                  const active = i === selIndex
                  return (
                    <li key={cmd.id}>
                      <button
                        onMouseEnter={() => setSelIndex(i)}
                        onClick={() => applyCommand(cmd)}
                        className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left transition-colors duration-200 ${
                          active ? 'bg-surface-hover' : ''
                        }`}
                      >
                        <span
                          className={`flex h-6 w-6 shrink-0 items-center justify-center rounded border transition-colors duration-200 ${
                            active
                              ? 'border-[var(--border-strong)] text-accent'
                              : 'border-[var(--border-subtle)] text-fg-subtle'
                          }`}
                        >
                          <Icon size={13} />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-[13px] text-fg">{cmd.label}</span>
                          <span className="block truncate text-[11px] text-fg-faint">{cmd.hint}</span>
                        </span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            </div>,
            document.body
          )}
      </div>
    )
  }
)

export default CodeMirrorEditor
