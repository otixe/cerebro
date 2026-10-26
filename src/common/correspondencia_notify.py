"""FUENTE ÚNICA del aviso automático de asignación de tablero -> CORRESPONDENCIA.

Qué reemplaza
-------------
Hasta hoy `bus_notify.notify_assignment()` insertaba en `bus_messages` +
`bus_deliveries`. Con el bus en sólo-lectura (410 desde 2026-09-15 01:29) el aviso
de tablero quedó como LA ÚNICA escritura viva a esa tabla: medido, los tres
`bus_messages` posteriores al cierre son avisos (forge→merlin 01:58,
rudeus-roxy→rudeus 01:51 y 01:27) y el último no-aviso es de las 00:15.
Este módulo escribe el aviso en Correspondencia y deja de tocar el bus.

Por qué escribe SQL en vez de llamar a `correspondencia.publish()`
-----------------------------------------------------------------
Los mismos tres motivos que `bus_notify` documenta para no llamar al REST, más uno
nuevo:
  1. `publish()` vive en `gateway/correspondencia.py`, que NO está horneado en las
     imágenes de mcp ni de sinapsis-bff.
  2. `publish()` depende de `db.pg()` (el pool del gateway) y de
     `fastapi.HTTPException`: importarlo desde otra puerta arrastraría medio
     servicio.
  3. Llamar al gateway por HTTP acoplaría la UI al gateway y ROMPERÍA LA
     ATRIBUCIÓN: el BFF sólo tiene keys de OTROS agentes y su actor real es un
     humano de `sessions`.
  4. `publish()` responde 422 ante un destinatario inexistente. Acá un destinatario
     irresoluble NO puede hacer fallar la operación de tablero.
Lo que SÍ se reusa es el resolvedor: `correspondencia_core` son los bytes exactos
de `_registry` / `normalize_mailbox` / `individual_mailbox` / `_new_root` /
`_insert_deliveries` extraídos del fuente desplegado.

Contrato — idéntico al de `bus_notify.notify_assignment`
-------------------------------------------------------
`notify_assignment_correspondencia(pg, actor, target, content, tid)`; devuelve el
buzón avisado (str) o None; NUNCA lanza. La firma no cambia A PROPÓSITO: así los
SIETE call sites reales (gateway/tasks_core.py ×3 vía `_notify_if_leader`,
mcp/mcp_server.py ×3, sinapsis/server.py ×1) NO se tocan.

Las propiedades que pidió la spec, y dónde están
------------------------------------------------
· buzón canónico       -> `resolve_notify_mailbox()`, sobre `_core._registry`
· tipo `inform`        -> `TIPO`, con `abierto=False` (se cierra con OK, no abre hilo)
· prioridad derivada   -> `map_priority()` desde `tasks.priority`
· `refs_task`          -> el uuid de la tarea; el cuerpo NO repite el contenido
· NO bloqueante        -> `except Exception` global; el fallo se REGISTRA (log +
                          `task_activity`), no se traga en silencio
· idempotencia         -> `pg_advisory_xact_lock` + SELECT de duplicado exacto
                          DENTRO DE UNA VENTANA de `DEDUP_WINDOW_SECONDS`, SIN
                          DDL nueva. La ventana es lo que separa «doble disparo»
                          de «evento repetido»: sin ella la dedup por texto
                          suprimía el 23,9% de los avisos reales (medido).
"""
import hashlib
import logging
import os

import correspondencia_core as _core

log = logging.getLogger("correspondencia_notify")

# ── PRIORIDAD ────────────────────────────────────────────────────────────────
# MEDIDO en la tabla `tasks` el 2026-09-15: los valores existentes son EXACTAMENTE
# tres -- high(1029), medium(804), low(158). No hay ningún otro, así que este mapa
# cubre el 100% de las 1991 filas y `PRIORIDAD_DEFAULT` sólo puede activarse con un
# valor nuevo o vacío (una tarea creada sin priority por una puerta futura).
# El destino es el CHECK de `correspondencia_mensajes`: urgente|medio|bajo.
PRIORITY_MAP = {"high": "urgente", "medium": "medio", "low": "bajo"}
PRIORIDAD_DEFAULT = "medio"          # el mismo default que la columna en la DB

