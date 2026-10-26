"""conversations.py - Conversacion entre pares REPLAYABLE (Cerebro v2, Gap C, 2bac4713).

El bus HOY (bus_messages/bus_deliveries) es notificacion inbox/ack. Este modulo agrega
la CAPA DE LECTURA replayable encima, SIN cambiar como se publica (bus_publish sigue igual):
reconstruye un hilo completo por conversation_id, ordenado cronologicamente, con todos los
participantes, performatives (request/inform), refs_task y estado de entrega por recipient.

Arquitectura:
  - thread(conversation_id): LEE bus_messages (+bus_deliveries) de una conversacion,
    ordenado por created_at (fallback ts). Es el "replay" del hilo.
  - list_threads(agent=None): hilos en los que participo un agente (sender o recipient),
    o todos si agent=None, con ultimo mensaje, count y participantes.
  - group(participants_key): replay de una conversacion IMPLICITA (mensajes SIN
    conversation_id agrupados por participantes). Ver abajo.
  - stats(): totales de hilos/mensajes/participantes.

CONVERSACIONES IMPLICITAS (2026-08-18, fase 3):
  La mayoria de los mensajes del bus se publica SIN conversation_id (se persiste como
  cadena VACIA, nunca NULL => "sin hilo" es `IS NULL OR = ''`, jamas solo `IS NULL`).
  Esos mensajes no pertenecen a ningun hilo declarado y serian invisibles. Se agrupan
  por CONJUNTO CANONICO DE PARTICIPANTES: sender union recipients, deduplicado, ordenado
  alfabeticamente y unido por '|' (ej. 'merlin|rias'). NO se agrupan por el campo crudo:
  eso fabricaria un unico "hilo fantasma" mezclando remitentes que nunca conversaron.
  Los grupos implicitos son ADITIVOS: solo aparecen con include_unthreaded=True, y cada
  item de la lista trae `kind` ('thread' | 'implicit') decidido por el BACKEND: el cliente
  nunca debe inferirlo de "¿tiene conversation_id?" (la cadena vacia lo enganaria).

FILTROS (2026-08-18, opcionales y combinables con AND):
  agent (participante), project (via refs_task -> tasks.project), since/until (fecha
  YYYY-MM-DD sobre ts) y q (coincidencia de TEXTO, no semantica: bus_messages no se
  indexa a Qdrant). Ausentes => cero cambio de comportamiento.

PROYECCION DEL CUERPO (2026-08-18, tarea 1cd25772):
  El default de esta capa es METADATA. El cuerpo de los mensajes (`content`) es lo caro
  y lo sensible: 735 mensajes = 1682 kB de content contra 35 kB de metadata (48x), y una
  llamada a group() para verificar un `count` devolvia la conversacion COMPLETA. La
  privacidad no puede depender de que quien tiene la key no mire: eso es una intencion,
  no un control.
  - thread() y group() aceptan `include_content` (default False). Sin el flag NO se
    selecciona `content` en el SQL: el cuerpo no sale de Postgres, asi que no puede
    filtrarse por un serializador distraido ni por un log. En su lugar viajan
    `content_len` (entero) y `content_preview` (PREVIEW_CHARS chars, espacios
    colapsados) que alcanzan para TRIAR sin leer.
  - list_threads() es una pregunta de FORMA ('que hilos hay'): su `last_message.content`
    se ACOTA siempre a PREVIEW_CHARS y NO tiene flag para traerlo completo.
    ⚠️ Se acota con left() y SIN colapsar espacios A PROPOSITO: el cliente MCP
    (bus_threads) renderiza `content[:60]`, y colapsar espacios cambiaria ese prefijo en
    2 de 107 hilos reales. Truncar preserva los primeros 60 chars byte a byte =>
    retrocompatible.

Reusa el pool Postgres existente (db.pg(), row_factory=dict_row), mismo patron que
semanticgraph.py. Additive: NO crea tablas, NO toca bus_publish. Solo LEE. Determinista.
"""
import db

