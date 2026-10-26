import { useAppStore } from '../store'
import type { RegistroBuzones } from './correspondenciaAdmin'

export interface TreeNode {
  name: string
  path: string
  type: 'dir' | 'file'
  children?: TreeNode[]
}

export interface NodeContent {
  path: string
  content: string
  version: string
  bytes: number
  lossy?: boolean
}

export interface SaveResult {
  ok: boolean
  path: string
  bytes: number
  version: string
}

export interface CreateResult {
  ok: boolean
  path: string
}

export interface SearchResult {
  path: string
  title: string | null
  score: number
  owner: string | null
  snippet: string
}

export interface SearchResponse {
  results: SearchResult[]
  error?: string
}

/** Error de API con código de estado HTTP adjunto Y el `detail` CRUDO del BFF.
 *
 *  🔴 `detail` no es un extra cosmético. El BFF devuelve OBJETOS en el detalle de sus
 *  rechazos con estado (`/api/chat/send` 409, `/api/chat/command` 403/409):
 *  `{error, agent, command, reason, status, detail, sent}`. Antes esto se pasaba como
 *  `message` de un Error, y `String(objeto)` lo convertía en **«[object Object]»** —
 *  o sea el motivo por el que un comando fue rechazado, que es exactamente el dato que
 *  hay que ver, llegaba a la pantalla borrado. Se guarda el objeto tal cual y el
 *  `message` se arma con sus campos legibles. */
export class ApiError extends Error {
  status: number
  detail?: unknown
  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** Campos del `detail` estructurado que devuelve el BFF en los rechazos del chat.
 *  Todos opcionales: se leen los que vengan y nunca se inventa el que falta. */
export interface ChatErrorDetail {
  error?: string
  agent?: string
  command?: string
  /** Motivo de un 403. Lo escribe el BFF (lista negra / runtime no autorizado). */
  reason?: string
  /** Estado de vida del agente en un 409. Mismo vocabulario que ChatAgentStatus. */
  status?: string
  detail?: string
  /** `false` EXPLÍCITO: el BFF afirma que NO escribió nada. Se muestra tal cual. */
  sent?: boolean
}

/** Extrae el `detail` estructurado de un error de API. Devuelve `null` si el error no
 *  traía objeto (un 500 de proxy, un fallo de red): así el llamador puede distinguir
 *  «el BFF explicó por qué» de «no hay explicación» en vez de mostrar campos vacíos. */
export function chatErrorDetail(e: unknown): ChatErrorDetail | null {
  if (!(e instanceof ApiError)) return null
  const d = e.detail
  if (!d || typeof d !== 'object' || Array.isArray(d)) return null
  return d as ChatErrorDetail
}

/** Mensaje legible a partir de un `detail` que puede ser string, objeto o nada.
 *  Fuente ÚNICA para apiGet y apiSend: si divergieran, el mismo 409 se leería distinto
 *  según el verbo con el que se pidió. */
function detailMessage(d: unknown, status: number, path: string): string {
  if (typeof d === 'string' && d.trim()) return d.trim()
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const o = d as Record<string, unknown>
    const parts = [o.error, o.reason, o.detail].filter(
      (x): x is string => typeof x === 'string' && !!x.trim()
    )
    if (parts.length) return parts.join(' — ')
  }
  return `Error ${status} en ${path}`
}

/** Lee el cuerpo de una respuesta fallida y devuelve su `detail` (o el cuerpo entero
 *  si no tiene ese campo). Nunca tira: un body no-JSON da `null`. */
async function readDetail(res: Response): Promise<unknown> {
  const body = await res.json().catch(() => null)
  if (body && typeof body === 'object' && 'detail' in body) {
    return (body as { detail?: unknown }).detail
  }
  return body
}

// Same-origin: el BFF sirve el build y proxya /login + /api/*
function authHeaders(): HeadersInit {
  const token = useAppStore.getState().token
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export async function login(
  username: string,
  password: string
): Promise<{ token: string; must_change_password: boolean }> {
  const res = await fetch('/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(detail?.detail || `Login falló (${res.status})`)
  }
  const data = (await res.json()) as {
    token: string
    must_change_password?: boolean
  }
  return { token: data.token, must_change_password: !!data.must_change_password }
}

export interface ScopeGrant {
  path_prefix: string
  can_write: boolean
}

export interface Me {
  id: number | null
  name: string
  kind: string
  role: string
  is_admin: boolean
  admin_user: string
  email: string | null
  team?: string | null
  is_lead?: boolean
  must_change_password: boolean
  emergency: boolean
  /** El principal está habilitado a LEER el bus de la flota (guard
   *  `_require_bus_reader` del BFF: humano + admin + allowlist nominal
   *  BUS_HUMAN_READERS). Lo resuelve el BACKEND: el frontend no puede inferirlo
   *  del rol, porque `admin` lo comparten seis agentes. Solo controla si la
   *  entrada del menú se ofrece; el permiso REAL lo hace cumplir el BFF. */
  can_read_bus?: boolean
  /** El principal puede PUBLICAR en el bus (human + admin + BUS_HUMAN_PUBLISHERS).
   *  Es independiente de lectura; sólo controla la oferta visual, el BFF hace cumplir. */
  can_publish_bus?: boolean
  /** El principal está habilitado a ENVIAR mensajes de chat a un agente (guard
   *  `_require_chat_sender` del BFF: humano + admin + allowlist nominal
   *  CHAT_HUMAN_SENDERS). Permiso SEPARADO de can_read_bus: leer el bus es una
   *  lectura, un send es EJECUCIÓN POR DELEGACIÓN en un proceso pi vivo. Solo
   *  controla si la entrada del menú se ofrece; el permiso REAL lo hace cumplir el
   *  BFF con 403 en cada request. */
  can_use_chat?: boolean
  scope_grants: ScopeGrant[]
}

/** Info del principal de la sesión actual (para "Mi cuenta" y el flujo de
 * cambio forzado de contraseña). */
export function fetchMe(): Promise<Me> {
  return apiGet<Me>('/me')
}

/** Rota la contraseña del principal actual. Al completar, el backend pone
 * must_change_password=false. */
export function changePassword(
  oldPassword: string,
  newPassword: string
): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/change-password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
}

/** Revoca la sesión actual en el backend (sessions.revoked_at). */
export function logoutSession(): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/logout', {})
}

// ---------- Fase 2 authz: panel de administración (solo role=admin) ----------

export interface AdminGrant {
  id: number
  path_prefix: string
  can_write: boolean
}

export interface AdminPrincipal {
  id: number
  name: string
  kind: string
  role: string
  team: string | null
  active: boolean
  owner_principal_id: number | null
  owner_name: string | null
  scope_grants: AdminGrant[]
}

/** Lista todos los principals (humanos + agentes) con owner y scopes. */
export function fetchAdminPrincipals(): Promise<{ principals: AdminPrincipal[] }> {
  return apiGet<{ principals: AdminPrincipal[] }>('/admin/principals')
}

export interface CreatedUser {
  ok: boolean
  id: number
  name: string
  role: string
  /** Contraseña temporal generada. Se muestra UNA vez; nunca se persiste. */
  temp_password: string
}

/** Crea un usuario humano. Devuelve la contraseña temporal (mostrar una vez). */
export function adminCreateUser(
  username: string,
  role: string,
  email?: string
): Promise<CreatedUser> {
  return apiSend<CreatedUser>('/admin/users', {
    username,
    role,
    email: email || undefined,
  })
}

/** Crea un scope_grant para un principal (carpeta del vault + can_write). */
export function adminCreateGrant(
  principalId: number,
  pathPrefix: string,
  canWrite: boolean
): Promise<{ ok: boolean; id: number }> {
  return apiSend<{ ok: boolean; id: number }>('/admin/grants', {
    principal_id: principalId,
    path_prefix: pathPrefix,
    can_write: canWrite,
  })
}

/** Revoca un scope_grant (set revoked_at). */
export function adminRevokeGrant(grantId: number): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/admin/grants/revoke', { grant_id: grantId })
}

/** Asocia (o desasocia con null) un agente a un usuario humano dueño. */
export function adminAssignAgent(
  agentId: number,
  ownerPrincipalId: number | null
): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/admin/assign-agent', {
    agent_id: agentId,
    owner_principal_id: ownerPrincipalId,
  })
}

/* ==================== CHAT con los agentes (cambio de implementación) ==================== */

/** Estado de vida de un agente. Vocabulario CERRADO: lo define el BFF y lo PUBLICA en
 *  `states` de /api/chat/agents. El front no lo inventa ni lo deriva.
 *  respondiendo = la sonda positiva del puente contestó y el proceso está escribiendo
 *  respondió    = el proceso contesta la sonda pero YA terminó su turno (OLA A1):
 *                 hay algo nuevo para leer. Es un SUBESTADO de «vivo», no un estado
 *                 muerto — el BFF manda `accepting_sends: true` en él.
 *  no-responde  = el puente está vivo pero el proceso del agente no contesta
 *  dormido      = el puente no es alcanzable (la máquina del agente está abajo/suspendida)
 *  no-configurado = el panel no tiene URL o credencial para ese agente
 *
 *  🔴 `respondiendo` y `respondió` son los DOS estados VIVOS y se muestran distintos a
 *  propósito: «esperá, está escribiendo» y «hay respuesta para leer» son decisiones
 *  distintas. Colapsarlos borraría justamente la que admin pidió ver. Y ningún
 *  predicado de «se le puede escribir» se escribe comparando contra un literal: para
 *  eso está `accepting_sends`, que es lo que el BFF declara. */
export type ChatAgentStatus =
  | 'respondiendo'
  | 'respondió'
  | 'no-responde'
  | 'dormido'
  | 'no-configurado'