# ── TIPO ─────────────────────────────────────────────────────────────────────
# `inform` (=> abierto=False). Es el requisito R4 de la tarea madre 500a0a78 en
# palabras de Otixe: un informativo se acusa con una reacción, no con un mensaje,
# «así evitamos que un mensaje informando una skill se convierta en chats eternos».
# Un aviso de tablero es el caso puro: no hay nada que discutir en el aviso, la
# conversación va en la tarea. Si alguien necesita objetar, F2 ya dejó
# `solicitar-revision`, que convierte este inform cerrado en hilo abierto.
TIPO = "inform"
ABIERTO = False

# ── LA REPARACIÓN DEL AVISO A HUMANOS, RETIRABLE POR SÍ SOLA ─────────────────
# `resolve_notify_target` del bus dejó de avisarle a @otixe el 2026-08-11 (su rama de
# humanos sólo dispara sin fila en `agents`, y él tiene fila con team=NULL): 39 avisos
# hasta esa fecha, CERO después, ~31 tareas/mes perdidas en silencio. El resolvedor
# canónico lo repara, y eso es TRÁFICO NUEVO EN LA BANDEJA DE UNA PERSONA.
# Por eso la reparación tiene su PROPIA palanca, separada de `CORRESPONDENCIA_NOTIFY`:
# si Otixe no quiere los avisos, sale con una env var y la MIGRACIÓN DE CANAL QUEDA
# INTACTA. Colapsar las dos en un flag obligaría a elegir entre su bandeja y F8.
# Default ON: la reparación se queda (decisión de Saruman). `off` restaura el
# comportamiento pre-reparación SÓLO para `kind='human'`; los agentes no se tocan.
NOTIFY_HUMANOS_ENV = "CORRESPONDENCIA_NOTIFY_HUMANOS"
# Allowlist de APAGADO (no de encendido): el default tiene que ser avisar, así que un
# valor ilegible o con un typo NO puede silenciar la bandeja de nadie por accidente.
NOTIFY_HUMANOS_OFF = frozenset({"off", "0", "false", "no", "cerrado", "apagado"})


def notify_humanos(env=None):
    """True si hay que avisarle a los buzones humanos. Se lee EN CADA LLAMADA."""
    try:
        source = os.environ if env is None else env
        raw = source.get(NOTIFY_HUMANOS_ENV, "")
        if not isinstance(raw, str):
            return True
        return raw.strip().lower() not in NOTIFY_HUMANOS_OFF
    except Exception:      # noqa: BLE001 - entorno ilegible => se avisa (default)
        return True

ASUNTO_MAX = 180        # `asunto` es texto libre en la DB; el título de una tarea
                        # puede tener cientos de chars (500a0a78 tiene ~300).

# ── ID CORTO EN EL ASUNTO ────────────────────────────────────────────────────
# Requisito de la spec de F8: el asunto lleva «id corto de la tarea + título».
# 8 hex es la MISMA convención con la que la flota ya direcciona tareas: el tablero
# acepta prefijos (`task_get` resuelve por prefijo) y las tareas se nombran así en las
# specs y en el tablero (`2ffc1594`, `500a0a78`). No se inventa un formato nuevo.
# Por qué en el asunto y no sólo en `refs_task`: `refs_task` es un uuid que la bandeja
# no muestra, así que sin esto dos avisos de tareas distintas con el mismo título son
# indistinguibles EN LA LISTA, que es donde se decide qué abrir.
ID_CORTO_LEN = 8
# EL TOPE, DECLARADO: `ASUNTO_MAX` = 180 chars TOTALES, prefijo incluido. El prefijo
# `[xxxxxxxx] ` (11 chars) NUNCA se trunca: se trunca la línea de aviso a 169 y el
# prefijo se antepone después. Un título de 300 chars pierde su cola, pero el id
# sobrevive -- al revés sería un asunto largo que no identifica nada.
CUERPO_MAX = 4000