# "Sin hilo" = conversation_id NULL **o cadena vacia**. bus_publish persiste '' por
# defecto (columna DEFAULT ''::text), asi que filtrar solo por IS NULL da 0 filas y
# parece que no falta nada. NO cambiar por `IS NULL` a secas.
UNTHREADED_WHERE = "(m.conversation_id IS NULL OR m.conversation_id = '')"
THREADED_WHERE = "m.conversation_id <> '' AND m.conversation_id IS NOT NULL"

# Largo del preview del cuerpo. Es una CONSTANTE DE CODIGO, no un parametro del usuario:
# por eso se puede interpolar en el SQL con int() sin abrir inyeccion (se fuerza el tipo
# en cada uso). No se expone por la API: un preview configurable por el llamador seria
# `include_content` con otro nombre.
# 120 y no 140: medido sobre la bandeja real de saruman (474.591 chars de cuerpo en 158
# mensajes), 140 daba 29,5 kB contra un techo de 30 kB — pasaba al filo. 120 da 26,5 kB,
# con margen. El invariante que se verifica NO es un absoluto sino un RATIO (reduccion
# >= 10x): un umbral fijo vence cuando el inbox crece, un ratio sobrevive.
PREVIEW_CHARS = 120


def _content_cols(include_content: bool, alias: str = "m") -> str:
    """Columnas de cuerpo para un SELECT de bus_messages, segun la proyeccion pedida.

    include_content=False (default) => `content` NO se selecciona: sale NULL y viajan
    solo `content_len` y `content_preview`. La diferencia importa: el cuerpo no llega
    NUNCA al proceso, asi que 'no filtrar' no depende de que la serializacion se acuerde
    de omitirlo. El preview colapsa espacios para que ocupe una sola linea al renderizar.

    include_content=True => cuerpo completo (lectura DELIBERADA, auditada en el endpoint).
    """
    cols = "length({a}.content) AS content_len".format(a=alias)
    if include_content:
        return cols + ", {a}.content AS content".format(a=alias)
    return cols + (", NULL::text AS content, "
                   "left(regexp_replace({a}.content, '\\s+', ' ', 'g'), {n}) "
                   "AS content_preview").format(a=alias, n=int(PREVIEW_CHARS))


def _order_col():
    """Orden cronologico: created_at primario, ts como fallback (ambos existen en el
    esquema; created_at es nullable, ts default now())."""
    return "COALESCE(m.created_at, m.ts)"


def _pk_col(alias="m"):
    """SQL de la clave canonica de participantes de UN mensaje: sender union recipients,
    DISTINCT, ordenado alfabeticamente, unido por '|'. Determinista: dos mensajes entre
    los mismos pares dan la misma clave sin importar quien envio."""
    return ("(SELECT string_agg(p, '|' ORDER BY p) FROM ("
            "SELECT DISTINCT unnest(array_append({a}.recipients, {a}.sender)) AS p) pk_q)"
            ).format(a=alias)


