import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  MessagesSquare,
  Loader2,
  ShieldAlert,
  RefreshCw,
  Send,
  Moon,
  VolumeX,
  Unplug,
  CircleDot,
  Brain,
  ChevronDown,
  ChevronRight,
  Cpu,
  Gauge,
  CircleDollarSign,
  CheckCheck,
  TerminalSquare,
  Lock,
} from 'lucide-react'
import {
  fetchChatAgents,
  fetchChatMessages,
  fetchChatState,
  fetchChatStats,
  fetchChatCommands,
  runChatCommand,
  sendChatMessage,
  fetchChatCapabilities,
  chatErrorDetail,
  ApiError,
  type ChatAgent,
  type ChatAgentStatus,
  type ChatCommand,
  type ChatMessage,
  type ChatState,
  type ChatStats,
  fetchChatPending,
  answerChatPending,
  fetchChatSubagents,
  fetchChatSubagent,
  runChatRpc,
} from '../lib/api'
import { TZ } from './taskShared'
import { encodeImageFiles, validateImageFiles, IMAGE_MIMES, shouldClearSentDraft, shouldInterceptImagePaste } from '../lib/chatImages'
import { useIsDesktop } from '../hooks/useIsDesktop'
import { useVisualViewport } from '../hooks/useVisualViewport'

/**
 * PANEL DE CHAT con los agentes (fase A, cambio de implementación).
 *
 * PRINCIPIO DE DISEÑO, y no es cosmético: esta vista NO es una lista de contactos, es
 * una lista de ESTADOS. Los cuerpos de los líderes viven en máquinas que se suspenden,
 * así que un panel que ofreciera "assistant" sin decir si está viva aceptaría mensajes que
 * se van al vacío — peor que decir "dormido". Por eso:
 *   · el estado de vida SIEMPRE está a la vista, nunca escondido detrás de un ícono;
 *   · un agente que no está `respondiendo` se muestra DESHABILITADO CON EL MOTIVO;
 *   · la caja de envío se bloquea con el motivo, no queda muda.
 * El BFF hace lo mismo del otro lado: `POST /api/chat/send` devuelve 409 con el estado
 * en vez de un 200 mentiroso. La UI no es la que protege — es la que no miente.
 */

/** ISO-8601 (UTC) -> 'YYYY-MM-DD HH:MM' en la zona de admin. Mismo helper `TZ` que
 *  el resto de Sinapsis: cortar el string (`slice(0,16)`) TRUNCA en vez de convertir
 *  y mostraría UTC crudo — bug ya pagado en BusView el . */
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
  const hh = p.hour === '24' ? '00' : p.hour
  return `${p.year}-${p.month}-${p.day} ${hh}:${p.minute}`
}

/** Presentación de cada estado de vida. Los CINCO tienen forma distinta a propósito:
 *  colapsar "dormido" con "no-configurado" en un genérico "no disponible" borraría la
 *  diferencia entre ESPERAR y ARREGLAR, que es justo la decisión que admin toma acá.
 *
 *  🔴 OLA A1 — `respondiendo` vs `respondió` es la distinción NUEVA y es la más fácil de
 *  arruinar, porque los dos son estados VIVOS y el reflejo sería pintarlos iguales:
 *    · `respondiendo` = está escribiendo AHORA. Verde + punto que late: esperá.
 *    · `respondió`    = terminó su turno, hay algo nuevo para LEER. Cyan + doble tilde.
 *  Se eligieron colores de familias distintas (emerald vs cyan) y no dos tonos del mismo
 *  verde: en el iPhone al sol, dos tonos vecinos son el mismo color. Y además cambia el
 *  ÍCONO, para que la diferencia no dependa sólo del color (daltonismo).
 *
 *  El vocabulario lo publica el BFF en `states` de /api/chat/agents; acá sólo se le da
 *  forma. `stMeta` cae a `no-configurado` ante un estado desconocido: si el BFF agrega
 *  uno nuevo, la UI lo muestra en rojo con su nombre en vez de romperse. */
const ST_META: Record<
  ChatAgentStatus,
  { label: string; icon: typeof Moon; cls: string; dot: string; pulse?: boolean }
> = {
  respondiendo: {
    label: 'respondiendo',
    icon: CircleDot,
    cls: 'bg-emerald-500/15 text-emerald-300',
    dot: 'bg-emerald-400',
    // El punto late SOLO en este estado: es el único en que algo está pasando ahora.
    pulse: true,
  },
  respondió: {
    label: 'respondió',
    icon: CheckCheck,
    cls: 'bg-cyan-500/15 text-cyan-300',
    dot: 'bg-cyan-400',
  },
  'no-responde': {
    label: 'no responde',
    icon: VolumeX,
    cls: 'bg-amber-500/15 text-amber-300',
    dot: 'bg-amber-400',
  },
  dormido: {
    label: 'dormido',
    icon: Moon,
    cls: 'bg-slate-500/15 text-fg-subtle',
    dot: 'bg-slate-400',
  },
  'no-configurado': {
    label: 'no configurado',
    icon: Unplug,
    cls: 'bg-red-500/15 text-red-300',
    dot: 'bg-red-400',
  },
}

function stMeta(s: ChatAgentStatus) {
  return ST_META[s] ?? ST_META['no-configurado']
}

/** Error EXPLÍCITO. El 403 se distingue de "no hay agentes": una lista vacía
 *  silenciosa sería indistinguible de "no estás autorizado". */
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
            El chat con los agentes lo usa únicamente el remitente humano autorizado.
            Tu sesión no está habilitada para enviar mensajes.
          </p>
        </>
      ) : (
        <>
          <p className="text-[13px] font-medium text-fg-muted">
            No se pudo consultar el estado de los agentes
          </p>
          <p className="max-w-[420px] text-[12px] text-fg-subtle">
            {error instanceof Error ? error.message : 'Error desconocido'}
          </p>
        </>
      )}
    </div>
  )
}

/** Fila de un agente en el selector: nombre + ESTADO + motivo. El motivo va en el
 *  `title` y también visible cuando el agente está seleccionado: un botón gris sin
 *  explicación obliga a adivinar. */
function AgentRow({
  agent,
  active,
  onSelect,
}: {
  agent: ChatAgent
  active: boolean
  onSelect: () => void
}) {
  const m = stMeta(agent.status)
  const Icon = m.icon
  return (
    <button
      type="button"
      onClick={onSelect}
      title={agent.detail}
      className={`flex w-full flex-col gap-1 rounded-md border px-3 py-2 text-left transition-colors ${
        active
          ? 'border-[var(--border-strong)] bg-surface-hover'
          : 'border-[var(--border-subtle)] hover:bg-surface-hover'
      }`}
    >
      <span className="flex items-center gap-2">
        {/* El punto LATE sólo en `respondiendo`: movimiento = algo está pasando ahora.
            En `respondió` queda fijo (ya terminó) y así los dos estados vivos se
            distinguen incluso sin ver el color. */}
        <span
          className={`h-2 w-2 shrink-0 rounded-full ${m.dot} ${m.pulse ? 'animate-pulse' : ''}`}
          aria-hidden
        />
        <span className="truncate text-[13px] font-medium text-fg">{agent.name}</span>
        <span
          className={`ml-auto flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ${m.cls}`}
        >
          <Icon size={10} />
          {m.label}
        </span>
      </span>
      <span className="line-clamp-2 text-[11px] leading-snug text-fg-subtle">
        {agent.detail}
      </span>
    </button>
  )
}

/** Miles con punto, al modo de admin: 24160 -> '24.160'. */
function milesAr(n: number): string {
  return new Intl.NumberFormat('es-AR').format(Math.round(n))
}

/** 🔴 LA REGLA CENTRAL DE ESTA VISTA, y no es cosmética.
 *
 *  Un 0% de contexto REAL (sesión recién arrancada, sin ningún turno) y un contexto
 *  DESCONOCIDO (el proceso no declara ventana, o acaba de compactar y `percent` viene
 *  `null`) producen el MISMO número. Mirando el porcentaje son indistinguibles.
 *  Lo único que los separa es el bool `context_known` que manda el puente.
 *
 *  Por eso: si `context_known` es `false`, o si `context_usage` falta / es `null`, o
 *  si `percent` no es un número, esto devuelve `null` y la barra escribe
 *  «contexto: desconocido». NUNCA se defaultea a 0: un «0%» inventado sería un falso
 *  verde — le diría a admin "hay toda la ventana libre" justo cuando el panel no
 *  sabe nada. */
function contextLabel(stats?: ChatStats | null): string | null {
  if (!stats) return null
  if (stats.context_known === false) return null
  const u = stats.context_usage
  if (!u) return null
  const pct = u.percent
  const tok = u.tokens
  const win = u.contextWindow
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return null
  const head = `${pct.toFixed(1)}%`
  if (typeof tok !== 'number' || typeof win !== 'number' || !win) return head
  return `${head} (${milesAr(tok)} / ${milesAr(win)} tok)`
}

/** Barra de estado de OPERACIÓN del agente: modelo, contexto, costo y razonamiento.
 *
 *  Existe para una decisión concreta de admin — «¿le sigo escribiendo o compacto?» —
 *  que hoy exige entrar por ssh al LXC. Es SOLO LECTURA: no hay ningún control para
 *  cambiar el modelo, el nivel de razonamiento ni para compactar. Eso serían
 *  escrituras sobre el proceso de otra agente y no están en esta fase.
 *
 *  Cada dato ausente se dice ausente («desconocido», «—»), nunca se rellena con un
 *  cero o un valor por defecto.
 *
 *  🔴 OLA A1 — la BANDERA de «está razonando AHORA» vive acá y NO es lo mismo que el
 *  chip `razonamiento:` que ya existía, ni que el toggle «mostrar razonamiento» de cada
 *  mensaje. Son tres cosas distintas y confundirlas fue el riesgo del pedido:
 *    · `razonamiento: high`  -> CONFIGURACIÓN del proceso (nivel). Estático.
 *    · «pensando…» (esta)    -> `isStreaming` de /api/chat/state: está trabajando AHORA.
 *    · «mostrar razonamiento»-> el `thinking` de UN mensaje YA terminado (ThinkingBlock).
 *  El primero se lee siempre, el segundo aparece y desaparece, el tercero es historia.
 *  Se reusa la MISMA query de /state que ya alimentaba la barra: no se agrega un segundo
 *  polling ni un estado paralelo que podría divergir del que muestra el nivel. */