# Versión del esquema de la clave de idempotencia. Si el formato del cuerpo cambia,
# subir esto es lo que evita que un aviso viejo tape uno nuevo.
EVENT_KEY_VERSION = "v1"

# ── VENTANA DE DEDUPLICACIÓN ─────────────────────────────────────────────────
# 🔴 REPARA UN BLOQUEANTE MEDIDO POR EL GATE (Grima, 2026-09-15).
#
# La clave de idempotencia se deriva del TEXTO del aviso, y «mismo texto» NO es
# «mismo evento»: la línea se arma con `tasks.title`, que es CONSTANTE entre
# eventos distintos del mismo disparador. Sin ventana, la dedup deja de ser una
# guarda contra el doble disparo y pasa a ser SUPRESIÓN de avisos reales.
#
# RE-MEDIDO POR MÍ sobre los datos de HOY (no copié el número del gate).
# Universo declarado: `bus_messages` con `refs_task IS NOT NULL` cuyo contenido
# arranca con uno de los tres prefijos de aviso => 599 filas (el gate midió 598;
# la diferencia es un `create` nuevo de las 07:03Z). Agrupado por la MISMA clave
# del predicado -- (refs_task, lower(remitente), buzón, contenido):
#
#   · repeticiones legítimas (pos>1):    143 de 599 = 23,9 %
#       submit 127/352 (36,1 %) · handoff 16/105 (15,2 %) · create 0/142
#   · gap MÍNIMO real entre repeticiones:  97,354 s   <-- el número que fija N
#   · p1 106,5 s · p5 300,2 s · p50 32 min · máx 13 d 13 h
#   · repeticiones bajo 1 / 5 / 10 / 30 / 60 / 90 s:  CERO en todas
#   · bajo 120 s ya hay 2  =>  120 sería incorrecto
#   · peor grupo: 7 eventos legítimos con texto idéntico
#
# N = 30 s (decisión de Saruman, 2026-09-15, sobre estos dos hallazgos MÍOS que
# invierten la carga de la prueba; la primera versión de este fix ponía 60):
#
#   (a) LA DEDUP PREVINO CERO DOBLES DISPAROS HISTÓRICOS. En 599 avisos no hay UNA
#       repetición bajo 10 s. O sea: su beneficio es PROSPECTIVO (una carrera que
#       puede pasar y nunca pasó) y su costo era MEDIDO (143 avisos suprimidos).
#       Cuando el beneficio es hipotético y el costo real, la protección tiene que
#       ser LO MÁS CHICA POSIBLE, no lo más holgada.
#   (b) EL DAÑO ES ASIMÉTRICO. Duplicar un aviso es molesto y VISIBLE: alguien lo
#       ve dos veces y se queja. Suprimirlo es SILENCIOSO y le oculta a Otixe que
#       algo espera su firma -- exactamente la clase de fallo que esta migración
#       vino a reparar (§2.2: 31 avisos/mes perdidos en silencio durante un mes).
#       Ante asimetría se elige el lado que falla RUIDOSO.
#
# 30 s cubre lo que de verdad hay que cubrir -- una carrera de milisegundos (3-4
# órdenes de magnitud de margen) y un reintento con backoff de 1/2/4 s (un orden) --
# y deja 67,354 s de margen sobre el mínimo real medido, contra 37,354 s con N=60.
#
# ⚠️ EL MARGEN ES EMPÍRICO, NO ESTRUCTURAL: 67 s de aire sobre una muestra de 143.
# Nada impide que un re-submit futuro llegue a los 20 s. Lo que la ventana
# garantiza es que el daño quede ACOTADO a 30 s en vez de ser permanente.
# ⚠️ Y UN PING-PONG MÁS RÁPIDO QUE N SIGUE COLAPSANDO: A->B, B->A, A->B en menos
# de 30 s pierde el tercer aviso. No está resuelto; está acotado.
# ⚠️ NO arregla la dirección contraria (texto editado, mismo evento => DUPLICA).
# Esa queda igual, y es la dirección FAIL-SAFE: un aviso de más es ruido, uno de
# menos es silencio.
#
# NO se hace configurable por entorno A PROPÓSITO: un valor grande reintroduce el
# bloqueante, y la palanca de reversión gruesa ya existe (`CORRESPONDENCIA_NOTIFY`).
DEDUP_WINDOW_SECONDS = 30