export interface ChatAgent {
  name: string
  status: ChatAgentStatus
  /** Motivo legible del estado. Se MUESTRA: un agente deshabilitado sin motivo
   *  obliga a adivinar si hay que esperar o arreglar algo. */
  detail: string
  accepting_sends?: boolean
  turn_health?: string | null
  booting?: boolean
  uptime_s?: number | null
}

export interface ChatMessage {
  id: string
  parentId?: string | null
  role: 'user' | 'assistant' | 'bashExecution' | 'toolResult'
  text?: string | null
  /** Razonamiento del turno, en campo SEPARADO del `text` a propósito: mezclarlos
   *  haría indistinguible lo que el agente DIJO de lo que PENSÓ, que es justo la
   *  distinción que se quiere ver. `null` = ese turno no produjo razonamiento (no es
   *  un error, y no es lo mismo que "el razonamiento está apagado" — eso lo dice
   *  `thinkingLevel` de /api/chat/state). El `thinkingSignature` del proveedor NO
   *  llega hasta acá: es material criptográfico de la conversación y se queda en el
   *  puente. */
  thinking?: string | null
  timestamp?: string | null
  stopReason?: string | null
}

/* ---- Estado de OPERACIÓN del proceso del agente (fase A.2, cambio de implementación) ---- */

/** Modelo que está usando el agente. `name` es el legible ('Claude Sonnet 5 (US)');
 *  el `id` crudo ('us.anthropic.claude-sonnet-5') no se muestra como etiqueta. */
export interface ChatModel {
  id?: string | null
  name?: string | null
  provider?: string | null
  contextWindow?: number | null
  maxTokens?: number | null
  reasoning?: boolean | null
}

export interface ChatState {
  agent: string
  model?: ChatModel | null
  /** Nivel de razonamiento del proceso ('max', 'high', 'off'…). Se MUESTRA: sin él,
   *  un mensaje sin `thinking` es ambiguo entre "no razonó este turno" y "está
   *  apagado". */
  thinkingLevel?: string | null
  isStreaming?: boolean | null
  isCompacting?: boolean | null
  autoCompactionEnabled?: boolean | null
  messageCount?: number | null
  pendingMessageCount?: number | null
  sessionId?: string | null
}

/** Contexto consumido. Los nombres son los del PUENTE (snake_case incluido): el BFF
 *  los propaga sin renombrar, así el front lee exactamente lo que el puente dice. */
export interface ChatContextUsage {
  tokens?: number | null
  contextWindow?: number | null
  percent?: number | null
}

export interface ChatStats {
  agent: string
  userMessages?: number | null
  assistantMessages?: number | null
  toolCalls?: number | null
  toolResults?: number | null
  totalMessages?: number | null
  tokens?: {
    input?: number | null
    output?: number | null
    cacheRead?: number | null
    cacheWrite?: number | null
    total?: number | null
  } | null
  /** Costo acumulado de la sesión, en USD. */
  cost?: number | null
  context_usage?: ChatContextUsage | null
  /** 🔴 EL CAMPO QUE DECIDE, no el número: un 0% legítimo (sesión recién arrancada) y
   *  un "no se puede saber" (sin modelo/ventana, o justo después de una compactación)
   *  son indistinguibles mirando `percent`. Si esto es `false` la UI dice
   *  «contexto: desconocido» y JAMÁS «0%». */
  context_known?: boolean | null
  context_unknown_reason?: string | null
}

/** Agentes con los que se puede chatear Y SU ESTADO DE VIDA. Devuelve 200 aunque
 *  TODOS estén dormidos: "no hay nadie despierto" es información, no un error. */
export function fetchChatAgents(): Promise<{
  agents: ChatAgent[]
  states: ChatAgentStatus[]
}> {
  return apiGet<{ agents: ChatAgent[]; states: ChatAgentStatus[] }>(
    '/api/chat/agents'
  )
}

/** Historial de la conversación con un agente. `since` es el cursor OPACO del
 *  puente (id de entrada de la sesión de pi): se manda tal cual, no se interpreta. */
export function fetchChatMessages(
  agent: string,
  since?: string
): Promise<{
  agent: string
  messages: ChatMessage[]
  cursor?: string | null
  leafId?: string | null
}> {
  const qs = new URLSearchParams({ agent })
  if (since) qs.set('since', since)
  return apiGet(`/api/chat/messages?${qs.toString()}`)
}

/** Estado del proceso pi del agente: modelo, nivel de razonamiento, streaming.
 *  SOLO LECTURA — cambiar el modelo o compactar NO se expone (sería una escritura
 *  sobre el proceso de otra agente, y eso necesita su propia autorización). */
export function fetchChatState(agent: string): Promise<ChatState> {
  const qs = new URLSearchParams({ agent })
  return apiGet<ChatState>(`/api/chat/state?${qs.toString()}`)
}

/** Consumo de la sesión: tokens, costo USD y contexto usado. Leer SIEMPRE
 *  `context_known` antes de usar `context_usage.percent`. */
export function fetchChatStats(agent: string): Promise<ChatStats> {
  const qs = new URLSearchParams({ agent })
  return apiGet<ChatStats>(`/api/chat/stats?${qs.toString()}`)
}

export interface ChatImage { mimeType: string; data: string }
export interface ChatImageCapability {
  supported: boolean
  reason?: string | null
  max_b64: number
  mimes: string[]
}

export function fetchChatCapabilities(agent: string): Promise<{agent: string; images: ChatImageCapability}> {
  return apiGet(`/api/chat/capabilities?${new URLSearchParams({agent}).toString()}`)
}

/** Envía un mensaje. El BFF devuelve **409** si el agente no está `respondiendo`
 *  (nunca un 200 mentiroso) y propaga el **202** del puente cuando lo acepta: la
 *  respuesta del agente llega después, por polling de fetchChatMessages. */
export function sendChatMessage(
  agent: string,
  message: string,
  images?: ChatImage[]
): Promise<{
  turn_id?: string
  accepted?: boolean
  queued?: boolean
  cursor_before?: string | null
}> {
  return apiSend('/api/chat/send', { agent, message, ...(images?.length ? {images} : {}) })
}

/* ---- Slash-commands del agente (OLA A1, cambio de implementación) ---- */

/** Un slash-command del catálogo de pi, ANOTADO por el BFF.
 *
 *  `allowed`/`denied_reason` los agrega el BFF, no el puente: el puente no sabe quién
 *  lee ni qué autorizó cada agente. Un comando con `allowed: false` EXISTE pero ese
 *  agente no autorizó que se escriba su runtime desde el panel — esconderlo haría
 *  indistinguible «no lo tiene» de «no te lo permiten». */
export interface ChatCommand {
  name: string
  description?: string | null
  /** De dónde sale el comando ('core', 'skill', 'extension'…). */
  source?: string | null
  sourceInfo?: string | null
  location?: string | null
  path?: string | null
  /** `false` = el BFF lo va a rechazar con 403. `undefined` = el BFF no anotó (versión
   *  vieja): se trata como permitido y la autoridad queda en la respuesta del POST. */
  allowed?: boolean | null
  denied_reason?: string | null
  /** Cómo se ejecuta este comando. NO es decorativo:
   *  - `prompt`: se manda el TEXTO `/nombre args` (extensiones, prompts, skills).
   *  - `rpc`   : se manda `POST /api/chat/rpc {command: rpc_command}`.
   *  🔴 No son intercambiables: la doc de pi dice que los built-in de la TUI
   *  "would not execute if sent via prompt" — mandarlos como texto hace que el
   *  modelo reciba la cadena y conteste algo, y el usuario crea que se ejecutó.
   *  Default `prompt` para no cambiar lo que ya funcionaba. */
  via?: 'prompt' | 'rpc' | null
  /** El comando RPC real cuando `via === 'rpc'` (ej: `new` → `new_session`). */
  rpc_command?: string | null
  /** Si escribe el runtime del agente (para avisar antes de un 403). */
  writes_runtime?: boolean | null
}

export interface ChatCommandsResponse {
  agent: string
  /** EXPLÍCITO y viaja aunque sea 0: «este agente no tiene comandos» (200 con count 0)
   *  no es lo mismo que «no pude preguntarle» (409/503). No se deriva de `commands`. */
  count?: number | null
  commands: ChatCommand[]
  /** Cuántos comandos se ELIMINARON por lista negra dura (hoy `bash`). Se muestra para
   *  que la ausencia sea VERIFICABLE y no se confunda con «el agente no lo tiene». */
  denied_hard?: number | null
  executable?: number | null
  source?: string | null
}

/** Catálogo de slash-commands del agente. Sólo LECTURA. */
export function fetchChatCommands(agent: string): Promise<ChatCommandsResponse> {
  const qs = new URLSearchParams({ agent })
  return apiGet<ChatCommandsResponse>(`/api/chat/commands?${qs.toString()}`)
}

/** EJECUTA un slash-command en el proceso del agente.
 *
 *  🔴 Es una ESCRITURA sobre el runtime de otro agente y NO se deshace: un comando
 *  ejecutado ya produjo efectos. El BFF pone los controles (lista negra, agentes que
 *  autorizaron, auditoría con el nombre del comando, sonda de estado antes de escribir).
 *  De este lado la regla es no disparar por parecido: se manda un nombre que el usuario
 *  eligió del catálogo o escribió exacto.
 *
 *  Códigos que devuelve el BFF y que el llamador tiene que distinguir:
 *    403 -> no autorizado, CON `reason` en el detalle (≠ «no existe»)
 *    409 -> el agente no puede recibir, CON `status` y `detail` (`sent: false`)
 *    422 -> el nombre no tiene forma de comando de pi
 *    504 -> el puente no contestó: la ejecución quedó INDETERMINADA
 *    202 -> aceptado; la respuesta llega después por polling de fetchChatMessages */
