"""FUENTE UNICA de la notificacion de tablero -> bus (tarea 2929a26a, 2026-08-11).

Por que existe este modulo
--------------------------
Hasta hoy la logica de "avisarle por el bus a quien recibe una tarea" estaba
DUPLICADA en dos puertas (gateway/tasks_core.py y mcp/mcp_server.py) y AUSENTE
en una tercera que nadie habia contado: el BFF de Sinapsis, que implementa el
ciclo completo del tablero (create/claim/comment/handoff/submit/review/block/
unblock) escribiendo directo a Postgres y con CERO referencias a bus_messages.
Efecto medido: cualquier derivacion hecha desde la UI web era invisible para el
destinatario, incluso si era lider.

Sigue el MISMO patron ya validado por la flota para `tasks_rbac.py` (tarea
c6b61bd1, 2026-08-05): el modulo vive en `common/` y se HORNEA en cada imagen
via `additional_contexts` + `COPY --from=common`. Deliberadamente NO se resolvio
con "una puerta llama a la otra por HTTP":
  1. `POST /bus/publish` NO EXISTE. El bus por REST es solo lectura
     (/bus/thread, /bus/threads). Lo midio @merlin el 2026-07-30 y lo confirme
     el 2026-08-11.
  2. Acoplaria la UI al gateway por red: si el gateway cae, la UI dejaria de
     poder operar el tablero. Hoy no depende de el.
  3. Rompe la ATRIBUCION. El `sender` del bus se deriva de la API key
     (anti-spoof). El BFF solo tiene keys de OTROS agentes en su entorno
     (SARUMAN_API_KEY, MERLIN_API_KEY, ...) y su actor real es un HUMANO de la
     tabla `sessions`. Llamar al REST con una key ajena haria que todo aviso
     originado por Otixe en la UI dijera que lo mando otro agente: es el mismo
     bug de suplantacion que Sinapsis ya cerro en su propia superficie (H3,
     `_bff_actor()` deriva el actor de la sesion, "NUNCA del body").
Con este modulo cada puerta pasa SU PROPIO actor y nadie suplanta a nadie.

Contrato
--------
- `notify_assignment(pg, actor, target, content, tid)` es la unica entrada.
- Es BEST-EFFORT y NUNCA propaga: un fallo del bus jamas debe romper el flujo de
  tareas. Devuelve el nombre del destinatario notificado, o None.
- Abre su PROPIA conexion (fuera de la transaccion de la operacion) usando la
  factory `pg` que le pasa el caller. Asi cada puerta usa su propio pool sin que
  este modulo importe nada del entorno.
"""

import logging
import os

log = logging.getLogger("bus_notify")

# Fallback SOLO para destinatarios que no tienen fila en `agents` (hoy: otixe,
# que es humano). La fuente de verdad de "quien es lider" es la COLUMNA
# `agents.is_lead`, NO esta constante.
#
# ⚠️ POR QUE NO SE HARDCODEA LA LISTA (defecto que cometi y medi el 2026-08-11):
# escribi este modulo con LEADERS = {saruman,rias,merlin,radagast,rudeus,otixe},
# copiada de tasks_core.py, y mis 10 predicados sinteticos dieron 10/10. Al medir
# contra la DB REAL aparecio que **monica tiene is_lead=true, team='monica' y
# execution_class='orchestrator'**: es lider de su propio equipo y mi constante
# la EXCLUIA. Con el hardcode, un handoff a monica (10 en la historia) seguia sin
# avisar, y su subagente `nero` (team=monica, auditor) tampoco resolvia.
# Un test sintetico solo puede falsar los casos que su autor imagino; el universo
# de destinatarios vive en la tabla. Leerla cuesta una query.
# NOTA: hay otras dos constantes LEADERS en el arbol (tasks_core.py/mcp_server.py
# con 6 miembros, y db.py con 4 para provision de keys). Este modulo no depende de
# ninguna, a proposito: una constante duplicada es una copia que se pudre.
FALLBACK_LEADERS = {n.strip().lower() for n in os.environ.get('BUS_NOTIFY_FALLBACK_LEADERS', 'admin').split(',') if n.strip()}