def map_priority(task_priority):
    """`tasks.priority` -> prioridad de Correspondencia. Nunca lanza."""
    return PRIORITY_MAP.get((task_priority or "").strip().lower(), PRIORIDAD_DEFAULT)


def _one_line(text, limit):
    r"""Colapsa a una línea, saca NUL y trunca. NO es un sanitizador.

    No se reusa `sanitize.repair_surrogates` (que sí usa `publish()`) por dos
    motivos medidos: vive en `gateway/` y no está en las otras dos imágenes, y el
    texto de un aviso se arma con `tasks.title`, que YA salió de Postgres -- una
    columna `text` no puede contener NUL ni surrogates inválidos. El
    `replace("\x00","")` queda como defensa del caso en que un caller futuro pase
    texto que no venga de la DB.
    """
    clean = " ".join((text or "").replace("\x00", "").split())
    if len(clean) > limit:
        clean = clean[:limit - 1].rstrip() + "…"
    return clean


def short_id(tid):
    """Id corto de la tarea: los primeros `ID_CORTO_LEN` chars del uuid.

    Sin `-` y en minúscula, igual que como lo escribe la flota. Si `tid` viniera
    vacío (no puede: el INSERT tiene FK a `tasks`), devuelve "" y el asunto queda
    como antes en vez de mostrar un `[]` vacío.
    """
    raw = str(tid or "").strip().lower().replace("-", "")
    return raw[:ID_CORTO_LEN]


def build_subject(content, tid):
    """`[id-corto] línea de aviso`. El id NUNCA se trunca; la línea sí.

    El tope total es `ASUNTO_MAX` (180), prefijo incluido: se trunca la línea de aviso
    a lo que quede después de reservar el prefijo, y el prefijo se antepone. Así el
    asunto identifica la tarea incluso con un título de 300 chars.
    """
    sid = short_id(tid)
    if not sid:
        return _one_line(content, ASUNTO_MAX) or "Aviso del tablero"
    prefijo = "[%s] " % sid
    resto = _one_line(content, ASUNTO_MAX - len(prefijo)) or "Aviso del tablero"
    return prefijo + resto


def build_body(content, tid):
    """El cuerpo es el AVISO, no el contenido.

    Sigue la guía de la F6 (`claridad-contextual`): el mensaje apunta a su fuente en
    vez de repetirla. `refs_task` lleva el puntero estructurado; esta línea lo dice
    en texto para quien lea la bandeja sin resolver la referencia.
    """
    return "%s\n\ntarea: %s\nEl detalle vive en la tarea (refs_task); este aviso no lo repite." % (
        _one_line(content, CUERPO_MAX) or "Aviso del tablero", tid)


def event_key(tid, remitente, mailbox, content):
    """Clave determinista del EVENTO, para idempotencia.

    Se deriva de lo que el call site ya pasa -- (tarea, remitente, buzón, línea de
    aviso) -- porque cambiar la firma obligaría a tocar los siete call sites y ese
    es exactamente el riesgo que esta migración evita. La línea de aviso es lo que
    DISCRIMINA el disparador: create dice "Nueva tarea asignada:", handoff "Tarea
    recibida por handoff:" y submit "Tarea entregada para tu firma:" (medido: 141 /
    105 / 343 mensajes históricos con esos prefijos).

    `remitente` es el nombre CANÓNICO de la fila de `agents`, no el string que pasó
    el caller: así `@merlin` y `merlin` producen la misma clave.

    🔴 LÍMITE DE ESTA CLAVE, MEDIDO POR EL GATE Y REPARADO CON UNA VENTANA:
    la línea discrimina el DISPARADOR, no el EVENTO. Dos eventos legítimos del
    mismo disparador sobre la misma tarea (el caso más frecuente del sistema:
    entrega -> FAIL -> arreglo -> RE-ENTREGA) producen la MISMA clave. Por eso
    esta clave NO se usa sola: el `SELECT` de duplicado la acota a
    `DEDUP_WINDOW_SECONDS`. Sin esa ventana la clave colapsaba 143 de 599 avisos
    históricos (23,9%; submit 36,1%; hasta 7 eventos en un grupo).
    """
    raw = "%s|%s|%s|%s|%s" % (EVENT_KEY_VERSION, tid,
                              (remitente or "").strip().lower(), mailbox,
                              _one_line(content, CUERPO_MAX))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot(cur):
    """UN solo `_registry` por aviso: resolver actor y destinatario sobre el MISMO
    snapshot evita que una revocación concurrente los deje inconsistentes."""
    individuals, _teams, aliases = _core._registry(cur)
    by_name = {str(row["name"]).strip().lower(): box
               for box, row in individuals.items()}
    return individuals, aliases, by_name


