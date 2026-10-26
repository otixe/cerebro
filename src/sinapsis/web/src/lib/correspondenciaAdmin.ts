/** Lógica PURA del scope de Correspondencia para el panel de admin.
 *
 *  POR QUÉ ESTE MÓDULO EXISTE (y no está dentro del componente): el panel no tiene
 *  runner de tests y no se le agrega uno por una feature. Sacando las decisiones a
 *  funciones puras quedan verificables con el `tsc` que el panel YA tiene, sin tocar
 *  el árbol de dependencias. Lo que queda fuera de esta frontera (el cableado JSX)
 *  no está cubierto por comportamiento y está declarado como hueco en el REPORT.
 *
 *  REGLA DE ORO DE ESTE ARCHIVO: acá no se DERIVA identidad. El buzón canónico llega
 *  del servidor (`GET /correspondencia/buzones` → `propio`). La versión anterior del
 *  componente reimplementaba `individual_mailbox()` en TypeScript leyendo
 *  `meta.subagents_by_team`; eso era una segunda fuente de verdad de la identidad, que
 *  es exactamente lo que se vino a unificar.
 */

/** Ámbito que el panel está mirando.
 *  · `propio`  — mi buzón. Para un admin NO se traduce a "sin parámetro": ver
 *                `buzonParaScope`.
 *  · `global`  — vista de flota (sólo admin): el servidor la da omitiendo `buzon`.
 *  · `buzon`   — un buzón concreto, propio o ajeno (sólo admin).
 */
export type ScopeCorrespondencia =
  | { kind: 'propio' }
  | { kind: 'global' }
  | { kind: 'buzon'; buzon: string }

export interface ContextoScope {
  /** Lo dice el SERVIDOR (`es_admin` del registro), no el rol de la sesión: `admin`
   *  lo comparten varios agentes y sólo `kind='human' AND role='admin'` es admin acá. */
  esAdmin: boolean
  /** Buzón canónico propio, tal como lo derivó el servidor. '' = todavía no se sabe. */
  propio: string
}

export type ResultadoScope =
  | { ok: true; buzon: string }
  | { ok: false; motivo: string }

export const MOTIVO_SIN_PARTICIPACION =
  'No participás de este hilo: el servidor rechaza la descarga (403). Se ve el nombre ' +
  'porque la bandeja lo incluye, no porque tengas acceso al contenido.'

const MOTIVO_PROPIO_DESCONOCIDO =
  'No se pudo resolver tu buzón canónico en el servidor, así que no se consulta nada: ' +
  'para un admin, omitir el buzón significa VISTA GLOBAL, y eso no es lo que pediste.'

/** Qué valor de `buzon` mandarle a `GET /correspondencia/bandeja`.
 *
 *  '' significa OMITIR el parámetro, y ojo que su semántica depende de quién pregunta:
 *  para un NO-admin el servidor lo fija a su propio buzón; para un ADMIN significa
 *  VISTA GLOBAL. De ahí que el scope `propio` de un admin mande el buzón EXPLÍCITO
 *  (lección de F3) y que, si no lo conocemos, la respuesta correcta sea NO CONSULTAR
 *  en vez de caer a global por descuido.
 */
export function buzonParaScope(
  scope: ScopeCorrespondencia,
  ctx: ContextoScope,
): ResultadoScope {
  if (!ctx.esAdmin) {
    // Un no-admin no elige: el servidor lo fija a su buzón. Se omite el parámetro a
    // propósito, así el cliente no vuelve a derivar la identidad ni puede pedir otro.
    return { ok: true, buzon: '' }
  }
  if (scope.kind === 'global') return { ok: true, buzon: '' }
  const objetivo = scope.kind === 'buzon' ? scope.buzon.trim() : ctx.propio.trim()
  if (!objetivo) {
    return {
      ok: false,
      motivo: scope.kind === 'buzon'
        ? 'Buzón vacío: elegí uno del registro.'
        : MOTIVO_PROPIO_DESCONOCIDO,
    }
  }
  return { ok: true, buzon: objetivo }
}

