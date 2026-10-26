import { useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Radio,
  Loader2,
  ShieldAlert,
  MessageSquare,
  Users,
  ArrowLeft,
  Link2,
  RefreshCw,
  Unlink,
  Search,
  FilterX,
  Filter,
  ArrowDownWideNarrow,
  ArrowUpWideNarrow,
} from 'lucide-react'
import {
  fetchBusConversations,
  fetchBusThread,
  fetchBusGroup,
  fetchMeta,
  fetchMe,
  publishBus,
  getBusPublishEnvironmentError,
  BusPublishUncertainError,
  ApiError,
  type BusConversationItem,
  type BusFilters,
  type BusMessage,
  type BusPublishInput,
  type BusPublishResponse,
} from '../lib/api'
import { TZ } from './taskShared'

/**
 * ISO-8601 (UTC) → 'YYYY-MM-DD HH:MM' en la zona del panel (VITE_TIMEZONE o la del navegador).
 *
 * BUG CORREGIDO : antes hacia `iso.slice(0,16).replace('T',' ')`, que TRUNCA
 * el texto en vez de CONVERTIR la zona. Todo el backend corre en UTC (host Etc/UTC,
 * postgres UTC, contenedor sin TZ), asi que se mostraba la hora UTC cruda: un mensaje
 * de las 09:30 locales aparecia como 13:30 (UTC-4). Cortar un string no es
 * convertir una fecha.
 *
 * Se usa el MISMO helper que el resto de Sinapsis (`TZ` de taskShared) para que no haya
 * dos verdades sobre la hora en la misma app: esta vista era la unica que no lo hacia.
 * `Intl.DateTimeFormat` maneja el horario de verano solo — Chile cambia de UTC-4 a UTC-3,
 * asi que un offset fijo (-4) volveria a estar mal medio ano.
 */
function stamp(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: TZ,
  }).formatToParts(d).reduce<Record<string, string>>((a, x) => {
    a[x.type] = x.value
    return a
  }, {})
  if (!p.year) return ''
  // hour12:false puede devolver '24' a medianoche en algunos runtimes; se normaliza a '00'.
  const hh = p.hour === '24' ? '00' : p.hour
  return `${p.year}-${p.month}-${p.day} ${hh}:${p.minute}`
}

/** Chip del performative (request/inform/…): color estable, sin paleta nueva. */
function performativeChip(p?: string | null): string {
  const v = (p || '').toLowerCase()
  if (v === 'request') return 'bg-amber-500/15 text-amber-300'
  if (v === 'inform') return 'bg-sky-500/15 text-sky-300'
  if (v === 'propose') return 'bg-violet-500/15 text-violet-300'
  return 'bg-surface-hover text-fg-subtle'
}

/**
 * Mensaje de error EXPLÍCITO. El 403 se distingue a propósito de "no hay hilos":
 * una lista vacía silenciosa sería indistinguible de "no podés verlo".
 */
function ErrorState({ error }: { error: unknown }) {
  const forbidden = error instanceof ApiError && error.status === 403
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
        <ShieldAlert size={24} className={forbidden ? 'text-red-400' : 'text-fg-faint'} />
      </div>
      {forbidden ? (
        <>
          <p className="text-[13px] font-medium text-red-400">No autorizado</p>
          <p className="max-w-[420px] text-[12px] text-fg-subtle">
            El bus de la flota lo lee únicamente el lector humano autorizado. Tu
            sesión no está habilitada para ver este contenido.
          </p>
        </>
      ) : (
        <>
          <p className="text-[13px] font-medium text-fg-muted">
            No se pudo leer el bus
          </p>
          <p className="max-w-[420px] text-[12px] text-fg-subtle">
            {error instanceof Error ? error.message : 'Error desconocido'}
          </p>
        </>
      )}
    </div>
  )
}

/**
 * Lista de mensajes. El backend los devuelve en orden cronológico ASCENDENTE (el más
 * antiguo primero), que es lo correcto para leer un hilo como conversación.
 *
 * `newestFirst` invierte la vista SIN pedir de nuevo al servidor (pedido de admin
 * : poder ver lo más reciente arriba también DENTRO de los mensajes).
 * Se copia el array antes de invertir porque `.reverse()` MUTA: aplicado directo sobre
 * el array de react-query estaría mutando la caché, y el resultado dependería de cuántas
 * veces se renderizó el componente — un bug que no se ve hasta que alguien toca el botón
 * dos veces.
 */