def _box_of(raw, individuals, aliases, by_name):
    """Buzón individual de un nombre plano o de un alias `@…`. None si no existe."""
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value.startswith("@"):
        candidate = aliases.get(value, value)
        return candidate if candidate in individuals else None
    return by_name.get(value)


def _fallback_inactivo(cur, raw, individuals, aliases):
    """Política del BUS para un destinatario SIN buzón activo: líder de su equipo.

    POR QUÉ EXISTE. `_box_of` resuelve contra el snapshot de `_registry`, que lee
    `WHERE active=true`. Un subagente con `active=false` no tiene buzón, así que
    Correspondencia devolvía `(None, "sin buzon activo")` = SILENCIO, mientras el
    bus SÍ le avisaba: `bus_notify.resolve_notify_target` nunca chequea `active`
    del destinatario y cae en su rama de subagente -> líder del equipo.
    Medido (2026-09-16): son OCHO identidades reales con `active=false`, `team`
    poblado y líder activo -- `eris`, `linia`, `nanahoshi`, `pursena`, `roxy`,
    `sylphie` (team radagast) y `zz-sub-prueba-borrar`, `zz-test-member` (team
    saruman). Hoy el bus les avisa al líder; al cortar el bus, sin esta rama no
    les avisaría NADIE.

    Se eligió la salida BENIGNA: preservar el aviso, no rechazarlo. El destino es
    el LÍDER (nunca el inactivo), que es exactamente la política del bus y tiene el
    mismo motivo sustantivo: una entrega dirigida a un agente que no corre queda
    en 'pendiente' y nadie la drena.

    TRES LÍMITES DELIBERADOS:
     1. Sólo nombres PLANOS. Un `@buzón` no se busca en `agents.name`; mapear
        `@equipo/local` de vuelta a una fila exigiría invertir el stripping de
        prefijo de `individual_mailbox`, que no es inyectivo.
     2. Sólo si la fila está `active=false`. Una fila ACTIVA sin buzón es otro
        problema (fila inconsistente, ya salteada y logueada por `_registry`) y
        taparlo acá borraría su síntoma.
     3. NO se replica el `if is_lead: return who` del bus, que avisa a un LÍDER
        inactivo directamente. Acá ese buzón NO EXISTE en `individuals`, y
        entregar a un buzón inexistente crea `correspondencia_entregas` que nadie
        puede leer ni drenar. Un líder inactivo cae por `aliases` (que sólo se
        puebla desde filas active=true con is_lead=true) y devuelve None.
        DIVERGENCIA DECLARADA respecto del bus, no un olvido.
    """
    if raw.startswith("@"):
        return None, ""
    try:
        cur.execute(
            "SELECT team, is_lead, active FROM agents WHERE lower(name) = %s LIMIT 1",
            (raw,))
        row = cur.fetchone()
    except Exception:      # noqa: BLE001 - contrato: el aviso no puede lanzar
        return None, ""
    if not row:
        return None, ""

    def _f(key, idx):
        return row[key] if isinstance(row, dict) else row[idx]

    if bool(_f("active", 2)):
        return None, ""                     # límite 2
    team = str(_f("team", 0) or "").strip().lower()
    if not team:
        return None, ""
    lead_box = aliases.get("@" + team)
    if not lead_box or lead_box not in individuals:
        return None, ""                     # límite 3 (incluye líder inactivo)
    return lead_box, ("lider del equipo del subagente INACTIVO %s "
                      "(politica del bus preservada)" % raw)