/** Ejecuta un comando por la vía RPC (`POST /api/chat/rpc`).
 *
 *  Es la vía de los built-in de pi: `/new` → `new_session`, `/compact` → `compact`,
 *  `/model` → `get_available_models`. **No** se pueden mandar como texto: la doc de pi
 *  dice que no se ejecutarían, y el modelo contestaría a la cadena literal.
 *
 *  Devuelve el `data` crudo del comando RPC, que cambia según el comando (una lista de
 *  modelos, un árbol de sesión, un ok). El llamador lo muestra como venga: proyectarlo
 *  a una forma fija acá haría perder información que no conozco de antemano.
 */
export function runChatRpc(
  agent: string,
  command: string,
  params: Record<string, unknown> = {}
): Promise<{ agent?: string; command?: string; data?: unknown }> {
  return apiSend('/api/chat/rpc', { agent, command, params })
}

export function runChatCommand(
  agent: string,
  command: string,
  args = ''
): Promise<{
  turn_id?: string
  accepted?: boolean
  queued?: boolean
  cursor_before?: string | null
}> {
  const body: { agent: string; command: string; args?: string } = { agent, command }
  if (args.trim()) body.args = args.trim()
  return apiSend('/api/chat/command', body)
}

/* ---- OLA A2: diálogos pendientes, subagentes en vivo (cambio de implementación) ---- */

/** Un `extension_ui_request` de pi capturado por el puente.
 *
 *  🔴 Hay DOS clases y no se pueden mezclar:
 *   · BLOQUEANTE (`confirm`/`select`/`input`/`editor`): el proceso del agente está
 *     DETENIDO esperando una decisión. Requiere acción.
 *   · AVISO (`notify`/`setStatus`/…): el agente informó algo y siguió. No requiere nada.
 *  El BFF los separa en `pending` vs `notices` y `count` cuenta SOLO los bloqueantes.
 *  Medido en los 5 agentes: assistant-a, assistant-e, assistant-g y assistant-b tienen un `notify` de
 *  identidad de su arranque en `notices`. Contarlos dejaría el indicador encendido para
 *  siempre, y un indicador que nunca se apaga no informa nada.
 *
 *  `asked_at` es un epoch en SEGUNDOS (float), NO un ISO-8601 como el resto de la API:
 *  viene de `time.time()` del puente (medido: 1787519472.9781976). Pasárselo a
 *  `new Date()` crudo daría 1970 — hay que multiplicar por 1000. */
export interface ChatPendingDialog {
  id: string
  /** 'confirm' | 'select' | 'input' | 'editor' para los bloqueantes; 'notify',
   *  'setStatus'… para los avisos. Es un string ABIERTO a propósito: si pi agrega un
   *  método nuevo, la UI lo muestra con su nombre en vez de romperse. */
  method: string
  title?: string | null
  message?: string | null
  placeholder?: string | null
  /** Opciones de un `select`. Pueden venir como strings o como objetos con label, así
   *  que se normalizan en la vista con guardas y no se asume la forma acá. */
  options?: unknown[] | null
  timeout?: number | null
  /** epoch en SEGUNDOS (float). Ver nota de arriba. */
  asked_at?: number | null
}

export interface ChatPendingResponse {
  agent: string
  /** SOLO los bloqueantes. Viaja explícito y NO se deriva de `pending.length`. */
  count?: number | null
  pending: ChatPendingDialog[]
  /** Avisos. NO son diálogos: no llevan botones ni cuentan para el indicador. */
  notices?: ChatPendingDialog[] | null
  /** Vocabulario de métodos bloqueantes que DECLARA el puente. El front no lo
   *  inventa: si mañana `editor` deja de bloquear, el cambio llega por acá. */
  blocking_methods?: string[] | null
  source?: string | null
}

/** Diálogos que el agente está esperando. */
export function fetchChatPending(agent: string): Promise<ChatPendingResponse> {
  const qs = new URLSearchParams({ agent })
  return apiGet<ChatPendingResponse>(`/api/chat/pending?${qs.toString()}`)
}

/** RESPONDE un diálogo y DESBLOQUEA al agente.
 *
 *  🔴 Es una ESCRITURA sobre el runtime de otro agente y no se deshace: una vez que se
 *  confirma, el agente ya siguió con esa decisión. El BFF exige que el agente haya
 *  autorizado escritura de runtime (403 con `reason` si no).
 *
 *  ⚠️ El puente CANCELA cuando no viaja ningún campo explícito. Por eso las tres formas
 *  son EXCLUYENTES en el tipo y el llamador elige una: `{confirmed}`, `{value}` o
 *  `{cancelled}`. Mandar un objeto vacío por descuido cancelaría en silencio. */
export function answerChatPending(
  agent: string,
  id: string,
  answer: { confirmed: boolean } | { value: string } | { cancelled: true }
): Promise<{ ok?: boolean; answered?: boolean; id?: string }> {
  return apiSend('/api/chat/pending', { agent, id, ...answer })
}

/** Una sesión de subagente EN DISCO (`<session-dir>/tasks/<id>.jsonl`).
 *
 *  🔴 `recent` es una HEURÍSTICA DECLARADA por el puente: el jsonl fue tocado hace
 *  <120s. El archivo NO tiene señal de proceso vivo, así que la UI dice «actividad
 *  reciente» y JAMÁS «corriendo»: afirmar que un proceso está vivo sobre un mtime sería
 *  inventar un dato que nadie midió. Un subagente que murió hace 10s da `recent: true`
 *  igual, y un subagente pensando sin escribir da `false`. */
export interface ChatSubagent {
  id: string
  file?: string | null
  size?: number | null
  /** epoch en SEGUNDOS (float), igual que `asked_at`. */
  mtime?: number | null
  age_s?: number | null
  recent?: boolean | null
}

export interface ChatSubagentsResponse {
  agent: string
  count?: number | null
  subagents: ChatSubagent[]
  source?: string | null
}

/** Sesiones de subagentes de un agente. */
export function fetchChatSubagents(agent: string): Promise<ChatSubagentsResponse> {
  const qs = new URLSearchParams({ agent })
  return apiGet<ChatSubagentsResponse>(`/api/chat/subagents?${qs.toString()}`)
}

/** Una entrada CRUDA del JSONL de la sesión del subagente.
 *
 *  🔴 NO son `ChatMessage` ya proyectados: son las líneas del árbol de pi tal como las
 *  escribe el runtime, y el puente las manda sin transformar (`json.loads` por línea,
 *  últimas 200). Forma MEDIDA sobre un jsonl real de pi 0.83.0 (48 entradas):
 *    type=session               → {version, id, timestamp, cwd, parentSession}
 *    type=model_change          → {provider, modelId}
 *    type=thinking_level_change → {thinkingLevel}
 *    type=message               → {message:{role, content, timestamp, …}}
 *  y dentro de `message`: role ∈ {user, assistant, toolResult}; `content` es un ARRAY de
 *  bloques con `type` ∈ {text, toolCall} (`toolCall` trae {name, arguments}).
 *  `toolResult` agrega {toolName, toolCallId, isError}. En la muestra medida: 26
 *  toolCall, 25 toolResult/text, 4 assistant/text, 1 user/text — o sea el trabajo de un
 *  subagente es MAYORITARIAMENTE herramientas, no prosa, y una vista que sólo mostrara
 *  texto se vería casi vacía.
 *
 *  Por eso el tipo es deliberadamente laxo y la proyección se hace en la vista con
 *  guardas: asumir la estructura acá haría que un tipo nuevo de entrada rompa el render
 *  en vez de mostrarse como desconocido. */
export interface ChatSubagentEntry {
  type?: string | null
  id?: string | null
  parentId?: string | null
  timestamp?: string | null
  message?: {
    role?: string | null
    content?: unknown
    timestamp?: string | null
    toolName?: string | null
    isError?: boolean | null
  } | null
  [k: string]: unknown
}

export interface ChatSubagentDetail {
  agent: string
  id: string
  count?: number | null
  entries: ChatSubagentEntry[]
}

/** Contenido de UNA sesión de subagente: lo que alimenta el chat dividido. */
export function fetchChatSubagent(
  agent: string,
  id: string
): Promise<ChatSubagentDetail> {
  const qs = new URLSearchParams({ agent, id })
  return apiGet<ChatSubagentDetail>(`/api/chat/subagent?${qs.toString()}`)
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: authHeaders() })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const d = await readDetail(res)
    throw new ApiError(res.status, detailMessage(d, res.status, path), d)
  }
  return (await res.json()) as T
}

async function apiSend<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const d = await readDetail(res)
    throw new ApiError(res.status, detailMessage(d, res.status, path), d)
  }
  return (await res.json()) as T
}

export function fetchTree(): Promise<TreeNode> {
  return apiGet<TreeNode>('/api/tree')
}

export function fetchNode(path: string): Promise<NodeContent> {
  return apiGet<NodeContent>(`/api/node?path=${encodeURIComponent(path)}`)
}

export async function fetchImageBytes(path: string): Promise<Blob> {
  const res = await fetch(`/api/image_bytes?path=${encodeURIComponent(path)}`, {
    headers: authHeaders(),
  })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) throw new ApiError(res.status, `Error ${res.status} al cargar imagen`)
  return await res.blob()
}

export function saveFile(path: string, content: string, baseVersion: string | null): Promise<SaveResult> {
  return apiSend<SaveResult>('/api/file_save', { path, content, base_version: baseVersion })
}

export function createFile(path: string, content = ''): Promise<CreateResult> {
  return apiSend<CreateResult>('/api/file_create', { path, content })
}

export function searchVault(q: string): Promise<SearchResponse> {
  return apiGet<SearchResponse>(`/api/search?q=${encodeURIComponent(q)}`)
}

export interface RenameResult {
  ok: boolean
  from: string
  to: string
}