function OpsBar({
  state,
  stats,
  loading,
  error,
  isDesktop,
}: {
  state?: ChatState | null
  stats?: ChatStats | null
  loading: boolean
  error: boolean
  isDesktop: boolean
}) {
  // Hook ANTES de cualquier return condicional (la rama de error retorna temprano).
  const [open, setOpen] = useState(false)
  const modelName = state?.model?.name || null
  const ctx = contextLabel(stats)
  const unknownWhy = stats?.context_unknown_reason || ''
  const cost = typeof stats?.cost === 'number' ? stats.cost : null
  const think = (state?.thinkingLevel || '').trim()

  if (error) {
    return (
      <div className="flex items-center gap-1.5 border-b border-[var(--border-subtle)] bg-surface/40 px-4 py-1.5 text-[11px] text-fg-subtle">
        <ShieldAlert size={11} className="shrink-0 text-amber-300" />
        No se pudo leer el estado de operación del agente (modelo, contexto, costo).
      </div>
    )
  }

  // ==================== MÓVIL: barra compacta ====================
  // Las cinco métricas no caben en 360px CSS: medido, la barra ocupaba 5 filas y
  // «1.000.000 tok» se partía en 8 líneas, empujando la conversación fuera de la
  // pantalla. Se muestran las DOS que sostienen la decisión de admin («¿le sigo
  // escribiendo o compacto?») y el resto queda a un tap. Nada se borra: se colapsa.
  if (!isDesktop) {
    // El porcentaje se deriva del MISMO string que ya pasó los guardas de
    // `contextLabel` (`context_known`, `percent` numérico). Recalcularlo acá
    // duplicaría esos guardas y podría divergir: un «0%» inventado sería
    // exactamente el falso verde que `contextLabel` existe para evitar.
    const pctOnly = ctx ? ctx.split(' ')[0] : null
    return (
      <div
        data-ops-bar="compact"
        className="border-b border-[var(--border-subtle)] bg-surface/40 text-[11px] text-fg-subtle lg:hidden"
      >
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label="estado de operación del agente"
          className="flex min-h-[44px] w-full items-center gap-2 px-3 py-1 text-left"
        >
          <Cpu size={12} className="shrink-0 text-fg-faint" />
          <span className="truncate text-fg-muted" title={state?.model?.id || ''}>
            {modelName || 'modelo desconocido'}
          </span>
          {/* La bandera va en la fila COLAPSADA, no en el detalle: «está trabajando
              ahora» caduca en segundos y esconderla detrás de un tap la haría
              inútil justo cuando informa. */}
          <BusyFlag
            streaming={!!state?.isStreaming}
            compacting={!!state?.isCompacting}
            thinkLevel={think}
          />
          <span className="ml-auto flex shrink-0 items-center gap-1">
            <Gauge size={12} className="text-fg-faint" />
            {pctOnly ? (
              <span className="text-fg-muted">{pctOnly}</span>
            ) : (
              <span className="text-amber-300">desconocido</span>
            )}
          </span>
          {open ? (
            <ChevronDown size={12} className="shrink-0 text-fg-faint" />
          ) : (
            <ChevronRight size={12} className="shrink-0 text-fg-faint" />
          )}
        </button>

        {open && (
          <div
            data-ops-detail="1"
            className="flex flex-col gap-1.5 border-t border-[var(--border-subtle)] px-3 py-2"
          >
            {loading && (
              <span className="flex items-center gap-1">
                <Loader2 size={11} className="animate-spin" /> leyendo estado…
              </span>
            )}
            <span className="flex items-start gap-1.5">
              <Cpu size={11} className="mt-0.5 shrink-0 text-fg-faint" />
              <span className="min-w-0 break-words">
                <span className="text-fg-muted">{modelName || 'modelo desconocido'}</span>
                {state?.model?.provider && (
                  <span className="text-fg-faint"> · {state.model.provider}</span>
                )}
              </span>
            </span>
            <span className="flex items-start gap-1.5">
              <Gauge size={11} className="mt-0.5 shrink-0 text-fg-faint" />
              <span className="min-w-0 break-words">
                <span className="text-fg-faint">contexto: </span>
                {ctx ? (
                  <span className="text-fg-muted">{ctx}</span>
                ) : (
                  <span className="text-amber-300">
                    desconocido{unknownWhy ? ` (${unknownWhy})` : ''}
                  </span>
                )}
              </span>
            </span>
            <span className="flex items-start gap-1.5">
              <CircleDollarSign size={11} className="mt-0.5 shrink-0 text-fg-faint" />
              <span className="min-w-0 break-words text-fg-muted">
                {cost === null ? '—' : `${cost.toFixed(4)} USD`}
              </span>
            </span>
            <span className="flex items-start gap-1.5">
              <Brain size={11} className="mt-0.5 shrink-0 text-fg-faint" />
              <span className="min-w-0 break-words">
                <span className="text-fg-faint">razonamiento: </span>
                <span className="text-fg-muted">{think || 'desconocido'}</span>
              </span>
            </span>
            {typeof stats?.totalMessages === 'number' && (
              <span className="text-fg-faint">{stats.totalMessages} mensajes</span>
            )}
            {/* `compactando…` ya lo muestra BusyFlag en la fila colapsada: repetirlo
                acá haría creer que son dos cosas distintas. */}
          </div>
        )}
      </div>
    )
  }

  return (
    <div
      data-ops-bar="full"
      className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-[var(--border-subtle)] bg-surface/40 px-4 py-1.5 text-[11px] text-fg-subtle"
    >
      {loading && (
        <span className="flex items-center gap-1">
          <Loader2 size={11} className="animate-spin" /> leyendo estado…
        </span>
      )}

      {/* Modelo: se muestra el `name` legible. El id crudo va en el title, para
          quien lo necesite, sin ocupar la barra. */}
      <span className="flex items-center gap-1" title={state?.model?.id || ''}>
        <Cpu size={11} className="shrink-0 text-fg-faint" />
        <span className="text-fg-muted">{modelName || 'modelo desconocido'}</span>
        {state?.model?.provider && (
          <span className="text-fg-faint">· {state.model.provider}</span>
        )}
      </span>

      {/* Contexto. `null` => «desconocido» CON el motivo si el puente lo dio. */}
      <span
        className="flex items-center gap-1"
        title={
          ctx
            ? 'porcentaje de la ventana de contexto ya consumido en esta sesión'
            : `el puente no puede informar el contexto${unknownWhy ? `: ${unknownWhy}` : ''}`
        }
      >
        <Gauge size={11} className="shrink-0 text-fg-faint" />
        <span className="text-fg-faint">contexto:</span>
        {ctx ? (
          <span className="text-fg-muted">{ctx}</span>
        ) : (
          <span className="text-amber-300">desconocido</span>
        )}
      </span>

      {/* Costo de la sesión. */}
      <span className="flex items-center gap-1" title="costo acumulado de esta sesión">
        <CircleDollarSign size={11} className="shrink-0 text-fg-faint" />
        <span className="text-fg-muted">
          {cost === null ? '—' : `${cost.toFixed(4)} USD`}
        </span>
      </span>

      {/* Nivel de razonamiento. NO es adorno: sin esto, un turno sin `thinking` es
          ambiguo entre "no razonó" y "está apagado". */}
      <span
        className="flex items-center gap-1"
        title="nivel de razonamiento del proceso (solo lectura)"
      >
        <Brain size={11} className="shrink-0 text-fg-faint" />
        <span className="text-fg-faint">razonamiento:</span>
        <span className="text-fg-muted">{think || 'desconocido'}</span>
      </span>

      {/* BANDERA de trabajo en curso. Va DESPUÉS del chip `razonamiento:` a propósito:
          se leen juntos («razonamiento: high» + «razonando…» = está usándolo ahora) sin
          poder confundirse, porque una tiene ícono animado y la otra no. */}
      <BusyFlag
        streaming={!!state?.isStreaming}
        compacting={!!state?.isCompacting}
        thinkLevel={think}
      />

      {typeof stats?.totalMessages === 'number' && (
        <span className="text-fg-faint">{stats.totalMessages} mensajes</span>
      )}
    </div>
  )
}

/** BANDERA de trabajo en curso: el agente está generando AHORA (`isStreaming`).
 *
 *  🔴 Es DISTINTA del chip `razonamiento: <nivel>` (configuración) y del toggle
 *  «mostrar razonamiento» de un mensaje (historia de un turno terminado). Esta aparece y
 *  desaparece; las otras dos no. Para que no se confundan tienen forma distinta: ésta
 *  lleva ícono ANIMADO y fondo violeta, las otras son texto plano.
 *
 *  Dice «pensando…» y no «razonando…» cuando `thinkingLevel` está apagado: afirmar que
 *  razona un proceso con el razonamiento en `off` sería inventar capacidad. El dato de
 *  `isStreaming` sólo prueba que está GENERANDO. */
function BusyFlag({
  streaming,
  compacting,
  thinkLevel,
}: {
  streaming: boolean
  compacting: boolean
  thinkLevel: string
}) {
  // La compactación gana: mientras compacta, el turno del usuario no avanza y eso es lo
  // que hay que saber. Nunca se muestran las dos etiquetas juntas (confundiría el motivo
  // de la espera).
  if (!streaming && !compacting) return null
  const reasons = (thinkLevel || '').toLowerCase()
  const reasoning = !!reasons && reasons !== 'off' && reasons !== 'none'
  const label = compacting
    ? 'compactando…'
    : reasoning
      ? 'razonando…'
      : 'generando…'
  return (
    <span
      data-busy-flag={compacting ? 'compacting' : 'streaming'}
      title={
        compacting
          ? 'el proceso está compactando su contexto: el turno no avanza hasta que termine'
          : reasoning
            ? `el agente está generando su respuesta con razonamiento ${reasons}`
            : 'el agente está generando su respuesta (razonamiento apagado)'
      }
      className={`flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] ${
        compacting
          ? 'bg-amber-500/15 text-amber-300'
          : 'bg-violet-500/15 text-violet-300'
      }`}
    >
      {compacting ? (
        <Loader2 size={10} className="shrink-0 animate-spin" />
      ) : (
        <Brain size={10} className="shrink-0 animate-pulse" />
      )}
      {label}
    </span>
  )
}

/** Razonamiento de UN turno. COLAPSADO por default y visualmente distinto del texto
 *  de la respuesta (borde punteado, cursiva, otro color): si se pareciera al mensaje,
 *  no se podría distinguir una afirmación del agente de su deliberación. Son bloques
 *  de varios párrafos — expandirlos por default sepultaría la respuesta.
 *
 *  Si no hay razonamiento este componente NO se renderiza (lo decide el llamador):
 *  un control que abre la nada es peor que su ausencia. */
function ThinkingBlock({ text, abierto = false }: { text: string; abierto?: boolean }) {
  // 🔴 `abierto` lo decide el llamador: el razonamiento del ÚLTIMO mensaje arranca
  // VISIBLE (pedido de admin: «debe ser siempre visible mientras lo tenga»), los
  // anteriores siguen colapsados — abrir 40 bloques históricos haría el scroll inusable.
  const [open, setOpen] = useState(abierto)
  // Si llega razonamiento nuevo mientras está cerrado y es el último, se abre: el
  // usuario pidió verlo "cargándose de forma inmediata", no tener que buscarlo.
  useEffect(() => {
    if (abierto) setOpen(true)
  }, [abierto])
  return (
    <div className="max-w-[min(680px,88%)] self-start">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex min-h-[44px] items-center gap-1 rounded px-1 py-0.5 text-[10px] text-fg-faint hover:bg-surface-hover hover:text-fg-subtle lg:min-h-0"
      >
        {open ? (
          <ChevronDown size={10} className="shrink-0" />
        ) : (
          <ChevronRight size={10} className="shrink-0" />
        )}
        <Brain size={10} className="shrink-0" />
        <span className="whitespace-nowrap">
          {open ? 'ocultar razonamiento' : 'mostrar razonamiento'}
        </span>
        {/* El contador de caracteres es contexto útil en escritorio, pero a 360px
            empujaba el label a 4 líneas (medido). Se oculta, no se elimina. */}
        <span className="hidden text-fg-faint lg:inline">
          · {milesAr(text.length)} caracteres
        </span>
      </button>
      {open && (
        <div className="mt-1 whitespace-pre-wrap break-words rounded-lg border border-dashed border-[var(--border-subtle)] bg-transparent px-3 py-2 text-[12px] italic leading-relaxed text-fg-subtle">
          {text}
        </div>
      )}
    </div>
  )
}

/** Filtra el catálogo por lo que se escribió después de la `/`.
 *
 *  Dos reglas y las dos tienen motivo:
 *   · el prefijo pesa MÁS que la subcadena (`/com` pone `compact` antes que
 *     `skill:tdd-compacto`): al tipear se espera completar lo que se empezó a escribir;
 *   · los NO autorizados van al final, nunca se sacan. Verlos es la información útil
 *     («existe pero este agente no lo autorizó»); esconderlos haría indistinguible eso
 *     de «no lo tiene». Lo único que no aparece es la lista negra dura, y eso lo saca el
 *     BFF antes de mandarlo: el front no puede «desfiltrarla».
 *
 *  Búsqueda case-insensitive sobre nombre Y descripción: 32 comandos no se recuerdan de
 *  memoria, y `/tdd` tiene que encontrar la skill aunque no sepas cómo se llama. */
function filterCommands(cmds: ChatCommand[], q: string): ChatCommand[] {
  const needle = q.trim().toLowerCase()
  const scored = cmds.map((c, i) => {
    const name = (c.name || '').toLowerCase()
    const desc = (c.description || '').toLowerCase()
    let rank = -1
    if (!needle) rank = 0
    else if (name.startsWith(needle)) rank = 0
    else if (name.includes(needle)) rank = 1
    else if (desc.includes(needle)) rank = 2
    const denied = c.allowed === false ? 1 : 0
    return { c, i, rank, denied }
  })
  return scored
    .filter((s) => s.rank >= 0)
    .sort(
      (a, b) => a.denied - b.denied || a.rank - b.rank || a.i - b.i
    )
    .map((s) => s.c)
}

/** Desplegable de slash-commands: catálogo del agente + ejecución.
 *
 *  🔴 POR QUÉ ABRE HACIA ARRIBA (`bottom-full`) Y NO HACIA ABAJO: el composer está al
 *  fondo de la pantalla y en el iPhone además tiene el teclado debajo. Un desplegable
 *  que creciera hacia abajo taparía el propio campo donde estás escribiendo — no se
 *  vería lo que se tipea, que es la única forma de filtrar. Va anclado arriba del
 *  composer, con `max-h-[45vh]` (el mismo tope que ya usa MobileAgentPicker) para que
 *  nunca se coma la conversación entera, y scroll propio.
 *
 *  Cada fila es `min-h-[44px]` en móvil: es un tap target, y elegir mal acá significa
 *  EJECUTAR un comando equivocado en el proceso de otro agente. En `lg:` baja a 0 porque
 *  con mouse la precisión no es el problema y 44px por fila desperdiciaría la lista. */