def resolve_notify_mailbox(cur, target, snapshot=None):
    """A QUÉ BUZÓN CANÓNICO se avisa. Devuelve (buzón, motivo) o (None, motivo).

    Reemplaza a `bus_notify.resolve_notify_target`, que devolvía un NOMBRE PLANO
    (`saruman`, `merlin`) -- direccionamiento que Correspondencia no acepta: sus
    buzones son `@equipo/agente` y el humano es `@otixe`.

    Se CONSERVA la política de destino del bus (subagente -> líder de su equipo) y
    su motivo sustantivo, que vale igual acá: un subagente MUERE al terminar su
    turno, así que una entrega dirigida a él puede no leerse nunca y acumula
    `correspondencia_entregas` en 'pendiente' que nadie puede drenar. El líder
    persiste entre sesiones y es quien reparte el trabajo.

    🔴 DIFERENCIA DE COMPORTAMIENTO, DECLARADA Y MEDIDA -- no es un efecto colateral
    inadvertido: `resolve_notify_target('otixe')` devuelve None HOY. Su rama de
    humanos (`FALLBACK_LEADERS`) sólo dispara cuando el destinatario NO TIENE FILA
    en `agents`, y otixe sí la tiene: active=true, kind='human', role='admin',
    is_lead=false, team=NULL -> cae en `if not team: return None, "subagente sin
    team"`. Efecto medido: recibió 39 avisos entre 2026-07-21 y 2026-08-11 y CERO
    desde entonces, justo desde que se creó `bus_notify.py`; en los últimos 30 días
    se le asignaron 31 tareas, con 31 avisos perdidos en silencio.
    Este resolvedor usa `_registry`, que sí tiene su buzón (`@otixe`, vía la rama
    `kind == 'human'` de `individual_mailbox`) => el aviso a Otixe VUELVE a salir.
    Reproducir el None habría exigido romper el resolvedor canónico a propósito.
    """
    raw = (target or "").strip().lower()
    if not raw:
        return None, "destinatario vacio"
    if snapshot is None:
        snapshot = _snapshot(cur)
    individuals, aliases, by_name = snapshot
    box = _box_of(raw, individuals, aliases, by_name)
    if not box:
        # Antes de rendirse: política del bus para inactivos con team y líder
        # activo. La degradación va ACÍ y no dentro de `_box_of` a propósito --
        # `_box_of` resuelve TAMBIÉN el ACTOR (ver `notify_assignment_correspondencia`),
        # y degradarlo ahí haría que un REMITENTE inactivo empiece a resolver, que
        # es un efecto distinto del pedido y no aparecería en una medición cuyo
        # sujeto es el destinatario. El seam correcto es el más angosto que
        # produce el efecto pedido.
        degradado, motivo_deg = _fallback_inactivo(cur, raw, individuals, aliases)
        if degradado:
            return degradado, motivo_deg
        return None, "destinatario %r sin buzon activo" % raw
    row = individuals[box]
    if str(row.get("kind") or "agent").strip().lower() == "human":
        if not notify_humanos():
            # Se registra como DECISIÓN, no como fallo: es una política puesta a
            # propósito, y el motivo NOMBRA la variable para que quien lea el hilo de
            # la tarea sepa por qué no salió y cómo volver a activarlo.
            return None, ("aviso a humanos desactivado por %s (la migracion de canal "
                          "sigue activa)" % NOTIFY_HUMANOS_ENV)
        return box, "humano (buzon propio)"
    if bool(row.get("is_lead")):
        return box, "lider (is_lead)"
    team = str(row.get("team") or "").strip().lower()
    if not team:
        return None, "subagente sin team (no resoluble)"
    lead_box = aliases.get("@" + team)
    if not lead_box or lead_box not in individuals:
        # `aliases['@team']` sólo se puebla desde una fila active=true con
        # is_lead=true => que exista YA prueba que el líder está activo. No hace
        # falta la segunda query que hacía `resolve_notify_target`.
        return None, "el equipo %r no tiene lider activo" % team
    if lead_box == box:
        return None, "team propio sin is_lead"
    return lead_box, "lider del equipo del subagente %s" % raw