export interface DeleteResult {
  ok: boolean
  /** Ruta relativa dentro de .trash donde quedó el archivo (soft delete). */
  trashed_to: string
}

export interface UploadResult {
  ok: boolean
  path: string
}

/** Renombra/mueve un archivo .md (el backend aún no admite carpetas). 409 si el destino ya existe. */
export function renameFile(path: string, newPath: string): Promise<RenameResult> {
  return apiSend<RenameResult>('/api/file_rename', { path, new_path: newPath })
}

/** Borrado SOFT: mueve el archivo a `.trash/` (recuperable). */
export function deleteFile(path: string): Promise<DeleteResult> {
  return apiSend<DeleteResult>('/api/file_delete', { path })
}

export interface DirStats {
  files: number
  symlinks: number
  truncated: boolean
  max_files: number
  allowed: boolean
}

export interface DeleteDirResult extends DeleteResult {
  files: number
  symlinks: number
}

export function dirStats(path: string): Promise<DirStats> {
  return apiGet<DirStats>(`/api/dir_stats?path=${encodeURIComponent(path)}`)
}

export function deleteDir(path: string): Promise<DeleteDirResult> {
  return apiSend<DeleteDirResult>('/api/dir_delete', { path })
}

/** Sube un adjunto (no-md) al directorio `dir`. Devuelve la ruta final en el vault. */
export async function uploadFile(file: File, dir: string): Promise<UploadResult> {
  const fd = new FormData()
  fd.append('file', file)
  fd.append('dir', dir)
  // No fijar Content-Type: el navegador agrega el boundary del multipart.
  const res = await fetch('/api/file_upload', {
    method: 'POST',
    headers: authHeaders(),
    body: fd,
  })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new ApiError(res.status, detail?.detail || `Error ${res.status} al subir`)
  }
  return (await res.json()) as UploadResult
}

// ---------- Fase C: tareas (tablero Kanban) ----------

export type TaskStatus =
  | 'todo'
  | 'in_progress'
  | 'submitted'
  | 'in_review'
  | 'blocked'
  | 'deferred'
  | 'done'
  | 'cancelled'

export interface Task {
  id: string
  title: string
  description: string | null
  project: string | null
  status: TaskStatus
  priority: string | null
  assignee: string | null
  /** Subagente que ejecuta la tarea (texto libre). '' = sin subagente. */
  subagent: string | null
  due_date: string | null
  /** Tarea padre (subtareas). null = tarea raíz. */
  parent_id: string | null
  /** Timestamps (timestamptz::text desde el backend). Para filtrar por mes. */
  updated_at?: string | null
  created_at?: string | null
  /** Etiquetas (text[] en la BD). Un "proyecto" es un tag `proyecto/<nombre>`. */
  tags?: string[] | null
  /** Agente que TOMÓ la tarea (claim). '' = sin tomar. (F2 issues) */
  claimed_by?: string
  /** Momento en que se tomó la tarea (timestamptz::text). */
  claimed_at?: string | null
  /** Fecha+hora REAL de cierre (se estampa al pasar a done/cancelled). */
  closed_at?: string | null
  /** SOLO UI (no viene del backend): con qué fecha la ubicó el calendario/timeline.
   *  'due' = fecha comprometida · 'created' = fecha de creación (respaldo). */
  _dateSource?: 'due' | 'created'
  /** SOLO UI: fecha EFECTIVA usada para ordenar dentro de un grupo (ISO). */
  _sortDate?: string | null
  /** Motivo de bloqueo cuando status='blocked' (dependency|external|info|resource). */
  blocked_reason?: string | null
  /** Organizacion (id/slug, ej 'acme'). La deriva el backend del proyecto via
   *  trigger, pero se puede fijar explicitamente. Opcional: puede no venir si el
   *  backend todavia es viejo. (jerarquia Organizacion -> Proyecto, ) */
  organization?: string | null
}

export interface TasksResponse {
  tasks: Task[]
}

export interface TaskInput {
  title?: string
  project?: string
  /** Organizacion de la tarea (id/slug). Allowlisted en el backend. */
  organization?: string
  priority?: string
  assignee?: string
  subagent?: string
  description?: string
  due_date?: string
  status?: TaskStatus
  tags?: string[]
  /** Padre (subtareas). BFF legacy: '' desvincula; REST/MCP públicos usan '-'. */
  parent_id?: string
}

/** Lista TODAS las tareas (incluye status='cancelled'). El frontend
 *  decide qué mostrar por columna y filtra Terminadas/Canceladas por mes. */
export interface ApprovalItem {
  id: string
  title: string
  status: string
  creator: string
  creator_kind: string
  version: number
  scope_hash: string
  created_at: string | null
  updated_at: string | null
}

export interface ApprovalReview {
  task_id: string
  version: number
  status: string
  scope_hash: string
  creator: string
  creator_kind: string
  approved_at: string | null
  revoked_at: string | null
  finished_at: string | null
  event_count: number
  effect_count: number
  title: string
  description: string | null
  created_at: string | null
  action_type: string
  resource_kind: string
  resource_id: string
  scope_version: number
  scope_canonical_text: string
}

export type ApprovalInboxStatus = 'pending_approval' | 'aprobada' | 'all'

export function approvalInbox(status: ApprovalInboxStatus = 'pending_approval', cursor?: string): Promise<{ items: ApprovalItem[]; next_cursor: string | null }> {
  const query = new URLSearchParams({ limit: '50', status })
  if (cursor) query.set('cursor', cursor)
  return apiGet(`/api/approval/inbox?${query.toString()}`)
}

export function approvalReview(id: string): Promise<ApprovalReview> {
  return apiGet(`/api/approval/requests/${encodeURIComponent(id)}/review`)
}

export function approvalDecision(id: string, action: 'approve' | 'revoke' | 'request-changes', body: {
  expected_version: number; expected_hash: string; reason?: string
}): Promise<unknown> {
  return apiSend(`/api/approval/requests/${encodeURIComponent(id)}/${action}`, body)
}

export function fetchTasks(): Promise<TasksResponse> {
  return apiGet<TasksResponse>('/api/tasks')
}

// ---------- Jerarquia Organizacion -> Proyecto (, cambio de implementación) ----------
// Antes un "proyecto" era solo `SELECT DISTINCT project FROM tasks`: no habia forma
// de decir que example-suite pertenece a acme. Ahora hay tablas reales `organizations` y
// `projects`, y `tasks.organization`.

/** Organizacion (nivel superior): 'acme', 'personal', … */
export interface Organization {
  id: string
  name: string
  /** Color hex ('#0ea5e9') para el chip. Puede venir vacio. */
  color: string
  sort_order: number
}

/** Proyecto con su organizacion. Reemplaza conceptualmente al `string` suelto. */
export interface ProjectRef {
  id: string
  name: string
  org_id: string
  /** 'active' | 'archived' (archivar, NUNCA borrar). */
  status: string
  sort_order: number
}

/** Metadata dinamica de la app: roster de agentes/lideres/subagentes por equipo
 *  y proyectos existentes. Provista por el backend (tablas agents/tasks). */
export interface Meta {
  agents: string[]
  leads: string[]
  leader_teams?: Record<string, string>
  subagents_by_team: Record<string, string[]>
  /** Proyectos como string[] — forma HISTORICA, se conserva para no romper los
   *  consumidores existentes (Board, HomeView, CommandPalette, TaskDialog…). */
  projects: string[]
  /** Organizaciones activas. Opcional: ausente si el backend es previo al deploy. */
  organizations?: Organization[]
  /** Proyectos con org_id. Opcional: ausente si el backend es previo al deploy.
   *  Cuando existe, es la fuente preferida sobre `projects`. */
  projects_v2?: ProjectRef[]
}

/** Trae la metadata dinamica (GET /api/meta). */
export function fetchMeta(): Promise<Meta> {
  return apiGet<Meta>('/api/meta')
}

// --- Administracion de organizaciones y proyectos (GET: cualquier sesion;
// POST: solo role=admin, el backend exige _require_admin). ---

/** Organizacion con conteos, tal como la devuelve GET /api/organizations. */
export interface OrganizationRow extends Organization {
  active: boolean
  /** Proyectos no archivados de la organizacion. */
  projects: number
  tasks_total: number
  tasks_open: number
}

/** Proyecto con su organizacion y conteos (GET /api/projects). */
export interface ProjectRow extends ProjectRef {
  org_name: string
  tasks_total: number
  tasks_open: number
}

/** Organizaciones con conteo de proyectos y tareas. */
export function fetchOrganizations(): Promise<{ organizations: OrganizationRow[] }> {
  return apiGet<{ organizations: OrganizationRow[] }>('/api/organizations')
}

/** Proyectos con su organizacion. `org` filtra por organizacion. */
export function fetchProjects(
  org = '',
  includeArchived = false
): Promise<{ projects: ProjectRow[] }> {
  const qs = new URLSearchParams()
  if (org) qs.set('org', org)
  if (includeArchived) qs.set('include_archived', 'true')
  const q = qs.toString()
  return apiGet<{ projects: ProjectRow[] }>('/api/projects' + (q ? `?${q}` : ''))
}

/** Crea un proyecto DENTRO de una organizacion. Idempotente por id:
 *  `created=false` significa que el id ya existia (no es un error). */
export function projectCreate(input: {
  id: string
  org_id: string
  name?: string
  sort_order?: number
}): Promise<{ project: ProjectRef | null; created: boolean }> {
  return apiSend<{ project: ProjectRef | null; created: boolean }>(
    '/api/project_create',
    input
  )
}

/** Edita un proyecto. Con `org_id` lo REASIGNA de organizacion y (salvo
 *  reassign_tasks=false) arrastra `organization` de todas sus tareas.
 *  `tasks_reassigned` informa cuantas tareas se movieron. */
