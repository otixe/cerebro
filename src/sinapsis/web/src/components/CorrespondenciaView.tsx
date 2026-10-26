import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  ArrowLeft,
  CheckCircle2,
  Download,
  Image as ImageIcon,
  Inbox,
  Loader2,
  MailPlus,
  MessageSquareReply,
  Paperclip,
  RefreshCw,
  Send,
  Users,
  X,
} from 'lucide-react'
import {
  ApiError,
  descargarAdjuntoCorrespondencia,
  fetchCorrespondenciaBandeja,
  fetchCorrespondenciaBuzones,
  getBusPublishEnvironmentError,
  leerCorrespondencia,
  okCorrespondencia,
  publicarCorrespondencia,
  reaccionarCorrespondencia,
  responderCorrespondencia,
  solicitarRevisionCorrespondencia,
  subirAdjuntoCorrespondencia,
  type CorrespondenciaAdjunto,
  type CorrespondenciaEnvioInput,
  type CorrespondenciaMensaje,
  type CorrespondenciaPrioridad,
  type CorrespondenciaTipo,
  type Me,
  type Meta,
} from '../lib/api'
import {
  accionOfrecida,
  adjuntoAccionable,
  claveScope,
  enviadosTitulo,
  MOTIVO_SIN_PARTICIPACION,
  motivoSinConsultar,
  planBandeja,
  raicesParticipadas,
  scopeEsPropio,
  scopeEtiqueta,
  type ScopeCorrespondencia,
} from '../lib/correspondenciaAdmin'
import { useIsDesktop } from '../hooks/useIsDesktop'
import { useVisualViewport } from '../hooks/useVisualViewport'
import { useAppStore } from '../store'
import { contentPresentation } from '../lib/correspondenciaContent'
import { verifyRaster } from '../lib/rasterMime'
import { safeDownloadName } from '../lib/safeDownloadName'
import { TZ } from './taskShared'

interface Props {
  me: Me
}

type ViewKind = 'pendientes' | 'archivo'

interface ThreadSummary {
  raiz: string
  asunto: string
  prioridad: CorrespondenciaPrioridad
  mensajes: CorrespondenciaMensaje[]
}

const inputCls =
  'w-full min-h-11 rounded-md border border-[var(--border-subtle)] bg-surface px-3 py-2 text-[12px] text-fg outline-none focus:border-[rgba(255,255,255,0.2)] disabled:opacity-50 lg:min-h-0'

/** Campo de destinatarios con chips. Reusa `inputCls` para heredar el alto
 *  tactil (`min-h-11`) y el borde/fondo del resto de los campos; `flex-wrap`
 *  hace que los chips bajen de linea en vez de desbordar, y `min-w-0` permite
 *  que la celda del grid se encoja por debajo de su min-content en movil. */
const chipsFieldCls =
  `${inputCls} flex min-w-0 flex-wrap items-center gap-1 focus-within:border-[rgba(255,255,255,0.2)]`

/** Orden de tabulación de un contenedor. Se usa para ATRAPAR el `Tab` dentro del
 *  popup: sin esto el foco se va a la bandeja de atrás, que con `aria-modal` ya
 *  se declaró inerte — el lector de pantalla y el teclado dirían cosas distintas. */
const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'

function focusablesIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    // `offsetParent === null` descarta lo que está oculto por un ancestro; el
    // input de adjuntos es `sr-only` (1px) pero SÍ es tabulable y debe entrar.
    (el) => el.offsetParent !== null || getComputedStyle(el).position === 'fixed'
  )
}

function stamp(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone: TZ,
  }).formatToParts(d).reduce<Record<string, string>>((acc, item) => {
    acc[item.type] = item.value
    return acc
  }, {})
  const hour = parts.hour === '24' ? '00' : parts.hour
  return parts.year ? `${parts.year}-${parts.month}-${parts.day} ${hour}:${parts.minute}` : ''
}

function codeOrder(a: string, b: string): number {
  const suffix = (code: string) => {
    const part = code.split('.')[1]
    return part ? Number(part) : 0
  }
  return suffix(a) - suffix(b) || a.localeCompare(b)
}

function dedupeMessages(groups: CorrespondenciaMensaje[][]): CorrespondenciaMensaje[] {
  const found = new Map<string, CorrespondenciaMensaje>()
  groups.flat().forEach((message) => found.set(message.id, message))
  return [...found.values()]
}

function groupThreads(messages: CorrespondenciaMensaje[]): ThreadSummary[] {
  const groups = new Map<string, CorrespondenciaMensaje[]>()
  messages.forEach((message) => {
    const current = groups.get(message.raiz) ?? []
    current.push(message)
    groups.set(message.raiz, current)
  })
  return [...groups.entries()].map(([raiz, threadMessages]) => {
    const ordered = [...threadMessages].sort((a, b) => codeOrder(a.codigo, b.codigo))
    const root = ordered.find((message) => message.codigo === raiz) ?? ordered[0]
    return {
      raiz,
      asunto: root?.asunto ?? raiz,
      prioridad: root?.prioridad ?? 'medio',
      mensajes: ordered,
    }
  }).sort((a, b) => {
    const ta = Math.max(...a.mensajes.map((message) => Date.parse(message.creado_en) || 0))
    const tb = Math.max(...b.mensajes.map((message) => Date.parse(message.creado_en) || 0))
    return tb - ta
  })
}

function priorityClass(priority: CorrespondenciaPrioridad): string {
  if (priority === 'urgente') return 'bg-red-500/15 text-red-300'
  if (priority === 'bajo') return 'bg-slate-500/15 text-slate-300'
  return 'bg-amber-500/15 text-amber-300'
}

function errorMessage(cause: unknown): string {
  if (cause instanceof ApiError) return `Error ${cause.status}: ${cause.message}`
  return cause instanceof Error ? cause.message : 'No se pudo completar la acción.'
}

function mailboxOptions(meta: Meta | null, adminUser?: string): {
  options: string[]; teams: Map<string, string[]>; aliases: Map<string, string>
} {
  const options = new Set<string>()
  const teams = new Map<string, string[]>()
  const aliases = new Map<string, string>()
  for (const leadRaw of meta?.leads ?? []) {
    const lead = leadRaw.toLowerCase()
    const team = meta?.leader_teams?.[leadRaw]?.toLowerCase()
    if (!team) continue
    const alias = `@${team}`
    const canonical = `${alias}/${lead}`
    options.add(alias)
    options.add(canonical)
    aliases.set(alias, canonical)
  }
  const membersByTeam = new Map<string, string[]>()
  for (const [teamRaw, names] of Object.entries(meta?.subagents_by_team ?? {})) {
    membersByTeam.set(teamRaw.toLowerCase(), names)
  }
  for (const teamRaw of Object.values(meta?.leader_teams ?? {})) {
    const team = teamRaw.toLowerCase()
    if (!membersByTeam.has(team)) membersByTeam.set(team, [])
  }
  for (const [team, names] of membersByTeam) {
    const members = new Set<string>()
    const leader = aliases.get(`@${team}`)
    if (leader) members.add(leader)
    for (const raw of names) {
      const name = raw.toLowerCase()
      const local = name.startsWith(`${team}-`) ? name.slice(team.length + 1) : name
      const mailbox = `@${team}/${local}`
      options.add(mailbox)
      members.add(mailbox)
    }
    const wildcard = `@${team}/*`
    options.add(wildcard)
    teams.set(wildcard, [...members].sort())
  }
  if (adminUser && (meta?.agents ?? []).some((name) => name.toLowerCase() === adminUser.toLowerCase())) options.add(`@${adminUser.toLowerCase()}`)
  return { options: [...options].sort(), teams, aliases }
}

function parseRecipients(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).map((item) => item.trim().toLowerCase()).filter(Boolean))]
}