def resolve_notify_target(cur, target):
    """A QUIEN se le avisa realmente, dado el destinatario de la tarea.

    Devuelve (destinatario_efectivo, motivo) o (None, motivo) si no se avisa.

    El caso que este modulo viene a arreglar: un SUBAGENTE no estaba en LEADERS,
    asi que una tarea derivada a el no avisaba a NADIE. Medido el 2026-08-11:
    `rudeus-pursena` recibio 16 handoffs con 0 avisos; `grima` 2 con 0.
    Se notifica al LIDER DE SU EQUIPO, no al subagente, y el motivo es
    sustantivo: un subagente MUERE al terminar su turno, asi que un mensaje
    dirigido a el puede no leerse nunca y acumula bus_deliveries pending
    eternos. El lider persiste entre sesiones y es quien reparte el trabajo.
    """
    who = (target or "").strip().lower()
    if not who:
        return None, "destinatario vacio"
    # Se consulta la DB en vez de parsear el prefijo del nombre: `radagast-gwaihir`
    # y `gwaihir` son AMBOS destinatarios reales en task_activity, asi que un
    # split('-') acertaria en uno y fallaria en el otro.
    try:
        cur.execute(
            "SELECT team, is_lead, active FROM agents WHERE lower(name) = %s LIMIT 1",
            (who,))
        row = cur.fetchone()
    except Exception:
        return None, "no se pudo consultar agents"
    if not row:
        # Sin fila: solo se avisa si es un humano conocido (otixe opera por la UI
        # y no es un agente del Cerebro).
        if who in FALLBACK_LEADERS:
            return who, "humano conocido (sin fila en agents)"
        return None, "destinatario desconocido"

    def _f(k, i):
        return row[k] if isinstance(row, dict) else row[i]

    team = (_f("team", 0) or "").strip().lower()
    is_lead = bool(_f("is_lead", 1))

    # LIDER: se le avisa directo. La verdad la da la columna, no una constante.
    if is_lead:
        return who, "lider (is_lead)"

    # SUBAGENTE: se avisa al LIDER DE SU EQUIPO, no a el. Motivo sustantivo: un
    # subagente MUERE al terminar su turno, asi que un mensaje dirigido a el puede
    # no leerse nunca y acumula bus_deliveries pending eternos. El lider persiste
    # entre sesiones y es quien reparte el trabajo.
    if not team:
        return None, "subagente sin team (no resoluble)"
    if team == who:
        # Defensa: team propio pero is_lead=false. Evita self-loop.
        return None, "team propio sin is_lead"
    # Se VERIFICA que el team apunte a un lider real y ACTIVO, en vez de asumirlo.
    try:
        cur.execute(
            "SELECT is_lead, active FROM agents WHERE lower(name) = %s LIMIT 1", (team,))
        lrow = cur.fetchone()
    except Exception:
        return None, "no se pudo verificar el lider del equipo"
    if not lrow:
        return (team, "team %s sin fila pero es humano conocido" % team) \
            if team in FALLBACK_LEADERS else (None, "team %r sin fila en agents" % team)
    l_lead = bool(lrow["is_lead"] if isinstance(lrow, dict) else lrow[0])
    l_active = bool(lrow["active"] if isinstance(lrow, dict) else lrow[1])
    if l_lead and l_active:
        return team, "lider del equipo del subagente %s" % who
    return None, "team %r no es un lider activo" % team


def notify_assignment_bus(pg, actor, target, content, tid):
    """Avisa por el bus que `target` recibio la tarea `tid`. Best-effort.

    `pg` es la factory de conexion del caller (gateway: db._pg, sinapsis: _pg).
    `actor` es SIEMPRE el actor real de esa puerta: owner de la key en
    gateway/MCP, humano de la sesion en el BFF. Nunca se infiere aca.

    Devuelve el destinatario notificado (str) o None. NO lanza NUNCA.
    """
    act = (actor or "").strip().lower()
    try:
        with pg() as conn, conn.cursor() as cur:
            who, _motivo = resolve_notify_target(cur, target)
            if not who:
                return None
            # No avisarse a si mismo: un auto-handoff o una auto-asignacion no
            # es noticia para nadie. Esta guarda es la razon por la que mis 6
            # auto-entregas del 2026-08-10 no generaron mensaje, y era el
            # comportamiento correcto -- no un bug (tarea cd0821a0, punto C).
            if who == act:
                return None
            cur.execute(
                "INSERT INTO bus_messages (sender, recipients, performative, content, refs_task) "
                "VALUES (%s,%s,'request',%s,%s) RETURNING id",
                (actor, [who], content, tid))
            row = cur.fetchone()
            mid = row["id"] if isinstance(row, dict) else row[0]
            cur.execute(
                "INSERT INTO bus_deliveries (message_id, recipient, status) "
                "VALUES (%s,%s,'pending')", (mid, who))
            # Rastro en el hilo de la tarea: hace AUDITABLE que el aviso salio.
            # Sin esto, "no se enteró" y "no se le avisó" son indistinguibles.
            cur.execute(
                "INSERT INTO task_activity (task_id, agent, kind, body) "
                "VALUES (%s,%s,'comment',%s)",
                (tid, actor, "[bus] notificado a %s" % who))
            conn.commit()
        return who
    except Exception:
        # Silencio deliberado: el bus es un canal auxiliar. Que falle no puede
        # abortar un claim/handoff/submit ya aplicado.
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# DISPATCHER bus -> CORRESPONDENCIA (2026-09-15)
# ═══════════════════════════════════════════════════════════════════════════════
# El bus quedó en SÓLO-LECTURA hoy 01:29 (410 Gone). El aviso automático de
# asignación de tablero era la ÚNICA escritura viva que quedaba en `bus_messages`:
# medido, los tres mensajes posteriores al cierre son avisos (forge→merlin 01:58,
# rudeus-roxy→rudeus 01:51 y 01:27) y el último no-aviso es de las 00:15.
#
# `notify_assignment()` conserva NOMBRE y FIRMA a propósito: los SIETE call sites
# reales no se tocan (gateway/tasks_core.py ×3 vía `_notify_if_leader` L201/L527/L604,
# mcp/mcp_server.py ×3 L637/L1204/L1281, sinapsis/server.py ×1 L3768). Enumerados por
# AST, no por lista nominal.
#
# ⚠️ `claim` NO ESTÁ EN LA LISTA Y NO ES UN OLVIDO: enumeré los call sites por AST y
# `claim` no llama a este módulo en NINGUNA de las tres puertas. Nunca avisó. Esta
# migración no le agrega el aviso: eso sería comportamiento nuevo disfrazado de
# migración. Hay un test negativo que lo fija.
#
# El cuerpo viejo NO se borró: vive completo en `notify_assignment_bus()`, que es la
# rama de rollback. La reversión es una env var y un restart, sin rebuild.