export function projectUpdate(input: {
  id: string
  org_id?: string
  name?: string
  status?: string
  sort_order?: number
  reassign_tasks?: boolean
}): Promise<{ project: ProjectRef; tasks_reassigned: number }> {
  return apiSend<{ project: ProjectRef; tasks_reassigned: number }>(
    '/api/project_update',
    input
  )
}

/** Crea una organizacion. Idempotente por id. */
export function organizationCreate(input: {
  id: string
  name?: string
  color?: string
  sort_order?: number
}): Promise<{ organization: Organization | null; created: boolean }> {
  return apiSend<{ organization: Organization | null; created: boolean }>(
    '/api/organization_create',
    input
  )
}

/** Edita una organizacion. Desactivar es `active=false`, NUNCA borrar. */
export function organizationUpdate(input: {
  id: string
  name?: string
  color?: string
  sort_order?: number
  active?: boolean
}): Promise<{ organization: OrganizationRow }> {
  return apiSend<{ organization: OrganizationRow }>('/api/organization_update', input)
}

/** Crea una tarea. `title` es obligatorio. */
export function taskCreate(input: TaskInput): Promise<{ ok: boolean; task: Task }> {
  return apiSend<{ ok: boolean; task: Task }>('/api/task_create', input)
}

/** Tarea que quedó desbloqueada al pasar otra a done. */
export interface UnblockedTask {
  id: string
  title: string
  status: TaskStatus
  project: string | null
}

/** Actualiza campos de una tarea (incluye mover de columna: {id, status}).
 *  Al pasar a status='done', el backend devuelve `unblocked` con las tareas
 *  que dependían de ésta y que ahora quedaron listas. */
export function taskUpdate(
  input: TaskInput & { id: string }
): Promise<{ ok: boolean; task: Task; unblocked?: UnblockedTask[] }> {
  return apiSend<{ ok: boolean; task: Task; unblocked?: UnblockedTask[] }>(
    '/api/task_update',
    input
  )
}

/** Cancela una tarea (SOFT: status='cancelled', nunca borra). */
export function taskCancel(id: string): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/api/task_cancel', { id })
}

// ---------- F2: issues multi-agente (claim / hilo / handoff / review / block) ----------

/** Motivos de bloqueo permitidos por el backend (task_block). */
export type BlockReason = 'dependency' | 'external' | 'info' | 'resource'

/** Veredicto de revisión (gate/QA). */
export type ReviewVerdict = 'pass' | 'needs_changes'

/** Evento del hilo de actividad de una tarea (tabla task_activity).
 *  kind ∈ created|comment|status_change|claim|handoff|assign|note|result|review|block|unblock */
export interface TaskActivityEvent {
  id: string
  agent: string
  kind: string
  from_status: string | null
  to_status: string | null
  body: string
  created_at: string
}

export interface TaskActivityResponse {
  id: string
  activity: TaskActivityEvent[]
}

/** Ítem de bandeja (tasks_inbox): subconjunto de campos + flag `ready` (deps cumplidas). */
export interface InboxTask {
  id: string
  title: string
  project: string | null
  status: TaskStatus
  priority: string | null
  assignee: string | null
  claimed_by: string | null
  blocked_reason: string | null
  ready: boolean
}

export interface InboxResponse {
  agent: string
  inbox: InboxTask[]
}

/** Toma (claim) una tarea: claimed_by=actor, status→in_progress. Rechaza si ya
 *  está tomada por otro (409) o el status no lo permite (todo|in_review|blocked). */
export function taskClaim(id: string): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_claim', { id })
}

/** Agrega un comentario al hilo de actividad (no cambia el estado). */
export function taskComment(id: string, body: string): Promise<{ ok: boolean }> {
  return apiSend<{ ok: boolean }>('/api/task_comment', { id, body })
}

/** Deriva la tarea a otro agente: assignee=toAgent y libera el claim. */
export function taskHandoff(
  id: string,
  toAgent: string,
  note = ''
): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_handoff', {
    id,
    to_agent: toAgent,
    note,
  })
}

/** Entrega una tarea para auditoría del gate (in_progress→submitted). El backend
 *  registra submitted_by con el actor de la sesión y valida la transición. */
export function taskSubmit(
  id: string,
  note = ''
): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_submit', { id, note })
}

/** Gate de revisión sobre una tarea in_review: pass→done, needs_changes→in_progress. */
export function taskReview(
  id: string,
  verdict: ReviewVerdict,
  note = ''
): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_review', {
    id,
    verdict,
    note,
  })
}

/** Bloquea una tarea con un motivo (transición válida a 'blocked'). */
export function taskBlock(
  id: string,
  reason: BlockReason
): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_block', { id, reason })
}

/** Desbloquea una tarea (blocked→in_progress), limpia blocked_reason. */
export function taskUnblock(
  id: string,
  note = ''
): Promise<{ ok: boolean; task: Partial<Task> }> {
  return apiSend<{ ok: boolean; task: Partial<Task> }>('/api/task_unblock', { id, note })
}

/** Hilo cronológico de eventos de una tarea. */
export function fetchTaskActivity(id: string): Promise<TaskActivityResponse> {
  return apiGet<TaskActivityResponse>(`/api/task_activity?id=${encodeURIComponent(id)}`)
}

/** Bandeja de un agente (assignee): tareas activas + flag ready. `agent` es
 *  requerido por el backend (400 si vacío); el caller debe elegir un agente. */
export function fetchTasksInbox(agent = ''): Promise<InboxResponse> {
  return apiGet<InboxResponse>(`/api/tasks_inbox?agent=${encodeURIComponent(agent)}`)
}

// ---------- Cadenas: subtareas + dependencias entre tareas ----------

/** Subtarea directa (por parent_id). El backend excluye status='cancelled'. */
export interface Subtask {
  id: string
  title: string
  status: TaskStatus
  priority: string | null
  assignee: string | null
  project: string | null
}

/** Subtareas directas de una tarea. */
export function fetchSubtasks(id: string): Promise<{ subtasks: Subtask[] }> {
  return apiGet<{ subtasks: Subtask[] }>(`/api/subtasks?id=${encodeURIComponent(id)}`)
}

/** Tarea vinculada en una relación de dependencia (title/status). */
export interface DepTask {
  id: string
  title: string
  status: TaskStatus
}

/** Dependencias de una tarea: `blocked_by` (de qué depende) y `blocks` (a qué frena). */
export interface TaskDeps {
  id: string
  blocked_by: DepTask[]
  blocks: DepTask[]
}

export function fetchTaskDeps(id: string): Promise<TaskDeps> {
  return apiGet<TaskDeps>(`/api/task_deps?id=${encodeURIComponent(id)}`)
}

/** Crea dependencia: `taskId` queda BLOQUEADA POR `dependsOnId`.
 *  400 auto-dep, 409 ciclo directo, 404 si alguna no existe. */
export function taskLink(
  taskId: string,
  dependsOnId: string
): Promise<{ ok: boolean; task_id: string; depends_on_id: string }> {
  return apiSend('/api/task_link', { task_id: taskId, depends_on_id: dependsOnId })
}

/** Quita una dependencia (relación, NO borra tareas). 404 si no existe. */
export function taskUnlink(
  taskId: string,
  dependsOnId: string
): Promise<{ ok: boolean }> {
  return apiSend('/api/task_unlink', { task_id: taskId, depends_on_id: dependsOnId })
}

/** Tarea `todo` lista para empezar (sin deps o con todas sus deps `done`). */
export interface ReadyTask {
  id: string
  title: string
  project: string | null
  status: TaskStatus
  priority: string | null
  assignee: string | null
}

/** Tareas listas para empezar. `project` opcional filtra por proyecto exacto. */
export function fetchTasksReady(project?: string): Promise<{ tasks_ready: ReadyTask[] }> {
  const q = project ? `?project=${encodeURIComponent(project)}` : ''
  return apiGet<{ tasks_ready: ReadyTask[] }>(`/api/tasks_ready${q}`)
}

// ---------- Recientes (últimas notas editadas / adjuntos subidos) ----------

export interface RecentFile {
  path: string
  /** mtime en epoch (segundos). */
  mtime: number
  ext: string
  is_md: boolean
}

/** Archivos del vault ordenados por mtime desc (últimas ediciones/subidas). */
export function fetchRecent(limit = 20): Promise<{ recent: RecentFile[] }> {
  return apiGet<{ recent: RecentFile[] }>(`/api/recent?limit=${limit}`)
}

// ---------- Clima (Open-Meteo, público, sin API key; opt-in por VITE_WEATHER_*) ----------

export interface Weather {
  temperature: number
  weatherCode: number
}

/**
 * Clima actual vía Open-Meteo para VITE_WEATHER_LAT/LON. Endpoint público con CORS.
 * NO pasa por el BFF ni usa auth. Si falla, lanza y el caller degrada.
 */
/** Weather is OPT-IN: without VITE_WEATHER_LAT/LON the panel makes no third-party call. */
export const WEATHER_ENABLED = Boolean(import.meta.env.VITE_WEATHER_LAT && import.meta.env.VITE_WEATHER_LON)
export const WEATHER_LABEL: string = import.meta.env.VITE_WEATHER_LABEL || ''
export async function fetchWeather(): Promise<Weather> {
  const url =
    `https://api.open-meteo.com/v1/forecast?latitude=${encodeURIComponent(import.meta.env.VITE_WEATHER_LAT)}` +
    `&longitude=${encodeURIComponent(import.meta.env.VITE_WEATHER_LON)}&current=temperature_2m,weather_code&timezone=auto`
  const res = await fetch(url)
  if (!res.ok) throw new Error(`Open-Meteo ${res.status}`)
  const json = (await res.json()) as {
    current?: { temperature_2m?: number; weather_code?: number }
  }
  const cur = json.current
  if (!cur || typeof cur.temperature_2m !== 'number')
    throw new Error('Respuesta de clima inválida')
  return {
    temperature: cur.temperature_2m,
    weatherCode: typeof cur.weather_code === 'number' ? cur.weather_code : 0,
  }
}