/** Clave estable de react-query para un scope ya resuelto.
 *  Que el scope `propio` de un admin produzca la MISMA clave que su buzón explícito es
 *  deliberado: así el contador propio y el scope activo comparten una sola request
 *  cuando coinciden, y sólo hay dos cuando de verdad se está mirando otra cosa. */
export function claveScope(resuelto: ResultadoScope): string {
  if (!resuelto.ok) return 'bloqueado'
  return resuelto.buzon || '*'
}

export interface BuzonRegistro {
  buzon: string
  nombre: string
  equipo: string | null
  kind: string
  es_lider: boolean
  alias: string[]
  propio: boolean
}

export interface RegistroBuzones {
  identidad: string
  propio: string
  es_admin: boolean
  count: number
  buzones: BuzonRegistro[]
}

/** ¿La delegación llegó al gateway?
 *
 *  Un 200 NO alcanza como prueba: si `/correspondencia/buzones` no está declarado en
 *  DELEGATION_ROUTES, el gateway atiende a la credencial del BFF y no al humano. Hoy
 *  eso da 403 por construcción (esa credencial es kind='agent' y jamás es admin), pero
 *  el cliente igual cruza `identidad` contra su sesión: si mañana esa credencial
 *  cambiara de forma, el modo de falla volvería a ser silencioso, y un selector que
 *  miente es peor que un selector que no está.
 *
 *  Devuelve '' si está todo bien; si no, el motivo para mostrar en pantalla.
 */
export function desajusteDeDelegacion(
  registro: Pick<RegistroBuzones, 'identidad' | 'propio'>,
  nombreDeSesion: string,
): string {
  const visto = (registro.identidad || '').trim().toLowerCase()
  const esperado = (nombreDeSesion || '').trim().toLowerCase()
  if (!visto) {
    return 'El registro de buzones no dice a qué identidad respondió; no se confía en la lista.'
  }
  if (!esperado) return ''
  if (visto !== esperado) {
    return `El servidor respondió como «${visto}» y no como «${esperado}»: la ruta ` +
      '/correspondencia/buzones no está en DELEGATION_ROUTES del BFF. El selector ' +
      'mostraría el registro visto por otra identidad.'
  }
  if (!(registro.propio || '').trim()) {
    return 'El servidor no devolvió tu buzón canónico; sin él no se puede fijar el scope propio.'
  }
  return ''
}

/** Estado del registro de buzones reducido a lo que DECIDE, con los nombres que usa
 *  react-query. El componente pasa `registry.isSuccess` / `registry.data`; aca no se
 *  toca la libreria, asi que esta decision es verificable sin DOM y sin runner nuevo. */
export interface EstadoRegistro {
  isSuccess: boolean
  data?: Pick<RegistroBuzones, 'identidad' | 'propio' | 'es_admin'> | null
  isError?: boolean
}

export interface PlanBandeja {
  /** ¿Se puede DERIVAR algo del registro? Falso si fallo o si la identidad no cuadra. */
  registroConfiable: boolean
  /** Solo verdadero con registro confiable. Si no, se degrada a no-admin. */
  esAdmin: boolean
  /** Buzon canonico propio segun el servidor. '' con registro no confiable. */
  propio: string
  /** Motivo del desajuste de delegacion para mostrar en pantalla. '' = nada que decir. */
  avisoDelegacion: string
  /** El scope que se va a mirar de verdad. Con registro no confiable es siempre el
   *  propio: la seleccion del usuario no se pierde, pero no se obedece a ciegas. */
  scopeEfectivo: ScopeCorrespondencia
  propioResuelto: ResultadoScope
  activoResuelto: ResultadoScope
  /** ¿Se consulta la bandeja propia (contador «mios pendientes»)? */
  consultarPropio: boolean
  /** ¿Se consulta la bandeja del scope activo (la lista que se ve)? */
  consultarActivo: boolean
}