NOTIFY_MODE_ENV = "CORRESPONDENCIA_NOTIFY"
NOTIFY_MODES = ("correspondencia", "bus", "both")
# El default es el destino de la migración, NO fail-closed como en `bus_legacy.py`.
# Son problemas distintos: allá lo peligroso era que una escritura siguiera abierta;
# acá lo peligroso es volver a escribir en una tabla declarada de sólo-lectura.
NOTIFY_DEFAULT = "correspondencia"


def notify_mode(env=None):
    """Modo de aviso vigente. Un valor no reconocido NO se ignora en silencio."""
    env = os.environ if env is None else env
    raw = (env.get(NOTIFY_MODE_ENV) or "").strip().lower()
    if raw in NOTIFY_MODES:
        return raw
    if raw:
        log.warning("%s=%r no reconocido (validos: %s); se usa %r",
                    NOTIFY_MODE_ENV, raw, "|".join(NOTIFY_MODES), NOTIFY_DEFAULT)
    return NOTIFY_DEFAULT


def notify_assignment(pg, actor, target, content, tid):
    """Avisa que `target` recibió la tarea `tid`. Best-effort, NO lanza NUNCA.

    Mismo contrato que antes (devuelve destinatario avisado o None), distinto
    destino: ahora el aviso es un mensaje de CORRESPONDENCIA, no un `bus_message`.
    Lo que devuelve en el modo nuevo es el BUZÓN CANÓNICO (`@equipo/agente`), no el
    nombre plano; los callers sólo lo usan como bool (`bus_notified`), verificado en
    los siete call sites.

    Modos (`CORRESPONDENCIA_NOTIFY`):
      · `correspondencia` (default) -> sólo Correspondencia. `bus_messages` no crece.
      · `bus`                       -> sólo el bus viejo. Rollback exacto.
      · `both`                      -> los dos. Doble escritura, para una ventana de
                                       convivencia si alguien la pide.
    """
    mode = notify_mode()
    if mode == "bus":
        return notify_assignment_bus(pg, actor, target, content, tid)

    result = None
    try:
        # Import PEREZOSO y a propósito: si alguien despliega este archivo sin los
        # módulos nuevos, la rama `bus` sigue funcionando en vez de que el import
        # tumbe el módulo entero (y con él las tres puertas del tablero).
        import correspondencia_notify
        result = correspondencia_notify.notify_assignment_correspondencia(
            pg, actor, target, content, tid)
    except Exception as exc:      # noqa: BLE001 - contrato: no propaga nunca
        # 🔴 NO se cae a la rama del bus. Un fallback silencioso al bus resucitaría
        # la escritura que esta migración vino a cerrar, y el operador vería un
        # `bus_messages` creciendo sin ningún cambio de configuración que lo
        # explique. Se registra y se devuelve None: el tablero completa igual.
        log.warning("el aviso a Correspondencia no pudo ejecutarse (tarea=%s): %s: %s",
                    tid, type(exc).__name__, exc)

    if mode == "both":
        legacy = notify_assignment_bus(pg, actor, target, content, tid)
        return result or legacy
    return result