// ---------- Ola 2: grafo de wikilinks (para backlinks client-side) ----------

/** Nodo del grafo del Cerebro (`/api/graph`). Para notas: id = stem, path = ruta. */
export interface GraphNode {
  id: string
  path: string | null
  group: string
  type: 'note' | 'task' | 'project' | 'agent' | 'memory'
  val: number
  title?: string
}

/** Arista dirigida source→target (por stem). Refleja `[[target]]` dentro de source. */
export interface GraphLink {
  source: string
  target: string
}

export interface GraphData {
  nodes: GraphNode[]
  links: GraphLink[]
}

/**
 * Grafo de notas y wikilinks calculado por el Cerebro (endpoint existente,
 * cacheado server-side). Se reusa como índice de enlaces para computar los
 * backlinks de una nota sin recorrer las ~10k notas desde el cliente.
 */
export function fetchGraph(): Promise<GraphData> {
  return apiGet<GraphData>('/api/graph')
}

/** Fuerza al Cerebro a reconstruir el grafo (tras cambios en el árbol). */
export function refreshGraph(): Promise<{ nodes: number; links: number }> {
  return apiSend<{ nodes: number; links: number }>('/api/refresh', {})
}

// ---------- Ola 3a: bases de datos (carpeta = base; nota = fila; frontmatter = props) ----------

/** Valor de propiedad de frontmatter, tal como lo tipa el backend. */
export type PropValue =
  | string
  | number
  | boolean
  | null
  | Array<string | number | boolean | null>
  | Record<string, unknown>

/** Una nota vista como fila de base de datos. */
export interface CollectionItem {
  /** Ruta relativa dentro del vault. */
  path: string
  /** Título: propiedad `title` → primer H1 → nombre de archivo. */
  title: string
  /** mtime en epoch (segundos). */
  mtime: number
  /** Frontmatter parseado (clave → valor tipado). */
  properties: Record<string, PropValue>
}

export interface CollectionResponse {
  dir: string
  recursive: boolean
  count: number
  items: CollectionItem[]
}

/**
 * Colección = notas `.md` de una carpeta con su frontmatter parseado.
 * Evita N llamadas a /api/node desde el front para armar una base de datos.
 */
export function fetchCollection(
  dir: string,
  recursive = false
): Promise<CollectionResponse> {
  const q = `?dir=${encodeURIComponent(dir)}&recursive=${recursive ? 1 : 0}`
  return apiGet<CollectionResponse>(`/api/collection${q}`)
}

// ---------- Bus de la flota (solo lectura, solo el lector autorizado) ----------
// Proxy del gateway (GET /api/bus/threads, /api/bus/thread, /api/bus/group). El BFF gatea
// con _require_bus_reader: si la sesión no está habilitada devuelve 403 y la vista lo
// muestra EXPLÍCITO (nunca una lista vacía, que sería indistinguible de "no hay").
//
// Dos tipos de conversación, y el BACKEND es quien lo dice (campo `kind`):
//   - 'thread'   → hilo DECLARADO, con conversation_id.
//   - 'implicit' → mensajes publicados SIN conversation_id, RECONSTRUIDOS agrupando por
//                  conjunto de participantes (`participants_key`, ej. 'assistant-a|assistant-d').
// El front NUNCA debe inferir el tipo de "¿tiene conversation_id?": el campo se persiste
// como cadena VACÍA, no NULL, y esa inferencia da falsos positivos.

/**
 * Último mensaje de un hilo (resumen que devuelve /bus/threads).
 *
 * `content` viene SIEMPRE ACOTADO por el gateway (preview de ~120 chars) y NO hay forma
 * de pedirlo completo desde la lista: leer el cuerpo entero es trabajo del DETALLE, que
 * lo pide explícitamente y queda auditado. `content_len` es el largo REAL, así un preview
 * cortado no se confunde con un mensaje corto.
 */
export interface BusLastMessage {
  sender: string
  performative: string
  content: string
  /** Largo real del cuerpo en caracteres (el `content` de arriba puede venir cortado). */
  content_len?: number
  /** true si `content` es un preview cortado. */
  content_truncated?: boolean
  refs_task: string | null
  timestamp: string | null
}

/** Un hilo del bus (conversation_id no vacío) con su volumen y participantes. */
export interface BusThreadSummary {
  conversation_id: string
  /** Cantidad de mensajes del hilo. */
  messages: number
  participants: string[]
  started_at: string | null
  last_at: string | null
  last_message: BusLastMessage | null
}

export interface BusThreadsResponse {
  agent: string | null
  count: number
  threads: BusThreadSummary[]
}

/**
 * Item de la lista COMBINADA (`include_unthreaded=true`): hilo declarado o conversación
 * implícita. `kind` lo decide el backend y es el discriminante:
 * kind='thread' ⇒ `conversation_id` es el identificador;
 * kind='implicit' ⇒ `participants_key` es el identificador.
 */
export interface BusConversationItem {
  kind: 'thread' | 'implicit'
  conversation_id: string | null
  participants_key: string | null
  messages: number
  participants: string[]
  started_at: string | null
  last_at: string | null
  last_message: BusLastMessage | null
}

export interface BusConversationsResponse {
  agent: string | null
  count: number
  threads: BusConversationItem[]
  /** Desglose por tipo (solo con include_unthreaded). */
  counts?: { threads: number; implicit: number }
  /** Eco de los filtros que aplicó el gateway. */
  filters?: {
    agent: string | null
    project: string | null
    since: string | null
    until: string | null
    q: string | null
  }
}

/**
 * Un mensaje del replay de un hilo (GET /bus/thread) o de un grupo (GET /bus/group).
 *
 * ⚠一`content` es `string | null` A PROPOSITO (, cambio de implementación): el gateway NO
 * devuelve el cuerpo salvo que se pida `include_content=true`, y sin el flag el campo
 * llega en `null` acompañado de `content_len` + `content_preview`. El tipo obliga a que
 * el render decida qué hacer con la ausencia en vez de imprimir "null". Las vistas de
 * DETALLE piden el cuerpo (por eso siguen mostrando el texto completo); la LISTA no.
 */
export interface BusMessage {
  id: string
  sender: string
  recipients: string[]
  performative: string
  /** Cuerpo completo. `null` cuando la respuesta es metadata-only (sin include_content). */
  content: string | null
  /** Largo real del cuerpo, presente siempre (también cuando `content` es null). */
  content_len?: number
  /** Preview de ~120 chars, presente SOLO en las respuestas metadata-only. */
  content_preview?: string
  /** true si el preview está cortado respecto del cuerpo real. */
  content_truncated?: boolean
  refs_task: string | null
  timestamp: string | null
}

export interface BusThreadResponse {
  conversation_id: string
  resolved: string | null
  count: number
  participants: string[]
  /** Proyección que aplicó el gateway: false ⇒ los `content` vienen en null. */
  content_included?: boolean
  started_at?: string | null
  last_at?: string | null
  messages: BusMessage[]
  /** Presente cuando el gateway no encontró el hilo (sin mensajes). */
  error?: string
}

/** Replay de una conversación IMPLÍCITA (GET /bus/group). Misma forma que un hilo. */
export interface BusGroupResponse {
  participants_key: string
  kind: 'implicit'
  resolved: string | null
  count: number
  participants: string[]
  /** Proyección que aplicó el gateway: false ⇒ los `content` vienen en null. */
  content_included?: boolean
  started_at?: string | null
  last_at?: string | null
  messages: BusMessage[]
  /** Presente cuando no hay mensajes sin hilo para esa clave. */
  error?: string
}

/**
 * Filtros del bus. Todos OPCIONALES y combinables con AND; los ausentes no se envían,
 * así el backend conserva su comportamiento por defecto.
 * - `project`: vía la tarea referenciada. ⚠️ los mensajes sin `refs_task` NO tienen
 *   proyecto y quedan FUERA cuando se filtra por proyecto.
 * - `since`/`until`: 'YYYY-MM-DD'; `until` incluye el día completo.
 * - `q`: coincidencia de TEXTO (contenido, conversation_id, participantes). NO es
 *   búsqueda semántica: el bus no está indexado en Qdrant.
 */
export interface BusFilters {
  agent?: string
  project?: string
  since?: string
  until?: string
  q?: string
}

function busFilterParams(f: BusFilters = {}): URLSearchParams {
  const p = new URLSearchParams()
  if (f.agent) p.set('agent', f.agent)
  if (f.project) p.set('project', f.project)
  if (f.since) p.set('since', f.since)
  if (f.until) p.set('until', f.until)
  if (f.q) p.set('q', f.q)
  return p
}

/** Hilos del bus. `agent` opcional filtra por participante (sender o recipient). */
export function fetchBusThreads(agent = '', limit = 50): Promise<BusThreadsResponse> {
  const params = new URLSearchParams()
  if (agent) params.set('agent', agent)
  params.set('limit', String(limit))
  return apiGet<BusThreadsResponse>(`/api/bus/threads?${params.toString()}`)
}

/**
 * Lista COMBINADA: hilos declarados + conversaciones implícitas, intercalados por
 * recencia. Cada item trae `kind` (lo decide el backend). Acepta los filtros de
 * `BusFilters`.
 */
export function fetchBusConversations(
  filters: BusFilters = {},
  limit = 200
): Promise<BusConversationsResponse> {
  const params = busFilterParams(filters)
  params.set('include_unthreaded', 'true')
  params.set('limit', String(limit))
  return apiGet<BusConversationsResponse>(`/api/bus/threads?${params.toString()}`)
}