class _Skip(Exception):
    """El aviso NO se manda. `registrar` distingue decisión de fallo.

    No todo lo que no manda un aviso es un fallo: un autoaviso es el comportamiento
    CORRECTO y documentado (bus_notify: «una auto-entrega no es noticia para
    nadie»). Registrarlo como fallo en el hilo de la tarea metería ruido en cada
    auto-asignación y entrenaría a ignorar el rastro. Los fallos de verdad
    (destinatario irresoluble, remitente sin buzón, tarea inexistente) SÍ se
    registran: es el requisito «no falles en silencio».
    """

    def __init__(self, motivo, registrar=True):
        super().__init__(motivo)
        self.registrar = registrar


def _register(pg, tid, actor, target, motivo, level=logging.WARNING):
    """Deja el fallo OBSERVABLE. Best-effort, nunca lanza.

    Dos canales, a propósito:
      1. `logging` -> stderr del contenedor (`docker logs`). Sirve aunque la DB
         esté caída, que es justo cuando el canal 2 no puede funcionar.
      2. `task_activity` -> el hilo de la tarea, el MISMO lugar donde el bus dejaba
         `[bus] notificado a X`. Sin este rastro, "no se enteró" y "no se le avisó"
         son indistinguibles.
    Abre su PROPIA conexión: si el aviso falló por un error de SQL, la transacción
    original quedó abortada y no puede escribir nada más.
    """
    log.log(level, "aviso de tablero NO enviado: tarea=%s actor=%s target=%r motivo=%s",
            tid, actor, target, motivo)
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_activity (task_id, agent, kind, body) "
                "VALUES (%s,%s,'comment',%s)",
                (tid, actor,
                 "[correspondencia] aviso NO enviado a %r: %s" % (target, motivo)))
            conn.commit()
    except Exception as exc:      # noqa: BLE001 - el rastro es best-effort
        log.warning("tampoco se pudo registrar el fallo en task_activity (%s): %s",
                    tid, exc)