function MessageList({
  messages,
  newestFirst,
}: {
  messages: BusMessage[]
  newestFirst: boolean
}) {
  const ordered = newestFirst ? [...messages].reverse() : messages
  return (
    <ol className="flex flex-col gap-3">
      {ordered.map((m) => (
        <li
          key={m.id}
          className="rounded-lg border border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] p-3"
        >
          <div className="mb-1.5 flex flex-wrap items-center gap-2">
            <span className="text-[12px] font-medium text-fg">{m.sender}</span>
            {m.recipients.length > 0 && (
              <span className="text-[11px] text-fg-subtle">
                → {m.recipients.join(', ')}
              </span>
            )}
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${performativeChip(
                m.performative
              )}`}
            >
              {m.performative}
            </span>
            <span className="ml-auto text-[10px] text-fg-subtle tabular-nums">
              {stamp(m.timestamp)}
            </span>
          </div>
          {/*
            El cuerpo puede venir en `null`: desde  el gateway no proyecta
            `content` salvo que se pida include_content=true. Las dos vistas de detalle lo
            piden, así que el caso normal acá es texto completo; el fallback al preview
            existe para que un cambio de default NO deje la conversación en blanco ni
            imprima "null", y para que se vea que falta cuerpo en vez de simularlo.
          */}
          <p className="whitespace-pre-wrap break-words text-[12px] leading-relaxed text-fg-muted">
            {m.content ?? m.content_preview ?? ''}
            {m.content === null && m.content_truncated ? '…' : ''}
          </p>
          {m.content === null && (
            <p className="mt-1 text-[10px] text-amber-300/80">
              cuerpo no incluido en la respuesta ({m.content_len ?? 0} caracteres)
            </p>
          )}
          {m.refs_task && (
            <p className="mt-2 inline-flex items-center gap-1 font-mono text-[10px] text-fg-subtle">
              <Link2 size={10} className="text-fg-faint" />
              tarea {m.refs_task.slice(0, 8)}
            </p>
          )}
        </li>
      ))}
    </ol>
  )
}

interface BusComposerProps {
  suggestedRecipients?: string[]
  conversationId?: string
  pending: boolean
  unavailableReason: string
  onSend: (input: BusPublishInput) => Promise<BusPublishResponse | null>
}

/** Un solo compositor para mensaje nuevo y respuesta en hilo. No se monta si /me
 * no afirma can_publish_bus === true. La confirmación enumera la audiencia completa. */
function BusComposer({
  suggestedRecipients = [],
  conversationId = '',
  pending,
  unavailableReason,
  onSend,
}: BusComposerProps) {
  const [recipients, setRecipients] = useState(suggestedRecipients.join(', '))
  const [content, setContent] = useState('')
  const [performative, setPerformative] = useState<'inform' | 'request'>('inform')
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const normalized = useMemo(
    () => [...new Set(recipients.split(',').map((x) => x.trim().toLowerCase()).filter(Boolean))].sort(),
    [recipients]
  )
  const disabled = pending || Boolean(unavailableReason)

  const prepare = () => {
    if (disabled) return
    setError('')
    setSuccess('')
    if (!normalized.length) {
      setError('Elegí al menos un destinatario.')
      return
    }
    if (!content.trim()) {
      setError('El mensaje no puede estar vacío.')
      return
    }
    setConfirming(true)
  }

  const send = async () => {
    if (disabled) return
    setError('')
    setSuccess('')
    try {
      const result = await onSend({
        recipients: normalized,
        content,
        performative,
        conversation_id: conversationId,
      })
      if (!result) return
      setConfirming(false)
      setContent('')
      setSuccess(
        result.deduplicated
          ? `Intento ya recibido; no se duplicó. Mensaje ${result.id.slice(0, 8)}.`
          : `Mensaje ${result.id.slice(0, 8)} enviado a ${result.recipients.join(', ')}.`
      )
    } catch (cause) {
      if (cause instanceof BusPublishUncertainError) {
        setError(cause.message)
      } else if (cause instanceof ApiError && (cause.status === 403 || cause.status === 422)) {
        setError(`Error ${cause.status}: ${cause.message}`)
      } else {
        setError(cause instanceof Error ? cause.message : 'No se pudo enviar el mensaje.')
      }
    }
  }

  return (
    <section className="shrink-0 border-b border-[var(--border-subtle)] bg-surface/40 px-3 py-3 lg:px-5">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <strong className="text-[12px] text-fg">
          {conversationId ? 'Responder en este hilo' : 'Publicar mensaje nuevo'}
        </strong>
        {conversationId && (
          <span className="font-mono text-[10px] text-fg-subtle">hilo {conversationId}</span>
        )}
      </div>
      {unavailableReason && (
        <p role="alert" className="mb-2 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-300">
          {unavailableReason}
        </p>
      )}
      <div className="grid gap-2 lg:grid-cols-[minmax(180px,1fr)_120px]">
        <input
          value={recipients}
          onChange={(e) => { setRecipients(e.target.value); setConfirming(false) }}
          placeholder="destinatarios, separados por coma"
          className={inputCls}
          disabled={disabled}
        />
        <select
          value={performative}
          onChange={(e) => { setPerformative(e.target.value as 'inform' | 'request'); setConfirming(false) }}
          className={inputCls}
          disabled={disabled}
        >
          <option value="inform">inform</option>
          <option value="request">request</option>
        </select>
      </div>
      <textarea
        value={content}
        onChange={(e) => { setContent(e.target.value); setConfirming(false) }}
        placeholder="Mensaje para la flota…"
        rows={3}
        className="mt-2 w-full rounded-md border border-[var(--border-subtle)] bg-surface px-3 py-2 text-[13px] text-fg outline-none focus:border-[rgba(255,255,255,0.2)]"
        disabled={disabled}
      />
      {!confirming ? (
        <button
          type="button"
          onClick={prepare}
          disabled={disabled}
          className="mt-2 min-h-[44px] rounded-md bg-accent px-3 text-[12px] font-medium text-white disabled:opacity-50 lg:min-h-0 lg:py-1.5"
        >
          Revisar destinatarios
        </button>
      ) : (
        <div className="mt-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3">
          <p className="text-[12px] font-semibold text-amber-200">
            Se enviará a {normalized.length} agente{normalized.length === 1 ? '' : 's'}:
            {' '}{normalized.join(', ')}
          </p>
          <p className="mt-1 text-[11px] text-amber-100/80">
            La flota puede interpretar un mensaje del admin como una orden. Confirmá la audiencia.
          </p>
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={() => void send()}
              disabled={disabled}
              className="min-h-[44px] rounded-md bg-amber-500 px-3 text-[12px] font-semibold text-black disabled:opacity-50 lg:min-h-0 lg:py-1.5"
            >
              {pending ? 'Enviando…' : `Confirmar envío a ${normalized.length}`}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              disabled={disabled}
              className="min-h-[44px] rounded-md border border-[var(--border-subtle)] px-3 text-[12px] text-fg-muted lg:min-h-0 lg:py-1.5"
            >
              Volver
            </button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-[12px] text-red-400">{error}</p>}
      {success && <p role="status" className="mt-2 text-[12px] text-emerald-400">{success}</p>}
    </section>
  )
}

/** Cabecera común del detalle (volver + identificador + recargar). */
function DetailHeader({
  onBack,
  title,
  badge,
  isFetching,
  refetch,
}: {
  onBack: () => void
  title: string
  badge: ReactNode
  isFetching: boolean
  refetch: () => void
}) {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-strong)] px-3 py-2 lg:gap-3 lg:px-5 lg:py-3">
      {/* En movil este boton ES la navegacion hacia atras de la vista: tiene que
          ser tocable (44px de alto). En lg vuelve al tamano compacto. */}
      <button
        onClick={onBack}
        className="flex min-h-[44px] items-center gap-1.5 rounded-md border border-[var(--border-subtle)] px-3 py-1 text-[12px] text-fg-muted transition hover:bg-surface-hover lg:min-h-0 lg:px-2"
      >
        <ArrowLeft size={13} /> Conversaciones
      </button>
      {badge}
      {/* basis-full en movil: compartiendo fila con el boton "Conversaciones" y
          el badge, el conversation_id quedaba en unos pocos px y truncado a
          nada. En lg vuelve a compartir la fila como hoy. */}
      <h1 className="order-last min-w-0 basis-full truncate font-mono text-[13px] text-fg lg:order-none lg:basis-auto lg:flex-1">{title}</h1>
      <button
        onClick={refetch}
        title="Recargar"
        aria-label="Recargar"
        className="flex h-11 w-11 shrink-0 items-center justify-center text-fg-faint transition hover:text-fg lg:h-auto lg:w-auto"
      >
        {isFetching ? (
          <Loader2 size={13} className="animate-spin" />
        ) : (
          <RefreshCw size={13} />
        )}
      </button>
    </div>
  )
}

/** Replay cronológico de UN hilo DECLARADO (tiene conversation_id). */
function ThreadDetail({
  conversationId,
  newestFirst,
  onBack,
  composer,
}: {
  conversationId: string
  newestFirst: boolean
  onBack: () => void
  composer?: ReactNode
}) {
  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['bus-thread', conversationId],
    // includeContent=true: abrir un hilo ES la lectura del cuerpo. El default del gateway
    // es metadata, así que sin este flag la vista quedaría sin texto. Queda asentado en
    // audit_log como lectura deliberada — es exactamente el efecto buscado.
    queryFn: () => fetchBusThread(conversationId, true),
  })

  return (
    <div className="flex h-full flex-col">
      <DetailHeader
        onBack={onBack}
        title={conversationId}
        isFetching={isFetching}
        refetch={() => void refetch()}
        badge={
          <span className="inline-flex shrink-0 items-center gap-1 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent">
            <Radio size={10} /> Hilo
          </span>
        }
      />
      {composer}

      <div className="min-h-0 flex-1 overflow-auto p-3 lg:p-5">
        {isLoading && <p className="text-xs text-fg-subtle">Cargando el hilo…</p>}
        {isError && <ErrorState error={error} />}
        {!isLoading && !isError && data && (
          <>
            {data.error && (
              <p className="mb-4 text-[12px] text-amber-300">{data.error}</p>
            )}
            <div className="mb-4 flex flex-wrap items-center gap-3 text-[11px] text-fg-subtle">
              <span className="inline-flex items-center gap-1 tabular-nums">
                <MessageSquare size={11} className="text-fg-faint" />
                {data.count} mensaje{data.count === 1 ? '' : 's'}
              </span>
              {data.participants.length > 0 && (
                <span className="inline-flex items-center gap-1">
                  <Users size={11} className="text-fg-faint" />
                  {data.participants.join(', ')}
                </span>
              )}
              {data.started_at && (
                <span className="tabular-nums">desde {stamp(data.started_at)}</span>
              )}
            </div>

            {/* Cronológico ascendente: el gateway ya ordena por created_at ASC. */}
            <MessageList messages={data.messages} newestFirst={newestFirst} />

            {data.messages.length === 0 && !data.error && (
              <p className="text-[12px] text-fg-subtle">El hilo no tiene mensajes.</p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Replay de una conversación IMPLÍCITA: mensajes SIN conversation_id agrupados por
 * participantes. Se rotula explícitamente como reconstruida para que no se confunda
 * con un hilo declarado por los agentes.
 */
function GroupDetail({
  participantsKey,
  filters,
  newestFirst,
  onBack,
}: {
  participantsKey: string
  filters: BusFilters
  newestFirst: boolean
  onBack: () => void
}) {
  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['bus-group', participantsKey, filters],
    // includeContent=true por el mismo motivo que ThreadDetail: esta vista existe para
    // LEER la conversación. El 200 del limit se mantiene explicito para no cambiar de
    // comportamiento al agregar el cuarto parametro.
    queryFn: () => fetchBusGroup(participantsKey, filters, 200, true),
  })

  return (
    <div className="flex h-full flex-col">
      <DetailHeader
        onBack={onBack}
        title={participantsKey.split('|').join(' · ')}
        isFetching={isFetching}
        refetch={() => void refetch()}
        badge={
          <span className="inline-flex shrink-0 items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
            <Unlink size={10} /> Sin hilo
          </span>
        }
      />

      <div className="min-h-0 flex-1 overflow-auto p-3 lg:p-5">
        {isLoading && (
          <p className="text-xs text-fg-subtle">Reconstruyendo la conversación…</p>
        )}
        {isError && <ErrorState error={error} />}
        {!isLoading && !isError && data && (
          <>
            <p className="mb-3 rounded-md border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-[11px] text-amber-200/90">
              Conversación <strong>reconstruida</strong>: estos mensajes se publicaron
              sin <code className="font-mono">conversation_id</code>. No son un hilo
              declarado — se agrupan por participantes.
            </p>
            {data.error && (
              <p className="mb-4 text-[12px] text-amber-300">{data.error}</p>
            )}
            <div className="mb-4 flex flex-wrap items-center gap-3 text-[11px] text-fg-subtle">
              <span className="inline-flex items-center gap-1 tabular-nums">
                <MessageSquare size={11} className="text-fg-faint" />
                {data.count} mensaje{data.count === 1 ? '' : 's'}
              </span>
              {data.participants.length > 0 && (
                <span className="inline-flex items-center gap-1">
                  <Users size={11} className="text-fg-faint" />
                  {data.participants.join(', ')}
                </span>
              )}
              {data.started_at && (
                <span className="tabular-nums">desde {stamp(data.started_at)}</span>
              )}
            </div>

            <MessageList messages={data.messages} newestFirst={newestFirst} />

            {data.messages.length === 0 && !data.error && (
              <p className="text-[12px] text-fg-subtle">
                No hay mensajes sin hilo para estos participantes.
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Fila de la lista combinada. El tipo viene del backend (`item.kind`) y se distingue
 * de un vistazo: ícono, color de borde y etiqueta.
 *   - hilo declarado → ícono Radio (acento), etiqueta 'hilo', muestra el conversation_id.
 *   - implícito      → ícono Unlink (ámbar), etiqueta 'sin hilo · agrupado por
 *                      participantes', muestra los participantes.
 */
function ConversationRow({
  item,
  onOpen,
}: {
  item: BusConversationItem
  onOpen: () => void
}) {
  const implicit = item.kind === 'implicit'
  const title = implicit
    ? (item.participants_key || '').split('|').join(' · ')
    : item.conversation_id || ''
  return (
    <button
      onClick={onOpen}
      className={`group flex min-h-[44px] flex-col gap-1.5 rounded-lg border p-3 text-left transition ${
        implicit
          ? 'border-amber-500/25 bg-amber-500/[0.04] hover:border-amber-400/45 hover:bg-amber-500/[0.07]'
          : 'border-[var(--border-subtle)] bg-[rgba(255,255,255,0.02)] hover:border-[rgba(255,255,255,0.12)] hover:bg-[rgba(255,255,255,0.03)]'
      }`}
    >
      <div className="flex items-start gap-2">
        {implicit ? (
          <Unlink size={14} className="mt-0.5 shrink-0 text-amber-300" />
        ) : (
          <Radio size={14} className="mt-0.5 shrink-0 text-accent" />
        )}
        <span
          className={`min-w-0 flex-1 break-words text-[12px] lg:truncate ${
            implicit
              ? 'font-medium text-amber-100/90'
              : 'font-mono text-fg-muted group-hover:text-fg'
          }`}
        >
          {title}
        </span>
        <span className="shrink-0 text-[10px] text-fg-subtle tabular-nums">
          {stamp(item.last_at)}
        </span>
      </div>

      {/* Etiqueta de tipo: la diferencia tiene que ser evidente, no tipográfica. */}
      <div className="flex flex-wrap items-center gap-2">
        {implicit ? (
          <span className="inline-flex items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
            <Unlink size={9} /> sin hilo · agrupado por participantes
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent">
            <Radio size={9} /> hilo · conversation_id
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3 text-[11px] text-fg-subtle">
        <span className="inline-flex items-center gap-1 tabular-nums">
          <MessageSquare size={11} className="text-fg-faint" />
          {item.messages}
        </span>
        {item.participants.length > 0 && (
          <span className="inline-flex min-w-0 items-center gap-1">
            <Users size={11} className="text-fg-faint" />
            <span className="truncate">{item.participants.join(', ')}</span>
          </span>
        )}
      </div>
      {item.last_message && (
        /*
          Preview de la LISTA: el gateway ya lo acota a ~120 chars y acá NO se pide el
          cuerpo completo a proposito (listar no es leer). El '…' sale de
          `content_truncated`, que lo dice el backend con el largo real — no se infiere del
          largo del string recibido, que ya viene cortado.
        */
        <p className="line-clamp-2 text-[11px] leading-snug text-fg-subtle/80">
          <span className="text-fg-subtle">{item.last_message.sender}:</span>{' '}
          {item.last_message.content}
          {item.last_message.content_truncated ? '…' : ''}
        </p>
      )}
    </button>
  )
}

const EMPTY_FILTERS: BusFilters = { agent: '', project: '', since: '', until: '', q: '' }

function hasAnyFilter(f: BusFilters): boolean {
  return Boolean(f.agent || f.project || f.since || f.until || f.q)
}

const inputCls =
  // text-[16px] en movil: por debajo de 16px Safari iOS hace zoom automatico al
  // enfocar el control. min-h-[44px] para que sea tocable. En lg vuelve a 11px,
  // que es la densidad que tiene hoy la barra de filtros en escritorio.
  'min-h-[44px] rounded-md border border-[var(--border-subtle)] bg-surface px-2 py-1 text-[16px] text-fg-muted outline-none transition focus:border-[rgba(255,255,255,0.2)] lg:min-h-0 lg:text-[11px]'

/**
 * Vista "Bus": UNA sola sección con las dos clases de conversación del bus,
 * intercaladas por recencia y diferenciadas visualmente:
 *   - hilos DECLARADOS (con conversation_id) → GET /api/bus/thread
 *   - conversaciones IMPLÍCITAS (sin conversation_id, agrupadas por participantes)
 *     → GET /api/bus/group
 * El tipo lo decide el BACKEND (campo `kind`): acá no se infiere de "¿tiene
 * conversation_id?" porque ese campo se persiste como cadena vacía.
 *
 * Filtros (AND): agente, proyecto (vía la tarea referenciada), rango de fechas y
 * texto libre. La búsqueda es COINCIDENCIA DE TEXTO, no semántica: el bus no está
 * indexado vectorialmente.
 *
 * La lectura conserva _require_bus_reader. El compositor aparece únicamente si /me
 * devuelve can_publish_bus === true; el permiso real lo aplica el BFF con 403.
 */
export default function BusView() {
  const [open, setOpen] = useState<BusConversationItem | null>(null)
  // Orden de la vista. `true` = más reciente primero, y es el DEFAULT porque es lo que
  // se quiere al abrir el bus: ver qué pasó último. Vive acá (no en cada panel) para que
  // el orden sea uno solo en toda la sección: la lista y el detalle no pueden discrepar.
  const [newestFirst, setNewestFirst] = useState(true)
  // `draft` es lo que se tipea; `filters` es lo aplicado (lo que viaja al backend).
  const [draft, setDraft] = useState<BusFilters>(EMPTY_FILTERS)
  const [filters, setFilters] = useState<BusFilters>(EMPTY_FILTERS)
  const publishLatch = useRef(false)
  const [publishPending, setPublishPending] = useState(false)
  // Capturada al montar: el bloqueo se conoce antes de que admin redacte el mensaje.
  const [publishUnavailableReason] = useState(() => getBusPublishEnvironmentError())
  const queryClient = useQueryClient()
  // Observador sólo consumidor: App monta la misma query y hace el fetch. enabled:false evita que
  // montar BusView agregue un GET cuando esa caché compartida está ausente o stale.
  const me = useQuery({ queryKey: ['me'], queryFn: fetchMe, enabled: false })

  // Latch en el padre común: sobrevive al desmontaje del compositor de hilo al volver
  // a la lista. try/finally lo libera incluso ante timeout/error.
  const sendBusMessage = async (input: BusPublishInput): Promise<BusPublishResponse | null> => {
    if (publishLatch.current) return null
    publishLatch.current = true
    setPublishPending(true)
    try {
      const result = await publishBus(input)
      void queryClient.invalidateQueries({ queryKey: ['bus-conversations'] })
      if (input.conversation_id) {
        void queryClient.invalidateQueries({ queryKey: ['bus-thread', input.conversation_id] })
      }
      return result
    } finally {
      publishLatch.current = false
      setPublishPending(false)
    }
  }

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['bus-conversations', filters],
    queryFn: () => fetchBusConversations(filters),
  })

  // Roster de agentes y proyectos para los selectores (GET /api/meta, cualquier sesión).
  const meta = useQuery({ queryKey: ['meta'], queryFn: fetchMeta })
  const agents = useMemo(() => meta.data?.agents ?? [], [meta.data])
  const projects = useMemo(() => meta.data?.projects ?? [], [meta.data])

  const apply = (patch: Partial<BusFilters>) => {
    const next = { ...draft, ...patch }
    setDraft(next)
    setFilters(next)
  }
  const clear = () => {
    setDraft(EMPTY_FILTERS)
    setFilters(EMPTY_FILTERS)
  }

  // ⚠️ ESTE useMemo DEBE QUEDAR ANTES DEL `if (open)` DE ABAJO — NO LO MUEVAS.
  // Un hook después de un return condicional cambia la CANTIDAD de hooks entre
  // renders (con detalle abierto se sale antes y no se ejecuta; en la lista sí).
  // React detecta el cambio de orden y DESMONTA el árbol => pantalla EN NEGRO,
  // sin mensaje de error. Lo rompí así el  y tardé en verlo porque
  // `tsc` no lo detecta: es una regla de React, no de tipos.
  // ORDEN DE LA LISTA (pedido de admin ). El backend ya devuelve por último
  // mensaje descendente; acá se respeta o se invierte según el toggle, sin refetch.
  // `[...]` antes de `.sort()` porque sort MUTA: ordenar in-place el array de react-query
  // corrompe la caché y el resultado pasa a depender del número de renders.
  // Se ordena por `last_at` (el campo que el backend usa para intercalar hilos e
  // implícitos); si falta, el item cae al final en vez de romper la comparación.
  const rawItems = data?.threads ?? []
  const items = useMemo(() => {
    const val = (x: BusConversationItem) => {
      const t = x.last_at ? Date.parse(x.last_at) : NaN
      return Number.isNaN(t) ? -Infinity : t
    }
    return [...rawItems].sort((a, b) => (newestFirst ? val(b) - val(a) : val(a) - val(b)))
  }, [rawItems, newestFirst])

  if (open) {
    return open.kind === 'implicit' ? (
      <GroupDetail
        participantsKey={open.participants_key || ''}
        filters={filters}
        newestFirst={newestFirst}
        onBack={() => setOpen(null)}
      />
    ) : (
      <ThreadDetail
        conversationId={open.conversation_id || ''}
        newestFirst={newestFirst}
        onBack={() => setOpen(null)}
        composer={
          me.data?.can_publish_bus === true ? (
            <BusComposer
              suggestedRecipients={(open.participants ?? []).filter(
                (name) => name.toLowerCase() !== (me.data?.name ?? '').toLowerCase())}
              conversationId={open.conversation_id || ''}
              pending={publishPending}
              unavailableReason={publishUnavailableReason}
              onSend={sendBusMessage}
            />
          ) : undefined
        }
      />
    )
  }

  const nThreads = data?.counts?.threads ?? 0
  const nImplicit = data?.counts?.implicit ?? 0
  const filtered = hasAnyFilter(filters)

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-strong)] px-3 py-2 lg:gap-3 lg:px-5 lg:py-3">
        <h1 className="flex items-center gap-2 text-sm font-semibold text-fg">
          <Radio size={15} className="text-accent" /> Bus
        </h1>
        <button
          onClick={() => void refetch()}
          title="Recargar"
          aria-label="Recargar"
          className="flex h-11 w-11 shrink-0 items-center justify-center text-fg-faint transition hover:text-fg lg:h-auto lg:w-auto"
        >
          {isFetching ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <RefreshCw size={13} />
          )}
        </button>
        {/*
          Toggle de orden (pedido de admin ). Afecta la lista Y los mensajes
          de cada conversación: el estado vive en BusView, así que no puede pasar que la
          lista esté al revés que el detalle.
          El label dice el estado ACTUAL, no la acción — "más reciente primero" con la
          flecha hacia abajo es lo que se está viendo. Un botón que dice lo que hará
          obliga a deducir lo que muestra.
        */}
        <button
          onClick={() => setNewestFirst((v) => !v)}
          title={
            newestFirst
              ? 'Orden: más reciente primero (clic para invertir)'
              : 'Orden: más antiguo primero (clic para invertir)'
          }
          aria-pressed={newestFirst}
          className="flex min-h-[44px] items-center gap-1 rounded border border-[var(--border-subtle)] px-2 py-0.5 text-[11px] text-fg-subtle transition hover:border-[var(--border-strong)] hover:text-fg lg:min-h-0 lg:px-1.5"
        >
          {newestFirst ? (
            <ArrowDownWideNarrow size={12} />
          ) : (
            <ArrowUpWideNarrow size={12} />
          )}
          {/* Antes era `hidden sm:inline`: en movil el boton quedaba como un
              icono de flecha sin rotulo, y es el UNICO lugar que dice que orden
              esta activo (no hay hover para leer el title). Ademas `sm` no es el
              breakpoint del proyecto (el canonico es lg). Se muestra siempre. */}
          <span className="whitespace-nowrap">
            {newestFirst ? 'Reciente primero' : 'Antiguo primero'}
          </span>
        </button>
        {/* Cuántos resultados quedaron, y desglosados por tipo. */}
        <span className="flex w-full flex-wrap items-center gap-2 text-[11px] text-fg-subtle tabular-nums lg:ml-auto lg:w-auto">
          <span>
            {items.length} conversación{items.length === 1 ? '' : 'es'}
          </span>
          <span className="inline-flex items-center gap-1 rounded bg-accent/15 px-1.5 py-0.5 text-accent">
            <Radio size={9} /> {nThreads} hilo{nThreads === 1 ? '' : 's'}
          </span>
          <span className="inline-flex items-center gap-1 rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">
            <Unlink size={9} /> {nImplicit} sin hilo
          </span>
        </span>
      </div>

      {me.data?.can_publish_bus === true && (
        <BusComposer
          pending={publishPending}
          unavailableReason={publishUnavailableReason}
          onSend={sendBusMessage}
        />
      )}

      {/* Barra de filtros: en la MISMA sección, arriba de la lista. */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2 lg:px-5">
        <Filter size={12} className="text-fg-faint" />
        <select
          value={draft.agent || ''}
          onChange={(e) => apply({ agent: e.target.value })}
          className={inputCls}
          title="Filtrar por agente participante (sender o destinatario)"
        >
          <option value="">todos los agentes</option>
          {agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>

        <select
          value={draft.project || ''}
          onChange={(e) => apply({ project: e.target.value })}
          className={inputCls}
          title="Proyecto de la tarea referenciada (solo mensajes vinculados a una tarea)"
        >
          <option value="">todos los proyectos</option>
          {projects.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>

        <label className="inline-flex items-center gap-1 text-[11px] text-fg-subtle">
          desde
          <input
            type="date"
            value={draft.since || ''}
            onChange={(e) => apply({ since: e.target.value })}
            className={inputCls}
          />
        </label>
        <label className="inline-flex items-center gap-1 text-[11px] text-fg-subtle">
          hasta
          <input
            type="date"
            value={draft.until || ''}
            onChange={(e) => apply({ until: e.target.value })}
            className={inputCls}
          />
        </label>

        {/* 'buscar texto', NO 'buscar tema': es ILIKE, no búsqueda semántica. */}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            setFilters({ ...draft })
          }}
          className="flex w-full items-center gap-1 lg:w-auto"
        >
          <Search size={12} className="text-fg-faint" />
          <input
            type="search"
            value={draft.q || ''}
            onChange={(e) => setDraft({ ...draft, q: e.target.value })}
            placeholder="buscar texto…"
            title="Coincidencia de texto en contenido, conversation_id y participantes (no es búsqueda semántica)"
            className={`${inputCls} w-full lg:w-44`}
          />
        </form>

        {filtered && (
          <button
            onClick={clear}
            className="inline-flex min-h-[44px] items-center gap-1 rounded-md border border-[var(--border-subtle)] px-3 py-1 text-[11px] text-fg-muted transition hover:bg-surface-hover lg:min-h-0 lg:px-2"
          >
            <FilterX size={11} /> limpiar filtros
          </button>
        )}

        {draft.project && (
          <span className="text-[10px] text-amber-300/80">
            solo mensajes vinculados a una tarea
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-3 lg:p-5">
        {isLoading && (
          <p className="text-xs text-fg-subtle">Cargando conversaciones…</p>
        )}
        {isError && <ErrorState error={error} />}
        {!isLoading && !isError && items.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
              {filtered ? (
                <FilterX size={24} className="text-fg-faint" />
              ) : (
                <Radio size={24} className="text-fg-faint" />
              )}
            </div>
            {/* "Sin resultados con estos filtros" ≠ "el bus está vacío". */}
            {filtered ? (
              <>
                <p className="text-[13px] text-fg-muted">
                  Sin resultados con estos filtros.
                </p>
                <button
                  onClick={clear}
                  className="inline-flex min-h-[44px] items-center gap-1 rounded-md border border-[var(--border-subtle)] px-3 py-1 text-[11px] text-fg-muted transition hover:bg-surface-hover lg:min-h-0 lg:px-2"
                >
                  <FilterX size={11} /> limpiar filtros
                </button>
              </>
            ) : (
              <p className="text-[13px] text-fg-muted">
                El bus todavía no tiene mensajes.
              </p>
            )}
          </div>
        )}
        {items.length > 0 && (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {items.map((it) => (
              <ConversationRow
                key={
                  it.kind === 'implicit'
                    ? `i:${it.participants_key}`
                    : `t:${it.conversation_id}`
                }
                item={it}
                onOpen={() => setOpen(it)}
              />
            ))}
          </div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-1.5 border-t border-[var(--border-subtle)] px-3 py-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] text-[10px] text-fg-subtle/70 lg:px-5 lg:pb-2">
        <Radio size={10} className="text-fg-faint" />
        Bus de la flota · solo lectura · acceso restringido · la búsqueda es
        coincidencia de texto, no semántica
      </div>
    </div>
  )
}