/**
 * Replay cronológico de UN hilo. `conversationId` vacío => el BFF responde 422.
 *
 * `includeContent` pide los cuerpos completos: el default del gateway es metadata +
 * preview, así que la vista de DETALLE lo manda en `true` (es una lectura deliberada del
 * humano, y el BFF la asienta en audit_log). No lo uses para listar o contar.
 */
export function fetchBusThread(
  conversationId: string,
  includeContent = false
): Promise<BusThreadResponse> {
  const params = new URLSearchParams()
  params.set('conversation_id', conversationId)
  // Se omite en el default para no pisar el del backend.
  if (includeContent) params.set('include_content', 'true')
  return apiGet<BusThreadResponse>(`/api/bus/thread?${params.toString()}`)
}

/**
 * Replay cronológico de UNA conversación implícita (mensajes sin conversation_id).
 * `participants` vacío => el BFF responde 422. `includeContent`: mismo contrato que
 * `fetchBusThread` — el detalle lo pide, la lista no.
 */
export function fetchBusGroup(
  participants: string,
  filters: BusFilters = {},
  limit = 200,
  includeContent = false
): Promise<BusGroupResponse> {
  const params = busFilterParams({ ...filters, agent: undefined })
  params.set('participants', participants)
  params.set('limit', String(limit))
  if (includeContent) params.set('include_content', 'true')
  return apiGet<BusGroupResponse>(`/api/bus/group?${params.toString()}`)
}

// ---------- Publicación del bus: intento durable e idempotente ----------
export type BusPerformative = 'inform' | 'request'

export interface BusPublishInput {
  recipients: string[] | string
  content: string
  performative?: BusPerformative
  refs_task?: string
  conversation_id?: string
}

export interface BusPublishResponse {
  id: string
  sender: string
  recipients: string[]
  performative: BusPerformative
  created: boolean
  deduplicated: boolean
}

interface BusPublishAttempt {
  fingerprint: string
  idempotencyKey: string
  createdAt: number
}

const BUS_PUBLISH_STORAGE_KEY = 'sinapsis:bus-publish-attempts:v1'
export const BUS_PUBLISH_ATTEMPT_TTL_MS = 24 * 60 * 60 * 1000
export const BUS_PUBLISH_SECURE_CONTEXT_MESSAGE =
  'No se puede publicar de forma segura desde una conexión no cifrada. Abrí el panel en ' +
  (typeof window === 'undefined' ? 'https://localhost' : window.location.origin.replace(/^http:/, 'https:')) + '.'
const busPublishMemory = new Map<string, BusPublishAttempt>()

type BusPublishCrypto = Pick<Crypto, 'subtle' | 'randomUUID'>

function getBusPublishCrypto(): BusPublishCrypto | null {
  // El DOM lib tipa ambas APIs como obligatorias, pero en un origen HTTP Chromium las
  // retira en runtime. Partial evita convertir esa declaración estática en una sonda falsa.
  const candidate = globalThis.crypto as Partial<BusPublishCrypto> | undefined
  return candidate?.subtle && typeof candidate.randomUUID === 'function'
    ? candidate as BusPublishCrypto
    : null
}

/** Sonda sin efectos: la UI la captura al montar, antes de que se redacte un mensaje. */
export function getBusPublishEnvironmentError(): string {
  return getBusPublishCrypto() ? '' : BUS_PUBLISH_SECURE_CONTEXT_MESSAGE
}

function requireBusPublishEnvironment(): BusPublishCrypto {
  const crypto = getBusPublishCrypto()
  if (!crypto) throw new Error(BUS_PUBLISH_SECURE_CONTEXT_MESSAGE)
  return crypto
}

/** ÚNICA definición de vigencia. Edad negativa (reloj retrocedió) sigue vigente. */
export function isBusPublishAttemptLive(createdAt: number, now = Date.now()): boolean {
  return Number.isFinite(createdAt) && now - createdAt <= BUS_PUBLISH_ATTEMPT_TTL_MS
}

export function normalizeBusRecipients(raw: string[] | string): string[] {
  const parts = Array.isArray(raw) ? raw : raw.split(',')
  return [...new Set(parts.map((x) => x.trim().toLowerCase()).filter(Boolean))].sort()
}

/** Payload canónico del fingerprint. El texto existe sólo en memoria durante el hash. */
export function canonicalBusPublishPayload(input: BusPublishInput): string {
  return JSON.stringify({
    recipients: normalizeBusRecipients(input.recipients),
    content: input.content.trim(),
    performative: input.performative ?? 'inform',
    conversation_id: (input.conversation_id ?? '').trim(),
    refs_task: (input.refs_task ?? '').trim(),
  })
}

function loadBusPublishAttempts(now: number): BusPublishAttempt[] {
  let parsed: unknown
  try {
    parsed = JSON.parse(sessionStorage.getItem(BUS_PUBLISH_STORAGE_KEY) ?? '[]')
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const live = parsed.filter((x): x is BusPublishAttempt =>
    !!x && typeof x.fingerprint === 'string' && typeof x.idempotencyKey === 'string'
    && typeof x.createdAt === 'number' && isBusPublishAttemptLive(x.createdAt, now))
  try {
    sessionStorage.setItem(BUS_PUBLISH_STORAGE_KEY, JSON.stringify(live))
  } catch {
    // La purga es best-effort: una escritura fallida NO invalida lo que sí se leyó.
    // Descartarlo aquí perdería la UUID de un intento incierto y permitiría duplicarlo.
  }
  return live
}

function saveBusPublishAttempts(entries: BusPublishAttempt[]): void {
  try {
    // Sin tope por cantidad: sólo TTL puede retirar un intento incierto vivo.
    sessionStorage.setItem(BUS_PUBLISH_STORAGE_KEY, JSON.stringify(entries))
  } catch {
    // Storage bloqueado/lleno: la entrada sigue en memoria para esta carga.
  }
}

function newBusPublishUuid(): string {
  return requireBusPublishEnvironment().randomUUID()
}

async function busPublishFingerprint(canonical: string): Promise<string> {
  const digest = await requireBusPublishEnvironment().subtle.digest(
    'SHA-256', new TextEncoder().encode(canonical))
  return [...new Uint8Array(digest)].map((x) => x.toString(16).padStart(2, '0')).join('')
}

export async function getBusPublishAttempt(
  input: BusPublishInput,
  now = Date.now()
): Promise<BusPublishAttempt> {
  const canonical = canonicalBusPublishPayload(input)
  const key = await busPublishFingerprint(canonical)

  for (const [fingerprint, entry] of busPublishMemory) {
    if (!isBusPublishAttemptLive(entry.createdAt, now)) busPublishMemory.delete(fingerprint)
  }
  const memory = busPublishMemory.get(key)
  if (memory) return memory

  const entries = loadBusPublishAttempts(now)
  const stored = entries.find((x) => x.fingerprint === key)
  if (stored) {
    busPublishMemory.set(key, stored)
    return stored
  }

  const entry = { fingerprint: key, idempotencyKey: newBusPublishUuid(), createdAt: now }
  busPublishMemory.set(key, entry)
  saveBusPublishAttempts([...entries.filter((x) => x.fingerprint !== key), entry])
  return entry
}

export function clearBusPublishAttempt(entry: BusPublishAttempt): void {
  busPublishMemory.delete(entry.fingerprint)
  const entries = loadBusPublishAttempts(Date.now()).filter(
    (x) => !(x.fingerprint === entry.fingerprint && x.idempotencyKey === entry.idempotencyKey))
  saveBusPublishAttempts(entries)
}

export class BusPublishUncertainError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'BusPublishUncertainError'
  }
}

/** POST exclusivo (no apiSend): timeout/AbortController e idempotencia son propios. */
export async function publishBus(input: BusPublishInput): Promise<BusPublishResponse> {
  requireBusPublishEnvironment()
  const normalized: BusPublishInput = {
    recipients: normalizeBusRecipients(input.recipients),
    content: input.content.trim(),
    performative: input.performative ?? 'inform',
    conversation_id: (input.conversation_id ?? '').trim(),
    refs_task: (input.refs_task ?? '').trim(),
  }
  const attempt = await getBusPublishAttempt(normalized)
  const controller = new AbortController()
  const timeout = globalThis.setTimeout(() => controller.abort(), 15_000)
  let res: Response
  try {
    res = await fetch('/api/bus/publish', {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...normalized, idempotency_key: attempt.idempotencyKey }),
      signal: controller.signal,
    })
  } catch (error) {
    throw new BusPublishUncertainError(
      error instanceof Error && error.name === 'AbortError'
        ? 'Tiempo agotado: no sabemos si se envió.'
        : 'Error de red: no sabemos si se envió.')
  } finally {
    globalThis.clearTimeout(timeout)
  }

  if (res.status === 401) {
    clearBusPublishAttempt(attempt)
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const detail = await readDetail(res)
    // Sólo una respuesta definitiva retira la clave. 408/5xx son inciertos.
    if (res.status >= 400 && res.status < 500 && res.status !== 408) {
      clearBusPublishAttempt(attempt)
    }
    if (res.status === 408 || res.status >= 500) {
      throw new BusPublishUncertainError(
        `Respuesta ${res.status}: no sabemos si se envió.`)
    }
    throw new ApiError(res.status, detailMessage(detail, res.status, '/api/bus/publish'), detail)
  }

  // Un 2xx es definitivo aunque su JSON estuviera roto: no conservar una clave viva
  // que podría reciclarse horas después para un intento ya confirmado por HTTP.
  clearBusPublishAttempt(attempt)
  return (await res.json()) as BusPublishResponse
}


// ---------- Correspondencia F3: bandeja, hilos y acciones ----------
export type CorrespondenciaPrioridad = 'urgente' | 'medio' | 'bajo'
export type CorrespondenciaTipo = 'inform' | 'request'