def _like_escape(s):
    """Escapa los wildcards de LIKE en el input del usuario ('%', '_' y el propio '\\'),
    para que buscar '100%' o 'a_b' sea coincidencia LITERAL y no un comodin. Se usa con
    `ILIKE %s ESCAPE '\\'`. El valor viaja SIEMPRE como parametro ligado (%s): nunca se
    interpola en el SQL."""
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_date(s):
    """'YYYY-MM-DD' -> la misma cadena si es una fecha valida, None si no. Defensa en
    profundidad: el endpoint ya valida el formato con un pattern (422), esto evita que
    una llamada interna mal formada llegue al SQL."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        from datetime import date
        y, m, d = s.split("-")
        date(int(y), int(m), int(d))
    except Exception:
        return None
    return s


def _msg_filters(agent=None, project=None, since=None, until=None, q=None, alias="m"):
    """Filtros OPCIONALES sobre mensajes del bus -> (sql, params) para pegar a un WHERE
    ya abierto (cada fragmento arranca con ' AND '). Todos combinan con AND; ninguno
    presente => ('', []) => comportamiento identico al historico.

      agent   : el agente participa (sender O recipient).
      project : via refs_task -> tasks.project. OJO: los mensajes SIN refs_task no tienen
                proyecto posible y quedan FUERA (por diseno, no por bug).
      since   : ts >= since (inclusivo, dia completo desde 00:00).
      until   : ts < until + 1 dia => INCLUYE el dia entero de `until`. Comparar
                `ts <= until` se comeria ese dia (la fecha desnuda es su 00:00).
      q       : coincidencia de TEXTO (ILIKE, wildcards escapados) sobre content,
                conversation_id y la clave de participantes. NO es busqueda semantica.

    El SQL usa SIEMPRE parametros ligados (%s): jamas f-strings con valores del usuario.
    """
    sql, params = "", []
    ag = (agent or "").strip()
    if ag:
        sql += " AND ({a}.sender = %s OR %s = ANY({a}.recipients))".format(a=alias)
        params.extend([ag, ag])
    pr = (project or "").strip()
    if pr:
        sql += (" AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = {a}.refs_task "
                "AND t.project = %s)").format(a=alias)
        params.append(pr)
    d_since = _as_date(since)
    if d_since:
        sql += " AND {a}.ts >= %s::date".format(a=alias)
        params.append(d_since)
    d_until = _as_date(until)
    if d_until:
        sql += " AND {a}.ts < (%s::date + interval '1 day')".format(a=alias)
        params.append(d_until)
    txt = (q or "").strip()
    if txt:
        pat = "%" + _like_escape(txt) + "%"
        sql += (" AND ({a}.content ILIKE %s ESCAPE '\\' "
                "OR COALESCE({a}.conversation_id, '') ILIKE %s ESCAPE '\\' "
                "OR COALESCE(" + _pk_col(alias) + ", '') ILIKE %s ESCAPE '\\')"
                ).format(a=alias)
        params.extend([pat, pat, pat])
    return sql, params


def _msg_row(r, deliveries=None, include_content=False):
    """Serializa una fila de bus_messages a dict JSON-safe.

    `content` viaja SOLO con include_content=True (y en ese caso no hay preview: el cuerpo
    completo ya esta). Sin el flag el campo va en None y se acompanan `content_len` +
    `content_preview`, para que el consumidor pueda distinguir 'no lo pedi' de 'esta vacio'
    sin tener que inferirlo.
    """
    ts = r.get("order_ts")
    out = {
        "id": str(r["id"]),
        "sender": r["sender"],
        "recipients": list(r["recipients"]) if r["recipients"] is not None else [],
        "performative": r["performative"],
        "content": r["content"] if include_content else None,
        "content_len": int(r["content_len"] or 0),
        "refs_task": str(r["refs_task"]) if r["refs_task"] else None,
        "timestamp": ts.isoformat() if ts else None,
        "deliveries": deliveries if deliveries is not None else None,
    }
    if not include_content:
        out["content_preview"] = r.get("content_preview") or ""
        out["content_truncated"] = int(r["content_len"] or 0) > PREVIEW_CHARS
    return out


def thread(conversation_id: str, include_content: bool = False) -> dict:
    """Replay de un hilo: todos los mensajes de conversation_id ordenados cronologicamente,
    con sender/recipients/performative/refs_task/timestamp y estado de entrega
    (delivery status/ack por recipient). Determinista, solo lectura.

    El CUERPO es explicito: include_content=False (default) devuelve metadata +
    `content_len` + `content_preview` (el cuerpo no se selecciona en el SQL);
    include_content=True devuelve los cuerpos completos y el endpoint lo audita como
    lectura deliberada. La respuesta declara la proyeccion usada en `content_included`,
    para que el cliente no tenga que adivinar por que `content` es None."""
    cid = (conversation_id or "").strip()
    if not cid:
        return {"conversation_id": conversation_id, "resolved": None,
                "content_included": bool(include_content),
                "error": "conversation_id vacio", "messages": [], "count": 0}

    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.sender, m.recipients, m.performative, "
            + _content_cols(include_content) + ", "
            "       m.refs_task, " + _order_col() + " AS order_ts "
            "FROM bus_messages m WHERE m.conversation_id = %s "
            "ORDER BY " + _order_col() + " ASC, m.id ASC",
            (cid,))
        rows = cur.fetchall()
        if not rows:
            return {"conversation_id": cid, "resolved": None,
                    "content_included": bool(include_content),
                    "error": "no encontrada: sin mensajes para conversation_id %r" % cid,
                    "messages": [], "count": 0}

        msg_ids = [r["id"] for r in rows]
        # Estado de entrega por mensaje (recipient -> status/delivered/ack).
        cur.execute(
            "SELECT message_id, recipient, status, delivered_at, ack_at "
            "FROM bus_deliveries WHERE message_id = ANY(%s)",
            (msg_ids,))
        deliv_by_msg = {}
        for d in cur.fetchall():
            deliv_by_msg.setdefault(str(d["message_id"]), []).append({
                "recipient": d["recipient"],
                "status": d["status"],
                "delivered_at": d["delivered_at"].isoformat() if d["delivered_at"] else None,
                "ack_at": d["ack_at"].isoformat() if d["ack_at"] else None,
            })

    messages = []
    participants = set()
    performatives = {}
    for r in rows:
        deliveries = deliv_by_msg.get(str(r["id"]), [])
        messages.append(_msg_row(r, deliveries, include_content=include_content))
        participants.add(r["sender"])
        for rec in (r["recipients"] or []):
            participants.add(rec)
        performatives[r["performative"]] = performatives.get(r["performative"], 0) + 1

    first_ts = messages[0]["timestamp"]
    last_ts = messages[-1]["timestamp"]
    return {
        "conversation_id": cid,
        "resolved": cid,
        "count": len(messages),
        # Proyeccion EXPLICITA: distingue 'no pedi el cuerpo' de 'los mensajes estan vacios'.
        "content_included": bool(include_content),
        "participants": sorted(participants),
        "performatives": performatives,
        "started_at": first_ts,
        "last_at": last_ts,
        "messages": messages,
    }


def list_threads(agent: str = None, limit: int = 50, include_unthreaded: bool = False,
                 project: str = None, since: str = None, until: str = None,
                 q: str = None) -> dict:
    """Hilos del bus + (opcional) conversaciones IMPLICITAS, ordenados por ultimo mensaje
    descendente.

    Por defecto (include_unthreaded=False) devuelve EXACTAMENTE lo de siempre: solo hilos
    declarados (conversation_id no vacio) en los que participa `agent` (sender o recipient)
    o TODOS si agent=None, con count, participantes y ultimo mensaje. Cambio ADITIVO: la
    forma de la respuesta en ese modo no cambia (sin `kind`, sin `counts`) para no romper a
    los agentes que ya la consumen por MCP.

    Con include_unthreaded=True se suman los grupos implicitos (mensajes SIN conversation_id
    agrupados por conjunto canonico de participantes) y CADA item trae `kind`:
      - kind='thread'   -> identificador en `conversation_id`
      - kind='implicit' -> identificador en `participants_key` (ej. 'merlin|rias')
    El `kind` lo decide el BACKEND: el cliente NO debe inferirlo de "¿tiene conversation_id?",
    porque el campo se persiste como cadena vacia y lo enganaria.

    `limit` acota CADA tipo por separado (hasta `limit` hilos y hasta `limit` implicitos),
    no la lista combinada: asi pedir los implicitos no desplaza a los hilos declarados.

    Filtros opcionales, combinables con AND (ausentes => cero cambio de comportamiento):
      project      : proyecto de la tarea referenciada (refs_task -> tasks.project). Los
                     mensajes SIN refs_task NO tienen proyecto posible y quedan FUERA:
                     filtrar por proyecto ve solo los mensajes vinculados a una tarea.
      since/until  : fecha 'YYYY-MM-DD' sobre ts. `since` inclusivo; `until` incluye el DIA
                     COMPLETO (ts < until + 1 dia).
      q            : coincidencia de TEXTO en content / conversation_id / participantes
                     (ILIKE, wildcards del usuario escapados). NO es busqueda semantica:
                     bus_messages no se indexa a Qdrant. OJO: `q` sigue buscando sobre el
                     content COMPLETO en SQL aunque la respuesta solo devuelva el preview
                     — se filtra por lo que el mensaje dice, no por lo que se muestra.

    CUERPO (2026-08-18): esta es una pregunta de FORMA ('que hilos hay'), asi que
    `last_message.content` viene SIEMPRE acotado a PREVIEW_CHARS y NO hay flag para
    traerlo completo: el replay es el trabajo de thread()/group(). Se agrega
    `last_message.content_len` (largo real) para que un preview cortado no se confunda con
    un mensaje corto. Medido: los 107 ultimos-mensajes pesaban 304 kB, acotados 15 kB.
    ⚠️ El corte usa left() SIN colapsar espacios a proposito (ver _content_cols): el
    cliente MCP renderiza `content[:60]` y colapsarlos le cambiaria la salida.
    """
    limit = int(limit) if limit else 50
    ag = (agent or "").strip() or None
    fsql, fparams = _msg_filters(ag, project, since, until, q)

    with db.pg() as conn, conn.cursor() as cur:
        # 1) conversation_ids relevantes (con los filtros que apliquen), ordenados por
        #    el ultimo mensaje del hilo (mas reciente primero).
        where = THREADED_WHERE + fsql
        params = list(fparams)
        cur.execute(
            "SELECT m.conversation_id AS cid, "
            "       count(*) AS n, "
            "       max(" + _order_col() + ") AS last_ts, "
            "       min(" + _order_col() + ") AS first_ts "
            "FROM bus_messages m WHERE " + where + " "
            "GROUP BY m.conversation_id "
            "ORDER BY last_ts DESC NULLS LAST LIMIT %s",
            (*params, limit))
        head = cur.fetchall()
        if not head and not include_unthreaded:
            return {"agent": ag, "count": 0, "threads": []}

        parts_by_cid, last_by_cid = {}, {}
        if head:
            cids = [h["cid"] for h in head]
            # 2) participantes por hilo (sobre TODOS los mensajes del hilo, no solo del agente).
            cur.execute(
                "SELECT conversation_id AS cid, sender, recipients "
                "FROM bus_messages WHERE conversation_id = ANY(%s)",
                (cids,))
            for r in cur.fetchall():
                s = parts_by_cid.setdefault(r["cid"], set())
                s.add(r["sender"])
                for rec in (r["recipients"] or []):
                    s.add(rec)

            # 3) ultimo mensaje por hilo (DISTINCT ON sobre orden cronologico descendente).
            #    El cuerpo va ACOTADO (left, sin colapsar espacios: preserva el prefijo
            #    que renderiza el cliente MCP) + su largo real.
            cur.execute(
                "SELECT DISTINCT ON (m.conversation_id) m.conversation_id AS cid, "
                "       m.sender, m.performative, "
                "       left(m.content, " + str(int(PREVIEW_CHARS)) + ") AS content, "
                "       length(m.content) AS content_len, m.refs_task, "
                "       " + _order_col() + " AS order_ts "
                "FROM bus_messages m WHERE m.conversation_id = ANY(%s) "
                "ORDER BY m.conversation_id, " + _order_col() + " DESC, m.id DESC",
                (cids,))
            for r in cur.fetchall():
                last_by_cid[r["cid"]] = {
                    "sender": r["sender"],
                    "performative": r["performative"],
                    "content": r["content"],
                    "content_len": int(r["content_len"] or 0),
                    "content_truncated": int(r["content_len"] or 0) > PREVIEW_CHARS,
                    "refs_task": str(r["refs_task"]) if r["refs_task"] else None,
                    "timestamp": r["order_ts"].isoformat() if r["order_ts"] else None,
                }

        implicit_rows = []
        if include_unthreaded:
            # 4) grupos implicitos: los mensajes SIN hilo, agrupados por la clave canonica
            #    de participantes. El ultimo mensaje del grupo sale de la misma pasada con
            #    funciones de ventana (no hace falta un segundo DISTINCT ON).
            #    El cuerpo se ACOTA ya en la CTE: asi los 211 kB de cuerpos completos no
            #    se materializan ni dentro de la query. El filtro `q` (fsql) sigue
            #    evaluandose contra `m.content` COMPLETO, que esta en el WHERE de la tabla
            #    base: se busca sobre todo el texto y se devuelve solo el preview.
            cur.execute(
                "WITH sin_hilo AS ("
                "  SELECT m.id, m.sender, m.recipients, m.performative, "
                "         left(m.content, " + str(int(PREVIEW_CHARS)) + ") AS content, "
                "         length(m.content) AS content_len, "
                "         m.refs_task, " + _order_col() + " AS order_ts, "
                "         " + _pk_col("m") + " AS pk "
                "  FROM bus_messages m WHERE " + UNTHREADED_WHERE + fsql +
                "), agg AS ("
                "  SELECT pk, count(*) AS n, max(order_ts) AS last_ts, min(order_ts) AS first_ts "
                "  FROM sin_hilo GROUP BY pk"
                "), last_msg AS ("
                "  SELECT DISTINCT ON (pk) pk, sender, performative, content, content_len, "
                "         refs_task, order_ts "
                "  FROM sin_hilo ORDER BY pk, order_ts DESC, id DESC"
                ") "
                "SELECT a.pk, a.n, a.last_ts, a.first_ts, l.sender, l.performative, "
                "       l.content, l.content_len, l.refs_task, l.order_ts "
                "FROM agg a LEFT JOIN last_msg l ON l.pk = a.pk "
                "ORDER BY a.last_ts DESC NULLS LAST LIMIT %s",
                (*fparams, limit))
            implicit_rows = cur.fetchall()

    threads = []
    for h in head:
        cid = h["cid"]
        item = {
            "conversation_id": cid,
            "messages": int(h["n"]),
            "participants": sorted(parts_by_cid.get(cid, set())),
            "started_at": h["first_ts"].isoformat() if h["first_ts"] else None,
            "last_at": h["last_ts"].isoformat() if h["last_ts"] else None,
            "last_message": last_by_cid.get(cid),
        }
        if include_unthreaded:
            item["kind"] = "thread"
            item["participants_key"] = None
        threads.append(item)

    if not include_unthreaded:
        return {"agent": ag, "count": len(threads), "threads": threads}

    n_threads = len(threads)
    for r in implicit_rows:
        pk = r["pk"] or ""
        threads.append({
            "kind": "implicit",
            # Sin conversation_id declarado: el identificador del grupo es participants_key.
            "conversation_id": None,
            "participants_key": pk,
            "messages": int(r["n"]),
            "participants": [p for p in pk.split("|") if p],
            "started_at": r["first_ts"].isoformat() if r["first_ts"] else None,
            "last_at": r["last_ts"].isoformat() if r["last_ts"] else None,
            "last_message": ({
                "sender": r["sender"],
                "performative": r["performative"],
                "content": r["content"],
                "content_len": int(r["content_len"] or 0),
                "content_truncated": int(r["content_len"] or 0) > PREVIEW_CHARS,
                "refs_task": str(r["refs_task"]) if r["refs_task"] else None,
                "timestamp": r["order_ts"].isoformat() if r["order_ts"] else None,
            } if r["sender"] is not None else None),
        })

    # Intercalados por recencia: hilos declarados e implicitos compiten en la misma lista.
    threads.sort(key=lambda t: (t["last_at"] or ""), reverse=True)
    return {"agent": ag, "count": len(threads), "threads": threads,
            "counts": {"threads": n_threads, "implicit": len(threads) - n_threads},
            "filters": {"agent": ag, "project": (project or "").strip() or None,
                        "since": _as_date(since), "until": _as_date(until),
                        "q": (q or "").strip() or None}}


def group(participants: str, limit: int = 200, project: str = None, since: str = None,
          until: str = None, q: str = None, include_content: bool = False) -> dict:
    """Replay de una conversacion IMPLICITA: los mensajes SIN conversation_id cuyo conjunto
    canonico de participantes es `participants` (ej. 'merlin|rias'), en orden cronologico
    ASCENDENTE, con los mismos campos que thread() (sender, recipients, performative,
    refs_task, timestamp; el cuerpo segun `include_content`).

    Filtra SOLO mensajes sin hilo (conversation_id NULL o ''): un mensaje que ya pertenece a
    un hilo declarado NO aparece aca, para que no se cuente/lea dos veces.

    CUERPO EXPLICITO (2026-08-18): include_content=False (default) => metadata +
    `content_len` + `content_preview`, sin seleccionar el cuerpo en el SQL. Este es el
    endpoint donde ocurrio la fuga que origino el cambio: se llamo para verificar un
    `count == 12` y devolvio los 12 mensajes completos de una conversacion privada. El
    predicado era el count; el contenido no era necesario para probarlo.

    Si no hay mensajes para esa clave, devuelve la MISMA forma que thread() para un hilo
    inexistente (campo `error` poblado, messages=[], count=0). Acepta los mismos filtros
    opcionales que list_threads. Solo lectura, determinista."""
    key = (participants or "").strip()
    if not key:
        return {"participants_key": participants, "kind": "implicit", "resolved": None,
                "content_included": bool(include_content),
                "error": "participants vacio", "messages": [], "count": 0,
                "participants": []}
    limit = int(limit) if limit else 200
    fsql, fparams = _msg_filters(None, project, since, until, q)

    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.id, m.sender, m.recipients, m.performative, "
            + _content_cols(include_content) + ", m.refs_task, "
            "       " + _order_col() + " AS order_ts "
            "FROM bus_messages m "
            "WHERE " + UNTHREADED_WHERE + " AND " + _pk_col("m") + " = %s" + fsql + " "
            "ORDER BY " + _order_col() + " ASC, m.id ASC LIMIT %s",
            (key, *fparams, limit))
        rows = cur.fetchall()
        if not rows:
            return {"participants_key": key, "kind": "implicit", "resolved": None,
                    "content_included": bool(include_content),
                    "error": "no encontrada: sin mensajes sin hilo para participantes %r" % key,
                    "messages": [], "count": 0, "participants": []}

        msg_ids = [r["id"] for r in rows]
        cur.execute(
            "SELECT message_id, recipient, status, delivered_at, ack_at "
            "FROM bus_deliveries WHERE message_id = ANY(%s)",
            (msg_ids,))
        deliv_by_msg = {}
        for d in cur.fetchall():
            deliv_by_msg.setdefault(str(d["message_id"]), []).append({
                "recipient": d["recipient"],
                "status": d["status"],
                "delivered_at": d["delivered_at"].isoformat() if d["delivered_at"] else None,
                "ack_at": d["ack_at"].isoformat() if d["ack_at"] else None,
            })

    messages, participants, performatives = [], set(), {}
    for r in rows:
        messages.append(_msg_row(r, deliv_by_msg.get(str(r["id"]), []),
                                 include_content=include_content))
        participants.add(r["sender"])
        for rec in (r["recipients"] or []):
            participants.add(rec)
        performatives[r["performative"]] = performatives.get(r["performative"], 0) + 1

    return {
        "participants_key": key,
        "kind": "implicit",
        "resolved": key,
        "count": len(messages),
        "content_included": bool(include_content),
        "participants": sorted(participants),
        "performatives": performatives,
        "started_at": messages[0]["timestamp"],
        "last_at": messages[-1]["timestamp"],
        "messages": messages,
    }


def stats() -> dict:
    """Totales de la capa replayable: hilos distintos (conversation_id no vacio), mensajes
    totales del bus, mensajes con conversation_id, participantes distintos."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total_messages, "
            "       count(*) FILTER (WHERE conversation_id <> '' AND conversation_id IS NOT NULL) AS threaded_messages, "
            "       count(DISTINCT conversation_id) FILTER (WHERE conversation_id <> '' AND conversation_id IS NOT NULL) AS total_threads "
            "FROM bus_messages")
        r = cur.fetchone()
        cur.execute("SELECT count(DISTINCT sender) AS senders FROM bus_messages")
        s = cur.fetchone()
    return {
        "total_threads": int(r["total_threads"] or 0),
        "total_messages": int(r["total_messages"] or 0),
        "threaded_messages": int(r["threaded_messages"] or 0),
        "distinct_senders": int(s["senders"] or 0),
    }