/** Que consultar, dado el estado del registro y el scope elegido.
 *
 *  🔴 POR QUE ESTA FUNCION EXISTE Y NO ES UNA LINEA EN EL JSX. La primera version de
 *  esta feature escribia la decision a mano en el componente:
 *
 *      const listoParaConsultar = registry.isSuccess && !delegacionRota
 *      enabled: listoParaConsultar && resuelto.ok
 *
 *  y eso convertia CUALQUIER fallo del registro en una caida del correo. Medido en la
 *  libreria instalada (`@tanstack/react-query` 5.101.2, `queryObserver.js`): con
 *  `enabled:false` la consulta queda en `fetchStatus:'idle'` y reporta **`isLoading:false`
 *  y `error:null`** — o sea que la vista no muestra spinner ni error: muestra la lista
 *  con `count ?? 0`. Consecuencias reales de ese defecto:
 *
 *  · Si `/correspondencia/buzones` no esta en `DELEGATION_ROUTES`, el gateway responde
 *    403 y el usuario no veia «no pude leer el registro»: veia **su Correspondencia
 *    vacia**. La bandeja nunca se pedia.
 *  · Como el 403 es estructural (el gate de admin es del servidor), le pasaba a
 *    CUALQUIER no-admin, que hoy ve su buzon sin problema.
 *  · Y la palanca de rollback mas barata del RUNBOOK —sacar la ruta de
 *    `DELEGATION_ROUTES`— dejaba de ser «volver al comportamiento de hoy» y pasaba a
 *    ser apagar el correo de todos. Una palanca asi es peor que no tener palanca: se
 *    usa bajo presion y estaba documentada como segura.
 *
 *  ⇒ La regla correcta es DEGRADAR, no bloquear: si el registro no se puede confiar se
 *  olvida (`esAdmin=false`, `propio=''`) y `buzonParaScope` devuelve `buzon:''`, que es
 *  exactamente lo que manda el panel hoy en produccion — el servidor fija el buzon del
 *  caller. Se pierde el selector, no el correo. El cartel de aviso se conserva.
 *
 *  ⚠️ La unica puerta que queda cerrada es deliberada y NO es este caso: un admin con
 *  registro confiable pero SIN buzon canonico. Ahi `buzon:''` significaria VISTA GLOBAL
 *  y su bandeja se convertiria en la de la flota, que es lo que la tarea prohibe. Ese
 *  estado se BLOQUEA, y el componente tiene que decirlo en pantalla
 *  (`motivoSinConsultar`) en vez de renderizar una lista vacia.
 */
export function planBandeja(
  registro: EstadoRegistro,
  scope: ScopeCorrespondencia,
  nombreDeSesion: string,
): PlanBandeja {
  const datos = registro.data ?? null
  const avisoDelegacion = datos ? desajusteDeDelegacion(datos, nombreDeSesion) : ''
  const registroConfiable = Boolean(registro.isSuccess) && Boolean(datos) && !avisoDelegacion
  const esAdmin = registroConfiable && datos?.es_admin === true
  const propio = registroConfiable ? (datos?.propio ?? '') : ''
  const ctx: ContextoScope = { esAdmin, propio }
  // Sin registro confiable no se obedece un scope de admin: `esAdmin=false` ya lo
  // neutralizaria, pero fijarlo aca deja el invariante escrito y no dependiente de como
  // se comporte `buzonParaScope` en el futuro.
  const scopeEfectivo: ScopeCorrespondencia = registroConfiable ? scope : { kind: 'propio' }
  const propioResuelto = buzonParaScope({ kind: 'propio' }, ctx)
  const activoResuelto = buzonParaScope(scopeEfectivo, ctx)
  return {
    registroConfiable,
    esAdmin,
    propio,
    avisoDelegacion,
    scopeEfectivo,
    propioResuelto,
    activoResuelto,
    // 🔴 NO se exige `registro.isSuccess`: ahi estaba el bug. Lo unico que apaga una
    // consulta es que el scope no se pueda resolver sin mentir.
    consultarPropio: propioResuelto.ok,
    consultarActivo: activoResuelto.ok,
  }
}

/** Que decirle al usuario cuando la lista NO se consulto.
 *
 *  Existe porque «no pregunte» y «pregunte y no hay nada» se renderizaban igual. Si esto
 *  devuelve algo, el componente DEBE mostrarlo en lugar de la lista: una bandeja vacia
 *  que en realidad nunca se pidio es la falla que motivo este modulo.
 */
export function motivoSinConsultar(plan: PlanBandeja): string {
  if (plan.consultarActivo) return ''
  return plan.activoResuelto.ok ? '' : plan.activoResuelto.motivo
}