export interface CorrespondenciaAdjunto {
  id: string
  mensaje_id: string
  nombre: string
  tipo_mime: string
  tamano: number
  hash: string
  subido_por: string
  creado_en: string
  retirado_en: string | null
}

export interface CorrespondenciaAccion {
  etiqueta: string
  disponible: boolean
  archiva?: boolean
  permite_retirar?: boolean
  requiere_motivo?: boolean
}

export interface CorrespondenciaReaccion {
  buzon: string
  emoji: string
  gesto: 'ok' | 'expresiva' | string
  creado_en: string
}

export interface CorrespondenciaRevision {
  abierto_por: string
  motivo: string | null
  abierto_en: string
}

export interface CorrespondenciaMensaje {
  id: string
  codigo: string
  raiz: string
  remitente: string
  asunto: string
  prioridad: CorrespondenciaPrioridad
  /** Cuerpo completo cuando se pidió; `null` cuando la bandeja sólo trajo metadata. */
  contenido: string | null
  /** Preview de metadata. Puede ser `''` para un mensaje vacío legítimo. */
  contenido_preview?: string | null
  /** Largo real del cuerpo; no se deriva del preview porque éste puede truncarse. */
  contenido_len?: number | null
  /** Señal del gateway: el preview fue cortado, no un mensaje corto. */
  contenido_truncado?: boolean | null
  tipo: CorrespondenciaTipo
  abierto: boolean
  refs_task: string | null
  ref_vault: string | null
  creado_en: string
  buzon?: string
  estado?: string
  leido_en?: string | null
  archivado_en?: string | null
  via_lectura?: string | null
  audiencia_respuesta: string[]
  adjuntos: CorrespondenciaAdjunto[]
  reacciones: CorrespondenciaReaccion[]
  sin_reaccion: string[]
  revision: CorrespondenciaRevision | null
  acciones: {
    ok: CorrespondenciaAccion
    reaccion_expresiva: CorrespondenciaAccion
    solicitar_revision: CorrespondenciaAccion
  }
}

export interface CorrespondenciaBandeja {
  buzon: string
  vista: 'bandeja' | 'archivo'
  count: number
  items: CorrespondenciaMensaje[]
  enviados_count: number
  enviados: CorrespondenciaMensaje[]
}

export interface CorrespondenciaEnvioInput {
  destinatarios: string[]
  asunto: string
  prioridad: CorrespondenciaPrioridad
  contenido: string
  tipo: CorrespondenciaTipo
  refs_task?: string
  ref_vault?: string
}

export interface CorrespondenciaRespuestaInput {
  codigo: string
  contenido: string
  tipo: CorrespondenciaTipo
  prioridad?: CorrespondenciaPrioridad
  refs_task?: string
  ref_vault?: string
}

export interface CorrespondenciaEnvioResponse {
  id: string
  codigo: string
  raiz: string
  asunto?: string
  destinatarios: string[]
  entregas: number
  abierto: boolean
}

export interface CorrespondenciaLectura {
  codigo: string
  estado: string
  leido_en: string
  archivado_en: string | null
  /** Siempre llega completo al pedir UN mensaje por `/leer`. */
  mensaje: CorrespondenciaMensaje
}

/**
 * La bandeja conserva cuerpos por defecto por compatibilidad REST/BFF. El panel pide
 * metadata (`incluirContenido=false`) y abre cuerpos de a uno con `leerCorrespondencia`.
 */
export function fetchCorrespondenciaBandeja(
  archivo = false,
  buzon = '',
  incluirContenido = true
): Promise<CorrespondenciaBandeja> {
  const params = new URLSearchParams({
    archivo: String(archivo),
    incluir_contenido: String(incluirContenido),
  })
  if (buzon.trim()) params.set('buzon', buzon.trim())
  return apiGet<CorrespondenciaBandeja>(`/api/correspondencia/bandeja?${params.toString()}`)
}

/** Registro de buzones canónicos de las identidades ACTIVAS. Sólo admin: el gateway
 *  responde 403 a cualquier otra identidad, incluida la credencial del BFF cuando la
 *  ruta no está en DELEGATION_ROUTES. Ese 403 llega TAL CUAL: no se traduce a lista
 *  vacía, porque "no podés verlo" y "no hay nada" no pueden verse igual.
 *
 *  El buzón propio sale de acá (`propio`), derivado por el servidor. El panel ya no
 *  reimplementa `individual_mailbox()`. */
export function fetchCorrespondenciaBuzones(): Promise<RegistroBuzones> {
  return apiGet<RegistroBuzones>('/api/correspondencia/buzones')
}

export type { RegistroBuzones, BuzonRegistro } from './correspondenciaAdmin'


export async function subirAdjuntoCorrespondencia(
  codigo: string,
  file: File
): Promise<CorrespondenciaAdjunto & { codigo: string; blob_creado: boolean }> {
  const params = new URLSearchParams({
    codigo,
    nombre: file.name,
    tipo_mime: file.type || 'application/octet-stream',
  })
  const res = await fetch(`/api/correspondencia/adjuntos?${params.toString()}`, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const detail = await readDetail(res)
    throw new ApiError(res.status, detailMessage(detail, res.status, '/api/correspondencia/adjuntos'), detail)
  }
  return (await res.json()) as CorrespondenciaAdjunto & { codigo: string; blob_creado: boolean }
}

export async function descargarAdjuntoCorrespondencia(id: string): Promise<Blob> {
  const path = `/api/correspondencia/adjuntos/${encodeURIComponent(id)}/descargar`
  const res = await fetch(path, { headers: authHeaders() })
  if (res.status === 401) {
    useAppStore.getState().logout()
    throw new ApiError(401, 'Sesión expirada')
  }
  if (!res.ok) {
    const detail = await readDetail(res)
    throw new ApiError(res.status, detailMessage(detail, res.status, path), detail)
  }
  return await res.blob()
}

export function retirarAdjuntoCorrespondencia(id: string): Promise<{
  id: string; retirado_en: string; blob_conservado: true; hash: string
}> {
  return apiSend(`/api/correspondencia/adjuntos/${encodeURIComponent(id)}/retirar`, {})
}

export function publicarCorrespondencia(
  input: CorrespondenciaEnvioInput
): Promise<CorrespondenciaEnvioResponse> {
  return apiSend<CorrespondenciaEnvioResponse>('/api/correspondencia/publicar', input)
}

export function responderCorrespondencia(
  input: CorrespondenciaRespuestaInput
): Promise<CorrespondenciaEnvioResponse> {
  return apiSend<CorrespondenciaEnvioResponse>('/api/correspondencia/responder', input)
}

export function leerCorrespondencia(codigo: string): Promise<CorrespondenciaLectura> {
  return apiSend<CorrespondenciaLectura>('/api/correspondencia/leer', { codigo, via_lectura: 'panel' })
}

export function okCorrespondencia(codigo: string, emoji = '👍'): Promise<{
  archivados: number; codigos: string[]
}> {
  return apiSend('/api/correspondencia/ok', { codigo, emoji })
}

export function reaccionarCorrespondencia(
  codigo: string,
  emoji: string,
  retirar = false
): Promise<{ codigo: string; archiva: false; creadas: number; retiradas: number }> {
  return apiSend('/api/correspondencia/reaccion', { codigo, emoji, retirar })
}

export function solicitarRevisionCorrespondencia(
  codigo: string,
  motivo: string
): Promise<{ codigo: string; raiz: string; abierto: boolean; motivo: string }> {
  return apiSend('/api/correspondencia/solicitar-revision', { codigo, motivo })
}

// ---------- F3: Biblioteca (registro compartido de skills) ----------

/** Un skill del registro compartido, deduplicado por `source_path` en el backend. */
export interface SkillItem {
  /** Nombre del skill (source_path sin el prefijo 'skills/'). */
  name: string
  /** Ruta cruda en el índice ('skills/<name>'). */
  source_path: string
  /** Título del skill (title del chunk, fallback al nombre). */
  title: string
  /** Descripción: línea `DESCRIPCION:` del cuerpo, fallback al primer párrafo/título. */
  description: string
  tags: string[]
  /** Agente que publicó el skill (puede faltar en registros viejos). */
  agent_owner: string | null
  category: string | null
  /** Cantidad de chunks vigentes del skill (proxy de su tamaño). */
  chunk_count: number
  /** ISO-8601 o null. */
  created_at: string | null
  updated_at: string | null
}

export interface SkillsResponse {
  skills: SkillItem[]
  /** Skills devueltos en esta página. */
  count: number
  /** Total de skills que matchean el filtro (sin paginar). */
  total: number
  limit: number
  offset: number
  q: string
  tag: string
  has_more: boolean
}

/**
 * Skills vigentes del registro compartido (GET /api/skills). `q` filtra por
 * substring case-insensitive sobre nombre/título/texto; paginación limit/offset.
 */
export function fetchSkills(q = '', limit = 50, offset = 0): Promise<SkillsResponse> {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  return apiGet<SkillsResponse>(`/api/skills?${params.toString()}`)
}

/** Contenido completo de UNA skill (todos sus chunks vigentes concatenados). */
export interface SkillContent {
  /** Nombre del skill (sin el prefijo 'skills/'). */
  name: string
  /** Texto completo de la skill, chunks unidos en orden por doble salto de línea. */
  content: string
  /** Cantidad de chunks vigentes concatenados. */
  chunk_count: number
}

/**
 * Trae el CONTENIDO COMPLETO de una skill (GET /api/skill?name=…) para copiarlo
 * al portapapeles. 404 si la skill no existe o no está vigente.
 */
export function fetchSkillContent(name: string): Promise<SkillContent> {
  return apiGet<SkillContent>(`/api/skill?name=${encodeURIComponent(name)}`)
}