def notify_assignment_correspondencia(pg, actor, target, content, tid):
    """Crea el aviso de asignación en Correspondencia. Best-effort, NO lanza NUNCA.

    Devuelve el buzón avisado, o None si no se avisó (ya sea por decisión --
    autoaviso, destinatario irresoluble -- o por fallo).
    """
    act = (actor or "").strip().lower()
    tid_s = str(tid)
    motivo_fallo = None
    registrar = True
    try:
        with pg() as conn, conn.cursor() as cur:
            snapshot = _snapshot(cur)
            individuals, aliases, by_name = snapshot

            box, motivo = resolve_notify_mailbox(cur, target, snapshot)
            if not box:
                # Un destinatario vacío es lo que pasa en toda tarea creada SIN
                # assignee: no es un fallo del aviso, es que no hay a quién avisar.
                raise _Skip(motivo, registrar=bool(act and (target or "").strip()))

            # El remitente tiene que EXISTIR y estar activo: es el equivalente de
            # `caller_identity()` en publish(), que responde 403 si no. Acá no se
            # puede fallar, así que se salta el aviso y queda registrado.
            actor_box = _box_of(act, individuals, aliases, by_name)
            if not actor_box:
                raise _Skip("el remitente %r no tiene buzon activo" % act)
            remitente = str(individuals[actor_box]["name"]).strip()

            # No avisarse a sí mismo. Se compara el buzón PROPIO del actor (sin la
            # redirección subagente->líder) contra el buzón destino: es la
            # traducción exacta de la guarda `who == act` del bus. NO es un fallo.
            if actor_box == box:
                raise _Skip("autoaviso (%s)" % box, registrar=False)

            # La prioridad se LEE de la tarea en vez de viajar en la firma: así los
            # siete call sites quedan intactos.
            cur.execute("SELECT id, priority FROM tasks WHERE id::text = %s", (tid_s,))
            trow = cur.fetchone()
            if not trow:
                # `correspondencia_mensajes.refs_task` tiene FK a `tasks(id)`: sin
                # la tarea el INSERT fallaría con un error de integridad.
                raise _Skip("la tarea %s no existe" % tid_s)
            task_uuid = trow["id"]
            prioridad = map_priority(trow.get("priority"))

            asunto = build_subject(content, tid_s)
            cuerpo = build_body(content, tid_s)

            # ── IDEMPOTENCIA, SIN DDL NUEVA ──────────────────────────────────
            # El lock de transacción cierra la carrera entre el SELECT y el INSERT
            # (dos disparos concurrentes del mismo evento leerían los dos "no hay
            # duplicado"). Se libera solo en el commit/rollback: no hay lock
            # colgado si el proceso muere.
            key = event_key(tid_s, remitente, box, content)
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
            # La VENTANA es parte del predicado, no una optimización: acota la
            # dedup a la carrera real. `m.creado_en` tiene DEFAULT now() y es NOT
            # NULL, así que la comparación es siempre decidible. Va parametrizada
            # (nada de interpolar el intervalo en el SQL).
            cur.execute(
                "SELECT m.codigo FROM correspondencia_mensajes m "
                "JOIN correspondencia_entregas e ON e.mensaje_id = m.id "
                "WHERE m.refs_task = %s AND lower(m.remitente) = lower(%s) "
                "AND e.buzon = %s AND m.tipo = %s AND m.contenido = %s "
                "AND m.creado_en > now() - (%s * interval '1 second') LIMIT 1",
                (task_uuid, remitente, box, TIPO, cuerpo, DEDUP_WINDOW_SECONDS))
            dup = cur.fetchone()
            if dup:
                # SI EL SISTEMA SUPRIME UN AVISO, QUE SE VEA. La rama de duplicado
                # hacía commit + log.info y devolvía el buzón: para el caller y
                # para el hilo de la tarea era indistinguible de haber avisado, o
                # sea reintroducía justo la ambigüedad «no se enteró / no se le
                # avisó» que este módulo declara resuelta. El rastro va en la MISMA
                # transacción que el commit que libera el lock: si el rastro no
                # entra, la supresión tampoco queda registrada como consumada.
                cur.execute(
                    "INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'comment',%s)",
                    (tid, remitente,
                     "[correspondencia] aviso SUPRIMIDO por idempotencia: ya "
                     "existe %s a %s dentro de la ventana de %ds"
                     % (dup["codigo"], box, DEDUP_WINDOW_SECONDS)))
                conn.commit()       # libera el advisory lock
                log.info("aviso ya existente (idempotente, ventana %ds): %s -> %s tarea=%s",
                         DEDUP_WINDOW_SECONDS, dup["codigo"], box, tid_s)
                return box

            message_id, root = _core._new_root(cur)
            cur.execute(
                "INSERT INTO correspondencia_mensajes "
                "(id,codigo,raiz,remitente,asunto,prioridad,contenido,tipo,abierto,"
                "refs_task,ref_vault) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (message_id, root, root, remitente, asunto, prioridad, cuerpo,
                 TIPO, ABIERTO, task_uuid, None))
            _core._insert_deliveries(cur, message_id, [box])
            # Rastro en el hilo de la tarea: hace AUDITABLE que el aviso salió, y
            # ahora lleva el CÓDIGO, así que desde la tarea se llega al mensaje.
            cur.execute(
                "INSERT INTO task_activity (task_id, agent, kind, body) "
                "VALUES (%s,%s,'comment',%s)",
                (tid, remitente,
                 "[correspondencia] aviso %s a %s (prioridad %s)" % (root, box, prioridad)))
            conn.commit()
        return box
    except _Skip as skip:
        motivo_fallo = str(skip)
        registrar = skip.registrar
    except Exception as exc:        # noqa: BLE001 - contrato: no propaga nunca
        motivo_fallo = "%s: %s" % (type(exc).__name__, exc)
    if registrar:
        # Fuera del `with`: la conexión de arriba puede estar abortada.
        _register(pg, tid, actor, target, motivo_fallo)
    else:
        log.debug("aviso no enviado (decision, no fallo): tarea=%s motivo=%s",
                  tid, motivo_fallo)
    return None