function SlashPalette({
  commands,
  loading,
  error,
  deniedHard,
  query,
  activeIdx,
  onPick,
  onHover,
}: {
  commands: ChatCommand[]
  loading: boolean
  error: unknown
  deniedHard: number
  query: string
  activeIdx: number
  onPick: (c: ChatCommand) => void
  onHover: (i: number) => void
}) {
  return (
    <div
      id="slash-palette"
      data-slash-palette="1"
      role="listbox"
      aria-label="comandos disponibles"
      className="absolute bottom-full left-0 right-0 z-30 mb-2 max-h-[45vh] overflow-y-auto overscroll-contain rounded-md border border-[var(--border-strong)] bg-surface shadow-xl"
    >
      <div className="sticky top-0 flex items-center gap-1.5 border-b border-[var(--border-subtle)] bg-surface px-3 py-1.5 text-[10px] text-fg-faint">
        <TerminalSquare size={11} className="shrink-0" />
        <span className="truncate">
          {loading
            ? 'leyendo el catálogo del agente…'
            : `${commands.length} comando${commands.length === 1 ? '' : 's'}${
                query ? ` para «${query}»` : ''
              }`}
        </span>
        {/* La ausencia de `bash` se DECLARA. Sin esto, no listarlo sería
            indistinguible de que el agente no lo tenga. */}
        {deniedHard > 0 && (
          <span
            className="ml-auto shrink-0 text-fg-faint"
            title="comandos que el panel no expone por ninguna vía (ejecución arbitraria de código en el host del agente)"
          >
            {deniedHard} oculto{deniedHard === 1 ? '' : 's'} por política
          </span>
        )}
      </div>

      {error ? (
        <p className="px-3 py-3 text-[12px] text-amber-300">
          No se pudo leer el catálogo de comandos:{' '}
          {error instanceof Error ? error.message : 'error'}
        </p>
      ) : loading ? (
        <span className="flex items-center gap-2 px-3 py-3 text-[12px] text-fg-subtle">
          <Loader2 size={12} className="animate-spin" /> consultando comandos…
        </span>
      ) : !commands.length ? (
        <p className="px-3 py-3 text-[12px] text-fg-subtle">
          {query
            ? `Ningún comando coincide con «${query}».`
            : 'Este agente no expone comandos.'}
        </p>
      ) : (
        commands.map((c, i) => {
          const denied = c.allowed === false
          const active = i === activeIdx
          return (
            <button
              key={`${c.name}-${i}`}
              type="button"
              role="option"
              aria-selected={active}
              // `onMouseDown` con preventDefault y NO `onClick`: un click roba el foco
              // del textarea antes de disparar, y al volver el foco el cursor salta al
              // final. Con mousedown el foco nunca se va.
              onMouseDown={(e) => {
                e.preventDefault()
                onPick(c)
              }}
              onMouseEnter={() => onHover(i)}
              title={denied ? c.denied_reason || 'no autorizado' : c.description || c.name}
              className={`flex min-h-[44px] w-full flex-col items-start gap-0.5 border-b border-[var(--border-subtle)] px-3 py-2 text-left last:border-b-0 lg:min-h-0 ${
                active ? 'bg-surface-hover' : 'hover:bg-surface-hover'
              }`}
            >
              <span className="flex w-full items-center gap-1.5">
                {denied ? (
                  <Lock size={11} className="shrink-0 text-amber-300" />
                ) : (
                  <TerminalSquare size={11} className="shrink-0 text-accent" />
                )}
                <span
                  className={`truncate font-mono text-[13px] ${
                    denied ? 'text-fg-subtle' : 'text-fg'
                  }`}
                >
                  /{c.name}
                </span>
                {c.source && (
                  <span className="ml-auto shrink-0 rounded bg-surface-hover px-1.5 py-0.5 text-[10px] text-fg-faint">
                    {c.source}
                  </span>
                )}
              </span>
              {/* El motivo del rechazo REEMPLAZA a la descripción cuando está
                  denegado: es el dato que decide si insistir o no. */}
              {denied ? (
                <span className="line-clamp-2 text-[11px] leading-snug text-amber-300/90">
                  no autorizado: {c.denied_reason || 'sin motivo declarado'}
                </span>
              ) : c.description ? (
                <span className="line-clamp-2 text-[11px] leading-snug text-fg-subtle">
                  {c.description}
                </span>
              ) : null}
            </button>
          )
        })
      )}
    </div>
  )
}

/** Historial. Solo se renderizan los turnos de CONVERSACIÓN (user/assistant): las
 *  entradas `bashExecution`/`toolResult` del árbol de pi son el trabajo interno del
 *  agente, no el diálogo, y mostrarlas acá convertiría el chat en un log.
 *
 *  Un turno entra si tiene TEXTO. El razonamiento se cuelga de ese turno; no crea
 *  turnos propios. */
/** Burbuja de "está trabajando" DENTRO del chat, al final de la conversación.
 *
 *  🔴 Pedido de admin con su motivo, que es correcto: «debe salir en el chat debajo del
 *  mensaje que mandé para que sea visual que está haciendo algo… si no hay nada en 3
 *  segundos, un humano piensa que el sistema es lento y está pegado».
 *
 *  El indicador ya existía (`BusyFlag`) pero vive ARRIBA, en la barra de operación:
 *  uno manda un mensaje, mira DONDE ESCRIBIÓ, y ahí no pasaba nada. La misma
 *  información en el lugar equivocado no informa.
 *
 *  Se muestra con `pendiente` (optimista, apenas se manda) o con `streaming` (el agente
 *  contesta de verdad). Lo primero es lo que cubre los primeros segundos, que son los
 *  que deciden si el sistema "se siente" vivo.
 */
function TypingBubble({ nivel, streaming }: { nivel?: string | null; streaming: boolean }) {
  // Si hay nivel de razonamiento declarado, se dice "razonando"; si no, "escribiendo".
  // No se afirma que razona un modelo que no razona: sería inventar capacidad.
  const think = (nivel || '').trim()
  const etiqueta = think && think !== 'off' ? 'razonando' : 'escribiendo'
  return (
    <div className="flex flex-col items-start gap-1">
      <div className="flex max-w-[min(680px,88%)] items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-surface px-3 py-2">
        {/* tres puntos que laten desfasados: señal de vida inmediata, sin esperar datos */}
        <span className="flex items-center gap-1">
          <span className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:0ms]" />
          <span className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:150ms]" />
          <span className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-accent [animation-delay:300ms]" />
        </span>
        <span className="text-[12px] text-fg-muted">
          {etiqueta}
          {streaming ? '' : '…'}
        </span>
        {think && think !== 'off' ? (
          <span className="rounded bg-accent/15 px-1.5 text-[10px] text-accent">{think}</span>
        ) : null}
      </div>
    </div>
  )
}

function History({ messages, trabajando, nivel, streaming }: {
  messages: ChatMessage[]
  // 🔴 `trabajando` es OPTIMISTA: se prende apenas admin manda algo, SIN esperar al
  // backend. Los primeros segundos son los que deciden si el sistema se siente vivo —
  // su criterio: «si no hay nada en 3 segundos, un humano piensa que está pegado».
  trabajando: boolean
  nivel?: string | null
  streaming: boolean
}) {
  // 🔴 ANTES este filtro exigía `text` no vacío ⇒ un turno del agente que todavía NO
  // tiene texto (justo cuando está razonando) NO SE RENDERIZABA: el razonamiento en
  // curso era invisible POR CONSTRUCCIÓN. Ahora pasa si tiene texto O razonamiento.
  const turns = messages.filter(
    (m) =>
      (m.role === 'user' || m.role === 'assistant') &&
      ((m.text || '').trim() || (m.thinking || '').trim())
  )
  if (!turns.length) {
    return (
      <p className="py-10 text-center text-[12px] text-fg-subtle">
        No hay mensajes en esta conversación todavía.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-3">
      {turns.map((m) => {
        const mine = m.role === 'user'
        return (
          <div
            key={m.id}
            className={`flex flex-col gap-1 ${mine ? 'items-end' : 'items-start'}`}
          >
            <span className="px-1 text-[10px] text-fg-faint">
              {mine ? 'vos' : 'agente'}
              {m.timestamp ? ` · ${stamp(m.timestamp)}` : ''}
            </span>
            <div
              className={`max-w-[min(680px,88%)] whitespace-pre-wrap break-words rounded-lg border px-3 py-2 text-[13px] leading-relaxed ${
                mine
                  ? 'border-[var(--border-strong)] bg-surface-hover text-fg'
                  : 'border-[var(--border-subtle)] bg-surface text-fg'
              }`}
            >
              {m.text}
            </div>
            {/* Toggle SOLO si ese turno realmente razonó: `thinking` null/vacío =>
                nada que abrir. */}
            {(m.thinking || '').trim() ? (
              <ThinkingBlock
                text={(m.thinking || '').trim()}
                // 🔴 el ÚLTIMO arranca ABIERTO: «el razonamiento debe ser siempre visible
                // mientras lo tenga». Los anteriores colapsados: abrir 40 bloques
                // históricos de golpe haría el scroll inusable.
                abierto={m.id === turns[turns.length - 1]?.id}
              />
            ) : null}
          </div>
        )
      })}
      {/* 🔴 La burbuja va AL FINAL del chat, debajo del último mensaje: es donde admin
          está mirando cuando acaba de escribir. El mismo dato arriba en la barra de
          operación no informa — la información correcta en el lugar equivocado no
          informa. */}
      {trabajando ? <TypingBubble nivel={nivel} streaming={streaming} /> : null}
    </div>
  )
}

/** MÓVIL: el selector de agentes NO puede ser una columna fija.
 *
 *  Medido en el DOM real: con el `aside` de 264px, a 360px CSS la conversación
 *  queda en 96px y el texto sale a 0.71 palabras por línea — el síntoma exacto que
 *  reportó admin. El defecto no es desborde (el `body` tiene `overflow-x:hidden`),
 *  es APLASTAMIENTO: el `aside` no cede y el `flex-1` absorbe toda la falta de ancho.
 *
 *  Acá el selector colapsa a UNA fila con el agente activo y SU ESTADO DE VIDA —que
 *  es justo el dato que esta vista existe para no esconder— y se despliega en la
 *  lista completa al tap. Se usa `useIsDesktop` (1024px), el mismo umbral que ya
 *  comparten AppShell y Sidebar: un tercer breakpoint dejaría el Sidebar global en
 *  drawer mientras esta vista sigue en modo escritorio. */
/** Epoch en SEGUNDOS (float) -> hora local.
 *
 *  🔴 NO se reusa `stamp()`: ese espera ISO-8601 y el puente manda epoch float
 *  (medido: 1787519472.9781976). Pasarle esto a `stamp()` muestra 1970, y es el
 *  error más fácil de cometer en esta ola. Se agrega función aparte en vez de
 *  cambiar `stamp()`, que ya lo usan otras vistas.
 */
/** Runtimes con consentimiento explícito para comandos que cambian su contexto.
 *  `/new` y `/compact` afectan una sesión de trabajo: nunca se autoriza por inercia.
 *  La lista de UI se configura con VITE_CHAT_CMD_WRITE_AGENTS y queda vacía por
 *  defecto. Es preventiva, no un control de seguridad: el BFF hace cumplir su
 *  propia CHAT_CMD_WRITE_AGENTS en cada request y prevalece si ambas divergen.
 */
const CHAT_WRITE_AGENTS = (import.meta.env.VITE_CHAT_CMD_WRITE_AGENTS ?? '').split(',').map((name: string) => name.trim().toLowerCase()).filter(Boolean)

/** Agrupa los agentes por PADRE para que la lista no sea una pila plana.
 *
 *  Pedido de admin: «que haya chats agrupados… o voy a terminar con una cantidad de
 *  chats separados sin ordenar».
 *
 *  🔴 LA JERARQUIA NO VIENE DEL BACKEND: se deriva del NOMBRE. `assistant-e-beta` empieza
 *  con `assistant-e-` ⇒ es hijo de `assistant-e`. Es una CONVENCION DE NOMBRES, no un dato
 *  estructural, y está aislada en esta función a propósito: un `assistant-a-forge` futuro
 *  funciona igual sin tocar nada, y el día que el backend exponga la jerarquía de
 *  verdad se reemplaza esto por ese dato.
 *
 *  El estado del GRUPO es el PEOR de sus miembros: si un hijo está dormido, el grupo
 *  lo muestra. Un grupo que dijera "respondiendo" con un miembro caído mentiría.
 */
function agruparAgentes<T extends { name: string; status: ChatAgentStatus }>(
  agents: T[],
): { padre: T; hijos: T[] }[] {
  const nombres = new Set(agents.map((a) => a.name))
  const padreDe = (n: string): string | null => {
    // el prefijo más largo que TAMBIÉN es un agente conocido
    let mejor: string | null = null
    for (const cand of nombres) {
      if (cand !== n && n.startsWith(cand + '-')) {
        if (!mejor || cand.length > mejor.length) mejor = cand
      }
    }
    return mejor
  }
  const grupos: { padre: T; hijos: T[] }[] = []
  const porNombre = new Map(agents.map((a) => [a.name, a]))
  for (const a of agents) {
    if (padreDe(a.name)) continue // es hijo: se cuelga de su padre
    const hijos = agents.filter((h) => padreDe(h.name) === a.name)
    grupos.push({ padre: a, hijos })
  }
  // agentes cuyo "padre" no está en la lista: se muestran solos para no perderlos
  for (const a of agents) {
    const p = padreDe(a.name)
    if (p && !porNombre.has(p)) grupos.push({ padre: a, hijos: [] })
  }
  return grupos
}

/** El estado PEOR de un grupo. Orden de gravedad, de peor a mejor. */
function peorEstado(estados: ChatAgentStatus[]): ChatAgentStatus {
  const orden = ['no-configurado', 'dormido', 'no-responde', 'respondió', 'respondiendo']
  let peor = estados[0] || 'no-configurado'
  for (const e of estados) {
    if (orden.indexOf(e) < orden.indexOf(peor)) peor = e
  }
  return peor
}

/** Cadencia de sondeo segun la ACTIVIDAD de admin (pedido suyo, ):
 *  «que inicie el sondaje cuando yo mando un mensaje, ahí que sea rápido, y si detecta
 *  que no envío después de 240 segundos que pare el sondaje».
 *
 *  MEDIDO antes: 7 polls permanentes (messages 5s, pending 6s, subagents 10s,
 *  subagent 4s, agents 20s, state y stats 15s) ⇒ ~40 requests/min para siempre,
 *  incluso con la pestaña abierta sin usar.
 *
 *  TRES fases, porque «rápido» y «apagado» solos no alcanzan:
 *    activo   (0-20s del último envío)  → 1.5s   estás esperando la respuesta
 *    tibio    (20-240s)                 → 6s     leyendo; todavía puede llegar algo
 *    dormido  (>240s sin enviar)        → OFF    cero requests
 *
 *  🔴 NO SE APAGA NUNCA si el agente está streaming o hay un diálogo pendiente. Cortar
 *  el sondeo mientras el agente escribe dejaría la respuesta a medias; apagarlo con un
 *  diálogo abierto esconderia que el agente está bloqueado esperando a admin. El
 *  apagado es por INACTIVIDAD DE admin, no por reloj: un timer que ignora el estado
 *  convierte una optimización en pérdida de datos.
 */
type PollTier = 'activo' | 'tibio' | 'dormido'

const POLL_ACTIVO_MS = 1_500
const POLL_TIBIO_MS = 6_000
const POLL_ACTIVO_HASTA_MS = 20_000
const POLL_DORMIR_MS = 240_000

function usePollTier(mantenerDespierto: boolean): {
  tier: PollTier
  ping: () => void
  msgMs: number | false
  bgMs: number | false
} {
  // marca del último envío de admin. Arranca AHORA: abrir el panel es actividad.
  const [ultimo, setUltimo] = useState<number>(() => Date.now())
  const [ahora, setAhora] = useState<number>(() => Date.now())

  // Un solo timer de 1s mueve el reloj. No dispara requests: sólo recalcula la fase.
  useEffect(() => {
    const t = setInterval(() => setAhora(Date.now()), 1_000)
    return () => clearInterval(t)
  }, [])

  // Volver a la pestaña cuenta como actividad: si admin la trae al frente, quiere ver.
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === 'visible') setUltimo(Date.now())
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  const delta = ahora - ultimo
  let tier: PollTier =
    delta < POLL_ACTIVO_HASTA_MS ? 'activo' : delta < POLL_DORMIR_MS ? 'tibio' : 'dormido'
  // el override: mientras el agente trabaja o espera una decisión, nunca se duerme
  if (mantenerDespierto && tier === 'dormido') tier = 'tibio'

  const msgMs = tier === 'activo' ? POLL_ACTIVO_MS : tier === 'tibio' ? POLL_TIBIO_MS : false
  // el estado de fondo (agents/state/stats) va más lento que los mensajes: cambia menos
  const bgMs = tier === 'activo' ? POLL_TIBIO_MS : tier === 'tibio' ? 15_000 : false

  return { tier, ping: () => setUltimo(Date.now()), msgMs, bgMs }
}