function expandRecipients(raw: string[], roster: ReturnType<typeof mailboxOptions>): {
  expanded: string[]; invalid: string[]
} {
  const valid = new Set(roster.options)
  const expanded = new Set<string>()
  const invalid: string[] = []
  raw.forEach((mailbox) => {
    const team = roster.teams.get(mailbox)
    const canonical = roster.aliases.get(mailbox) ?? mailbox
    if (team) team.forEach((member) => expanded.add(member))
    else if (valid.has(canonical)) expanded.add(canonical)
    else invalid.push(mailbox)
  })
  return { expanded: [...expanded].sort(), invalid }
}


const PREVIEW_IMAGE_MIME = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp',
])

function fileList(files: FileList | null): File[] {
  return files ? Array.from(files) : []
}

function humanBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.rel = 'noopener'
  anchor.click()
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

export function AttachmentImage({ attachment }: { attachment: CorrespondenciaAdjunto }) {
  const [url, setUrl] = useState('')
  const [notPreviewable, setNotPreviewable] = useState(false)
  useEffect(() => {
    let active = true
    let objectUrl = ''
    void descargarAdjuntoCorrespondencia(attachment.id).then(async (blob) => {
      const bytes = new Uint8Array(await blob.arrayBuffer())
      if (!active) return
      const mime = PREVIEW_IMAGE_MIME.has(attachment.tipo_mime)
        ? await verifyRaster(new Blob([bytes], { type: blob.type })) : null
      if (!active) return
      if (!mime) {
        setNotPreviewable(true)
        return
      }
      objectUrl = URL.createObjectURL(new Blob([bytes], { type: mime }))
      setUrl(objectUrl)
    }).catch(() => { if (active) setNotPreviewable(true) })
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [attachment.id, attachment.tipo_mime])
  if (notPreviewable) return <div className="flex h-20 items-center justify-center text-fg-faint"><Paperclip size={24} /></div>
  if (!url) return <div className="flex h-32 items-center justify-center text-fg-faint"><ImageIcon size={24} /></div>
  return <img src={url} alt={attachment.nombre} loading="lazy" decoding="async" className="h-32 w-full object-cover" />
}

function AttachmentList({ attachments, participa, onDownload }: {
  attachments: CorrespondenciaAdjunto[]
  /** ¿El caller participa del hilo? La bandeja embute la metadata de adjuntos SIN
   *  chequear participación, así que en vista global se ven nombres de adjuntos de
   *  hilos ajenos mientras `/adjuntos` responde 403. No se ocultan (eso diría "este
   *  hilo no tiene adjuntos", otra mentira): se muestran DESHABILITADOS con el motivo,
   *  el mismo idiom que ya usa `retirado`. */
  participa: boolean
  onDownload: (attachment: CorrespondenciaAdjunto) => void
}) {
  if (!attachments.length) return null
  return (
    <div className="mt-3 grid gap-2 sm:grid-cols-2">
      {attachments.map((attachment) => {
        const retired = Boolean(attachment.retirado_en)
        const accionable = adjuntoAccionable(retired, participa)
        const sinAcceso = !retired && !participa
        return <button key={attachment.id} type="button" disabled={!accionable}
          title={sinAcceso ? MOTIVO_SIN_PARTICIPACION : undefined}
          onClick={() => { if (accionable) onDownload(attachment) }}
          className="overflow-hidden rounded-md border border-[var(--border-subtle)] bg-surface/60 text-left hover:bg-surface-hover disabled:cursor-not-allowed disabled:opacity-60">
          {accionable && PREVIEW_IMAGE_MIME.has(attachment.tipo_mime) ? (
            <AttachmentImage attachment={attachment} />
          ) : (
            <div className="flex h-20 items-center justify-center text-fg-faint"><Paperclip size={24} /></div>
          )}
          <span className="flex items-center gap-2 px-2 py-2 text-[11px] text-fg-muted">
            <span className="min-w-0 flex-1 truncate">{attachment.nombre}</span>
            <span className="shrink-0 text-fg-faint">{retired ? 'retirado' : sinAcceso ? 'sin acceso' : humanBytes(attachment.tamano)}</span>
            {accionable && <Download size={13} />}
          </span>
        </button>
      })}
    </div>
  )
}

function AudienceConfirmation({ audience, pending, onConfirm, onBack }: {
  audience: string[]
  pending: boolean
  onConfirm: () => void
  onBack: () => void
}) {
  return (
    <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
      <p className="text-[12px] font-semibold text-amber-200">
        Se enviará a {audience.length} destinatario{audience.length === 1 ? '' : 's'}:
        {' '}{audience.join(', ') || 'nadie'}
      </p>
      <p className="mt-1 text-[11px] text-amber-100/80">
        Un mensaje del admin puede interpretarse como una orden. Confirmá la audiencia completa.
      </p>
      <div className="mt-2 flex gap-2">
        <button type="button" onClick={onConfirm} disabled={pending}
          className="min-h-11 rounded-md bg-amber-500 px-3 text-[12px] font-semibold text-black disabled:opacity-50 lg:min-h-0 lg:py-2">
          {pending ? 'Enviando…' : `Confirmar envío a ${audience.length}`}
        </button>
        <button type="button" onClick={onBack} disabled={pending}
          className="min-h-11 rounded-md border border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted lg:min-h-0 lg:py-2">
          Volver
        </button>
      </div>
    </div>
  )
}

function NewMessageComposer({ canPublish, onDone, adminUser }: {
  canPublish: boolean
  onDone: () => void
  adminUser?: string
}) {
  const meta = useAppStore((state) => state.meta)
  const roster = useMemo(() => mailboxOptions(meta, adminUser), [meta, adminUser])
  const [recipients, setRecipients] = useState<string[]>([])
  const [draft, setDraft] = useState('')
  const draftRef = useRef<HTMLInputElement>(null)
  const [subject, setSubject] = useState('')
  const [priority, setPriority] = useState<CorrespondenciaPrioridad>('medio')
  const [type, setType] = useState<CorrespondenciaTipo>('inform')
  const [content, setContent] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [confirming, setConfirming] = useState(false)
  const [pending, setPending] = useState(false)
  const sendLatch = useRef(false)
  const [error, setError] = useState('')
  const unavailable = getBusPublishEnvironmentError()
  const audience = useMemo(() => expandRecipients(recipients, roster), [recipients, roster])
  /** Buzones inexistentes, para pintar EL CHIP en rojo al agregarlo en vez de
   *  reventar recién al enviar. Sale de `expandRecipients`, la misma función
   *  que corre `prepare()`: el chip rojo y el error de envío no pueden
   *  discrepar por construcción. */
  const invalidMailboxes = useMemo(() => new Set(audience.invalid), [audience])

  // ── VENTANA FLOTANTE (pedido de admin: «es casi como un popup») ────────────
  const [open, setOpen] = useState(false)
  /** Estado de la GUARDA de cierre. No es un `window.confirm`: bloquea el hilo,
   *  no se puede medir y en móvil aparece pegado a la barra del navegador. */
  const [askDiscard, setAskDiscard] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const guardRef = useRef<HTMLDivElement>(null)
  const keepEditingRef = useRef<HTMLButtonElement>(null)
  /** El click en el overlay sólo cuenta si EMPEZÓ en el overlay. Sin esto,
   *  arrastrar para seleccionar texto desde dentro del panel y soltar afuera
   *  dispara el cierre y descarta el mensaje. */
  const overlayArmed = useRef(false)
  const wasOpen = useRef(false)
  const titleId = useId()
  const guardTitleId = useId()
  const isDesktop = useIsDesktop()
  /** Alto REALMENTE visible. `dvh` sigue las barras del navegador pero NO informa
   *  el teclado virtual (en iOS además desplaza la página). Se reusa el hook que
   *  ya está en producción para el composer del chat en vez de escribir otro;
   *  en escritorio `enabled=false` lo deja inerte, sin listeners. */
  const vv = useVisualViewport(!isDesktop)

  /** Hay algo que se PERDERÍA al cerrar ⇒ condición de la guarda. `priority` y
   *  `type` no cuentan: son selects con default, nadie los «escribió». */
  const dirty =
    recipients.length > 0 || draft.trim() !== '' || subject.trim() !== '' ||
    content.trim() !== '' || files.length > 0

  const closeNow = () => {
    setAskDiscard(false)
    setOpen(false)
  }

  /** Cierre PEDIDO por el usuario (✕, `Escape`, click en el overlay). Con
   *  contenido cargado NO cierra: pregunta. Las tres vías pasan por acá, así que
   *  la guarda no puede quedar sin cubrir en una de ellas. */
  const requestClose = () => {
    if (dirty) {
      setAskDiscard(true)
      return
    }
    closeNow()
  }

  const discardAndClose = () => {
    setRecipients([])
    setDraft('')
    setSubject('')
    setContent('')
    setFiles([])
    setConfirming(false)
    setError('')
    closeNow()
  }

  // Foco al abrir → DESTINATARIOS; al cerrar → de vuelta al botón que abrió.
  // La restitución va en un efecto (no junto a `setOpen(false)`) para que corra
  // DESPUÉS de que React desmonte el panel; si no, el navegador puede mandar el
  // foco al `body` al desaparecer el elemento enfocado.
  useEffect(() => {
    if (!open) {
      if (wasOpen.current) {
        wasOpen.current = false
        triggerRef.current?.focus()
      }
      return
    }
    wasOpen.current = true
    const timer = window.setTimeout(() => {
      if (draftRef.current && !draftRef.current.disabled) draftRef.current.focus()
      else panelRef.current?.focus()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [open])

  // La guarda arranca con el foco en la opción SEGURA: un Enter reflejo sigue
  // editando en vez de descartar el mensaje.
  useEffect(() => {
    if (!askDiscard) return
    const timer = window.setTimeout(() => keepEditingRef.current?.focus(), 0)
    return () => window.clearTimeout(timer)
  }, [askDiscard])

  // El fondo no scrollea mientras el popup está abierto (si no, a 1280 quedan dos
  // scrolls compitiendo). Se restaura el valor INLINE anterior, no un literal:
  // `body` trae `overflow-x:hidden` de la hoja de estilos y no hay que pisarlo.
  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  // `Escape` + focus trap. En captura sobre `document` para ver la tecla antes
  // que los handlers del formulario; el `Tab` sólo se intercepta en los BORDES,
  // así el `Tab` que confirma un chip (que a propósito NO hace preventDefault)
  // sigue funcionando igual que en producción.
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      // Con la guarda arriba, el trap se cierra sobre la guarda: no se puede
      // tabular a los campos de un mensaje que se está preguntando si descartar.
      const root = askDiscard ? guardRef.current : panelRef.current
      if (!root) return
      if (event.key === 'Escape') {
        event.preventDefault()
        if (askDiscard) setAskDiscard(false)
        else requestClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusablesIn(root)
      if (!items.length) {
        event.preventDefault()
        panelRef.current?.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      const inside = !!active && root.contains(active)
      if (event.shiftKey) {
        if (!inside || active === first) {
          event.preventDefault()
          last.focus()
        }
      } else if (!inside || active === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [open, askDiscard, dirty])

  /** Confirma `raw` como uno o más chips (acepta lista separada por comas o
   *  espacios) y vacía el token en edición. Devuelve la lista RESULTANTE
   *  porque el estado de React todavía no está actualizado en este mismo
   *  handler y `prepare()` necesita el valor real. */
  const addRecipients = (raw: string): string[] => {
    const tokens = parseRecipients(raw)
    if (!tokens.length) return recipients
    const next = [...recipients]
    for (const token of tokens) if (!next.includes(token)) next.push(token)
    setRecipients(next)
    setDraft('')
    return next
  }

  const removeRecipient = (mailbox: string) => {
    setRecipients((current) => current.filter((item) => item !== mailbox))
    setConfirming(false)
  }

  const prepare = () => {
    setError('')
    // El token a medio escribir NO se descarta: se confirma como chip (mismo
    // criterio que el blur). Se valida contra el valor devuelto porque
    // `audience` todavía refleja el estado anterior en este mismo handler.
    const target = expandRecipients(addRecipients(draft), roster)
    if (!canPublish) return setError('Tu sesión no puede publicar Correspondencia.')
    if (unavailable) return setError(unavailable)
    if (target.invalid.length) {
      return setError(`Buzón inexistente: ${target.invalid.join(', ')}. Revisá la sintaxis sugerida.`)
    }
    if (!target.expanded.length) return setError('Elegí al menos un destinatario.')
    if (!subject.trim()) return setError('El asunto es obligatorio.')
    if (!content.trim()) return setError('El mensaje no puede estar vacío.')
    setConfirming(true)
  }

  const send = async () => {
    if (sendLatch.current) return
    sendLatch.current = true
    setPending(true)
    setError('')
    const input: CorrespondenciaEnvioInput = {
      // El payload usa exactamente la misma audiencia canónica confirmada en preview.
      destinatarios: audience.expanded,
      asunto: subject.trim(),
      prioridad: priority,
      contenido: content.trim(),
      tipo: type,
    }
    try {
      const sent = await publicarCorrespondencia(input)
      try {
        for (const file of files) await subirAdjuntoCorrespondencia(sent.codigo, file)
      } catch (cause) {
        throw new Error(`El mensaje ${sent.codigo} fue enviado, pero falló un adjunto: ${errorMessage(cause)}`)
      }
      setFiles([])
      setRecipients([])
      setDraft('')
      setSubject('')
      setContent('')
      setConfirming(false)
      // Envío exitoso ⇒ la ventana se cierra sola. Va ANTES de `onDone()` para
      // que el refetch de la bandeja ocurra con el popup ya cerrado.
      setAskDiscard(false)
      setOpen(false)
      onDone()
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      sendLatch.current = false
      setPending(false)
    }
  }

  const trigger = (
    <button ref={triggerRef} type="button" data-probe="composer-trigger"
      onClick={() => { setError(''); setOpen(true) }}
      aria-haspopup="dialog" aria-expanded={open}
      className="flex min-h-11 shrink-0 items-center gap-2 rounded-md bg-accent px-3 text-[11px] font-semibold text-white lg:min-h-10">
      <MailPlus size={13} /> Mensaje nuevo
    </button>
  )

  const dialog = (
    // El overlay ATENÚA el fondo y es la 3ª vía de cierre. `items-stretch` + panel
    // a `100dvh` hace que en móvil el popup SEA la pantalla; desde `sm` es una
    // tarjeta centrada.
    <div data-probe="composer-overlay"
      className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/70 backdrop-blur-sm sm:items-center sm:p-4"
      onMouseDown={(event) => { overlayArmed.current = event.target === event.currentTarget }}
      onClick={(event) => {
        const onOverlay = overlayArmed.current && event.target === event.currentTarget
        overlayArmed.current = false
        if (onOverlay) requestClose()
      }}>
      <div ref={panelRef} data-probe="composer-panel"
        role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}
        // Con teclado abierto se usa el alto REAL y se compensa el desplazamiento de
        // iOS. Mismo patrón que el composer del chat, que ya está en producción.
        style={vv.keyboardOpen
          ? { height: `${vv.height}px`, maxHeight: `${vv.height}px`, transform: `translateY(${vv.offsetTop}px)` }
          : undefined}
        className="relative flex h-[100dvh] w-full min-w-0 flex-col overflow-hidden bg-panel shadow-2xl outline-none sm:h-auto sm:max-h-[90dvh] sm:max-w-2xl sm:rounded-xl sm:border sm:border-[var(--border-strong)]">
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2 lg:px-4">
          <h2 id={titleId} className="mr-auto flex min-w-0 items-center gap-2 text-[13px] font-semibold text-fg">
            <MailPlus size={15} className="shrink-0 text-accent" />
            <span className="truncate">Mensaje nuevo</span>
          </h2>
          <button type="button" onClick={requestClose} data-probe="composer-close"
            aria-label="Cerrar mensaje nuevo"
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-fg-muted hover:bg-surface-hover lg:min-h-10 lg:min-w-10">
            <X size={16} />
          </button>
        </div>
        {/* SCROLL INTERNO PROPIO: `min-h-0 flex-1` es lo que permite que el pie con
            el botón de enviar quede fijo y alcanzable con el panel lleno.
            `overflow-x-hidden` no tapa nada: se mide overflow por geometría. */}
        <div data-probe="composer-body" className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-3 lg:p-4">
          {unavailable && <p role="alert" className="mb-2 text-[12px] text-red-400">{unavailable}</p>}
          <div className="grid gap-2 lg:grid-cols-[minmax(220px,1fr)_150px_130px]">
            <div>
              <div
                className={`${chipsFieldCls}${!canPublish || Boolean(unavailable) ? ' opacity-50' : ''}`}
                onMouseDown={(event) => {
                  // Click en el fondo del campo (no en un chip ni en su ✕) enfoca
                  // el input, como cualquier campo de mail.
                  if (event.target !== event.currentTarget) return
                  event.preventDefault()
                  draftRef.current?.focus()
                }}>
                {recipients.map((mailbox) => {
                  const bad = invalidMailboxes.has(mailbox)
                  return (
                    // EL CHIP ENTERO es el área táctil (44px de alto en móvil vía
                    // `min-h-11`, compacto en escritorio vía `lg:min-h-6`) y la ✕ es
                    // sólo la afordancia VISUAL. En escritorio el piso es `lg:min-h-6`
                    // (24px) y NO `lg:min-h-0`: MEDIDO, con `lg:min-h-0` el chip quedaba
                    // en 20,5px y los targets bajo el mínimo WCAG 2.2 AA (24px) subían
                    // de 1 a 10 a 1280px. Así el área de toque y el tamaño
                    // visual quedan desacoplados sin agregar un target de 24px, que
                    // empeoraría la métrica de targets <44 que ya está en producción.
                    <button key={mailbox} type="button" data-chip={mailbox} data-invalid={bad ? 'true' : 'false'}
                      aria-label={`Quitar ${mailbox}`}
                      title={bad ? `Buzón inexistente: ${mailbox}` : `Quitar ${mailbox}`}
                      onClick={() => removeRecipient(mailbox)}
                      className={`flex min-h-11 min-w-0 max-w-full items-center gap-1 rounded px-1.5 py-0.5 text-[11px] lg:min-h-6 ${bad ? 'border border-red-500/50 bg-red-500/15 text-red-300' : 'bg-accent-bg text-fg'}`}>
                      <span className="truncate">{mailbox}</span>
                      <span aria-hidden="true" className="shrink-0 text-fg-muted">✕</span>
                    </button>
                  )
                })}
                <input
                  ref={draftRef}
                  // `min-h-11` también acá: el input es un target por derecho propio y
                  // hoy en producción mide 44px (lo hereda de `inputCls`). Sin esto el
                  // conteo de targets <44 SUBIRÍA respecto de lo desplegado. Y en
                  // escritorio `lg:min-h-6` (24px) y no `lg:min-h-0`: MEDIDO, con 0 el
                  // input quedaba en 18px, bajo el mínimo WCAG 2.2 AA de 24px.
                  className="min-h-11 min-w-0 flex-1 basis-24 bg-transparent text-[12px] text-fg outline-none disabled:opacity-50 lg:min-h-6"
                  value={draft}
                  list="correspondencia-mailboxes"
                  aria-label="Destinatarios"
                  onChange={(event) => {
                    setConfirming(false)
                    const value = event.target.value
                    const inputType = (event.nativeEvent as InputEvent).inputType
                    // Elegir del <datalist> REEMPLAZA todo el valor del input;
                    // Chromium lo reporta como 'insertReplacementText' (no como
                    // tipeo). Ese caso se confirma como chip directamente: es
                    // exactamente el defecto que hacía imposible el 2º destinatario.
                    if (inputType === 'insertReplacementText' || inputType === undefined) {
                      addRecipients(value)
                      return
                    }
                    // Un separador cierra el token en edición. Va en onChange (no
                    // en onKeyDown) para cubrir también teclados móviles e IME.
                    if (/[\s,]/.test(value)) {
                      const tail = value.replace(/^.*[\s,]/, '')
                      addRecipients(value.slice(0, value.length - tail.length))
                      setDraft(tail)
                      return
                    }
                    setDraft(value)
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      // Enter confirma el chip; NUNCA envía el formulario.
                      event.preventDefault()
                      setConfirming(false)
                      addRecipients(draft)
                      return
                    }
                    if (event.key === 'Tab' && draft.trim()) {
                      // Se confirma el chip pero NO se hace preventDefault: el foco
                      // sigue saliendo del campo, que es lo que espera la
                      // navegación por teclado.
                      setConfirming(false)
                      addRecipients(draft)
                      return
                    }
                    if (event.key === 'Backspace' && !draft && recipients.length) {
                      event.preventDefault()
                      removeRecipient(recipients[recipients.length - 1])
                    }
                  }}
                  onPaste={(event) => {
                    const text = event.clipboardData.getData('text')
                    // Sin separadores es un token solo: lo maneja el flujo normal.
                    if (!/[\s,]/.test(text)) return
                    event.preventDefault()
                    setConfirming(false)
                    addRecipients(`${draft}${text}`)
                  }}
                  onBlur={() => {
                    // Texto pendiente al perder el foco se CONFIRMA, no se pierde.
                    if (!draft.trim()) return
                    setConfirming(false)
                    addRecipients(draft)
                  }}
                  placeholder={recipients.length ? 'Agregar otro…' : '@lider/lider, @lider, @lider/*, @lider/agente, @admin'}
                  disabled={!canPublish || Boolean(unavailable)}
                />
              </div>
              <datalist id="correspondencia-mailboxes">
                {roster.options.map((mailbox) => <option key={mailbox} value={mailbox} />)}
              </datalist>
              <p className="mt-1 text-[10px] text-fg-subtle">
                @lider/lider = líder (canónico) · @lider = alias personal · @lider/* = equipo completo · @lider/agente = subagente
              </p>
            </div>
            <select className={inputCls} value={priority}
              onChange={(event) => { setPriority(event.target.value as CorrespondenciaPrioridad); setConfirming(false) }}>
              <option value="urgente">urgente</option><option value="medio">medio</option><option value="bajo">bajo</option>
            </select>
            <select className={inputCls} value={type}
              onChange={(event) => { setType(event.target.value as CorrespondenciaTipo); setConfirming(false) }}>
              <option value="inform">inform</option><option value="request">request</option>
            </select>
          </div>
          <input className={`${inputCls} mt-2`} value={subject}
            onChange={(event) => { setSubject(event.target.value); setConfirming(false) }} placeholder="Asunto" />
          <textarea className={`${inputCls} mt-2 min-h-24 lg:min-h-24`} value={content}
            onChange={(event) => { setContent(event.target.value); setConfirming(false) }} placeholder="Mensaje…" />
          <label className="mt-2 flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-dashed border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted lg:min-h-0 lg:py-2">
            <Paperclip size={14} /> {files.length ? `${files.length} adjunto${files.length === 1 ? '' : 's'}` : 'Adjuntar imágenes o archivos'}
            <input className="sr-only" type="file" multiple onChange={(event) => { setFiles(fileList(event.target.files)); setConfirming(false) }} />
          </label>
          {files.length > 0 && <p className="mt-1 text-[10px] text-fg-faint">{files.map((file) => `${file.name} (${humanBytes(file.size)})`).join(' · ')}</p>}
        </div>
        <div data-probe="composer-footer" className="shrink-0 border-t border-[var(--border-subtle)] p-3 lg:p-4">
          {error && <p role="alert" className="mb-2 text-[12px] text-red-400">{error}</p>}
          {!confirming ? (
            <button type="button" onClick={prepare} disabled={pending || !canPublish || Boolean(unavailable)}
              data-probe="composer-submit"
              className="min-h-11 rounded-md bg-accent px-3 text-[12px] font-semibold text-white disabled:opacity-50 lg:min-h-0 lg:py-2">
              Revisar destinatarios
            </button>
          ) : (
            <AudienceConfirmation audience={audience.expanded} pending={pending}
              onConfirm={() => void send()} onBack={() => setConfirming(false)} />
          )}
        </div>
        {askDiscard && (
          // GUARDA. `alertdialog` porque interrumpe para pedir una decisión, y va
          // sobre el panel (no en el pie) para que no se pueda ignorar.
          <div ref={guardRef} data-probe="composer-guard"
            role="alertdialog" aria-modal="true" aria-labelledby={guardTitleId}
            className="absolute inset-0 z-10 flex items-center justify-center bg-black/75 p-4">
            <div className="w-full max-w-sm rounded-lg border border-amber-500/40 bg-panel p-4 shadow-2xl">
              <p id={guardTitleId} className="text-[13px] font-semibold text-amber-200">
                ¿Descartar el mensaje?
              </p>
              <p className="mt-1 text-[12px] text-fg-muted">
                {recipients.length > 0 && `${recipients.length} destinatario${recipients.length === 1 ? '' : 's'}`}
                {recipients.length > 0 && (subject.trim() || content.trim() || files.length > 0) ? ' · ' : ''}
                {subject.trim() ? 'asunto' : ''}
                {subject.trim() && (content.trim() || files.length > 0) ? ' · ' : ''}
                {content.trim() ? 'mensaje escrito' : ''}
                {content.trim() && files.length > 0 ? ' · ' : ''}
                {files.length > 0 ? `${files.length} adjunto${files.length === 1 ? '' : 's'}` : ''}
                {' — se pierde si cerrás.'}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button ref={keepEditingRef} type="button" onClick={() => setAskDiscard(false)}
                  data-probe="composer-guard-keep"
                  className="min-h-11 rounded-md bg-accent px-3 text-[12px] font-semibold text-white lg:min-h-0 lg:py-2">
                  Seguir editando
                </button>
                <button type="button" onClick={discardAndClose} data-probe="composer-guard-discard"
                  className="min-h-11 rounded-md border border-red-500/50 px-3 text-[12px] text-red-300 lg:min-h-0 lg:py-2">
                  Descartar y cerrar
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )

  return (
    <>
      {trigger}
      {/* Al `body`: dentro del árbol de la bandeja un ancestro con `overflow`,
          `transform` o `filter` recorta o re-ancla un `position:fixed`. */}
      {open && createPortal(dialog, document.body)}
    </>
  )
}

function MessageBody({ message, onLoad, pending, error }: {
  message: CorrespondenciaMensaje
  onLoad: (codigo: string) => void
  pending: boolean
  error?: string
}) {
  const presentation = contentPresentation(message)
  if (presentation.kind === 'full') {
    return <p className="mt-2 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-fg-muted">{presentation.text}</p>
  }
  if (presentation.kind === 'empty') {
    return <p className="mt-2 text-[13px] italic text-fg-faint">{presentation.text}</p>
  }
  return (
    <div className="mt-2 rounded-md border border-dashed border-[var(--border-subtle)] bg-surface/40 p-2">
      {presentation.text && <p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-fg-muted">{presentation.text}</p>}
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-fg-faint">
        <span>{presentation.kind === 'preview' ? 'Vista previa' : 'Contenido no cargado'} · {presentation.length} caracteres</span>
        <button type="button" disabled={pending} onClick={() => onLoad(message.codigo)}
          className="min-h-8 rounded border border-[var(--border-subtle)] px-2 text-[11px] text-fg-muted hover:bg-surface-hover disabled:opacity-50">
          {pending ? 'Cargando…' : 'Ver completo'}
        </button>
        {error && <span role="alert" className="text-red-400">{error}</span>}
      </div>
    </div>
  )
}

function ThreadDetail({ thread, selectedCode, me, participa, onBack, onChanged, onLoadMessage, initialError }: {
  thread: ThreadSummary
  selectedCode: string
  me: Me
  participa: boolean
  onBack: () => void
  onChanged: () => void
  onLoadMessage: (codigo: string) => Promise<void>
  initialError: string
}) {
  const [replying, setReplying] = useState(false)
  const [reply, setReply] = useState('')
  const [replyFiles, setReplyFiles] = useState<File[]>([])
  const [replyType, setReplyType] = useState<CorrespondenciaTipo>('inform')
  const [replyConfirming, setReplyConfirming] = useState(false)
  const [emoji, setEmoji] = useState('❤️')
  const [reason, setReason] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(initialError)
  const [loadingCodes, setLoadingCodes] = useState<Set<string>>(() => new Set())
  const [loadErrors, setLoadErrors] = useState<Record<string, string>>({})
  const initialErrorRef = useRef(initialError)
  // `openThread` mounts the detail before its automatic `/leer` resolves. Adopt
  // only that late parent error; errors created inside this detail stay local and
  // cannot be overwritten by an unrelated parent render.
  useEffect(() => {
    if (initialError && initialError !== initialErrorRef.current) setError(initialError)
    initialErrorRef.current = initialError
  }, [initialError])
  const latch = useRef(false)
  const selected = thread.mensajes.find((message) => message.codigo === selectedCode) ?? thread.mensajes.at(-1)!
  const root = thread.mensajes.find((message) => message.codigo === thread.raiz) ?? thread.mensajes[0]
  const senders = [...new Set(thread.mensajes.map((message) => message.remitente))].sort()
  const replyAudience = root.audiencia_respuesta
  const canWrite = me.can_publish_bus === true && participa

  const act = async (operation: () => Promise<unknown>, onSuccess?: () => void) => {
    if (latch.current) return
    latch.current = true
    setPending(true)
    setError('')
    try {
      await operation()
      onChanged()
      onSuccess?.()
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      latch.current = false
      setPending(false)
    }
  }

  const loadMessage = async (codigo: string) => {
    if (loadingCodes.has(codigo)) return
    setLoadingCodes((current) => new Set(current).add(codigo))
    setLoadErrors((current) => {
      const { [codigo]: _cleared, ...remaining } = current
      return remaining
    })
    try {
      await onLoadMessage(codigo)
    } catch (cause) {
      setLoadErrors((current) => ({ ...current, [codigo]: errorMessage(cause) }))
    } finally {
      setLoadingCodes((current) => {
        const next = new Set(current)
        next.delete(codigo)
        return next
      })
    }
  }

  const prepareReply = () => {
    setError('')
    if (!reply.trim()) return setError('La respuesta no puede estar vacía.')
    setReplyConfirming(true)
  }

  const sendReply = () => {
    if (!reply.trim()) return setError('La respuesta no puede estar vacía.')
    void act(async () => {
      const sent = await responderCorrespondencia({ codigo: selected.codigo, contenido: reply.trim(), tipo: replyType })
      try {
        for (const file of replyFiles) await subirAdjuntoCorrespondencia(sent.codigo, file)
      } catch (cause) {
        throw new Error(`La respuesta ${sent.codigo} fue enviada, pero falló un adjunto: ${errorMessage(cause)}`)
      }
      setReplyFiles([])
      setReply('')
      setReplyConfirming(false)
      setReplying(false)
    })
  }

  const downloadAttachment = (attachment: CorrespondenciaAdjunto) => {
    void act(async () => {
      const blob = await descargarAdjuntoCorrespondencia(attachment.id)
      downloadBlob(blob, safeDownloadName(attachment.nombre))
    })
  }

  const requestReview = () => {
    if (!reason.trim()) return setError('El motivo es obligatorio para solicitar revisión.')
    void act(async () => {
      await solicitarRevisionCorrespondencia(root.codigo, reason.trim())
      setReason('')
    })
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b border-[var(--border-subtle)] p-3 lg:p-4">
        <div className="flex items-start gap-3">
          <button type="button" onClick={onBack} className="flex h-9 w-9 min-h-11 min-w-11 items-center justify-center rounded-md border border-[var(--border-subtle)] text-fg-muted lg:min-h-0 lg:min-w-0">
            <ArrowLeft size={16} />
          </button>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate text-[15px] font-semibold text-fg">{thread.asunto}</h2>
              <span className={`rounded px-2 py-0.5 text-[10px] ${priorityClass(thread.prioridad)}`}>{thread.prioridad}</span>
              <span className="font-mono text-[10px] text-fg-subtle">{thread.raiz}</span>
            </div>
            <div className="mt-2 flex items-start gap-2 text-[11px] text-fg-subtle">
              <Users size={13} className="mt-0.5 shrink-0" />
              <div>
                <p>Remitentes: {senders.join(', ') || '—'}</p>
                <p>Audiencia efectiva si respondés: {replyAudience.join(', ') || 'nadie'}</p>
                <p className="mt-1 text-fg-faint">Los participantes del hilo son inmutables. Para hablar con menos gente, creá un mensaje nuevo.</p>
              </div>
            </div>
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 lg:p-4">
        <ol className="space-y-3">
          {thread.mensajes.map((message) => (
            <li key={message.id} className={`rounded-lg border p-3 ${message.codigo === selected.codigo ? 'border-accent/50 bg-accent-bg' : 'border-[var(--border-subtle)] bg-surface/30'}`}>
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <strong className="text-fg">{message.remitente}</strong>
                <span className="font-mono text-fg-subtle">{message.codigo}</span>
                <span className="rounded bg-surface-hover px-1.5 py-0.5 text-fg-subtle">{message.tipo}</span>
                <span className="ml-auto text-fg-faint">{stamp(message.creado_en)}</span>
              </div>
              <MessageBody message={message} onLoad={(codigo) => void loadMessage(codigo)} pending={loadingCodes.has(message.codigo)} error={loadErrors[message.codigo]} />
              <AttachmentList attachments={message.adjuntos ?? []} participa={participa} onDownload={downloadAttachment} />
              <div className="mt-3 grid gap-2 text-[11px] text-fg-subtle lg:grid-cols-2">
                <p>Reacciones: {message.reacciones.length ? message.reacciones.map((item) => `${item.buzon} ${item.emoji}`).join(' · ') : 'ninguna'}</p>
                <p>Sin reacción: {message.sin_reaccion.length ? message.sin_reaccion.join(', ') : 'nadie'}</p>
              </div>
              {message.revision && <p className="mt-2 rounded bg-violet-500/10 px-2 py-1 text-[11px] text-violet-300">Revisión solicitada por {message.revision.abierto_por}: {message.revision.motivo}</p>}
            </li>
          ))}
        </ol>

        {!participa && (
          <p className="mt-4 rounded-lg border border-[var(--border-subtle)] bg-surface/40 p-3 text-[11px] text-fg-subtle">
            Estás viendo este hilo como administrador y no participás de él: no hay
            acciones porque el servidor las rechaza (404 en OK/lectura/reacción, 403 al
            responder). Los adjuntos aparecen deshabilitados por el mismo motivo.
          </p>
        )}
        {canWrite && accionOfrecida(selected.acciones.ok.disponible, participa) && (
          <section className="mt-4 rounded-lg border border-[var(--border-subtle)] p-3">
            <h3 className="text-[12px] font-semibold text-fg">Acciones sobre {selected.codigo}</h3>
            <p className="mt-1 text-[11px] text-fg-subtle">OK significa procesado y archiva. Una reacción es expresiva y no archiva.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button type="button" disabled={pending} onClick={() => void act(() => okCorrespondencia(selected.codigo), onBack)}
                className="min-h-11 rounded-md bg-emerald-600 px-3 text-[12px] font-semibold text-white disabled:opacity-50 lg:min-h-0 lg:py-2">
                {selected.acciones.ok.etiqueta}
              </button>
              {accionOfrecida(selected.acciones.reaccion_expresiva.disponible, participa) && <>
                <input aria-label="Emoji de reacción" className="w-20 rounded-md border border-[var(--border-subtle)] bg-surface px-2 text-center" value={emoji} onChange={(event) => setEmoji(event.target.value)} />
                <button type="button" disabled={pending || !emoji.trim()} onClick={() => void act(() => reaccionarCorrespondencia(selected.codigo, emoji.trim()))}
                  className="min-h-11 rounded-md border border-[var(--border-subtle)] px-3 text-[12px] text-fg disabled:opacity-50 lg:min-h-0 lg:py-2">
                  {selected.acciones.reaccion_expresiva.etiqueta}
                </button>
                {selected.acciones.reaccion_expresiva.permite_retirar && <button type="button" disabled={pending || !emoji.trim()} onClick={() => void act(() => reaccionarCorrespondencia(selected.codigo, emoji.trim(), true))}
                  className="min-h-11 rounded-md border border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted disabled:opacity-50 lg:min-h-0 lg:py-2">Retirar reacción</button>}
              </>}
            </div>
          </section>
        )}

        {canWrite && root.acciones.solicitar_revision.disponible && (
          <section className="mt-4 rounded-lg border border-violet-500/25 bg-violet-500/5 p-3">
            <h3 className="text-[12px] font-semibold text-violet-200">{root.acciones.solicitar_revision.etiqueta}</h3>
            <p className="mt-1 text-[11px] text-fg-subtle">Este inform está cerrado: no se puede responder hasta abrir una revisión.</p>
            <textarea className={`${inputCls} mt-2`} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Motivo obligatorio" />
            <button type="button" disabled={pending || !reason.trim()} onClick={requestReview}
              className="mt-2 min-h-11 rounded-md bg-violet-600 px-3 text-[12px] font-semibold text-white disabled:opacity-50 lg:min-h-0 lg:py-2">{root.acciones.solicitar_revision.etiqueta}</button>
          </section>
        )}

        {canWrite && root.abierto && (
          <section className="mt-4 rounded-lg border border-[var(--border-subtle)] p-3">
            {!replying ? <button type="button" onClick={() => setReplying(true)} className="min-h-11 rounded-md bg-accent px-3 text-[12px] font-semibold text-white lg:min-h-0 lg:py-2">
              <MessageSquareReply size={14} className="mr-2 inline" />Responder
            </button> : <>
              <h3 className="text-[12px] font-semibold text-fg">Responder</h3>
              <label className="mt-2 block text-[11px] text-fg-subtle">Asunto heredado</label>
              <input className={inputCls} value={thread.asunto} readOnly />
              <label className="mt-2 block text-[11px] text-fg-subtle">Audiencia efectiva de la respuesta (inmutable)</label>
              <div className="rounded-md border border-[var(--border-subtle)] bg-surface px-3 py-2 text-[11px] text-fg-muted">{replyAudience.join(', ') || 'nadie'}</div>
              <p className="mt-1 text-[10px] text-fg-faint">La informa el backend y ya excluye tu propio buzón. Para hablar con menos gente, creá un mensaje nuevo.</p>
              <select className={`${inputCls} mt-2`} value={replyType} onChange={(event) => { setReplyType(event.target.value as CorrespondenciaTipo); setReplyConfirming(false) }}>
                <option value="inform">inform</option><option value="request">request</option>
              </select>
              <textarea className={`${inputCls} mt-2 min-h-24 lg:min-h-24`} value={reply} onChange={(event) => { setReply(event.target.value); setReplyConfirming(false) }} placeholder="Respuesta…" />
              <label className="mt-2 flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-dashed border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted lg:min-h-0 lg:py-2">
                <Paperclip size={14} /> {replyFiles.length ? `${replyFiles.length} adjunto${replyFiles.length === 1 ? '' : 's'}` : 'Adjuntar imágenes o archivos'}
                <input className="sr-only" type="file" multiple onChange={(event) => { setReplyFiles(fileList(event.target.files)); setReplyConfirming(false) }} />
              </label>
              {!replyConfirming ? (
                <div className="mt-2 flex gap-2">
                  <button type="button" disabled={pending || !reply.trim()} onClick={prepareReply} className="min-h-11 rounded-md bg-accent px-3 text-[12px] font-semibold text-white disabled:opacity-50 lg:min-h-0 lg:py-2">Revisar destinatarios</button>
                  <button type="button" disabled={pending} onClick={() => { setReplying(false); setReplyConfirming(false) }} className="min-h-11 rounded-md border border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted lg:min-h-0 lg:py-2">Cancelar</button>
                </div>
              ) : (
                <AudienceConfirmation audience={replyAudience} pending={pending}
                  onConfirm={sendReply} onBack={() => setReplyConfirming(false)} />
              )}
            </>}
          </section>
        )}
        {error && <p role="alert" className="mt-3 text-[12px] text-red-400">{error}</p>}
      </div>
    </div>
  )
}

function ThreadList({ title, threads, empty, onOpen }: {
  title: string
  threads: ThreadSummary[]
  empty: string
  onOpen: (thread: ThreadSummary) => void
}) {
  const summary = (message: CorrespondenciaMensaje) => {
    const presentation = contentPresentation(message)
    return presentation.kind === 'empty'
      ? 'Mensaje vacío.'
      : presentation.kind === 'full'
        ? presentation.text
        : presentation.text || 'Contenido no cargado.'
  }
  return (
    <section>
      <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-fg-subtle">{title}</h2>
      {!threads.length ? <p className="rounded-lg border border-dashed border-[var(--border-subtle)] p-5 text-center text-[12px] text-fg-faint">{empty}</p> : (
        <div className="space-y-2">{threads.map((thread) => {
          const latest = thread.mensajes.at(-1)!
          return <button key={thread.raiz} type="button" onClick={() => onOpen(thread)} className="w-full rounded-lg border border-[var(--border-subtle)] bg-surface/30 p-3 text-left transition-colors hover:bg-surface-hover">
            <div className="flex flex-wrap items-center gap-2">
              <strong className="min-w-0 flex-1 truncate text-[13px] text-fg">{thread.asunto}</strong>
              <span className={`rounded px-1.5 py-0.5 text-[10px] ${priorityClass(thread.prioridad)}`}>{thread.prioridad}</span>
              <span className="rounded bg-surface-hover px-1.5 py-0.5 text-[10px] text-fg-subtle">{thread.mensajes.length}</span>
            </div>
            <p className="mt-1 line-clamp-2 text-[11px] text-fg-subtle"><span className="text-fg-muted">{latest.remitente}:</span> {summary(latest)}</p>
            <p className="mt-1 text-[10px] text-fg-faint">{thread.raiz} · {stamp(latest.creado_en)}</p>
          </button>
        })}</div>
      )}
    </section>
  )
}

export default function CorrespondenciaView({ me }: Props) {
  const [view, setView] = useState<ViewKind>('pendientes')
  const [open, setOpen] = useState<{ raiz: string; selectedCode: string } | null>(null)
  const [openError, setOpenError] = useState('')
  const [loadedMessages, setLoadedMessages] = useState<Record<string, CorrespondenciaMensaje>>({})
  // Every open/close is a new UI selection, including reopening the same code. An
  // old `/leer` may still mark the delivery read or fill its code-keyed cache, but
  // it must never mutate the currently selected detail's error/loading UI.
  const selectionGeneration = useRef(0)
  const messageRequestGeneration = useRef(new Map<string, number>())
  const queryClient = useQueryClient()
  const [scope, setScope] = useState<ScopeCorrespondencia>({ kind: 'propio' })
  // El registro de buzones lo da el SERVIDOR. Antes acá se reimplementaba
  // `individual_mailbox()` con `meta.subagents_by_team`: una segunda fuente de verdad
  // de la identidad, que es justo lo que se vino a unificar.
  const registry = useQuery({
    queryKey: ['correspondencia', 'buzones'],
    queryFn: fetchCorrespondenciaBuzones,
    retry: false,
  })
  // 🔴 TODA la decisión de QUÉ consultar vive en `planBandeja` (lógica pura, medida
  // contra la librería real en tests/test_query_enabled.mjs). Acá abajo sólo queda
  // CABLEADO. La versión anterior escribía la decisión a mano en este componente:
  //     const listoParaConsultar = registry.isSuccess && !delegacionRota
  //     enabled: listoParaConsultar && resuelto.ok
  // y con eso un registro FALLIDO (403 por falta de DELEGATION_ROUTES, o cualquier
  // no-admin: el gate de admin es del servidor) apagaba las CUATRO consultas de bandeja.
  // react-query con `enabled:false` reporta `isLoading:false` y `error:null`, así que la
  // vista no mostraba ni spinner ni error: mostraba Correspondencia VACÍA con contador 0
  // en lugar del buzón del usuario. Ahora DEGRADA a scope propio — se pierde el selector,
  // no el correo — y el cartel de aviso se conserva.
  const plan = planBandeja(
    { isSuccess: registry.isSuccess, data: registry.data },
    scope,
    me.name,
  )
  const esAdmin = plan.esAdmin
  const propio = plan.propio
  const ctxScope = { esAdmin, propio }
  // Lo que de verdad se está mirando. Si el registro no es confiable el plan lo fija en
  // el propio, y la etiqueta tiene que decir eso y no lo que el usuario había elegido.
  const scopeVisible = plan.scopeEfectivo
  // Un 200 no prueba que la delegación llegó: el plan cruza la identidad que respondió
  // el servidor contra la de la sesión. Si no coincide, la ruta no está delegada.
  const delegacionRota = plan.avisoDelegacion
  const registryError = registry.error ? errorMessage(registry.error) : ''
  const buzonPropio = plan.propioResuelto.ok ? plan.propioResuelto.buzon : ''
  const buzonActivo = plan.activoResuelto.ok ? plan.activoResuelto.buzon : ''
  // Si esto trae texto, la lista NO se consultó y hay que decirlo en pantalla: «no
  // pregunté» y «pregunté y no hay nada» no pueden verse igual.
  const sinConsultar = motivoSinConsultar(plan)

  // Dos pares de consultas: MÍAS (fijas) y del scope ACTIVO. Cuando el scope es el
  // propio las claves coinciden y react-query las unifica en una sola request; sólo
  // hay dos cuando de verdad se está mirando otra bandeja. Así el contador de
  // pendientes propio nunca se convierte en "todos los pendientes de todos".
  const ownPending = useQuery({
    queryKey: ['correspondencia', 'pendientes', claveScope(plan.propioResuelto)],
    queryFn: () => fetchCorrespondenciaBandeja(false, buzonPropio, false),
    enabled: plan.consultarPropio,
  })
  const ownArchive = useQuery({
    queryKey: ['correspondencia', 'archivo', claveScope(plan.propioResuelto)],
    queryFn: () => fetchCorrespondenciaBandeja(true, buzonPropio, false),
    enabled: plan.consultarPropio,
  })
  const pending = useQuery({
    queryKey: ['correspondencia', 'pendientes', claveScope(plan.activoResuelto)],
    queryFn: () => fetchCorrespondenciaBandeja(false, buzonActivo, false),
    enabled: plan.consultarActivo,
  })
  const archive = useQuery({
    queryKey: ['correspondencia', 'archivo', claveScope(plan.activoResuelto)],
    queryFn: () => fetchCorrespondenciaBandeja(true, buzonActivo, false),
    enabled: plan.consultarActivo,
  })
  // Participación según el predicado real del servidor (remitente O con entrega),
  // alimentada SÓLO por las consultas propias. Una vista ajena o global no puede
  // aportar acá: sus `items` son entregas de otros.
  const participadas = useMemo(
    () => raicesParticipadas([ownPending.data ?? {}, ownArchive.data ?? {}]),
    [ownPending.data, ownArchive.data],
  )
  const scopeMio = scopeEsPropio(scopeVisible, ctxScope)

  const hydrate = (messages: CorrespondenciaMensaje[]) =>
    messages.map((message) => loadedMessages[message.codigo] ?? message)
  const allMessages = useMemo(() => hydrate(dedupeMessages([
    pending.data?.items ?? [], pending.data?.enviados ?? [], archive.data?.items ?? [], archive.data?.enviados ?? [],
  ])), [pending.data, archive.data, loadedMessages])
  const allThreads = useMemo(() => groupThreads(allMessages), [allMessages])
  const pendingThreads = useMemo(() => groupThreads(hydrate(dedupeMessages([pending.data?.items ?? []]))), [pending.data, loadedMessages])
  const archiveThreads = useMemo(() => groupThreads(hydrate(dedupeMessages([archive.data?.items ?? []]))), [archive.data, loadedMessages])
  const sentThreads = useMemo(() => groupThreads(hydrate(dedupeMessages([pending.data?.enviados ?? [], archive.data?.enviados ?? []]))), [pending.data, archive.data, loadedMessages])
  const opened = open ? allThreads.find((thread) => thread.raiz === open.raiz) : undefined

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['correspondencia'] })
  }

  const loadFullMessage = async (codigo: string) => {
    const requestGeneration = (messageRequestGeneration.current.get(codigo) ?? 0) + 1
    messageRequestGeneration.current.set(codigo, requestGeneration)
    const result = await leerCorrespondencia(codigo)
    // A newer request for this exact code won; never let an out-of-order old body
    // overwrite it. Different codes may safely keep their own cache entries.
    if (messageRequestGeneration.current.get(codigo) === requestGeneration) {
      setLoadedMessages((current) => ({ ...current, [result.mensaje.codigo]: result.mensaje }))
    }
  }

  const closeThread = () => {
    selectionGeneration.current += 1
    setOpenError('')
    setOpen(null)
  }

  const openThread = async (thread: ThreadSummary) => {
    const selection = ++selectionGeneration.current
    const selected = thread.mensajes.at(-1)!
    setOpenError('')
    setOpen({ raiz: thread.raiz, selectedCode: selected.codigo })
    if (selected.buzon && !selected.archivado_en && participadas.has(thread.raiz)) {
      try {
        await loadFullMessage(selected.codigo)
        if (selectionGeneration.current !== selection) return
        invalidate()
      } catch (cause) {
        if (selectionGeneration.current !== selection) return
        setOpenError(errorMessage(cause))
      }
    }
  }

  if (opened && open) {
    return <ThreadDetail key={`${open.raiz}:${open.selectedCode}:${selectionGeneration.current}`} thread={opened} selectedCode={open.selectedCode} me={me} participa={participadas.has(opened.raiz)} onBack={closeThread} onChanged={invalidate} onLoadMessage={loadFullMessage} initialError={openError} />
  }

  const active = view === 'pendientes' ? pending : archive
  const activeThreads = view === 'pendientes' ? pendingThreads : archiveThreads
  const loading = pending.isLoading || archive.isLoading
  const fetchError = active.error

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-strong)] px-3 py-2 lg:px-5 lg:py-3">
        <h1 className="mr-auto flex items-center gap-2 text-sm font-semibold text-fg"><Inbox size={16} className="text-accent" /> Correspondencia</h1>
        <span className={`rounded px-2 py-1 text-[10px] ${scopeMio ? 'bg-surface-hover text-fg-subtle' : 'bg-amber-500/15 text-amber-300'}`}>
          {scopeEtiqueta(scopeVisible, ctxScope)}
        </span>
        {esAdmin && !delegacionRota && (
          <select aria-label="Buzón a mirar" value={scope.kind === 'buzon' ? scope.buzon : scope.kind}
            onChange={(event) => {
              const value = event.target.value
              setOpen(null)
              if (value === 'propio' || value === 'global') setScope({ kind: value })
              else setScope({ kind: 'buzon', buzon: value })
            }}
            className="min-h-11 rounded-md border border-[var(--border-subtle)] bg-surface px-2 text-[11px] text-fg-muted lg:min-h-10">
            <option value="propio">Mi buzón ({propio || '…'})</option>
            <option value="global">Todos los buzones (global)</option>
            {(registry.data?.buzones ?? []).map((item) => (
              <option key={item.buzon} value={item.buzon}>{item.buzon}{item.propio ? ' (yo)' : ''}</option>
            ))}
          </select>
        )}
        <NewMessageComposer canPublish={me.can_publish_bus === true} adminUser={me.admin_user} onDone={invalidate} />
        <button type="button" onClick={() => { void pending.refetch(); void archive.refetch() }} className="flex min-h-11 items-center gap-2 rounded-md border border-[var(--border-subtle)] px-3 text-[11px] text-fg-muted lg:min-h-10">
          <RefreshCw size={13} className={pending.isFetching || archive.isFetching ? 'animate-spin' : ''} /> Recargar
        </button>
      </header>
      {(delegacionRota || registryError) && (
        <p role="alert" className="shrink-0 border-b border-amber-500/25 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200 lg:px-5">
          {delegacionRota || registryError}
        </p>
      )}
      <nav className="flex shrink-0 gap-2 border-b border-[var(--border-subtle)] px-3 py-2 lg:px-5">
        <button type="button" onClick={() => setView('pendientes')} className={`min-h-11 rounded-md px-3 py-2 text-[12px] lg:min-h-0 ${view === 'pendientes' ? 'bg-accent text-white' : 'bg-surface text-fg-muted'}`}>
          <Inbox size={13} className="mr-1.5 inline" /> Pendientes {pending.data?.count ?? 0}
        </button>
        <button type="button" onClick={() => setView('archivo')} className={`min-h-11 rounded-md px-3 py-2 text-[12px] lg:min-h-0 ${view === 'archivo' ? 'bg-accent text-white' : 'bg-surface text-fg-muted'}`}>
          <Archive size={13} className="mr-1.5 inline" /> Archivo {archive.data?.count ?? 0}
        </button>
        {!scopeMio && (
          <span className="ml-auto self-center rounded bg-surface px-2 py-1 text-[11px] text-fg-subtle">
            Míos pendientes: {ownPending.data?.count ?? 0}
          </span>
        )}
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto p-3 lg:p-5">
        {sinConsultar ? (
          <p role="alert" className="rounded-lg border border-amber-500/25 bg-amber-500/10 p-4 text-[12px] text-amber-200">
            {sinConsultar}
          </p>
        ) : loading ? <div className="flex justify-center py-16"><Loader2 className="animate-spin text-accent" /></div> : fetchError ? (
          <p role="alert" className="rounded-lg border border-red-500/25 bg-red-500/10 p-4 text-[12px] text-red-300">{errorMessage(fetchError)}</p>
        ) : <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,2fr)_minmax(280px,1fr)]">
          <div>
            <ThreadList title={`${view === 'pendientes' ? 'Pendientes' : 'Archivo'} · ${scopeEtiqueta(scopeVisible, ctxScope)} (${active.data?.count ?? 0})`} threads={activeThreads} empty={view === 'pendientes' ? 'No hay mensajes pendientes.' : 'El archivo está vacío.'} onOpen={(thread) => void openThread(thread)} />
            {openError && <p role="alert" className="mt-3 text-[12px] text-red-400">{openError}</p>}
          </div>
          <ThreadList title={enviadosTitulo(propio, sentThreads.length)} threads={sentThreads} empty="No hay mensajes enviados." onOpen={(thread) => { setOpen({ raiz: thread.raiz, selectedCode: thread.mensajes.at(-1)!.codigo }) }} />
        </div>}
      </div>
      <footer className="shrink-0 border-t border-[var(--border-subtle)] px-3 py-2 text-[10px] text-fg-faint lg:px-5">
        <CheckCircle2 size={11} className="mr-1 inline" /> Abrir sólo registra lectura; el mensaje sale de Pendientes únicamente con OK o al responder.
        <Send size={11} className="ml-3 mr-1 inline" /> Las respuestas conservan todos los participantes del hilo.
      </footer>
    </div>
  )
}