interface BandejaParcial {
  items?: { raiz: string }[]
  enviados?: { raiz: string }[]
}

/** Raíces en las que el CALLER participa, según el predicado real del servidor
 *  (`_thread_participant`: remitente del hilo O con entrega en él).
 *
 *  Se alimenta SÓLO de las consultas fijadas al buzón propio, nunca de una vista ajena
 *  o global: ahí `items` son entregas de otros y `enviados` sigue siendo mío (el
 *  servidor filtra los enviados por el caller, no por el buzón consultado). Mezclarlas
 *  inventaría participación que el servidor no reconoce.
 */
export function raicesParticipadas(propias: BandejaParcial[]): Set<string> {
  const raices = new Set<string>()
  for (const bandeja of propias) {
    for (const mensaje of bandeja?.items ?? []) if (mensaje?.raiz) raices.add(mensaje.raiz)
    for (const mensaje of bandeja?.enviados ?? []) if (mensaje?.raiz) raices.add(mensaje.raiz)
  }
  return raices
}

export function participaEnHilo(raiz: string, participadas: Set<string>): boolean {
  return participadas.has(raiz)
}

/** ¿Se ofrece una acción de escritura sobre este mensaje?
 *
 *  `acciones.*.disponible` del backend es `has_delivery`, o sea "este mensaje tiene una
 *  entrega", NO "vos podés operarlo". En vista global todo item tiene entrega — de
 *  otro — así que el backend declara `true` sobre mensajes donde `leer`/`ok`/`reaccion`
 *  después dan 404 y `responder` da 403. Se cruza con la participación PROPIA: ofrecer
 *  un botón que va a fallar es peor que no ofrecerlo.
 */
export function accionOfrecida(disponibleSegunBackend: boolean, participa: boolean): boolean {
  return Boolean(disponibleSegunBackend) && participa
}

/** ¿Se puede intentar la descarga de un adjunto?
 *  Un adjunto retirado ya estaba deshabilitado (410); esto agrega el caso ajeno (403).
 *  Además apaga el preview: `AttachmentImage` descarga al MONTAR, así que un adjunto
 *  ajeno de tipo imagen generaba un 403 sin que nadie tocara nada. */
export function adjuntoAccionable(retirado: boolean, participa: boolean): boolean {
  return !retirado && participa
}

/** Rótulo de la lista de enviados. UNA sola forma para los tres scopes.
 *
 *  `enviados` es SIEMPRE el correo enviado por el caller, no por el buzón consultado
 *  (el servidor filtra `WHERE lower(remitente)=lower(caller)`). Llamarlo «Enviados» a
 *  secas dentro de una vista ajena sería mentir; nombrar al dueño una sola vez, para
 *  los tres scopes, evita tener una verdad condicional que mantener.
 */
export function enviadosTitulo(propio: string, hilos: number): string {
  const dueno = (propio || '').trim() || 'vos'
  return `Enviados por vos (${dueno}) · ${hilos} hilos`
}

/** Etiqueta del ámbito activo. Distingue MI bandeja de una que estoy administrando:
 *  «tengo esto pendiente» y «estoy mirando la bandeja de otro» no son lo mismo. */
export function scopeEtiqueta(scope: ScopeCorrespondencia, ctx: ContextoScope): string {
  if (scope.kind === 'global') return 'Vista global de la flota · administrando'
  if (scope.kind === 'buzon') {
    const objetivo = scope.buzon.trim()
    if (objetivo && objetivo === ctx.propio.trim()) return `Mi buzón (${objetivo})`
    return `Buzón de ${objetivo || '?'} · administrando`
  }
  return `Mi buzón (${ctx.propio.trim() || 'sin resolver'})`
}

/** ¿El scope activo es el mío? Si no, los contadores del scope NO son mis pendientes
 *  y hay que mostrar el propio aparte. */
export function scopeEsPropio(scope: ScopeCorrespondencia, ctx: ContextoScope): boolean {
  if (scope.kind === 'global') return false
  if (scope.kind === 'buzon') {
    const objetivo = scope.buzon.trim()
    return Boolean(objetivo) && objetivo === ctx.propio.trim()
  }
  return true
}