function stampEpoch(secs?: number | null): string {
  if (!secs || secs <= 0) return '—'
  try {
    return new Date(secs * 1000).toLocaleTimeString('es-CL', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch {
    return '—'
  }
}

/** Hace cuánto, en palabras cortas. Para `age_s` de los subagentes. */
function haceCuanto(segs?: number | null): string {
  if (segs == null) return '—'
  if (segs < 60) return `hace ${Math.round(segs)}s`
  if (segs < 3600) return `hace ${Math.round(segs / 60)}m`
  return `hace ${Math.round(segs / 3600)}h`
}

/** DIÁLOGOS PENDIENTES: el agente está BLOQUEADO esperando una decisión de admin.
 *
 *  🔴 Esto no es un adorno: pi emite `extension_ui_request` (confirm/select/input/
 *  editor) y hasta que alguien responda, el agente NO AVANZA. Antes de la ola A2 el
 *  puente no lo capturaba y el agente quedaba colgado para siempre, invisible.
 *
 *  `count` viene del puente y cuenta SOLO los bloqueantes. Los `notices`
 *  (notify/setStatus/…) se muestran como avisos discretos: medido que assistant-a, assistant-e,
 *  assistant-g y assistant-b emiten un `notify` de identidad en cada arranque — contarlos
 *  dejaría el indicador encendido en 4 de 5 agentes de forma permanente, y un
 *  indicador que nunca se apaga es peor que no tenerlo.
 *
 *  ⚠️ Si el agente no autorizó escritura desde el panel, responder da 403. Los botones
 *  se muestran IGUAL y el error se explica: esconderlos ocultaría que el agente está
 *  bloqueado, que es justamente lo que hay que ver.
 */
function PendingDialogs({ agent, canWrite, pollMs }: {
  agent: string
  canWrite: boolean
  // El intervalo lo decide el componente padre según la actividad de admin: acá no se
  // puede llamar `usePollTier` de nuevo o cada componente tendría su propio reloj.
  pollMs: number | false
}) {
  const qc = useQueryClient()
  const [texto, setTexto] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const q = useQuery({
    queryKey: ['chat', 'pending', agent],
    queryFn: () => fetchChatPending(agent),
    enabled: !!agent,
    refetchInterval: pollMs,
    retry: false,
  })
  const dialogos = q.data?.pending ?? []
  const avisos = q.data?.notices ?? []
  const d = dialogos[0]

  async function responder(payload: { confirmed: boolean } | { value: string } | { cancelled: true }) {
    if (!d) return
    setErr(null)
    try {
      await answerChatPending(agent, d.id, payload)
      setTexto('')
      qc.invalidateQueries({ queryKey: ['chat', 'pending', agent] })
      qc.invalidateQueries({ queryKey: ['chat', 'messages', agent] })
    } catch (e: unknown) {
      const any = e as { body?: { detail?: { reason?: string; error?: string } }; message?: string }
      setErr(any?.body?.detail?.reason || any?.body?.detail?.error || any?.message || 'no se pudo responder')
    }
  }

  if (!d) {
    if (avisos.length === 0) return null
    // avisos: informativos, sin botones, deliberadamente discretos
    return (
      <div className="border-t border-[var(--border-subtle)] px-3 py-1.5 text-[11px] text-fg-faint lg:px-4">
        {avisos.slice(-1).map((n) => (
          <div key={n.id || n.message} className="truncate">
            <span className="text-fg-muted">aviso:</span> {n.message || n.title}
          </div>
        ))}
      </div>
    )
  }

  const metodo = (d.method || '').toLowerCase()
  return (
    <div className="border-t-2 border-amber-500/60 bg-amber-500/10 px-3 py-3 lg:px-4">
      <div className="mb-2 flex items-center gap-2">
        <span className="inline-flex h-2 w-2 shrink-0 animate-pulse rounded-full bg-amber-400" />
        <span className="text-[13px] font-semibold text-amber-300">
          {agent} espera una decisión
        </span>
        <span className="text-[11px] text-fg-faint">
          {metodo} · {stampEpoch(d.asked_at)}
        </span>
      </div>
      {d.title ? <div className="mb-1 text-[13px] font-medium text-fg">{d.title}</div> : null}
      {d.message ? <div className="mb-2 whitespace-pre-wrap break-words text-[13px] text-fg-muted">{d.message}</div> : null}

      {metodo === 'confirm' ? (
        <div className="flex flex-wrap gap-2">
          <button onClick={() => responder({ confirmed: true })}
            className="min-h-[44px] rounded bg-emerald-600/80 px-4 text-[13px] font-medium text-white hover:bg-emerald-600 lg:min-h-0 lg:py-1.5">Sí</button>
          <button onClick={() => responder({ confirmed: false })}
            className="min-h-[44px] rounded bg-red-600/70 px-4 text-[13px] font-medium text-white hover:bg-red-600 lg:min-h-0 lg:py-1.5">No</button>
        </div>
      ) : metodo === 'select' && Array.isArray(d.options) ? (
        <div className="flex flex-col gap-1.5">
          {d.options.map((o: unknown, i: number) => {
            const label = typeof o === 'string' ? o : JSON.stringify(o)
            return (
              <button key={i} onClick={() => responder({ value: label })}
                className="min-h-[44px] rounded border border-[var(--border-subtle)] bg-surface px-3 text-left text-[13px] text-fg hover:border-[var(--border-strong)] lg:min-h-0 lg:py-1.5">{label}</button>
            )
          })}
        </div>
      ) : (
        // input / editor: campo de texto. `editor` usa varias líneas.
        <div className="flex flex-col gap-2">
          <textarea value={texto} onChange={(e) => setTexto(e.target.value)}
            placeholder={d.placeholder || 'tu respuesta…'}
            rows={metodo === 'editor' ? 4 : 2}
            className="w-full resize-y rounded border border-[var(--border-subtle)] bg-surface px-3 py-2 text-[16px] text-fg outline-none focus:border-[var(--border-strong)] lg:text-[13px]" />
          <div className="flex gap-2">
            <button onClick={() => responder({ value: texto })} disabled={!texto.trim()}
              className="min-h-[44px] rounded bg-accent/80 px-4 text-[13px] font-medium text-white disabled:opacity-40 lg:min-h-0 lg:py-1.5">Responder</button>
          </div>
        </div>
      )}

      <button onClick={() => responder({ cancelled: true })}
        className="mt-2 min-h-[44px] text-[12px] text-fg-faint underline hover:text-fg-muted lg:min-h-0">cancelar el diálogo</button>

      {!canWrite ? (
        <div className="mt-2 text-[11px] text-amber-300/80">
          ⚠️ {agent} todavía no autorizó que se le modifique el runtime desde el
          panel, así que responder va a dar 403. No es una restricción sobre vos: es
          que falta el permiso de {agent}. Se muestra igual porque el agente ESTÁ
          bloqueado y eso hay que verlo.
        </div>
      ) : null}
      {err ? <div className="mt-2 break-words text-[12px] text-red-300">{err}</div> : null}
    </div>
  )
}

/** Una entrada del JSONL de un subagente.
 *
 *  🔴 MEDIDO sobre un archivo real de pi 0.83.0 (48 entradas): el trabajo de un
 *  subagente es 80% HERRAMIENTAS — 26 toolCall, 25 toolResult, 4 assistant/text,
 *  1 user/text. `content` es un ARRAY de bloques con type ∈ {text, toolCall}.
 *  ⇒ Una vista que sólo renderice texto se ve CASI VACÍA. Se renderizan los toolCall
 *  con su nombre, que es donde está lo que el subagente hace de verdad.
 */
function SubagentEntry({ e }: { e: Record<string, unknown> }) {
  const tipo = String(e.type || '')
  if (tipo !== 'message') {
    // session / model_change / thinking_level_change: metadata, se muestra tenue
    return <div className="px-2 py-0.5 text-[11px] text-fg-faint">· {tipo.replace(/_/g, ' ')}</div>
  }
  const msg = (e.message || e) as Record<string, unknown>
  const role = String(msg.role || '?')
  const content = msg.content
  const bloques = Array.isArray(content) ? content : []
  const color = role === 'assistant' ? 'text-fg' : role === 'toolResult' ? 'text-fg-faint' : 'text-fg-muted'
  return (
    <div className="border-b border-[var(--border-subtle)]/40 px-2 py-1.5">
      <div className="mb-0.5 text-[10px] uppercase tracking-wide text-fg-faint">{role}</div>
      {bloques.length === 0 ? (
        <div className={`whitespace-pre-wrap break-words text-[12px] ${color}`}>
          {String((msg as { text?: string }).text || '').slice(0, 2000) || '—'}
        </div>
      ) : (
        bloques.map((b: unknown, i: number) => {
          const bb = b as Record<string, unknown>
          const bt = String(bb.type || '')
          if (bt === 'toolCall') {
            const nombre = String(bb.name || bb.toolName || 'tool')
            return (
              <div key={i} className="my-0.5 flex items-start gap-1.5 text-[12px]">
                <span className="shrink-0 rounded bg-accent/20 px-1.5 py-0.5 font-mono text-[10px] text-accent">{nombre}</span>
                <span className="min-w-0 break-words text-fg-faint">
                  {JSON.stringify(bb.args ?? bb.input ?? {}).slice(0, 220)}
                </span>
              </div>
            )
          }
          const txt = String(bb.text || '')
          if (!txt) return null
          return (
            <div key={i} className={`whitespace-pre-wrap break-words text-[12px] ${color}`}>
              {txt.slice(0, 2000)}
            </div>
          )
        })
      )}
    </div>
  )
}

/** SUBAGENTES: lo que pidió admin — «que salga si existe un subagente en ejecución y
 *  al presionarlo se divida el chat para ver lo que está haciendo en tiempo real».
 *
 *  El dato NO viene de un comando: `subagents:sessions` no existe en pi (la extensión
 *  sólo registra eventos). El puente lee `<session-dir>/tasks/*.jsonl`, que es la
 *  fuente real.
 *
 *  ⚠️ `recent` es una HEURÍSTICA declarada por el puente (jsonl tocado hace <120s). El
 *  archivo NO tiene señal de proceso vivo, así que la UI dice «actividad reciente» y
 *  NUNCA «corriendo»: afirmar que un proceso está vivo sin poder verlo sería inventar.
 */
function SubagentsPanel({ agent, sel, onSel, pollMs }: {
  agent: string
  sel: string | null
  onSel: (id: string | null) => void
  pollMs: number | false
}) {
  const q = useQuery({
    queryKey: ['chat', 'subagents', agent],
    queryFn: () => fetchChatSubagents(agent),
    enabled: !!agent,
    refetchInterval: pollMs,
    retry: false,
  })
  const subs = q.data?.subagents ?? []
  const det = useQuery({
    queryKey: ['chat', 'subagent', agent, sel],
    queryFn: () => fetchChatSubagent(agent, sel as string),
    enabled: !!agent && !!sel,
    // el subagente ABIERTO se mira ahora: va al doble de frecuencia del resto
    refetchInterval: pollMs === false ? false : Math.max(2_000, (pollMs as number) / 2),
    retry: false,
  })

  if (subs.length === 0) return null

  if (sel) {
    const entries = (det.data?.entries ?? []) as Record<string, unknown>[]
    return (
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
          <button onClick={() => onSel(null)}
            className="min-h-[44px] shrink-0 rounded px-2 text-[13px] text-accent hover:bg-surface lg:min-h-0 lg:py-1">← volver</button>
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-muted">{sel}</span>
          <span className="shrink-0 text-[11px] text-fg-faint">{entries.length} entradas</span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {entries.length === 0 ? (
            <div className="p-3 text-[12px] text-fg-faint">sin entradas todavía</div>
          ) : (
            entries.map((e, i) => <SubagentEntry key={i} e={e} />)
          )}
        </div>
      </div>
    )
  }

  return (
    <div className="border-t border-[var(--border-subtle)] px-3 py-2 lg:px-4">
      <div className="mb-1.5 text-[11px] font-medium text-fg-muted">
        subagentes ({subs.length})
      </div>
      <div className="flex flex-col gap-1">
        {subs.map((sb) => (
          <button key={sb.id} onClick={() => onSel(sb.id)}
            className="flex min-h-[44px] items-center gap-2 rounded border border-[var(--border-subtle)] bg-surface px-2.5 text-left hover:border-[var(--border-strong)] lg:min-h-0 lg:py-1.5">
            {sb.recent ? (
              <span className="inline-flex h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-emerald-400" />
            ) : (
              <span className="inline-flex h-1.5 w-1.5 shrink-0 rounded-full bg-fg-faint/40" />
            )}
            {/* 🔴 MEDIDO a 360 CSS: la fila entera daba 490px y se salía. El uuid
                completo no aporta nada en un celular — lo que hace falta es
                DISTINGUIR dos subagentes, no leer el id. En móvil se muestra la hora
                (los primeros 16 caracteres del nombre, que es el timestamp) y en `lg:`
                el id completo. */}
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg">
              <span className="lg:hidden">{String(sb.id).slice(0, 16)}</span>
              <span className="hidden lg:inline">{sb.id}</span>
            </span>
            <span className="shrink-0 whitespace-nowrap text-[10px] text-fg-faint">
              {/* "actividad reciente", NUNCA "corriendo": el archivo no prueba que el
                  proceso esté vivo. En móvil se abrevia para que la fila no desborde. */}
              {sb.recent ? (
                <>
                  <span className="lg:hidden">activo</span>
                  <span className="hidden lg:inline">actividad reciente</span>
                </>
              ) : (
                haceCuanto(sb.age_s)
              )}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}

/** Un GRUPO de agentes en la lista: el padre y sus hijos, colapsable.
 *
 *  Pedido de admin: «que diga assistant-e grupo y que al pincharlo salga el de assistant-e y todos
 *  sus subagentes… o voy a terminar con una cantidad de chats separados sin ordenar».
 *
 *  El chip de estado del grupo muestra el PEOR de sus miembros: un grupo que dijera
 *  "respondiendo" con un hijo dormido mentiría sobre lo que hay adentro.
 */
function AgentGroup({ padre, hijos, selected, renderItem }: {
  // 🔴 Se usa el tipo REAL `ChatAgent`, no una versión reducida {name,status}:
  // `AgentRow` pide el objeto completo (necesita `detail`), y si el backend agrega
  // campos mañana esto sigue compilando sin tocar nada.
  padre: ChatAgent
  hijos: ChatAgent[]
  selected: string
  // NO recibe onSelect: cada fila la construye `renderItem`, que ya trae el suyo.
  renderItem: (a: ChatAgent) => React.ReactNode
}) {
  // abierto por defecto si el seleccionado está adentro: nunca esconder al activo
  const contiene = selected === padre.name || hijos.some((h) => h.name === selected)
  const [open, setOpen] = useState(contiene)
  if (hijos.length === 0) return <>{renderItem(padre)}</>
  const peor = peorEstado([padre.status, ...hijos.map((h) => h.status)])
  const pm = stMeta(peor)
  return (
    <div className="rounded-md border border-[var(--border-subtle)]/60">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex min-h-[44px] w-full items-center gap-1.5 px-2 text-left hover:bg-surface-hover lg:min-h-0 lg:py-1.5"
      >
        <ChevronRight
          size={12}
          className={`shrink-0 text-fg-faint transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-fg">
          {padre.name} <span className="text-fg-faint">grupo</span>
        </span>
        <span className="shrink-0 text-[10px] text-fg-faint">{1 + hijos.length}</span>
        {/* `dot` de ST_META es SOLO la clase de color; tamaño y forma van acá. */}
        {pm ? (
          <span
            title={pm.label}
            className={`inline-flex h-1.5 w-1.5 shrink-0 rounded-full ${pm.dot} ${pm.pulse ? 'animate-pulse' : ''}`}
          />
        ) : null}
      </button>
      {open ? (
        <div className="border-t border-[var(--border-subtle)]/40 pl-3">
          {renderItem(padre)}
          {hijos.map((h) => renderItem(h))}
        </div>
      ) : null}
    </div>
  )
}

/** Resultado de un comando RPC de LECTURA, en un panel accionable.
 *
 *  🔴 QA de admin: «al presionar /model debería darme la lista… que al presionar me
 *  abra un menú mostrando los modelos disponibles». Antes el front ejecutaba el
 *  comando y TIRABA el resultado (yo lo normalicé a `{accepted}` para arreglar un
 *  error de tipos, y me llevé la funcionalidad con él).
 *
 *  Cada comando devuelve una forma DISTINTA y eso no se puede aplanar:
 *    get_available_models          -> data.models[]  {id, name, provider, reasoning}
 *    get_available_thinking_levels -> data.levels[]  ["off","low","high",…]
 *    get_tree / get_session_stats  -> lo suyo
 *  Se entienden los dos casos ACCIONABLES y el resto se muestra como JSON formateado.
 *  Proyectar todo a una forma común perdería datos que no conozco de antemano.
 *
 *  Y lo importante: los modelos y los niveles son CLICKEABLES. Elegir uno ejecuta
 *  `set_model`/`set_thinking_level`, que es lo que se hace en la TUI. Una lista que
 *  sólo se mira obliga a escribir el comando a mano con el id exacto.
 */
function RpcResultPanel({ res, onPick, onClose, busy }: {
  res: { command?: string; data?: unknown } | null
  onPick: (rpc: string, params: Record<string, unknown>, etiqueta: string) => void
  onClose: () => void
  busy: boolean
}) {
  if (!res) return null
  const data = (res.data || {}) as Record<string, unknown>
  const cmd = String(res.command || '')
  const modelos = Array.isArray(data.models) ? (data.models as Record<string, unknown>[]) : null
  const niveles = Array.isArray(data.levels) ? (data.levels as string[]) : null

  return (
    <div className="border-t-2 border-accent/50 bg-accent/5">
      <div className="flex items-center gap-2 px-3 py-1.5 lg:px-4">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-accent">/{cmd}</span>
        <button
          onClick={onClose}
          aria-label="cerrar resultado"
          className="min-h-[44px] shrink-0 px-2 text-[12px] text-fg-faint hover:text-fg lg:min-h-0 lg:py-1"
        >
          cerrar
        </button>
      </div>

      <div className="max-h-[45vh] overflow-y-auto px-3 pb-3 lg:px-4">
        {modelos ? (
          <div className="flex flex-col gap-1">
            <div className="pb-1 text-[11px] text-fg-faint">
              {modelos.length} modelos · tocá uno para cambiarlo
            </div>
            {modelos.map((m, i) => {
              const id = String(m.id || '')
              return (
                <button
                  key={id || i}
                  disabled={busy}
                  // 🔴 set_model necesita {provider, modelId}, NO {model}. Medido:
                  // con {model} pi devuelve 503 "Model not found: undefined/undefined",
                  // que es su forma de decir que faltaron los DOS campos. La lista ya
                  // trae `provider` en cada item, así que están a mano.
                  onClick={() =>
                    onPick(
                      'set_model',
                      { provider: String(m.provider || ''), modelId: id },
                      `modelo → ${id}`
                    )
                  }
                  className="flex min-h-[44px] items-center gap-2 rounded border border-[var(--border-subtle)] bg-surface px-2.5 text-left hover:border-[var(--border-strong)] disabled:opacity-40 lg:min-h-0 lg:py-1.5"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-fg">{id}</span>
                  {m.reasoning ? (
                    <span className="shrink-0 rounded bg-accent/20 px-1.5 text-[10px] text-accent">razona</span>
                  ) : null}
                  <span className="shrink-0 text-[10px] text-fg-faint">{String(m.provider || '')}</span>
                </button>
              )
            })}
          </div>
        ) : niveles ? (
          <div className="flex flex-wrap gap-1.5">
            <div className="w-full pb-1 text-[11px] text-fg-faint">
              tocá un nivel para cambiarlo
            </div>
            {niveles.map((n) => (
              <button
                key={n}
                disabled={busy}
                onClick={() => onPick('set_thinking_level', { level: n }, `razonamiento → ${n}`)}
                className="min-h-[44px] rounded border border-[var(--border-subtle)] bg-surface px-3 font-mono text-[12px] text-fg hover:border-[var(--border-strong)] disabled:opacity-40 lg:min-h-0 lg:py-1.5"
              >
                {n}
              </button>
            ))}
          </div>
        ) : (
          // Cualquier otro comando: el JSON como viene. No se proyecta a una forma
          // fija porque cada comando devuelve la suya y aplanarlas perdería datos.
          <pre className="whitespace-pre-wrap break-words rounded bg-surface p-2 text-[11px] text-fg-muted">
            {JSON.stringify(data, null, 2).slice(0, 4000)}
          </pre>
        )}
      </div>
    </div>
  )
}

function MobileAgentPicker({
  agents,
  selected,
  loading,
  fetching,
  onSelect,
  onRefresh,
}: {
  agents: ChatAgent[]
  selected: string
  loading: boolean
  fetching: boolean
  onSelect: (name: string) => void
  onRefresh: () => void
}) {
  const [open, setOpen] = useState(false)
  const current = agents.find((a) => a.name === selected) || null
  const m = current ? stMeta(current.status) : null
  return (
    <div
      data-agent-picker="1"
      className="shrink-0 border-b border-[var(--border-subtle)] lg:hidden"
    >
      <div className="flex items-center gap-1 px-1">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label="elegir agente"
          className="flex min-h-[44px] min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-left hover:bg-surface-hover"
        >
          <MessagesSquare size={14} className="shrink-0 text-accent" />
          {m && (
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${m.dot} ${m.pulse ? 'animate-pulse' : ''}`}
              aria-hidden
            />
          )}
          <span className="truncate text-[13px] font-medium text-fg">
            {current ? current.name : loading ? 'consultando estado…' : 'elegí un agente'}
          </span>
          {m && (
            <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${m.cls}`}>
              {m.label}
            </span>
          )}
          {open ? (
            <ChevronDown size={14} className="ml-auto shrink-0 text-fg-faint" />
          ) : (
            <ChevronRight size={14} className="ml-auto shrink-0 text-fg-faint" />
          )}
        </button>
        <button
          type="button"
          onClick={onRefresh}
          title="Re-consultar el estado de vida"
          aria-label="Re-consultar el estado de vida"
          className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-fg-faint hover:bg-surface-hover hover:text-fg"
        >
          <RefreshCw size={14} className={fetching ? 'animate-spin' : ''} />
        </button>
      </div>

      {open && (
        // `gap-2` a propósito: con las filas pegadas un tap acierta el agente
        // equivocado, y acá eso significa escribirle al agente equivocado.
        <div className="flex max-h-[45vh] flex-col gap-2 overflow-y-auto border-t border-[var(--border-subtle)] p-2">
          {loading ? (
            <span className="flex items-center gap-2 px-1 py-3 text-[12px] text-fg-subtle">
              <Loader2 size={12} className="animate-spin" /> consultando estado…
            </span>
          ) : !agents.length ? (
            <p className="px-1 py-3 text-[12px] text-fg-subtle">
              No hay agentes configurados para chat.
            </p>
          ) : (
            agents.map((a) => (
              <AgentRow
                key={a.name}
                agent={a}
                active={a.name === selected}
                onSelect={() => {
                  onSelect(a.name)
                  setOpen(false)
                }}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}

export default function ChatView() {
  // subagente abierto en el panel dividido. null = ninguno.
  const [subagentSel, setSubagentSel] = useState<string | null>(null)
  // Resultado del último comando RPC de lectura (/model, /thinking, /tree…).
  // 🔴 Existe porque el `data` se estaba TIRANDO: yo normalicé el retorno de la
  // mutation a `{accepted}` para callar a TS y me llevé la funcionalidad.
  const [rpcRes, setRpcRes] = useState<{ command?: string; data?: unknown } | null>(null)

  // 🔴 CADENCIA POR ACTIVIDAD (pedido de admin). `mantenerDespierto` se calcula abajo
  // con el estado real: mientras el agente streamea o hay un diálogo, no se duerme.
  const [mantenerDespierto, setMantenerDespierto] = useState(false)
  const poll = usePollTier(mantenerDespierto)
  // Aviso de que una ACCIÓN se aplicó (/new, /compact, set_model…). Se muestra y se
  // borra solo: un cambio de estado sin confirmación deja al usuario sin saber si tomó.
  const [cmdOk, setCmdOk] = useState<string>('')
  // El aviso se borra solo: un cartel permanente se vuelve parte del decorado y se
  // deja de leer. 4s alcanza para verlo y no estorba después.
  useEffect(() => {
    if (!cmdOk) return
    const t = setTimeout(() => setCmdOk(''), 4000)
    return () => clearTimeout(t)
  }, [cmdOk])


  const qc = useQueryClient()
  const [selected, setSelected] = useState<string>('')
  const [draft, setDraft] = useState('')
  const [imageFiles, setImageFiles] = useState<File[]>([])
  const liveDraft = useRef({agent: selected, draft, files: imageFiles})
  liveDraft.current = {agent: selected, draft, files: imageFiles}
  const fileRef = useRef<HTMLInputElement>(null)
  const [sendError, setSendError] = useState<string>('')
  const endRef = useRef<HTMLDivElement | null>(null)
  // Umbral CANÓNICO del proyecto (1024px), centralizado en el hook y compartido
  // con las utilidades `lg:` de Tailwind. No se inventa uno nuevo acá.
  const isDesktop = useIsDesktop()
  // Alto REALMENTE visible. `100dvh`/`h-full` no sirven con el teclado abierto: el
  // teclado no cambia el viewport de LAYOUT (en iOS además desplaza la página, ver
  // `useVisualViewport`). Sólo se aplica por debajo de `lg` — en escritorio no hay
  // teclado virtual y `enabled: false` deja el hook inerte (sin listeners).
  const vv = useVisualViewport(!isDesktop)

  // El estado de vida CADUCA: una notebook se suspende sin avisar, así que se
  // re-consulta periódicamente. Sin esto el panel mostraría "respondiendo" sobre un
  // agente que ya murió, que es exactamente el falso verde que esta vista existe para
  // evitar.
  const agentsQ = useQuery({
    queryKey: ['chat', 'agents'],
    queryFn: fetchChatAgents,
    // agents: estado de fondo, cambia poco
    refetchInterval: poll.bgMs,
  })

  const agents = agentsQ.data?.agents ?? []
  const current = useMemo(
    () => agents.find((a) => a.name === selected) || null,
    [agents, selected]
  )

  // Única ruta de cambio de contacto (incluye la preselección): jamás llevar texto,
  // imágenes ni errores de A a B. El snapshot del envío en curso conserva su agent;
  // su completion sólo limpia si coincide con el borrador ACTUAL del mismo agente.
  function selectAgent(name: string) {
    if (name === selected) return
    setSelected(name)
    setDraft('')
    setImageFiles([])
    setSendError('')
    setSlashIdx(0)
    setRpcRes(null)
  }

  // Preselección: el primero que esté `respondiendo`; si no hay ninguno, el primero
  // de la lista (para que igual se VEA el motivo por el que no se puede hablar).
  useEffect(() => {
    if (selected || !agents.length) return
    selectAgent((agents.find((a) => a.status === 'respondiendo') || agents[0]).name)
  }, [agents, selected])

  // 🔴 «Se le puede escribir» se lee de `accepting_sends`, que es lo que el BFF DECLARA,
  // y NO de comparar contra el literal 'respondiendo'. Motivo medido (OLA A1): al
  // agregarse `respondió`, un agente que acaba de contestar cae en ESE estado, y la
  // comparación vieja dejaba el composer bloqueado justo en el momento en que uno quiere
  // responder. El fallback por status cubre a un BFF viejo que no manda el campo.
  const canSend =
    !!current &&
    (current.accepting_sends ??
      (current.status === 'respondiendo' || current.status === 'respondió'))
  // El agente está VIVO (contesta la sonda) — es lo que habilita pedirle estado y
  // catálogo. Hoy coincide con `canSend`; se nombra aparte porque son dos promesas
  // distintas y el día que difieran no hay que salir a buscar cuál era cuál.
  const isAlive = canSend
  const capQ = useQuery({
    queryKey: ['chat', 'capabilities', selected],
    queryFn: () => fetchChatCapabilities(selected),
    enabled: !!selected && canSend,
    refetchInterval: canSend ? poll.bgMs : false,
    retry: false,
  })
  const imageCap = capQ.data?.images
  const imageError = imageFiles.length ? validateImageFiles(imageFiles, imageCap) : null
  function addImages(files: File[]) {
    if (!files.length) return
    const next = [...imageFiles, ...files]
    const error = validateImageFiles(next, imageCap)
    if (error) { setSendError(error); return }
    setImageFiles(next)
    setSendError('')
  }

  // El historial se lee del árbol de sesión de pi vía el puente. Si el agente no está
  // alcanzable el BFF devuelve 409: NO se reintenta en loop contra un agente dormido.
  const msgsQ = useQuery({
    queryKey: ['chat', 'messages', selected],
    queryFn: () => fetchChatMessages(selected),
    enabled: !!selected && !!current && current.status !== 'no-configurado',
    // messages: lo que admin está esperando ⇒ va a la cadencia ACTIVA
    refetchInterval: canSend ? poll.msgMs : false,
    retry: false,
  })

  // Estado de OPERACIÓN (modelo/contexto/costo). Se consulta aparte de /messages
  // porque caduca a otro ritmo: el contexto avanza con cada turno. Solo si el agente
  // está `respondiendo` — pedirle stats a un proceso dormido daría 409 en loop.
  const opsEnabled = !!selected && canSend
  const stateQ = useQuery({
    queryKey: ['chat', 'state', selected],
    queryFn: () => fetchChatState(selected),
    enabled: opsEnabled,
    refetchInterval: opsEnabled ? poll.bgMs : false,
    retry: false,
  })

  // 🔴 El agente STREAMEANDO mantiene el sondeo vivo aunque admin no toque nada: cortarlo
  // mientras escribe dejaría la respuesta a medias en pantalla. El apagado es por
  // inactividad de admin, no por reloj ciego — un timer que ignora el estado convierte
  // una optimización en pérdida de datos.
  useEffect(() => {
    setMantenerDespierto(!!stateQ.data?.isStreaming)
  }, [stateQ.data?.isStreaming])
  const statsQ = useQuery({
    queryKey: ['chat', 'stats', selected],
    queryFn: () => fetchChatStats(selected),
    enabled: opsEnabled,
    refetchInterval: opsEnabled ? poll.bgMs : false,
    retry: false,
  })

  // ---------- Slash-commands (OLA A1) ----------
  // El desplegable se abre SOLO si el borrador empieza con `/` y NO tiene salto de
  // línea: `/algo` en la primera línea es un comando; una `/` dentro de un mensaje de
  // varias líneas es una barra. Se deriva del borrador en vez de guardar un bool aparte,
  // así no puede quedar «abierto» sobre un texto que ya no es un comando.
  const slashRaw = draft.startsWith('/') && !draft.includes('\n') ? draft.slice(1) : null
  // Lo que se tipeó después de la `/` y ANTES del primer espacio es el nombre; el resto
  // son argumentos (`/skill:tdd un test` -> command=skill:tdd, args='un test').
  const slashOpen = slashRaw !== null
  const [slashCmd, slashArgs] = (() => {
    if (slashRaw === null) return ['', '']
    const sp = slashRaw.indexOf(' ')
    return sp < 0 ? [slashRaw, ''] : [slashRaw.slice(0, sp), slashRaw.slice(sp + 1)]
  })()
  const [slashIdx, setSlashIdx] = useState(0)

  // El catálogo se pide UNA vez por agente y se cachea 5 min: son 32 comandos que no
  // cambian salvo que el agente instale una skill. `enabled` sólo con el agente vivo:
  // pedirle el catálogo a un proceso dormido daría 409 en loop.
  const cmdsQ = useQuery({
    queryKey: ['chat', 'commands', selected],
    queryFn: () => fetchChatCommands(selected),
    enabled: !!selected && isAlive,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const allCommands = cmdsQ.data?.commands ?? []
  const shownCommands = useMemo(
    () => (slashOpen ? filterCommands(allCommands, slashCmd) : []),
    [allCommands, slashOpen, slashCmd]
  )
  // El índice se reclama al filtro: si la lista se acortó, un índice viejo apuntaría a
  // otro comando y Enter EJECUTARÍA el equivocado en el proceso de otro agente.
  const activeIdx =
    shownCommands.length === 0 ? -1 : Math.min(slashIdx, shownCommands.length - 1)

  const runCmd = useMutation({
    // 🔴 RUTEO POR `via`. Los built-in de pi (/new, /compact, /model…) NO se ejecutan
    // mandándolos como texto: la doc de pi dice "would not execute if sent via prompt".
    // Si los mandáramos por la vía de texto, el modelo recibiría la cadena "/compact",
    // contestaría algo, y el usuario creería que compactó. Es el 200 mudo con forma de
    // éxito — el mismo patrón que kiro descartando imágenes.
    mutationFn: (v: { command: string; args: string; via?: string | null; rpc?: string | null }) => {
      if (v.via === 'rpc' && v.rpc) {
        // `args` de un comando RPC se interpreta como JSON de params si lo parece;
        // si no, se manda como `{value: args}` y el backend decide. No se inventa
        // un esquema por comando: cada uno tiene el suyo y está en su descripción.
        let params: Record<string, unknown> = {}
        const raw = (v.args || '').trim()
        if (raw) {
          try {
            const p = JSON.parse(raw)
            params = typeof p === 'object' && p !== null ? (p as Record<string, unknown>) : { value: raw }
          } catch {
            params = { value: raw }
          }
        }
        // Las dos ramas tienen que devolver el MISMO tipo o TS rechaza la union.
        // La mutation no necesita el `data` del RPC (cada comando devuelve otra forma):
        // solo saber que se acepto. Se normaliza en vez de forzar una forma comun, que
        // obligaria a inventar campos que no existen en una de las dos vias.
        // 🔴 SE CONSERVA el `data`: es lo que admin necesita ver (los 11 modelos, los
        // niveles de razonamiento, el árbol). Antes lo tiraba para que las dos ramas
        // tuvieran el mismo tipo — arreglé un error de compilación a costa de la
        // funcionalidad, que es peor que el error.
        return runChatRpc(selected, v.rpc, params).then((r) => ({
          accepted: true,
          command: v.rpc || v.command,
          data: r?.data,
        }))
      }
      return runChatCommand(selected, v.command, v.args).then((r) => ({
        accepted: r.accepted ?? true,
        command: v.command,
        data: undefined as unknown,
      }))
    },
    onSuccess: (r) => {
      // ejecutar un comando también es actividad: reinicia el reloj
      poll.ping()
      setDraft('')
      setSendError('')
      setSlashIdx(0)
      // 🔴 CONSULTA vs ACCIÓN, distinguidas por la FORMA de la respuesta.
      //
      // QA de admin: «el comando new no se manda bien». El backend funcionaba (200 y el
      // sessionId cambiaba de verdad), pero `new_session` devuelve `{cancelled: false}`
      // — que NO es undefined — así que mi condición abría el panel y le mostraba
      // `{"cancelled": false}` como JSON crudo. Un objeto técnico donde debía decir
      // «sesión nueva creada»: parecía que había fallado.
      //
      // Los comandos que responden `{cancelled}` son ACCIONES: lo que importa es si se
      // hizo, no su payload. Las CONSULTAS (models, levels, tree, stats) sí abren panel.
      const rr = r as { command?: string; data?: unknown } | undefined
      const dd = (rr?.data ?? undefined) as Record<string, unknown> | undefined
      const esAccion = !!dd && typeof dd === 'object' && 'cancelled' in dd
      if (esAccion) {
        // 🔴 Si una extensión la canceló hay que DECIRLO: un «listo» sobre algo que no
        // pasó es exactamente el 200 mudo que evito en todo lo demás.
        if (dd?.cancelled === true) {
          setSendError(
            `/${rr?.command || 'comando'}: una extensión del agente lo canceló. No se aplicó.`
          )
        } else {
          setCmdOk(`/${rr?.command || 'comando'}: aplicado`)
        }
      } else if (rr?.data !== undefined) {
        setRpcRes(rr)
      }
      // 202 = aceptado; la respuesta llega por polling. Se invalida lo mismo que en un
      // envío normal: un comando consume contexto y plata igual que un mensaje, y
      // `set_model`/`compact` cambian el estado de operación que muestra la barra.
      qc.invalidateQueries({ queryKey: ['chat', 'messages', selected] })
      qc.invalidateQueries({ queryKey: ['chat', 'agents'] })
      qc.invalidateQueries({ queryKey: ['chat', 'state', selected] })
      qc.invalidateQueries({ queryKey: ['chat', 'stats', selected] })
    },
    onError: (e) => {
      // 🔴 EL MOTIVO SE MUESTRA TAL CUAL LO MANDA EL BFF. Un «no se pudo ejecutar»
      // genérico borraría la única distinción que importa acá: «ese comando no existe»
      // (422) vs «existe pero no te lo autorizaron» (403 con `reason`) vs «el agente no
      // puede recibirlo ahora» (409 con `status`). El borrador NO se limpia: el comando
      // no se perdió y se puede corregir.
      const d = chatErrorDetail(e)
      const st = e instanceof ApiError ? e.status : 0
      let msg = e instanceof Error ? e.message : 'No se pudo ejecutar el comando'
      if (st === 403 && d?.reason) {
        msg = `No autorizado: ${d.reason}`
      } else if (st === 409) {
        // El estado del agente viene en el detalle: se dice cuál es, no «no disponible».
        const parts = [d?.status, d?.detail].filter(Boolean).join(' — ')
        msg = `No se ejecutó: ${d?.agent || selected} está ${parts || 'no disponible'}`
      } else if (st === 504) {
        // No se afirma que no se ejecutó: pudo haberse escrito al pipe.
        msg = `El puente no contestó: la ejecución quedó INDETERMINADA (no se puede afirmar que no corrió). ${msg}`
      }
      if (d?.sent === false && st !== 504) msg += ' · el BFF confirma que no se envió'
      setSendError(msg)
      qc.invalidateQueries({ queryKey: ['chat', 'agents'] })
    },
  })

  const send = useMutation({
    mutationFn: async ({agent, text, files}: {agent: string; text: string; files: File[]; draft: string}) => {
      if (!files.length) return sendChatMessage(agent, text)
      // Capability may have changed since selection: BFF rechecks again on send.
      const fresh = await fetchChatCapabilities(agent)
      const error = validateImageFiles(files, fresh.images)
      if (error) throw new Error(error)
      return sendChatMessage(agent, text, await encodeImageFiles(files))
    },
    onSuccess: (_result, sent) => {
      poll.ping()
      if (shouldClearSentDraft(sent, liveDraft.current)) {
        setDraft('')
        setImageFiles([])
        setSendError('')
      }
      // Invalidate the agent that actually received the message, not a later selection.
      qc.invalidateQueries({ queryKey: ['chat', 'messages', sent.agent] })
      qc.invalidateQueries({ queryKey: ['chat', 'agents'] })
      qc.invalidateQueries({ queryKey: ['chat', 'state', sent.agent] })
      qc.invalidateQueries({ queryKey: ['chat', 'stats', sent.agent] })
    },
    onError: (e, sent) => {
      // No mostrar un error de A sobre el composer de B: ni se limpia ni se
      // reinyecta el borrador anterior. Sólo el agente vigente recibe su error.
      if (sent.agent === liveDraft.current.agent) {
        setSendError(e instanceof Error ? e.message : 'No se pudo enviar')
      }
      qc.invalidateQueries({ queryKey: ['chat', 'agents'] })
    },
  })

  // Autoscroll al último mensaje. Depende TAMBIÉN de la geometría del viewport
  // visible: al abrir el teclado el navegador recorta/desplaza la vista y el último
  // mensaje queda tapado justo cuando estás escribiendo. `vv.height`/`vv.offsetTop`
  // sólo cambian de valor cuando el teclado aparece o desaparece, así que esto NO
  // corre en cada frame ni compite con el scroll manual del usuario.
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' })
  }, [msgsQ.data, vv.height, vv.offsetTop])

  /** Ejecuta un comando del catálogo. Se completa el nombre EXACTO del comando elegido
   *  y se conservan los argumentos ya tipeados. */
  function pickCommand(c: ChatCommand) {
    if (c.allowed === false) {
      // No se dispara un pedido que el BFF ya dijo que va a rechazar: el motivo YA está
      // en el catálogo. Se muestra ese motivo, que es el mismo que daría el 403.
      setSendError(
        `No autorizado: ${c.denied_reason || `${selected} no autorizó /${c.name} desde el panel`}`
      )
      return
    }
    if (!canSend || runCmd.isPending) return
    runCmd.mutate({ command: c.name, args: slashArgs, via: c.via, rpc: c.rpc_command })
  }

  function submit() {
    const text = draft.trim()
    if ((!text && !imageFiles.length) || !canSend || send.isPending || runCmd.isPending) return
    if (imageFiles.length && imageError) { setSendError(imageError); return }
    if (imageFiles.length && text.startsWith('/')) {
      setSendError('Las imágenes no se adjuntan a comandos. Quitalas para ejecutar el comando.')
      return
    }
    // Un borrador que es un comando se manda por /api/chat/command y NO por /send:
    // son dos rutas con auditoría distinta (la de comandos registra el NOMBRE) y con
    // autorización distinta (lista negra + agentes que autorizaron escritura de runtime).
    // Colarlo por /send saltearía esos controles escribiendo el mismo texto.
    if (slashOpen && slashCmd.trim()) {
      const hit =
        shownCommands[activeIdx] ||
        allCommands.find((c) => c.name.toLowerCase() === slashCmd.trim().toLowerCase())
      if (hit) {
        pickCommand(hit)
        return
      }
      // Ningún comando del catálogo coincide: se manda igual. pi es el que sabe si
      // existe (una skill instalada después del último fetch es un caso real), y el BFF
      // propaga su respuesta. Filtrar contra el catálogo cacheado haría invisible
      // cualquier comando nuevo.
      runCmd.mutate({ command: slashCmd.trim(), args: slashArgs })
      return
    }
    send.mutate({agent: selected, text, draft, files: imageFiles})
  }

  if (agentsQ.isError) return <ErrorState error={agentsQ.error} />

  return (
    /* Alto: `h-full` mientras NO hay teclado (comportamiento actual intacto). Con el
       teclado abierto se fija al alto VISIBLE y se compensa el desplazamiento que
       Safari iOS le aplica a la página (`offsetTop`), para que el composer quede
       pegado al borde del teclado en vez de irse fuera de la zona visible. El estilo
       inline es `undefined` en escritorio y sin teclado ⇒ cero efecto ahí. */
    <div
      className="flex h-full min-h-0"
      style={
        vv.keyboardOpen
          ? { height: `${vv.height}px`, maxHeight: `${vv.height}px`, transform: `translateY(${vv.offsetTop}px)` }
          : undefined
      }
    >
      {/* ---------- Selector de agentes CON estado de vida ---------- */}
      {/* Columna de agentes: SOLO escritorio. Por debajo de `lg` la reemplaza
          MobileAgentPicker y la conversación toma el 100% del ancho. */}
      <aside className="hidden w-[264px] shrink-0 flex-col border-r border-[var(--border-subtle)] lg:flex">
        <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
          <MessagesSquare size={14} className="text-accent" />
          <span className="text-[12px] font-medium text-fg">Agentes</span>
          <button
            type="button"
            onClick={() => agentsQ.refetch()}
            title="Re-consultar el estado de vida"
            className="ml-auto rounded p-1 text-fg-faint hover:bg-surface-hover hover:text-fg"
          >
            <RefreshCw size={12} className={agentsQ.isFetching ? 'animate-spin' : ''} />
          </button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2">
          {agentsQ.isLoading ? (
            <span className="flex items-center gap-2 px-1 py-3 text-[12px] text-fg-subtle">
              <Loader2 size={12} className="animate-spin" /> consultando estado…
            </span>
          ) : !agents.length ? (
            <p className="px-1 py-3 text-[12px] text-fg-subtle">
              No hay agentes configurados para chat.
            </p>
          ) : (
            // 🔴 AGRUPADO POR PADRE (pedido de admin: «que diga assistant-e grupo… o voy a
            // terminar con una cantidad de chats separados sin ordenar»).
            // `agruparAgentes` deriva la jerarquía del NOMBRE (assistant-e-beta ⇒ hijo de
            // assistant-e), que es una CONVENCIÓN, no un dato del backend: está aislada en
            // esa función para que el día que exista la jerarquía real se cambie ahí.
            // Un agente sin hijos se renderiza igual que antes (sin cabecera de grupo).
            agruparAgentes(agents).map(({ padre, hijos }) => (
              <AgentGroup
                key={padre.name}
                padre={padre}
                hijos={hijos}
                selected={selected}
                renderItem={(a) => (
                  <AgentRow
                    key={a.name}
                    agent={a}
                    active={a.name === selected}
                    onSelect={() => selectAgent(a.name)}
                  />
                )}
              />
            ))
          )}
        </div>
      </aside>

      {/* ---------- Conversación ---------- */}
      <section className="flex min-w-0 flex-1 flex-col">
        {!isDesktop && (
          <MobileAgentPicker
            agents={agents}
            selected={selected}
            loading={agentsQ.isLoading}
            fetching={agentsQ.isFetching}
            onSelect={selectAgent}
            onRefresh={() => agentsQ.refetch()}
          />
        )}
        {!current ? (
          <div className="flex flex-1 items-center justify-center text-[12px] text-fg-subtle">
            Elegí un agente.
          </div>
        ) : (
          <>
            <header className="flex shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2 lg:px-4">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${stMeta(current.status).dot} ${
                  stMeta(current.status).pulse ? 'animate-pulse' : ''
                }`}
                aria-hidden
              />
              <span className="shrink-0 text-[13px] font-medium text-fg">{current.name}</span>
              <span
                className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${stMeta(current.status).cls}`}
              >
                {stMeta(current.status).label}
              </span>
              {/* La bandera también en el header: en móvil la barra de operación está
                  COLAPSADA por default, y «está trabajando ahora» no puede depender de
                  que alguien la despliegue. Es el MISMO dato (`stateQ`), no una segunda
                  fuente. */}
              <BusyFlag
                streaming={!!stateQ.data?.isStreaming}
                compacting={!!stateQ.data?.isCompacting}
                thinkLevel={(stateQ.data?.thinkingLevel || '').trim()}
              />
              {/* `min-w-0` es lo que habilita el truncate: sin eso el motivo empuja
                  el header y el nombre del agente se corta a mitad de palabra. */}
              <span className="min-w-0 truncate text-[11px] text-fg-subtle">{current.detail}</span>
            </header>

            {/* Barra de operación. Se muestra solo cuando el agente puede contestar:
                con un proceso dormido no hay modelo ni contexto que informar, y una
                barra con todo en «desconocido» sugeriría una falla del panel en vez
                del estado real del agente (que ya dice el header). */}
            {opsEnabled && (
              <OpsBar
                state={stateQ.data}
                stats={statsQ.data}
                loading={stateQ.isLoading || statsQ.isLoading}
                error={stateQ.isError || statsQ.isError}
                isDesktop={isDesktop}
              />
            )}

            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3 lg:px-4 lg:py-4">
              {msgsQ.isLoading ? (
                <span className="flex items-center gap-2 text-[12px] text-fg-subtle">
                  <Loader2 size={12} className="animate-spin" /> cargando conversación…
                </span>
              ) : msgsQ.isError ? (
                <p className="text-[12px] text-fg-subtle">
                  No se pudo leer la conversación:{' '}
                  {msgsQ.error instanceof Error ? msgsQ.error.message : 'error'}
                </p>
              ) : (
                <History
                  messages={msgsQ.data?.messages ?? []}
                  // OPTIMISTA + REAL: `send.isPending`/`runCmd.isPending` cubren el
                  // instante en que se manda (antes de que el backend sepa nada), y
                  // `isStreaming` cubre mientras el agente contesta de verdad.
                  trabajando={
                    send.isPending || runCmd.isPending || !!stateQ.data?.isStreaming
                  }
                  nivel={stateQ.data?.thinkingLevel}
                  streaming={!!stateQ.data?.isStreaming}
                />
              )}
              <div ref={endRef} />
            </div>

            {/* ── PANELES DE LA OLA A2 ──────────────────────────────────────
                Van AQUI, entre la conversación y el composer, porque lo que bloquea
                al agente y lo que hace su subagente son parte de esta conversación,
                no de otra pantalla.

                🔴 Los diálogos van PRIMERO: si el agente está esperando una decisión,
                está DETENIDO, y eso pesa más que cualquier otra cosa en pantalla. */}
            {/* Aviso de acción aplicada (/new, /compact, cambio de modelo). Es la
                confirmación que faltaba: `new_session` devolvía {cancelled:false} y el
                usuario veía ese JSON crudo, no un "listo". */}
            {/* 🔴 Si el sondeo está DORMIDO hay que DECIRLO: un panel que dejó de
                refrescar y no lo avisa es indistinguible de uno colgado, y admin
                estaría mirando datos viejos creyendo que son de ahora. */}
            {poll.tier === 'dormido' ? (
              <button
                onClick={() => poll.ping()}
                className="flex min-h-[44px] w-full items-center gap-2 border-t border-[var(--border-subtle)] bg-surface px-3 text-left text-[12px] text-fg-faint hover:text-fg lg:min-h-0 lg:px-4 lg:py-1.5"
              >
                <span className="inline-flex h-1.5 w-1.5 shrink-0 rounded-full bg-fg-faint/40" />
                sondeo en pausa por inactividad · tocá acá o escribí para reactivarlo
              </button>
            ) : null}

            {cmdOk ? (
              <div className="border-t border-emerald-500/40 bg-emerald-500/10 px-3 py-1.5 text-[12px] text-emerald-300 lg:px-4">
                ✓ {cmdOk}
              </div>
            ) : null}

            {/* Resultado de un comando RPC de lectura, ACCIONABLE: elegir un modelo
                de la lista ejecuta `set_model`, como en la TUI. Va antes de los
                diálogos porque es la consecuencia directa de lo que el usuario acaba
                de tipear. */}
            <RpcResultPanel
              res={rpcRes}
              busy={runCmd.isPending}
              onClose={() => setRpcRes(null)}
              onPick={(rpc, params, etiqueta) => {
                setRpcRes(null)
                setSendError('')
                // Se ejecuta la escritura y se avisa qué se hizo: un cambio de modelo
                // sin confirmación deja al usuario sin saber si tomó.
                runCmd.mutate(
                  { command: etiqueta, args: JSON.stringify(params), via: 'rpc', rpc },
                  { onError: () => setSendError(`No se pudo aplicar: ${etiqueta}`) }
                )
              }}
            />

            {selected && (
              <PendingDialogs
                agent={selected}
                canWrite={CHAT_WRITE_AGENTS.includes(selected)}
                pollMs={poll.msgMs}
              />
            )}

            {/* Subagentes. En móvil el detalle REEMPLAZA la vista (con "volver"); en
                escritorio podría ir al lado, pero se mantiene el mismo flujo para no
                tener dos comportamientos que divergen. */}
            {selected && (
              <SubagentsPanel
                agent={selected}
                sel={subagentSel}
                onSel={setSubagentSel}
                pollMs={poll.bgMs}
              />
            )}

            {/* Caja de envío. Deshabilitada CON EL MOTIVO cuando el agente no puede
                recibir: un input gris sin explicación obliga a adivinar. */}
            <footer className="shrink-0 border-t border-[var(--border-subtle)] p-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] lg:pb-3">
              {!canSend && (
                <p className="mb-2 flex items-start gap-1.5 text-[11px] text-amber-300">
                  <ShieldAlert size={12} className="mt-0.5 shrink-0" />
                  <span>
                    No se puede enviar: {current.name} está{' '}
                    <strong>{stMeta(current.status).label}</strong>. {current.detail}
                  </span>
                </p>
              )}
              {sendError && (
                <p className="mb-2 text-[11px] text-red-300">{sendError}</p>
              )}
              {!imageCap?.supported && (
                <p className="mb-2 text-[11px] text-amber-300">Imágenes no disponibles: {imageCap?.reason || 'no se pudo comprobar /capabilities del agente'}. Los mensajes de texto siguen disponibles.</p>
              )}
              {imageFiles.length > 0 && <div className="mb-2 flex flex-wrap gap-2 text-[11px]">
                {imageFiles.map((f, i) => <span key={`${i}-${f.name}`} className="rounded border border-[var(--border-subtle)] px-2 py-1">
                  {f.name} ({(f.size / 1024).toFixed(1)} KB)
                  <button type="button" aria-label={`Quitar ${f.name}`} className="ml-2" onClick={() => setImageFiles(fs => fs.filter((_, j) => j !== i))}>×</button>
                </span>)}
              </div>}
              <input ref={fileRef} type="file" multiple accept={IMAGE_MIMES.join(',')} className="hidden" aria-label="Elegir imágenes" onChange={e => { addImages(Array.from(e.target.files || [])); e.target.value = '' }} />
              <button type="button" disabled={!canSend || !imageCap?.supported || send.isPending} onClick={() => fileRef.current?.click()} className="mb-2 rounded border border-[var(--border-subtle)] px-2 py-1 text-[11px] disabled:opacity-50">Adjuntar imagen</button>
              {/* Móvil: el composer es UNA sola fila. El botón Enviar NO es una fila
                  propia (antes se comía 44px de alto a ancho completo: pedido de
                  admin «ocupa espacio»): es un ICONO `absolute` al final del campo,
                  con caja tocable de 44×44 CSS y `aria-label` porque pierde el texto
                  visible. El `pr-14` del textarea reserva ese espacio para que el
                  texto no pase por debajo. En `lg:` el mismo botón vuelve a `static`
                  y recupera su label: en escritorio no hay problema de espacio y no
                  se toca lo que ya funciona. Es UN solo <button> a propósito — dos
                  botones (uno por breakpoint) divergirían al primer cambio.

                  🔴 Y el `text-[16px]` del textarea NO es cosmético: Safari en iOS hace
                  ZOOM AUTOMÁTICO al enfocar cualquier campo con font-size < 16px. Con
                  13px, tocar el textarea ampliaba y descentraba la página entera — que
                  es exactamente el «cuando escribo la pantalla no se adapta bien» que
                  reportó admin desde su iPhone. El meta viewport se deja INTACTO a
                  propósito: `maximum-scale=1`/`user-scalable=no` bloquean el zoom del
                  usuario y eso es una regresión de accesibilidad. En `lg:` vuelve a
                  13px porque en escritorio enfocar no dispara zoom. */}
              <div className="relative flex flex-col items-stretch gap-2 lg:flex-row lg:items-end">
                {/* La paleta es hermana del textarea dentro de este `relative`, y abre
                    hacia ARRIBA (`bottom-full`): hacia abajo taparía el campo donde se
                    escribe el filtro, y en el iPhone quedaría detrás del teclado. */}
                {slashOpen && canSend && (
                  <SlashPalette
                    commands={shownCommands}
                    loading={cmdsQ.isLoading}
                    error={cmdsQ.isError ? cmdsQ.error : null}
                    deniedHard={cmdsQ.data?.denied_hard ?? 0}
                    query={slashCmd}
                    activeIdx={activeIdx}
                    onPick={pickCommand}
                    onHover={setSlashIdx}
                  />
                )}
                <textarea
                  value={draft}
                  onPaste={(e) => {
                    const files = Array.from(e.clipboardData.files).filter(f => f.type.startsWith('image/'))
                    if (shouldInterceptImagePaste(imageCap, files)) { e.preventDefault(); addImages(files) }
                  }}
                  onChange={(e) => {
                    setDraft(e.target.value)
                    // El índice vuelve arriba en cada tecla: mantenerlo apuntaría a un
                    // comando distinto después de filtrar.
                    setSlashIdx(0)
                  }}
                  onKeyDown={(e) => {
                    // Con la paleta abierta, las flechas navegan la LISTA y no el texto,
                    // y Escape la cierra borrando la `/` (que es lo que la abre).
                    if (slashOpen && shownCommands.length) {
                      if (e.key === 'ArrowDown') {
                        e.preventDefault()
                        setSlashIdx((i) => (i + 1) % shownCommands.length)
                        return
                      }
                      if (e.key === 'ArrowUp') {
                        e.preventDefault()
                        setSlashIdx(
                          (i) => (i - 1 + shownCommands.length) % shownCommands.length
                        )
                        return
                      }
                      if (e.key === 'Tab') {
                        // Tab COMPLETA el nombre sin ejecutar: permite revisar antes de
                        // disparar algo irreversible.
                        const hit = shownCommands[activeIdx]
                        if (hit) {
                          e.preventDefault()
                          setDraft(`/${hit.name} `)
                        }
                        return
                      }
                    }
                    if (e.key === 'Escape' && slashOpen) {
                      e.preventDefault()
                      setDraft('')
                      setSlashIdx(0)
                      return
                    }
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      submit()
                    }
                  }}
                  // El envío de A no bloquea escribir el nuevo borrador de B.
                  // El botón Enviar sigue bloqueado hasta que termine la mutation.
                  disabled={!canSend || runCmd.isPending}
                  rows={2}
                  aria-autocomplete="list"
                  aria-expanded={slashOpen}
                  aria-controls={slashOpen ? 'slash-palette' : undefined}
                  placeholder={
                    canSend
                      ? `Escribile a ${current.name}…  (/ para comandos · Enter envía)`
                      : `${current.name} no puede recibir mensajes ahora`
                  }
                  aria-label={`Mensaje para ${current.name}`}
                  className="min-h-[44px] w-full resize-y rounded-md border border-[var(--border-subtle)] bg-surface py-2 pl-3 pr-14 text-[16px] text-fg outline-none placeholder:text-fg-faint focus:border-[var(--border-strong)] disabled:cursor-not-allowed disabled:opacity-50 lg:w-auto lg:flex-1 lg:pr-3 lg:text-[13px]"
                />
                <button
                  type="button"
                  onClick={submit}
                  disabled={!canSend || (!draft.trim() && !imageFiles.length) || !!imageError || send.isPending || runCmd.isPending}
                  // El label CAMBIA con lo que se va a hacer: mandar un mensaje y
                  // ejecutar un comando en el runtime de otro agente no son la misma
                  // acción, y el botón no puede decir lo mismo en los dos casos.
                  aria-label={
                    slashOpen && slashCmd.trim()
                      ? `Ejecutar /${slashCmd.trim()} en ${current.name}`
                      : 'Enviar mensaje'
                  }
                  title={
                    slashOpen && slashCmd.trim()
                      ? `Ejecutar /${slashCmd.trim()} en ${current.name}`
                      : undefined
                  }
                  className="absolute bottom-2 right-2 flex h-[44px] w-[44px] shrink-0 items-center justify-center gap-1.5 rounded-md border border-[var(--border-strong)] bg-surface-hover text-[12px] font-medium text-fg hover:bg-surface disabled:cursor-not-allowed disabled:opacity-50 lg:static lg:w-auto lg:justify-start lg:px-3"
                >
                  {send.isPending || runCmd.isPending ? (
                    <Loader2 size={18} className="animate-spin lg:h-[13px] lg:w-[13px]" />
                  ) : slashOpen && slashCmd.trim() ? (
                    <TerminalSquare size={18} className="lg:h-[13px] lg:w-[13px]" />
                  ) : (
                    <Send size={18} className="lg:h-[13px] lg:w-[13px]" />
                  )}
                  <span className="hidden lg:inline">
                    {slashOpen && slashCmd.trim() ? 'Ejecutar' : 'Enviar'}
                  </span>
                </button>
              </div>
            </footer>
          </>
        )}
      </section>
    </div>
  )
}
