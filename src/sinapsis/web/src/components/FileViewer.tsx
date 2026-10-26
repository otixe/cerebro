import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { Pencil, Save, Paperclip, Eye, EyeOff, X, Loader2, Download, FileDown, ChevronRight, FolderOpen } from 'lucide-react'
import { fetchNode, fetchTree, uploadFile, ApiError } from '../lib/api'
import { exportNoteHtml } from '../lib/exportHtml'
import { exportMarkdownZip } from '../lib/exportMarkdownZip'
import { saveNoteOperation } from '../lib/noteSave'
import { parseFrontmatter } from '../lib/frontmatter'
import { buildWikiIndex } from '../lib/wikilink'
import MarkdownView from './MarkdownView'
import CodeMirrorEditor, { type EditorHandle } from './CodeMirrorEditor'
import { useEditorStore, useShellStore } from '../store'
import { useToast } from './Toast'

interface Props {
  path: string | null
  onNavigate: (path: string) => void
}

function parentDir(path: string): string {
  const i = path.lastIndexOf('/')
  return i === -1 ? '' : path.slice(0, i)
}

export default function FileViewer({ path, onNavigate }: Props) {
  const qc = useQueryClient()
  const toast = useToast()
  const setDirty = useEditorStore((s) => s.setDirty)
  const openNotesBrowser = useShellStore((s) => s.openNotesBrowser)

  const { data: tree } = useQuery({ queryKey: ['tree'], queryFn: fetchTree })
  const wikiIndex = useMemo(() => buildWikiIndex(tree), [tree])

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['node', path],
    queryFn: () => fetchNode(path as string),
    enabled: !!path,
  })

  const [mode, setMode] = useState<'read' | 'edit'>('read')
  const [showPreview, setShowPreview] = useState(true)
  const [draft, setDraft] = useState('')
  const [baseVersion, setBaseVersion] = useState<string>()
  const [saving, setSaving] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const editorRef = useRef<EditorHandle | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // Only positively identified editor input can set this marker; content equality cannot.
  const userEditedAfterSend = useRef(false)
  const inFlightPath = useRef<string | null>(null)
  const retainedDraftPath = useRef<string | null>(null)

  useEffect(() => {
    retainedDraftPath.current = null
    userEditedAfterSend.current = false
    setMode('read')
  }, [path])

  useEffect(() => {
    // A cache publication/refetch must not consume input accepted during the POST.
    // Retain the draft's ORIGINAL base too; never rebase it onto an acknowledgement.
    if (data && inFlightPath.current !== path && retainedDraftPath.current !== path) {
      userEditedAfterSend.current = false
      setDraft(data.content)
      setBaseVersion(data.version)
    }
  }, [data, path])

  const dirty = mode === 'edit' && !!data && draft !== data.content

  useEffect(() => {
    setDirty(dirty)
    return () => setDirty(false)
  }, [dirty, setDirty])

  async function onSave() {
    if (!path || saving) return
    if (!baseVersion) {
      toast('Falta la versión de la nota. Recarga la página antes de guardar.', 'err')
      return
    }
    setSaving(true)
    inFlightPath.current = path
    userEditedAfterSend.current = false
    // un borrador viejo NUNCA puede viajar con un token más nuevo que él
    // Send the draft's own base, even when a prior POST has confirmed a newer version.
    await saveNoteOperation(path,
      async () => ({ path, content: draft, version: baseVersion, bytes: 0 }),
      () => draft,
      saved => {
        const hasLaterEdits = userEditedAfterSend.current
        retainedDraftPath.current = hasLaterEdits ? path : null
        if (!hasLaterEdits) {
          // Acknowledged content and version are one snapshot, never adopt just its token.
          setDraft(saved.content)
          setBaseVersion(saved.version)
        }
        qc.setQueryData(['node', path], saved)
        if (hasLaterEdits) {
          toast('Se guardó la versión enviada. Quedan cambios sin guardar.', 'err')
        } else {
          toast('Nota guardada')
          setMode('read')
        }
      }, toast)
    // Only release UI busy state here; success effects are generation-checked above.
    inFlightPath.current = null
    setSaving(false)
  }

  function onCancelEdit() {
    if (dirty && !window.confirm('Descartar los cambios sin guardar?')) return
    userEditedAfterSend.current = false
    retainedDraftPath.current = null
    if (data) {
      setDraft(data.content)
      setBaseVersion(data.version)
    }
    setMode('read')
  }

  async function handleUpload(file: File | undefined | null) {
    if (!file || !path || uploading) return
    setUploading(true)
    try {
      const dir = parentDir(path)
      const res = await uploadFile(file, dir)
      const rel = res.path.startsWith(dir + '/')
        ? res.path.slice(dir.length + 1)
        : res.path
      const alt = rel.replace(/\.[^./]+$/, '').split('/').pop() ?? rel
      const target = rel.includes(' ') ? `<${rel}>` : rel
      editorRef.current?.insertText(`![${alt}](${target})`)
      await qc.invalidateQueries({ queryKey: ['tree'] })
      toast('Adjunto subido')
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'No se pudo subir el adjunto'
      toast(msg, 'err')
    } finally {
      setUploading(false)
    }
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files?.[0]
    if (file) handleUpload(file)
  }

  function downloadBlob(blob: Blob, filename: string) {
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  }

  async function onExportMd() {
    if (!path || !data || exporting) return
    setExporting(true)
    try {
      const blob = await exportMarkdownZip(path, data.content, data.lossy)
      downloadBlob(blob, `${(path.split('/').pop() ?? 'nota').replace(/\.md$/i, '')}.zip`)
    } catch (e) {
      toast(e instanceof Error ? e.message : 'No se pudo descargar Markdown con imágenes', 'err')
    } finally {
      setExporting(false)
    }
  }

  async function onExportHtml() {
    if (!path || !data || exporting) return
    setExporting(true)
    try {
      const blob = await exportNoteHtml(path, data.content, wikiIndex)
      const stem = (path.split('/').pop() ?? 'nota').replace(/\.md$/i, '')
      downloadBlob(blob, `${stem}.html`)
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : 'No se pudo exportar el HTML (revisa imágenes y permisos)'
      toast(msg, 'err')
    } finally {
      setExporting(false)
    }
  }

  if (!path) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
            <Pencil size={22} className="text-fg-faint" />
          </div>
          <p className="max-w-[300px] text-[13px] text-fg-subtle">
            Sin nota abierta. Elegí una del árbol o abrí el buscador con{' '}
            <span className="keycap">⌘K</span>.
          </p>
        </div>
      </div>
    )
  }

  if (isLoading)
    return <div className="px-6 py-4 text-sm text-fg-subtle">Cargando…</div>

  if (isError)
    return (
      <div className="px-6 py-4 text-sm text-danger">
        {error instanceof Error ? error.message : 'Error al cargar el archivo'}
      </div>
    )

  const rawContent = data?.content ?? ''
  const { data: fm, body } = parseFrontmatter(rawContent)
  const fmEntries = Object.entries(fm)
  const draftBody = parseFrontmatter(draft).body

  const iconBtn =
    'flex h-7 items-center gap-1.5 rounded-md px-2 text-[12px] text-fg-subtle transition hover:bg-surface-hover hover:text-fg disabled:opacity-50'

  // Segmentos para el breadcrumb: carpetas clickeables + nombre de la nota.
  const noteSegments = path.split('/').filter(Boolean)
  const noteName = (noteSegments[noteSegments.length - 1] ?? path).replace(/\.md$/, '')
  const folderSegments = noteSegments.slice(0, -1)
  const noteDir = folderSegments.join('/')

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="glass flex h-10 shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-4">
        {/* Breadcrumb clickeable: cada carpeta revela ese nivel en el navegador de notas. */}
        <nav className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto text-[11px] text-fg-subtle">
          <button
            onClick={() => openNotesBrowser()}
            className="shrink-0 text-fg-muted transition hover:text-fg"
            title="Ver todo el arbol"
          >
            El Cerebro
          </button>
          {folderSegments.map((seg, i) => {
            const dirPath = folderSegments.slice(0, i + 1).join('/')
            return (
              <span key={dirPath} className="flex shrink-0 items-center gap-1.5">
                <ChevronRight size={13} className="text-fg-faint" />
                <button
                  onClick={() => openNotesBrowser(dirPath)}
                  className="text-fg-muted transition hover:text-fg"
                  title={`Ir a ${seg}`}
                >
                  {seg}
                </button>
              </span>
            )
          })}
          <span className="flex shrink-0 items-center gap-1.5">
            <ChevronRight size={13} className="text-fg-faint" />
            <span className="font-medium text-fg">{noteName}</span>
          </span>
        </nav>
        {/* Ir a la carpeta contenedora de la nota. */}
        <button
          onClick={() => openNotesBrowser(noteDir || undefined)}
          className={`${iconBtn} shrink-0`}
          title="Ir a la carpeta"
        >
          <FolderOpen size={14} />
        </button>
        <div className="flex shrink-0 items-center gap-1">
          {mode === 'read' ? (
            <>
              <button onClick={onExportMd} disabled={exporting} className={iconBtn} title="Descargar ZIP con Markdown e imágenes">
                {exporting ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />} .md ZIP
              </button>
              <button
                onClick={onExportHtml}
                disabled={exporting}
                className={iconBtn}
                title="Exportar HTML autocontenido"
              >
                {exporting ? <Loader2 size={14} className="animate-spin" /> : <FileDown size={14} />}
                HTML
              </button>
              <button onClick={() => setMode('edit')} className={iconBtn}>
                <Pencil size={14} /> Editar
              </button>
            </>
          ) : (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  handleUpload(e.target.files?.[0])
                  e.target.value = ''
                }}
              />
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className={iconBtn}
              >
                {uploading ? <Loader2 size={14} className="animate-spin" /> : <Paperclip size={14} />}
                {uploading ? 'Subiendo…' : 'Adjuntar'}
              </button>
              <button onClick={() => setShowPreview((p) => !p)} className={iconBtn}>
                {showPreview ? <EyeOff size={14} /> : <Eye size={14} />}
                {showPreview ? 'Ocultar' : 'Preview'}
              </button>
              <button onClick={onCancelEdit} className={iconBtn}>
                <X size={14} /> Cancelar
              </button>
              <button
                onClick={onSave}
                disabled={saving || !dirty}
                className="flex h-7 items-center gap-1.5 rounded-md bg-accent-bg px-2.5 text-[12px] font-medium text-white transition hover:bg-accent-hover disabled:opacity-50"
              >
                {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
                {saving ? 'Guardando…' : dirty ? 'Guardar' : 'Guardado'}
              </button>
            </>
          )}
        </div>
      </div>

      {dirty && (
        <div role="status" className="border-b border-[var(--border-subtle)] px-4 py-2 text-xs text-fg-subtle">
          Sin guardar: hay cambios en el editor{saving ? '; enviando la versión anterior…' : '.'}
        </div>
      )}

      {/* Cuerpo */}
      <div className="min-h-0 flex-1">
        {mode === 'read' ? (
          <motion.div
            id="note-scroll"
            key={path}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="h-full overflow-y-auto"
          >
            <div className="reading mx-auto px-4 py-6 sm:px-8 sm:py-8">
              {fmEntries.length > 0 && (
                <div className="mb-6 rounded-lg border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] px-4 py-3">
                  <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
                    {fmEntries.map(([k, v]) => (
                      <div key={k} className="contents">
                        <dt className="font-mono text-fg-subtle">{k}</dt>
                        <dd className="truncate text-fg-muted">{v}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
              <MarkdownView content={body} notePath={path} wikiIndex={wikiIndex} onNavigate={onNavigate} />
            </div>
          </motion.div>
        ) : (
          <div className="flex h-full min-h-0 flex-col lg:flex-row">
            <div
              className={`relative min-h-0 min-w-0 border-b border-[var(--border-subtle)] lg:border-b-0 lg:border-r ${
                showPreview ? 'h-1/2 lg:h-full lg:w-1/2' : 'h-full lg:w-full'
              }`}
              onDragOver={(e) => {
                e.preventDefault()
                if (!dragOver) setDragOver(true)
              }}
              onDragLeave={(e) => {
                if (e.currentTarget === e.target) setDragOver(false)
              }}
              onDrop={onDrop}
            >
              <CodeMirrorEditor ref={editorRef} value={draft} onChange={(value, meta) => {
                // Unknown/programmatic updates never manufacture a human edit marker.
                if (meta?.origin === 'user' && inFlightPath.current === path) {
                  userEditedAfterSend.current = true
                }
                setDraft(value)
              }} onRequestImageUpload={() => fileInputRef.current?.click()} />
              {dragOver && (
                <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center border-2 border-dashed border-accent bg-accent-bg/10 text-xs font-medium text-fg">
                  Soltá la imagen para adjuntarla
                </div>
              )}
            </div>
            {showPreview && (
              <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
                <div className="reading mx-auto px-4 py-6 sm:px-8 sm:py-8">
                  <MarkdownView content={draftBody} notePath={path} wikiIndex={wikiIndex} onNavigate={onNavigate} />
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
