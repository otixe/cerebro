import os
import datetime as _dt
import json

# FUENTE UNICA de la notificacion tablero->bus (common/, horneado por el Dockerfile)
import bus_notify
# FUENTE UNICA del cierre de escritura del bus viejo (common/, mismo mecanismo).
# El mensaje y el codigo de estado que emite este tool son LOS MISMOS OBJETOS que
# usa el REST: no son una copia que haya que sincronizar a mano.
import bus_legacy
import hashlib
import sys
import time
import httpx
import psycopg
from urllib.parse import quote
from psycopg.rows import dict_row
from pydantic import ConfigDict, ValidationError, create_model
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
from mcp.server.fastmcp.exceptions import ToolError

# FUENTE UNICA de la politica de tareas, compartida con el gateway REST
# (tarea c6b61bd1, 2026-08-05). Llega a esta imagen via `additional_contexts`
# de Compose: `COPY --from=common tasks_rbac.py ./` en mcp/Dockerfile. Sin esto
# la matriz RBAC, el resolvedor de prefijo y el gate de auto-firma volverian a
# ser copias que hay que sincronizar a mano.
import tasks_rbac
import approval_commands as approval
import agent_config

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
# DEPRECADA (Otixe 2026-08-05): NO usar. Existia para que un cliente sin key
# pudiera LEER, pero valia SARUMAN_API_KEY => toda lectura anonima se auditaba
# como saruman (atribucion falsa). Se conserva la lectura del env para no
# romper el arranque si el compose la sigue inyectando, pero NINGUN camino la
# consume. No reintroducir en _read_key ni en _require_agent.
_DEPRECATED_DEFAULT_KEY = os.environ.get("MCP_DEFAULT_KEY", "")

# FIX 1 (2026-08-04, tarea 5-fixes): el timeout del cliente HTTP al gateway estaba
# HARDCODEADO en 30s. Rias midio p95 degradado 12x con maximos de 139s: toda llamada
# lenta (search hibrido sobre fuentes de cientos de chunks, add-markdown con
# re-embedding) moria por timeout en el CLIENTE aunque el gateway terminara bien.
# Ahora es configurable por entorno, default 120s.
# NO se silencia la senal: cada llamada que supere CEREBRO_SLOW_MS se loguea a stderr
# con su duracion, para que la lentitud siga siendo visible y medible.
HTTP_TIMEOUT = float(os.environ.get("CEREBRO_HTTP_TIMEOUT", "120"))
# El eval de retrieval embeddea N queries y busca contra el corpus: es la operacion
# mas lenta expuesta por MCP. Medido 2026-08-16: 222s con 32 queries (24 pos + 8 neg).
# Con el HTTP_TIMEOUT generico de 120s se cortaba SIEMPRE. 600s da margen para que el
# golden set crezca sin volver a romperlo en silencio.
EVAL_TIMEOUT = float(os.environ.get("CEREBRO_EVAL_TIMEOUT", "600"))
SLOW_MS = float(os.environ.get("CEREBRO_SLOW_MS", "10000"))

# Largo del preview del cuerpo en las vistas del bus (bus_inbox y el render de
# bus_thread/bus_group sin include_content). Espeja gateway/conversations.PREVIEW_CHARS,
# que es la que aplica el SQL del gateway; aca se usa para el SQL propio de bus_inbox
# (que va DIRECTO a Postgres, no por el gateway) y para el corte del render.
# Es una constante de codigo, no un parametro del usuario.
_BUS_PREVIEW_CHARS = 120

# Idem para CORRESPONDENCIA (F11). Es el MISMO numero a proposito: el contrato
# "metadata + preview, cuerpo por pedido explicito" debe ser uno solo en las tres
# ventanas (bus_inbox, bus_thread/bus_group, correspondencia_bandeja). Espeja
# gateway/correspondencia.PREVIEW_CHARS, que es la que aplica el SQL.
_CORR_PREVIEW_CHARS = 120

PG = dict(
    host=os.environ.get("POSTGRES_HOST", "postgres"),
    port=int(os.environ.get("POSTGRES_PORT_INTERNAL", "5432")),
    user=os.environ.get("POSTGRES_USER"),
    password=os.environ.get("POSTGRES_PASSWORD"),
    dbname=os.environ.get("POSTGRES_DB"),
)

# Mapa key -> nombre de agente (para atribuir creator/owner).
# FIX (2026-08-03, tareas 7b829dfa + a61071dc): se retiraron PAPERCLIP, GEMINI, LURTZ,
# GRIMA, PALANTIR y CREBAIN. Los 4 ultimos estan REVOCADOS y los 2 primeros nunca
# tuvieron fila en `agents`. Mientras estuvieran en esta lista, reponer la variable en
# el .env los revivia POR MCP, incluso con el gateway ya corregido: el agujero de
# revocacion tenia DOS puertas y solo se habia cerrado una.
_AGENT_KEYS = {}
# Cerebro publico: misma lista configurable que el gateway (BOOTSTRAP_AGENTS, default 'admin').
for _a in [a.strip().upper() for a in os.environ.get("BOOTSTRAP_AGENTS", "admin").split(",") if a.strip()]:
    _v = os.environ.get(f"{_a}_API_KEY", "").strip()
    if _v:
        _AGENT_KEYS[_v] = _a.lower()

mcp = FastMCP("cerebro", host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8766")))

# FastMCP 1.28 genera el modelo de argumentos desde la firma y por defecto
# ignora extras. Se reemplaza SOLO el modelo de esta tool tras registrarla (abajo),
# evitando alterar las restantes tools del servidor.

# Sprint 5 (2026-07-17): lideres que reciben notificacion automatica en el bus
# cuando se les asigna una tarea (task_create / task_handoff).
LEADERS = agent_config.leaders()


class AuthError(Exception):
    """Escritura sin key valida -> 401 (enforcement anti-spoofing)."""
    pass


def _client_key(ctx):
    """Key del cliente (header X-Merlin-Key). SIN fallback."""
    try:
        h = ctx.request_context.request.headers
        return h.get("x-merlin-key") or h.get("X-Merlin-Key")
    except Exception:
        return None


def _read_key(ctx):
    """Key para LECTURA: SIEMPRE la del cliente. NUNCA un default.

    DECISION DE OTIXE (2026-08-05): "nadie deberia poder escribir o ver nada sin
    key propia".
    ANTES: `_client_key(ctx) or DEFAULT_KEY`, y DEFAULT_KEY = SARUMAN_API_KEY (lo
    inyecta docker-compose). Consecuencia: un cliente MCP sin key leia el Cerebro
    **bajo la identidad de saruman**, y el audit_log lo registraba como suyo =>
    ATRIBUCION FALSA. Rompia la trazabilidad: ante una lectura anomala no habia
    forma de distinguir al lider de un cliente sin identidad.
    AHORA devuelve None si no hay header, y el llamador falla con 401. Fallar es
    mejor que atribuir mal: el error se ve, la atribucion falsa no.
    (La ESCRITURA ya no dependia del default desde el fix de 7b829dfa, 2026-08-03.)"""
    return _client_key(ctx)


def _require_agent(ctx):
    """Para ESCRITURA: exige key valida. Devuelve (agent_owner, key).
    Sin key o invalida -> AuthError (401). NO usa DEFAULT_KEY: el owner se
    deriva SIEMPRE de la key autenticada (anti-spoofing).
    Valida contra .env O contra una fila ACTIVE de `agents` (keys provisionadas)."""
    k = _client_key(ctx)
    if not k:
        raise AuthError("401 unauthorized: la escritura requiere un header X-Merlin-Key valido")
    # FIX (2026-08-03, tarea 7b829dfa): la tabla `agents` es la FUENTE DE VERDAD, igual
    # que en el gateway. Antes se probaba el .env PRIMERO, asi que una key revocada
    # seguia autenticando por MCP aunque el gateway ya la rechazara.
    name = _agent_by_db_hash(k)
    if not name:
        # Bootstrap: solo vale el .env si el agente NO tiene fila en `agents`.
        env_name = _AGENT_KEYS.get(k)
        if env_name and not _agent_exists_db(env_name):
            name = env_name
    if not name:
        raise AuthError("401 unauthorized: la escritura requiere un header X-Merlin-Key valido")
    return name, k


# ---------------------------------------------------------------------------
# RBAC de tareas por execution_class - FIX de a61071dc (merlin, 2026-08-03).
# La matriz vivia SOLO en el gateway REST (app.py). Las tools task_* del MCP hablan
# DIRECTO a Postgres, asi que el RBAC del tablero era DECORATIVO por MCP: un agente
# read-only podia hacer task_review y cerrar su propio gate de QA, rompiendo el
# invariante de Harness 4.1 (nadie ejecuta + verifica + audita lo mismo).
# Medido: 27 tools del MCP van directo a Postgres, 15 de ellas escriben el tablero.
#
# DEDUPLICADO (tarea c6b61bd1, 2026-08-05): ya NO hay copia. La matriz y los
# conjuntos se IMPORTAN de `common/tasks_rbac.py`, la misma fuente que usa el
# gateway. El modulo llega a esta imagen via `additional_contexts` de Compose
# (gateway/ y mcp/ son build contexts distintos, asi que un COPY ../common no es
# posible: esa restriccion era la causa real de la duplicacion).
# Lo de abajo son ALIAS de reexport, NO definiciones: si cambias la politica aca
# los dos transportes vuelven a divergir. Editala en common/tasks_rbac.py.
# ---------------------------------------------------------------------------
_EXEC_CLASSES = tasks_rbac.EXECUTION_CLASSES
_CLS_ALL = tasks_rbac.CLS_ALL
_CLS_WRITER_PLUS = tasks_rbac.CLS_WRITER_PLUS
_CLS_REVIEW = tasks_rbac.CLS_REVIEW               # INVARIANTE: el ejecutor no se auto-aprueba
_CLS_ORCH = tasks_rbac.CLS_ORCH

_TASK_RBAC = tasks_rbac.TASK_RBAC


def _rbac_enforcing():
    return os.environ.get("RBAC_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def _agent_exists_db(name):
    """True si EXISTE fila para name, con independencia de active.
    Ante error de DB devuelve True => la via .env queda CERRADA (fail-closed)."""
    if not name:
        return True
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM agents WHERE lower(name)=lower(%s) LIMIT 1", (name,))
            return cur.fetchone() is not None
    except Exception:
        return True


def _execution_class(name):
    """Clase efectiva. Lider -> orchestrator. NULL/desconocida/sin fila -> read-only
    (FAIL-CLOSED, mismo criterio que resolve_execution_class del gateway)."""
    if not name:
        return "read-only"
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT is_lead, execution_class FROM agents "
                        "WHERE lower(name)=lower(%s) AND active=true LIMIT 1", (name,))
            row = cur.fetchone()
    except Exception:
        return "read-only"
    if not row:
        return "read-only"
    if row.get("is_lead"):
        return "orchestrator"
    cls = (row.get("execution_class") or "").strip().lower()
    return tasks_rbac.normalize_execution_class(cls)


def _rbac(agent, op):
    """Gate RBAC. Devuelve None si autoriza, o el MENSAJE de error si deniega.
    Se llama ANTES de tocar Postgres, igual que en el gateway.

    La POLITICA (que clase puede que operacion) vive en tasks_rbac, compartida con
    el gateway. Aca queda solo la traduccion a la firma de este transporte: string
    de error en vez de HTTPException."""
    if op not in tasks_rbac.TASK_RBAC:
        return None
    cls = _execution_class(agent)
    if tasks_rbac.is_allowed(cls, op):
        return None
    if not _rbac_enforcing():
        return None      # warn-only: mismo comportamiento que el gateway
    return ("403 forbidden: execution_class %r no autorizada para %r, permitidas: %s"
            % (cls, op, tasks_rbac.allowed_txt(op)))


def _agent_by_db_hash(key):
    """Fallback: resuelve un token provisionado a su name via sha256 en `agents`."""
    try:
        h = hashlib.sha256(key.encode()).hexdigest()
        with _pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM agents WHERE api_key_hash=%s AND active=true LIMIT 1", (h,))
            row = cur.fetchone()
        return row["name"] if row else None
    except Exception:
        return None


def _err_detail(e):
    """Extrae detail de un httpx.HTTPStatusError para mensajes de tool."""
    try:
        return e.response.json().get("detail", "")
    except Exception:
        return (e.response.text or "")[:200]


def _pg():
    return psycopg.connect(row_factory=dict_row, **PG)


def _audit(agent, action, resource):
    """Registra una escritura directa (tareas) en audit_log. Nunca rompe la op."""
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (agent, action, query, success) VALUES (%s,%s,%s,%s)",
                (agent, action, (resource or "")[:500], True))
            conn.commit()
    except Exception:
        pass


def _bus_notify_leader(cur, sender, recipient, content, refs_task):
    """Sprint 5: emite un bus_message performative='request' + bus_delivery pending
    para notificar a un lider que se le asigno/derivo una tarea. Corre dentro del
    cursor/transaccion de la operacion de tarea. El caller lo envuelve en try/except
    para que un fallo del bus NUNCA rompa el flujo de tareas (additive)."""
    cur.execute(
        "INSERT INTO bus_messages (sender, recipients, performative, content, refs_task) "
        "VALUES (%s,%s,'request',%s,%s) RETURNING id",
        (sender, [recipient], content, refs_task))
    mid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO bus_deliveries (message_id, recipient, status) VALUES (%s,%s,'pending')",
        (mid, recipient))
    return mid


def _log_slow(method, path, ms, err=None):
    """FIX 1: emite a stderr las llamadas lentas al gateway (>= SLOW_MS) y los
    timeouts/errores de transporte. stderr y NO stdout: en transporte stdio, stdout
    es el canal del protocolo MCP y escribir ahi lo corrompe."""
    try:
        tag = "HTTP-TIMEOUT" if err else "HTTP-SLOW"
        extra = f" err={type(err).__name__}" if err else ""
        print(f"[cerebro-mcp] {tag} {method} {path} {ms:.0f}ms "
              f"(timeout={HTTP_TIMEOUT:.0f}s){extra}", file=sys.stderr, flush=True)
    except Exception:
        pass


async def _call(method, path, key, payload=None, timeout: float | None = None):
    """FIX 1: wrapper con instrumentacion de duracion. La llamada real vive en
    _call_raw (timeout configurable)."""
    t0 = time.monotonic()
    try:
        out = await _call_raw(method, path, key, payload, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        _log_slow(method, path, (time.monotonic() - t0) * 1000.0, err=e)
        raise
    ms = (time.monotonic() - t0) * 1000.0
    if ms >= SLOW_MS:
        _log_slow(method, path, ms)
    return out


async def _call_raw(method, path, key, payload=None, timeout: float | None = None):
    # Sin key propia no se llama al gateway (Otixe 2026-08-05: "nadie deberia poder
    # escribir o ver nada sin key propia"). Se falla ACA con un mensaje explicito en
    # vez de mandar un header None: httpx lo rechazaria con un TypeError opaco y el
    # agente veria un error de transporte en lugar de "te falta la key".
    if not key:
        raise AuthError(
            "401 unauthorized: falta el header de autenticacion del Cerebro. "
            "Cada agente debe usar su PROPIA key: no hay identidad por default "
            "(antes una lectura sin key se auditaba como 'saruman').")
    async with httpx.AsyncClient(timeout=timeout or HTTP_TIMEOUT) as c:
        r = await c.request(method, f"{GATEWAY}{path}",
                            headers={"X-Merlin-Key": key}, json=payload)
        r.raise_for_status()
        return r.json()


def _fmt_score(r: dict) -> str:
    """FIX E1 (tarea 1dd87ff9): mostrar el numero INTERPRETABLE.
    Antes se imprimia `score`, que en modo hibrido es RRF con techo 0.0328 — leido
    contra la tabla de coseno (>0,7 muy relevante ... <0,3 irrelevante) hacia que
    TODO acierto se leyera como "probablemente irrelevante".
    `score_cosine=None` => el hit vino SOLO por BM25 (match lexico): se declara, no
    se imprime un 0 que se leeria como "malisimo".
    """
    cos = r.get("score_cosine")
    rrf = r.get("score_rrf")
    rank = r.get("rank")
    partes = []
    if cos is not None:
        partes.append(f"cos {cos:.3f}")
    else:
        partes.append("solo-BM25")
    if rrf is not None:
        partes.append(f"rrf {rrf:.4f}")
    elif r.get("score") is not None:
        partes.append(f"score {r.get('score'):.4f}")
    if rank:
        partes.append(f"#{rank}")
    return " · ".join(partes)


# ---------------- MEMORIA ----------------
@mcp.tool()
async def memory_search(query: str, top_k: int = 5, owner: str = "", category: str = "", include_superseded: bool = False, hybrid: bool = None, include_archived: bool = False, min_score: float = None, ctx: Context = None) -> str:
    """Busca semanticamente en el cerebro/memoria compartida de los agentes.
    Usalo ANTES de grep masivo para recuperar contexto historico, decisiones y notas.
    'owner' opcional (ej: 'saruman','radagast','indexer') filtra solo memorias de ese dueno; vacio = todo.
    'category' opcional (failure|correction|insight|preference|convention|tool-quirk|general) filtra por tipo; vacio = todas.
    'include_superseded' (default False): si True incluye memorias reemplazadas (bi-temporal); por default solo vigentes.
    'hybrid' (default None=on): fusiona busqueda vectorial + full-text BM25 (RRF) para acertar terminos exactos (ids/paths/comandos); False = solo vector.
    'include_archived' (default False): si True incluye notas archivadas (/Historico//Archive/...) desde el inicio; por default se excluyen y solo se traen (marcadas) si no hay ninguna vigente.
    'min_score' (default None=env): umbral de confianza sobre el score coseno; si el mejor resultado no lo alcanza devuelve 'sin resultados confiables'."""
    payload = {"query": query, "top_k": top_k}
    if owner:
        payload["agent_owner"] = owner
    if category:
        payload["category"] = category
    if include_superseded:
        payload["include_superseded"] = True
    if hybrid is not None:
        payload["hybrid"] = hybrid
    if include_archived:
        payload["include_archived"] = True
    if min_score is not None:
        payload["min_score"] = min_score
    try:
        data = await _call("POST", "/memory/search", _read_key(ctx), payload)
    except Exception as e:
        # FIX E5 (tarea 4f110f83): distinguir "NO PUDE BUSCAR" de "no encontre nada".
        # Antes cualquier fallo se veia como un error crudo de tool; ahora el agente
        # recibe una instruccion explicita de reintentar en vez de concluir que el
        # tema no esta documentado y salir a reescribir trabajo que ya existe.
        return ("ERROR: la BUSQUEDA FALLO — esto NO significa que no haya resultados.\n"
                f"Causa: {type(e).__name__}: {str(e)[:200]}\n"
                "REINTENTA. Si persiste, avisa al lider: el motor de busqueda esta caido.")
    res = data.get("results", [])
    if not res:
        if data.get("low_confidence"):
            return "Sin resultados confiables (por debajo del umbral de confianza)."
        # search_ok=True solo confirma que el motor corrio; no prueba ausencia fuera de filtros o scope.
        if data.get("search_ok"):
            filtros = []
            if owner:
                filtros.append(f"owner={owner!r}")
            if category:
                filtros.append(f"category={category!r}")
            if min_score is not None:
                filtros.append(f"min_score={min_score!r}")
            defaults = ", ".join(
                name for name, enabled in (
                    ("include_archived=False", include_archived),
                    ("include_superseded=False", include_superseded),
                ) if not enabled
            ) or "sin exclusiones por archivo/supersesion"
            if filtros:
                return (f"Sin resultados con los filtros aplicados ({', '.join(filtros)}; {defaults}): la busqueda FUNCIONO. "
                        "Este cero no prueba que no exista material sobre el tema: puede estar fuera de esos filtros o del alcance de lectura del caller. Reintenta sin filtros antes de concluir ausencia.")
            return (f"Sin resultados visibles ({defaults}): la busqueda FUNCIONO. "
                    "Este cero no prueba que no exista material sobre el tema fuera del alcance de lectura del caller. Si necesitas certeza sobre existencia, pedisela a tu lider o al dueno del material: volver a buscar no puede ampliar tu alcance de lectura.")
        return "Sin resultados."
    prefix = "[resultados archivados: no hay ninguna nota vigente sobre esto]\n\n" if data.get("from_archive") else ""
    return prefix + "\n\n---\n\n".join(
        f"[{_fmt_score(r)}] ({r.get('agent_owner', '?')}) [{r.get('category', 'general')}] {r.get('source_path', '')} :: {r.get('title', '') or ''}\n"
        f"{(r.get('text', '') or '')[:600]}"
        for r in res
    )


def _memory_add_indeterminate_message() -> str:
    """Safe caller guidance when the write outcome cannot be confirmed.

    Do not include the source path or submitted text: both can be sensitive and a
    failed tool envelope can be retained outside the intended memory scope.
    """
    return (
        "No se recibió confirmación; el estado de la escritura es indeterminado. "
        "No reintentar automáticamente: verificar el origen exacto conocido por quien llama; "
        "metadata no demuestra igualdad del contenido."
    )


def _memory_add_http_error_message(error: httpx.HTTPStatusError) -> str:
    """Classify only 5xx without reflecting response body, URL, or request headers."""
    status = error.response.status_code
    if 500 <= status <= 599:
        return (
            f"Error HTTP {status} del servidor; posible efecto parcial. "
            "No se recibió confirmación; el estado de la escritura es indeterminado. "
            "No reintentar automáticamente: verificar el origen exacto conocido por quien llama; "
            "metadata no demuestra igualdad del contenido. "
            "Causa: HTTPStatusError."
        )
    # Preserve existing 4xx classification rather than recasting it as ambiguity.
    return f"Error {status}: {_err_detail(error)}"


@mcp.tool()
async def memory_add(text: str, source_path: str, title: str = "", tags: str = "", category: str = "general", ctx: Context = None) -> str:
    """Guarda una memoria en el cerebro compartido. source_path identifica el origen
    (ej: 'memoria/saruman/decision-x'). tags separados por coma.
    'category' opcional (failure|correction|insight|preference|convention|tool-quirk|general; default general)
    tipa la memoria; 'failure' permite recuperar errores pasados via memory_search(category='failure').
    Queda atribuida al agente que llama (por su API key)."""
    agent, k = _require_agent(ctx)
    payload = {
        "text": text,
        "source_path": source_path,
        "title": title or None,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "category": category,
    }
    try:
        data = await _call("POST", "/memory/add", k, payload)
    except (httpx.TimeoutException, httpx.TransportError) as error:
        # Intentionally do not infer "not sent": a transport failure can occur
        # before dispatch, during dispatch, or after a durable write. No retry.
        raise ToolError(_memory_add_indeterminate_message()) from error
    except httpx.HTTPStatusError as error:
        # 401/403/422 retain their existing status/detail classification. Only 5xx
        # becomes an explicitly indeterminate write result.
        raise ToolError(_memory_add_http_error_message(error)) from error
    return f"Guardado: chunk_id={data.get('chunk_id')}, dims={data.get('dims')}"


@mcp.tool()
async def memory_supersede(old: str, new: str, ctx: Context = None) -> str:
    """Marca una memoria como REEMPLAZADA por otra, SIN borrarla (bi-temporal, Sprint 1).
    'old' y 'new' aceptan chunk_id (uuid) o source_path. La vieja queda con valid_until
    y superseded_by=new, y deja de aparecer en memory_search por default; con
    include_superseded=True se recupera. La nueva sigue vigente.
    Requiere key valida (escritura); atribuida al agente que llama."""
    agent, k = _require_agent(ctx)
    data = await _call("POST", "/memory/supersede", k, {"old": old, "new": new})
    _audit(agent, "memory_supersede", f"{data.get('superseded')}->{data.get('superseded_by')}")
    return (f"Reemplazada: {data.get('superseded')} superseded_by={data.get('superseded_by')} "
            f"(valid_until={data.get('valid_until')})")


@mcp.tool()
async def memory_stats(ctx: Context = None) -> str:
    """Estadisticas del cerebro: memorias activas, fuentes, agentes."""
    return str(await _call("GET", "/stats", _read_key(ctx)))


# ---------------- SKILLS ----------------
@mcp.tool()
async def skill_add(name: str, description: str, content: str, when_to_use: str = "", tags: str = "", ctx: Context = None) -> str:
    """Publica un skill (procedimiento reutilizable) al registro compartido del cerebro.
    Disponible para todos los agentes; se recupera por relevancia a la tarea.
    Firma: name (corto), description (que hace), content (pasos). Opcionales: when_to_use, tags (coma)."""
    agent, k = _require_agent(ctx)
    parts = [f"SKILL: {name}", f"DESCRIPCION: {description}"]
    if when_to_use:
        parts.append(f"CUANDO USAR: {when_to_use}")
    text = "\n".join(parts) + f"\n\n{content}"
    extra = [t.strip() for t in tags.split(",") if t.strip()]
    payload = {"text": text, "source_path": f"skills/{name}", "title": name, "tags": ["skill"] + extra}
    data = await _call("POST", "/memory/add", k, payload)
    return f"Skill '{name}' publicado: chunk_id={data.get('chunk_id')}"


@mcp.tool()
async def skill_search(task: str, top_k: int = 3, ctx: Context = None) -> str:
    """Busca skills relevantes a una tarea en el registro compartido del cerebro.
    Carga solo lo relevante (eficiencia de contexto), no todos los skills."""
    payload = {"query": task, "top_k": top_k, "tags": ["skill"]}
    data = await _call("POST", "/memory/search", _read_key(ctx), payload)
    res = data.get("results", [])
    if not res:
        return "No hay skills relevantes para esa tarea."
    # FIX 2026-08-07 (tarea a7118e71): usar _fmt_score en vez de imprimir `score`.
    # `score` en modo hibrido es RRF = 1/(RRF_K + rank + 1), o sea es FUNCION SOLO DEL
    # RANK: da 0.016 / 0.016 / 0.015 para CUALQUIER query, siempre. Leido como si fuera
    # relevancia parecia "el score no discrimina / el indice esta stale", cuando el
    # retrieval funcionaba bien (cos 0.74 / 0.73 / 0.72 en el mismo caso reportado).
    # El bug era de PRESENTACION: _fmt_score ya existia desde el FIX E1 y skill_search
    # no lo usaba -- un fix aplicado a memory_search y no propagado a esta tool.
    # Se muestra tambien el source_path: sin el, dos secciones de skills DISTINTAS se
    # ven iguales y no se puede saber que skill leer, que es el proposito de esta tool.
    return "\n\n---\n\n".join(
        f"[{_fmt_score(r)}] ({r.get('agent_owner', '?')}) "
        f"{r.get('source_path', '?')} :: {r.get('title', '')}\n"
        f"{(r.get('text', '') or '')[:800]}"
        for r in res
    )


@mcp.tool()
async def skill_remove(name: str, hard: bool = False, ctx: Context = None) -> str:
    """Retira un skill del registro compartido. Por defecto SOFT-delete (deleted_at=now,
    reversible, respeta la Regla de Oro): el skill deja de aparecer en skill_search pero
    NO se borra fisicamente. hard=True borra definitivo (solo con intencion explicita).
    Requiere key valida (escritura). Usa el endpoint /memory/forget sobre source_path=skills/<name>."""
    agent, k = _require_agent(ctx)
    mode = "hard" if hard else "soft"
    payload = {"mode": mode, "source_path": f"skills/{name}"}
    data = await _call("POST", "/memory/forget", k, payload)
    n = data.get("deleted", 0)
    return f"Skill {name!r} retirado (mode={mode}, chunks afectados={n}) por {agent}."


@mcp.tool()
async def skill_list(q: str = "", tag: str = "", limit: int = 50, offset: int = 0,
                     detail: bool = False, ctx: Context = None) -> str:
    """ENUMERA el catalogo de skills del registro compartido (que skills EXISTEN).
    Complemento de skill_search: search es por relevancia semantica a una tarea, esto es
    el indice completo. Los references hijos de una skill NO se listan (no son skills).

    Por DEFAULT devuelve solo `name` + `description` de cada skill (para NO quemar
    contexto). detail=True agrega tags, owner, chunks y references_count.
    Filtros: q (substring en nombre/title/texto) y tag (tag exacto).
    Paginacion: limit (default 50, max 500) + offset; la salida indica el total y si hay
    mas paginas. Para LEER el contenido de una skill usa skill_get(name)."""
    params = [f"limit={int(limit)}", f"offset={int(offset)}"]
    if q:
        params.append(f"q={quote(q)}")
    if tag:
        params.append(f"tag={quote(tag)}")
    if detail:
        params.append("include_references_count=true")
    try:
        data = await _call("GET", "/skills/list?" + "&".join(params), _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    skills = data.get("skills") or []
    total = data.get("total", len(skills))
    if not skills:
        filt = f" (q={q!r} tag={tag!r})" if (q or tag) else ""
        return f"No hay skills en el registro{filt}."
    off = data.get("offset", 0)
    more = "  (hay mas: subi offset)" if data.get("has_more") else ""
    lines = [f"SKILLS {off + 1}-{off + len(skills)} de {total}{more}"]
    for s in skills:
        lines.append(f"- {s.get('name')}: {s.get('description') or ''}")
        if detail:
            tags = ", ".join(s.get("tags") or []) or "-"
            lines.append(f"    owner={s.get('agent_owner') or '?'} tags=[{tags}] "
                         f"chunks={s.get('chunk_count')} refs={s.get('references_count', '?')} "
                         f"updated={s.get('updated_at') or '-'}")
    if not detail:
        lines.append("(detail=True para tags/owner/references; skill_get(name) para el contenido)")
    return chr(10).join(lines)


@mcp.tool()
async def skill_get(name: str, include_references: bool = True, ctx: Context = None) -> str:
    """Devuelve el contenido COMPLETO (sin truncar) de UNA skill, o de UNO de sus references.
    Usalo cuando ya sabes QUE queres leer. Para DESCUBRIR skills relevantes a una tarea usa
    skill_search (que devuelve un preview corto por eficiencia de contexto).

    'name' acepta 3 formas:
      - '<skill>'                       -> la skill raiz (ej: 'cerebro-guia-maestra')
      - 'skills/<skill>'                -> idem, con el prefijo del namespace
      - '<skill>/references/<archivo>'  -> el CONTENIDO de ese reference
    Es decir: el contenido de un reference se pide con esta MISMA tool pasando su path
    relativo tal como aparece en la lista REFERENCES (sin el prefijo 'skills/'). No existe
    una tool aparte para eso.

    La salida declara KIND: 'skill' o 'reference' (y PARENT si es reference), asi
    'REFERENCES (0)' no es ambiguo. 'include_references' (default True) ENUMERA los archivos
    de references/ de la skill (paths + tamanos) sin inlinear su contenido; un reference no
    tiene hijos, por eso no lista nada."""
    try:
        data = await _call("GET",
                           f"/skills/get?name={quote(name)}"
                           f"&include_references={'true' if include_references else 'false'}",
                           _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    kind = data.get("kind") or "skill"
    label = "REFERENCE" if kind == "reference" else "SKILL"
    head = (f"{label}: {data.get('name')}  (kind={kind}, chars={data.get('chars')}, "
            f"chunks={data.get('chunk_count')}, owner={(data.get('metadata') or {}).get('agent_owner')})")
    refs = data.get("references") or []
    lines = [head]
    if kind == "reference":
        lines.append(f"PARENT SKILL: {data.get('parent_skill')}")
    elif include_references:
        if refs:
            lines.append(f"REFERENCES ({data.get('references_count')}):")
            for r in refs:
                lines.append(f"  - {r.get('path')}  ({r.get('chars')} chars, {r.get('chunks')} chunks)")
        else:
            lines.append("REFERENCES (0): (ninguno)")
    lines.append("-" * 8)
    # sin slice de presentacion: el punto de esta tool es devolver TODO
    lines.append(data.get("content", ""))
    return chr(10).join(lines)


@mcp.tool()
async def skill_add_reference(skill: str, filename: str, content: str, tags: str = "",
                              ctx: Context = None) -> str:
    """Agrega un archivo de reference a una skill EXISTENTE del registro compartido.
    Queda como chunk hijo namespaceado (source_path='skills/<skill>/references/<filename>'),
    recuperable por si mismo con memory_search, listado por skill_get(<skill>) y legible con
    skill_get('<skill>/references/<filename>'). Si la skill raiz no existe -> error (no se
    crean references huerfanos). filename se sanitiza (sin '..', sin '/' inicial, sin
    traversal, sin caracteres de control). Idempotente: republicar el mismo filename PISA,
    no duplica. Tags: ['skill-reference','skill:<skill>'] + los tuyos; NO lleva el tag
    'skill', asi los references no contaminan el descubrimiento de skill_search."""
    agent, k = _require_agent(ctx)
    extra = [t.strip() for t in tags.split(",") if t.strip()]
    try:
        data = await _call("POST", "/skills/add-reference", k,
                           {"skill": skill, "filename": filename, "content": content,
                            "tags": extra})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "skill_add_reference", data.get("source_path", ""))
    warn = data.get("warnings") or []
    msg = (f"Reference '{data.get('filename')}' agregado a la skill '{data.get('skill')}' por {agent}: "
           f"{data.get('source_path')} ({data.get('chars')} chars, {data.get('chunk_count')} chunk(s))")
    if warn:
        msg += " | warnings: " + "; ".join(warn)
    return msg


# ---------------- TAREAS (reemplazo Kanban) ----------------
@mcp.tool()
async def task_create(title: str, description: str = "", project: str = "", priority: str = "medium",
                      assignee: str = "", subagent: str = "", tags: str = "", parent_id: str = "", due_date: str = "",
                      organization: str = "",
                      ctx: Context = None) -> str:
    """Crea una tarea en el tablero del cerebro (reemplazo del Kanban). Arranca en status 'todo'.
    priority: low|medium|high. assignee: equipo/lider responsable (otixe|merlin|rias|saruman|radagast).
    subagent: subagente que ejecuta la tarea (texto libre, ej: Lurtz/Grima/Forge). tags separados por coma.
    parent_id: id de tarea padre (subtareas/derivacion). due_date: YYYY-MM-DD.
    'organization' OPCIONAL (identificador de organización del deployment): si NO la pasas se
    DERIVA automaticamente del 'project'; si la pasas, gana lo explicito."""
    creator, _k = _require_agent(ctx)
    _deny = _rbac(creator, 'task_create')
    if _deny:
        return _deny
    taglist = [t.strip() for t in tags.split(",") if t.strip()]
    with _pg() as conn, conn.cursor() as cur:
        parent_resolved = None
        if parent_id:
            parent_resolved, parent_status, parent_error = tasks_rbac.resolve_parent_id_core(cur, parent_id)
            if parent_error:
                # A hierarchy rejection is a tool failure, not successful text a
                # client must parse. Keep the core's stable HTTP-class code in
                # the native FastMCP error envelope without exposing arguments.
                raise ToolError(f"Error {parent_status}: {parent_error}")
            _immutable_check(cur, parent_resolved)
        cur.execute(
            "INSERT INTO tasks (title, description, project, priority, assignee, subagent, creator, tags, parent_id, due_date, organization) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, organization",
            (title, description, project, priority, assignee, subagent, creator, taglist,
             parent_resolved, due_date or None, organization or None))
        _new = cur.fetchone()
        tid = _new["id"]
        _org = _new["organization"]
        cur.execute(
            "INSERT INTO task_activity (task_id, agent, kind, to_status, body) "
            "VALUES (%s,%s,'created','todo',%s)",
            (tid, creator, f"creada: {title}"))
        conn.commit()
    # Sprint 5: hook bus - si el assignee es un lider, notificar. Conexion/transaccion
    # aparte y try/except para que un fallo del bus NUNCA rompa la creacion de tarea.
    # DELEGADO a common/bus_notify.py (FUENTE UNICA, tarea 2929a26a 2026-08-11).
    bus_notify.notify_assignment(_pg, creator, assignee,
                                 f"Nueva tarea asignada: {title} (de {creator})", tid)
    _audit(creator, "task_create", f"task:{tid} {title}")
    return (f"Tarea creada: {str(tid)[:8]} [{title}] org={_org or '-'} proyecto={project or '-'} "
            f"prioridad={priority} equipo={assignee or '-'} asignado={subagent or '-'} creador={creator}")


@mcp.tool()
async def task_list(status: str = "", project: str = "", assignee: str = "",
                    include_done: bool = False, limit: int = 30,
                    organization: str = "", ctx: Context = None) -> str:
    """Lista tareas del tablero. Filtros opcionales: status (todo|in_progress|blocked|done|cancelled),
    project, assignee, organization (identificador configurado en el deployment).
    Por defecto excluye done/cancelled (include_done=True para verlas). Ordena por prioridad y fecha."""
    where, params = ["request_kind IS DISTINCT FROM 'approval_request'"], []
    if status:
        where.append("status = %s"); params.append(status)
    elif not include_done:
        where.append("status NOT IN ('done','cancelled')")
    if project:
        where.append("project = %s"); params.append(project)
    if organization:
        where.append("organization = %s"); params.append(organization)
    if assignee:
        where.append("assignee = %s"); params.append(assignee)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    q = (f"SELECT id,title,project,organization,status,priority,assignee,due_date FROM tasks {clause} "
         "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at LIMIT %s")
    params.append(limit)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    if not rows:
        return "No hay tareas para ese filtro."
    return "\n".join(
        f"[{r['status']}] ({r['priority']}) {r['title']} — org:{r['organization'] or '-'} "
        f"proj:{r['project'] or '-'} "
        f"asig:{r['assignee'] or '-'} | id:{str(r['id'])[:8]}"
        for r in rows)


@mcp.tool()
async def task_update(task_id: str, status: str = "", assignee: str = "", subagent: str = "", priority: str = "",
                      project: str = "", title: str = "", description: str = "",
                      organization: str = "", due_date: str = "", parent_id: str | None = None, ctx: Context = None) -> str:
    """Actualiza una tarea (mover de columna Kanban, reasignar, cambiar prioridad).
    task_id acepta id completo o sus primeros caracteres. Solo actualiza los campos que pases.
    'organization' OPCIONAL: si la pasas se persiste explicita; si cambias solo 'project',
    la organization se re-deriva del nuevo proyecto.
    'due_date' (2026-08-12) YYYY-MM-DD para (re)agendar; '-' la LIMPIA (desagendar);
    '' = no tocar. task_create la aceptaba desde el inicio y esta NO: una tarea con
    'parent_id' se omite/no cambia con null; '-' lo limpia explícitamente. Los
    aliases none/null/clear son entradas inválidas, no solicitudes de mutación.
    """
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_update')
    if _deny:
        return _deny
    fields, params = [], []
    for col, val in [("status", status), ("assignee", assignee), ("subagent", subagent), ("priority", priority),
                     ("project", project), ("title", title), ("description", description),
                     ("organization", organization)]:
        if val:
            fields.append(f"{col} = %s"); params.append(val)
    # due_date (2026-08-12, saruman): SEPARADO del loop porque necesita 3 estados,
    # no 2. Para los demas campos "" = "no tocar" y no hay forma de expresar "borrar";
    # una fecha SI tiene que poder limpiarse. PARIDAD EXACTA con el REST
    # (tasks_core.update): si las dos puertas divergen, el agente que use MCP obtiene
    # un comportamiento distinto al que use REST para la MISMA operacion, y eso es
    # peor que no tener el campo.
    parent_clear = parent_id == "-"
    if parent_id is not None:
        if parent_clear:
            fields.append("parent_id = NULL")
        else:
            parent_marker = parent_id
    else:
        parent_marker = None
    if due_date:
        _dd = due_date.strip().lower()
        if _dd in ("-", "none", "null", "clear"):
            fields.append("due_date = NULL")
        else:
            try:
                _parsed = _dt.datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
            except ValueError:
                return (f"due_date invalida '{due_date}'; formato YYYY-MM-DD "
                        f"(o '-' para limpiarla).")
            fields.append("due_date = %s"); params.append(_parsed.isoformat())
    if not fields and parent_id is None:
        return "Nada para actualizar (pasa al menos un campo)."
    with _pg() as conn, conn.cursor() as cur:
        # Same cooperative transaction lock as REST. It precedes both child and
        # ancestor reads, so READ COMMITTED sees the preceding hierarchy commit.
        if parent_id is not None:
            tasks_rbac.acquire_parent_write_guard(cur)
        tid, err = _resolve_id(cur, task_id)
        if err:
            # A rejected task id is a FastMCP failure envelope too; returning
            # ordinary text makes a missing update indistinguishable from success.
            raise ToolError(f"Error 404: {err}")
        if parent_id is not None and not parent_clear:
            resolved_parent, parent_status, parent_error = tasks_rbac.resolve_parent_id_core(
                cur, parent_marker, child_id=tid, guard_held=True)
            if parent_error:
                # Only parent-contract rejections become ToolError here; do not
                # broaden this into a catch-all for unrelated task failures.
                raise ToolError(f"Error {parent_status}: {parent_error}")
            _immutable_check(cur, resolved_parent)
            fields.append("parent_id = %s")
            params.append(resolved_parent)
        fields.append("updated_at = now()")
        q = (f"UPDATE tasks SET {', '.join(fields)} "
             "WHERE id::text = %s RETURNING id, title, status, organization")
        params2 = params + [tid]
        prev = {}
        if status:
            cur.execute("SELECT id::text AS id, status FROM tasks WHERE id::text = %s", (tid,))
            prev = {r["id"]: r["status"] for r in cur.fetchall()}
        cur.execute(q, params2)
        rows = cur.fetchall()
        if status:
            for _r in rows:
                _frm = prev.get(str(_r["id"]))
                if _frm != status:
                    cur.execute(
                        "INSERT INTO task_activity (task_id, agent, kind, from_status, to_status) "
                        "VALUES (%s,%s,'status_change',%s,%s)",
                        (_r["id"], agent, _frm, status))
        unblocked = []
        if status == "done" and len(rows) == 1:
            # tareas que dependian de esta y que ahora quedan listas (todas sus deps done)
            cur.execute(
                "SELECT DISTINCT t.id::text AS id, t.title, t.project "
                "FROM tasks t JOIN task_deps d ON d.task_id = t.id "
                "WHERE d.depends_on_id = %s AND t.status = 'todo' AND t.request_kind IS DISTINCT FROM 'approval_request' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_deps d2 JOIN tasks dt ON dt.id = d2.depends_on_id "
                "  WHERE d2.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done')",
                (rows[0]["id"],))
            unblocked = cur.fetchall()
        conn.commit()
    _audit(agent, "task_update", f"task:{task_id} status={status or '-'}")
    if not rows:
        return f"No se encontro tarea con id '{task_id}'."
    if len(rows) > 1:
        return f"Ambiguo: {len(rows)} tareas coinciden con '{task_id}'. Usa el id completo."
    r = rows[0]
    msg = (f"Tarea {str(r['id'])[:8]} actualizada -> status={r['status']} "
           f"org={r.get('organization') or '-'} ({r['title']})")
    if unblocked:
        ub = "; ".join(f"{u['title']} ({u['id'][:8]})" for u in unblocked)
        msg += f"\nDesbloqueadas (ahora listas para empezar): {ub}"
    elif status == "done":
        msg += "\nDesbloqueadas: ninguna."
    return msg


# No hay wrapper **kwargs: el modelo exacto es la frontera de validacion MCP.
# FastMCP construye por firma; `create_model` conserva esa forma plana y hace que
# schema y ejecucion rechacen de la misma forma propiedades inexistentes.
def _install_strict_task_arguments(tool_name, model_name, fields):
    """Close one explicitly selected task tool without widening other MCP tools."""
    detail = f"Parametros no permitidos para {tool_name}."

    class _Metadata(FuncMetadata):
        async def call_fn_with_arg_validation(self, *args, **kwargs):
            try:
                return await super().call_fn_with_arg_validation(*args, **kwargs)
            except ValidationError as exc:
                if any(error.get("type") == "extra_forbidden" for error in exc.errors()):
                    raise ValueError(detail) from None
                raise

    class _Arguments(ArgModelBase):
        model_config = ConfigDict(extra="forbid")

    arguments = create_model(model_name, __base__=_Arguments, **fields)
    tool = mcp._tool_manager._tools[tool_name]
    tool.fn_metadata = _Metadata(
        arg_model=arguments, output_schema=tool.fn_metadata.output_schema,
        output_model=tool.fn_metadata.output_model, wrap_output=tool.fn_metadata.wrap_output,
    )
    tool.parameters = arguments.model_json_schema(by_alias=True)


_TASK_CREATE_EXTRA_DETAIL = "Parametros no permitidos para task_create."
_TASK_CREATE_EXTRA_ERROR = "Error executing tool task_create: " + _TASK_CREATE_EXTRA_DETAIL
_install_strict_task_arguments("task_create", "task_createArguments", {
    "title": (str, ...), "description": (str, ""), "project": (str, ""),
    "priority": (str, "medium"), "assignee": (str, ""), "subagent": (str, ""),
    "tags": (str, ""), "parent_id": (str, ""), "due_date": (str, ""),
    "organization": (str, ""),
})

_TASK_UPDATE_EXTRA_DETAIL = "Parametros no permitidos para task_update."
_TASK_UPDATE_EXTRA_ERROR = "Error executing tool task_update: " + _TASK_UPDATE_EXTRA_DETAIL
_install_strict_task_arguments("task_update", "task_updateArguments", {
    "task_id": (str, ...), "status": (str, ""), "assignee": (str, ""),
    "subagent": (str, ""), "priority": (str, ""), "project": (str, ""),
    "title": (str, ""), "description": (str, ""), "organization": (str, ""),
    "due_date": (str, ""), "parent_id": (str | None, None),
})


@mcp.tool()
async def task_get(task_id: str, ctx: Context = None) -> str:
    """Detalle completo de una tarea (acepta id o prefijo) + sus subtareas."""
    with _pg() as conn, conn.cursor() as cur:
        # FIX 2026-08-05: usa el resolvedor UNIFICADO en vez de SQL inline. La copia
        # inline no tenia guarda de entrada vacia: con task_id='' el patron LIKE '%'
        # matchea la tabla ENTERA y devolvia "Ambiguo: 996 coinciden" en vez de
        # "falta task_id". Era la CUARTA copia del mismo patron (el dedup de
        # c6b61bd1 cubrio _resolve_id pero no los SQL inline).
        tid, err = _resolve_visible_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT * FROM tasks WHERE id::text = %s", (tid,))
        rows = cur.fetchall()
        if not rows:
            return f"No existe tarea '{task_id}'."
        t = rows[0]
        cur.execute("SELECT id,title,status FROM tasks WHERE parent_id=%s AND request_kind IS DISTINCT FROM 'approval_request'", (t["id"],))
        subs = cur.fetchall()
    out = [f"# {t['title']} ({t['status']}/{t['priority']})",
           f"organizacion: {t.get('organization') or '-'} | proyecto: {t['project'] or '-'} "
           f"| asignado: {t['assignee'] or '-'} | creador: {t['creator'] or '-'}",
           f"due: {t['due_date'] or '-'} | tags: {', '.join(t['tags'] or [])} | id: {t['id']}",
           "", (t["description"] or "(sin descripcion)")]
    if subs:
        out.append("")
        out.append("Subtareas:")
        out += [f"  - [{x['status']}] {x['title']} ({str(x['id'])[:8]})" for x in subs]
    return chr(10).join(out)


@mcp.tool()
async def project_summary(project: str = "", organization: str = "", ctx: Context = None) -> str:
    """Resumen del tablero por ORGANIZACION -> proyecto -> estado (jerarquia completa).
    Vacio = todas las organizaciones y proyectos. 'organization' filtra una sola org;
    'project' filtra un solo proyecto."""
    # M1 (auditoria 2026-07-28): los fragmentos de WHERE son literales HARDCODEADOS
    # del fuente (allowlist), nunca texto del input. Los VALORES van parametrizados.
    frags, params = ["t.request_kind IS DISTINCT FROM 'approval_request'"], []
    if project:
        frags.append("t.project = %s"); params.append(project)
    if organization:
        frags.append("t.organization = %s"); params.append(organization)
    assert all(f in ("t.request_kind IS DISTINCT FROM 'approval_request'", "t.project = %s", "t.organization = %s") for f in frags)
    where = ("WHERE " + " AND ".join(frags)) if frags else ""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.project, t.organization, t.status, count(*) n, "
            "       o.name AS org_name, o.sort_order AS org_sort "
            "FROM tasks t LEFT JOIN organizations o ON o.id = t.organization "
            f"{where} "
            "GROUP BY t.project, t.organization, t.status, o.name, o.sort_order "
            "ORDER BY o.sort_order NULLS LAST, t.organization, t.project", params)
        rows = cur.fetchall()
    if not rows:
        return "Sin tareas."
    orgs = {}
    for r in rows:
        oid = r["organization"] or "(sin organizacion)"
        o = orgs.setdefault(oid, {"name": r["org_name"] or oid,
                                  "sort": r["org_sort"], "by_status": {}, "projects": {}})
        o["by_status"][r["status"]] = o["by_status"].get(r["status"], 0) + r["n"]
        o["projects"].setdefault(r["project"] or "(sin proyecto)", {})[r["status"]] = r["n"]
    lines = []
    for oid, o in sorted(orgs.items(),
                         key=lambda kv: (kv[1]["sort"] is None, kv[1]["sort"] or 0, kv[0])):
        ototal = sum(o["by_status"].values())
        oparts = ", ".join(f"{k}:{v}" for k, v in sorted(o["by_status"].items()))
        lines.append(f"## {o['name']} [{oid}] (total {ototal}) -> {oparts}")
        for proj, st in sorted(o["projects"].items()):
            total = sum(st.values())
            parts = ", ".join(f"{k}:{v}" for k, v in sorted(st.items()))
            lines.append(f"   - {proj} (total {total}) -> {parts}")
    return chr(10).join(lines)


# ---------------- CADENAS / DEPENDENCIAS DE TAREAS ----------------
def _resolve_id(cur, tid):
    """Resuelve un id completo o prefijo a un uuid unico. Devuelve (id_texto, error).

    La LOGICA vive en tasks_rbac.resolve_id_core (fuente unica compartida con el
    gateway, tarea c6b61bd1). Aca queda SOLO la traduccion a la firma de este
    transporte: tupla (valor, error) en vez de excepcion. No dupliques el SQL aca.

    NOTA de cambio de comportamiento (unico de este refactor, era un BUG): con
    tid='' o solo espacios ahora devuelve 'falta task_id'. Antes el LIKE '%'
    matcheaba la tabla ENTERA y el error era 'Ambiguo: 994 tareas'."""
    tid_ok, _status, detail = tasks_rbac.resolve_id_core(cur, tid)
    if detail:
        return None, detail
    return tid_ok, None


def _resolve_visible_id(cur, tid):
    """Ordinary read resolver; approvals look identical to absent IDs and prefixes."""
    if not (tid or "").strip():
        return None, "falta task_id"
    cur.execute("SELECT id::text AS id FROM tasks WHERE request_kind IS DISTINCT FROM 'approval_request' "
                "AND (id::text = %s OR id::text LIKE %s) LIMIT 2", (tid, tid + "%"))
    rows = cur.fetchall()
    if not rows:
        return None, f"No existe tarea '{tid}'."
    if len(rows) > 1:
        return None, f"Ambiguo: varias tareas coinciden con '{tid}'. Usa el id completo."
    return rows[0]["id"], None


def _immutable_check(cur, *ids):
    """Lock both relation endpoints in a stable order within the writer transaction."""
    cur.execute("SELECT id, request_kind FROM tasks WHERE id = ANY(%s::uuid[]) "
                "ORDER BY id FOR SHARE", (list(ids),))
    if any(row["request_kind"] == "approval_request" for row in cur.fetchall()):
        raise ToolError("Error 409: approval_request_immutable")


@mcp.tool()
async def task_link(task_id: str, depends_on_id: str, ctx: Context = None) -> str:
    """Encadena tareas: 'task_id' queda BLOQUEADA POR 'depends_on_id' (no esta lista para
    empezar hasta que depends_on_id este en done). Usala para armar cadenas/orden de trabajo.
    Rechaza auto-dependencia y ciclo directo (si ya existe la relacion inversa).
    Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_link')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        did, err = _resolve_id(cur, depends_on_id)
        if err:
            return err
        _immutable_check(cur, tid, did)
        if tid == did:
            return "Auto-dependencia no permitida: una tarea no puede depender de si misma."
        cur.execute("SELECT 1 FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s",
                    (did, tid))
        if cur.fetchone():
            return "Ciclo directo: la dependencia inversa ya existe. No se creo la relacion."
        cur.execute("INSERT INTO task_deps (task_id, depends_on_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (tid, did))
        conn.commit()
    _audit(agent, "task_link", f"task:{tid} depends_on:{did}")
    return f"Dependencia creada: {tid[:8]} queda bloqueada por {did[:8]}."


@mcp.tool()
async def task_unlink(task_id: str, depends_on_id: str, ctx: Context = None) -> str:
    """Quita una dependencia entre tareas: elimina SOLO la relacion, NO borra ninguna tarea.
    Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_unlink')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        did, err = _resolve_id(cur, depends_on_id)
        if err:
            return err
        _immutable_check(cur, tid, did)
        cur.execute("DELETE FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s "
                    "RETURNING task_id::text AS task_id", (tid, did))
        row = cur.fetchone()
        conn.commit()
    _audit(agent, "task_unlink", f"task:{tid} depends_on:{did}")
    if not row:
        return f"No existia dependencia entre {tid[:8]} y {did[:8]}."
    return f"Dependencia eliminada: {tid[:8]} ya no depende de {did[:8]} (tareas intactas)."


@mcp.tool()
async def task_deps(task_id: str, ctx: Context = None) -> str:
    """Muestra las dependencias de una tarea. blocked_by: tareas de las que depende (deben
    estar done para poder empezar). blocks: tareas que dependen de esta (se pueden desbloquear
    al cerrarla). Acepta id completo o prefijo."""
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_visible_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
                    "WHERE d.task_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' ORDER BY dt.status, dt.title", (tid,))
        blocked_by = cur.fetchall()
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.task_id "
                    "WHERE d.depends_on_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' ORDER BY dt.status, dt.title", (tid,))
        blocks = cur.fetchall()

    def _fmt(rows):
        return "\n".join(f"  - [{r['status']}] {r['title']} ({r['id'][:8]})" for r in rows) or "  (ninguna)"

    return (f"Tarea {tid[:8]} — dependencias:\n"
            f"blocked_by (espera a que esten done):\n{_fmt(blocked_by)}\n"
            f"blocks (se desbloquean al cerrar esta):\n{_fmt(blocks)}")


@mcp.tool()
async def tasks_ready(project: str = "", assignee: str = "", ctx: Context = None) -> str:
    """Lista tareas en 'todo' LISTAS para empezar: sin dependencias, o con TODAS sus deps en done.
    Es 'lo siguiente de la cadena'. Usala para descubrir autonomamente que trabajo tomar.
    'project' opcional filtra por proyecto. 'assignee' opcional filtra por responsable (server-side)."""
    proj = project or None
    who = assignee or None
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.priority, t.assignee "
            "FROM tasks t WHERE t.status = 'todo' AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND (%s::text IS NULL OR t.project = %s) "
            "AND (%s::text IS NULL OR t.assignee = %s) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done') "
            "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.project, t.title",
            (proj, proj, who, who))
        rows = cur.fetchall()
    if not rows:
        return "No hay tareas listas para empezar."
    return "\n".join(
        f"({r['priority']}) {r['title']} — proj:{r['project'] or '-'} "
        f"asig:{r['assignee'] or '-'} | id:{r['id'][:8]}"
        for r in rows)


@mcp.tool()
async def task_cancel(task_id: str, ctx: Context = None) -> str:
    """Cancela una tarea de forma SOFT (status='cancelled'). NUNCA borra datos (Regla de Oro).
    Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_cancel')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("UPDATE tasks SET status='cancelled', updated_at=now() WHERE id::text = %s "
                    "RETURNING title", (tid,))
        row = cur.fetchone()
        conn.commit()
    _audit(agent, "task_cancel", f"task:{tid}")
    return f"Tarea {tid[:8]} cancelada (soft, status=cancelled): {row['title']}."


# ---------------- VAULT (escritura de archivos .md) ----------------
@mcp.tool()
async def vault_write(path: str, content: str, if_match: str | None = None,
                      include_metadata: bool = False, ctx: Context = None) -> str | dict:
    """Crea o actualiza un archivo .md REAL en el vault de Obsidian.

    Por defecto conserva el texto histórico de éxito. Para el protocolo CAS,
    pasá el ETag quoted obtenido de ``vault_read(include_metadata=True)`` como
    ``if_match`` y ``include_metadata=True`` para recibir un objeto con el ETag
    resultante de los bytes persistidos. El bridge ``cb_vault_write`` aún no
    expone ``if_match``; no es una garantía CAS global.
    """
    agent, k = _require_agent(ctx)
    payload = {"path": path, "content": content, "overwrite": True}
    if if_match is not None:
        payload["if_match"] = if_match
    try:
        data = await _call("POST", "/vault/write", k, payload)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            try:
                current = e.response.json().get("etag")
            except ValueError:
                current = None
            raise ToolError(f"Conflicto de versión (409); releé antes de escribir. ETag actual: {current or 'archivo ausente'}") from e
        raise
    _audit(agent, "vault_write", data.get("path", path))
    if include_metadata:
        return {
            "content": data.get("content", ""), "path": data.get("path", path),
            "bytes": data.get("bytes"), "etag": data.get("etag"),
        }
    return (f"Escrito por {agent}: {data.get('path')} "
            f"({data.get('bytes')} bytes). El indexer lo indexara.")


@mcp.tool()
async def vault_read(path: str, include_metadata: bool = False,
                     ctx: Context = None) -> str | dict:
    """Lee un archivo del vault de Obsidian (bajo obsidian-vault/).

    Por defecto devuelve exactamente el contenido crudo histórico, sin ETag ni
    prefijos. Con ``include_metadata=True`` devuelve ``content``, ``path``,
    ``bytes`` y el ETag quoted; usá ese objeto explícito para CAS. path relativo
    a obsidian-vault (ej: '50 Agents/Saruman/nota.md'). Bloquea traversal; cap 2 MB.
    """
    agent, k = _require_agent(ctx)
    try:
        data = await _call("GET", f"/vault/read?path={quote(path)}", k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "vault_read", data.get("path", path))
    if include_metadata:
        return {
            "content": data.get("content", ""), "path": data.get("path", path),
            "bytes": data.get("bytes"), "etag": data.get("etag"),
        }
    return data.get("content", "")


# ---------------- GESTION DE AGENTES / KEYS (solo LIDERES) ----------------
@mcp.tool()
async def agent_provision(name: str, ctx: Context = None) -> str:
    """Provisiona una API key para un subagente de TU equipo. SOLO lideres
    (merlin/rias/saruman/radagast). Genera un token fuerte, lo guarda hasheado
    (sha256, nunca en claro) y te lo devuelve UNA sola vez: guardalo, no se puede
    recuperar. La key funciona de inmediato (sin restart). name activo -> error;
    no permite provisionar un lider. El subagente queda en team=<vos>."""
    agent, k = _require_agent(ctx)
    try:
        data = await _call("POST", "/agents/provision", k, {"name": name})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "agent_provision", f"provisioned {data.get('name')} team={data.get('team')}")
    return (f"Key provisionada por {agent} para '{data.get('name')}' (team={data.get('team')}).\n"
            f"TOKEN (unica vez, guardalo YA): {data.get('key')}")


@mcp.tool()
async def leader_provision(name: str, authorized_by: str = "", ctx: Context = None) -> str:
    """Provisiona una API key para un NUEVO LIDER con equipo propio. SOLO lideres.

    ⚠️ REQUIERE AUTORIZACION EXPLICITA DE OTIXE (politica 2026-08-05): hay que pasar
    `authorized_by='otixe'`, y SOLO si te lo autorizo de verdad. Un lider nuevo nace
    `orchestrator` (todas las operaciones del tablero) y con capacidad de crear MAS
    lideres, o sea es una escalada en cadena: no se hace por iniciativa propia.
    Token fuerte, sha256, se devuelve UNA sola vez: guardalo, no se puede recuperar.
    name ya activo -> error; lider canonico -> error. team = <name> (propio)."""
    agent, k = _require_agent(ctx)
    # Guarda local con mensaje util: sin esto el gateway devuelve 403 igual, pero el
    # agente veria un error de autorizacion sin entender que le falta un parametro.
    if (authorized_by or "").strip().lower() != "otixe":
        return ("crear un LIDER requiere autorizacion explicita de Otixe. Si te la dio, "
                "volve a llamar con authorized_by='otixe'. Un lider nuevo nace "
                "orchestrator y puede crear mas lideres, por eso no se hace solo.")
    try:
        data = await _call("POST", "/agents/provision-leader", k,
                           {"name": name, "authorized_by": "otixe"})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "leader_provision", f"provisioned-leader {data.get('name')} by={agent}")
    return (f"LIDER provisionado por {agent}: '{data.get('name')}' "
            f"(team={data.get('name')}, is_lead=true).\n"
            f"TOKEN (unica vez, guardalo YA): {data.get('key')}")


@mcp.tool()
async def agent_revoke(name: str, ctx: Context = None) -> str:
    """Revoca (desactiva) la key de un subagente de TU equipo. SOLO lideres.
    Soft: active=false + revoked_at. La key deja de autenticar al instante, pero
    los datos historicos del agente (memoria/tareas) quedan intactos. No podes
    revocar un lider ni un agente de otro equipo."""
    agent, k = _require_agent(ctx)
    try:
        data = await _call("POST", "/agents/revoke", k, {"name": name})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "agent_revoke", f"revoked {data.get('name')}")
    return f"Agente '{data.get('name')}' revocado (active=false). Datos historicos intactos."


@mcp.tool()
async def agent_list(ctx: Context = None) -> str:
    """Lista los agentes de TU equipo (name, active, created_at). SOLO lideres."""
    agent, k = _require_agent(ctx)
    try:
        data = await _call("GET", "/agents/list", k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    ags = data.get("agents", [])
    if not ags:
        return f"El equipo de {agent} no tiene agentes provisionados."
    return "\n".join(
        f"- {a['name']} | active={a['active']} | created={a['created_at']}" for a in ags)




# ==== F1 ISSUES (additive) ====
# Maquina de estados (SDD secc.4). needs_changes/failed = eventos, no estados.
_VALID_STATUSES = {"todo", "in_progress", "blocked", "in_review", "submitted", "done", "deferred", "cancelled"}
_TRANSITIONS = {
    "todo": {"in_progress", "blocked", "deferred", "cancelled"},
    "in_progress": {"in_review", "submitted", "done", "blocked", "deferred", "cancelled"},
    "in_review": {"done", "in_progress", "cancelled"},
    "submitted": {"done", "in_progress", "cancelled"},
    "blocked": {"in_progress", "deferred", "cancelled"},
    "deferred": {"todo", "in_progress", "cancelled"},
    "done": {"todo", "in_progress"},
    "cancelled": set(),
}


def _can_transition(frm, to):
    if frm == to:
        return True
    return to in _TRANSITIONS.get(frm, set())


@mcp.tool()
async def task_claim(task_id: str, ctx: Context = None) -> str:
    """Toma (claim) una tarea del tablero: la pasa a in_progress y te la asigna como
    ejecutor (claimed_by = owner de tu key). Solo si status in (todo|in_review|blocked)
    y no esta tomada por OTRO agente (evita doble-toma; en ese caso pedi un handoff).
    Registra evento 'claim'. Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_claim')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT status, claimed_by FROM tasks WHERE id::text = %s", (tid,))
        t = cur.fetchone()
        st = t["status"]
        cb = t["claimed_by"] or ""
        if st not in ("todo", "in_review", "blocked"):
            return f"No se puede tomar {tid[:8]}: status={st} (solo todo|in_review|blocked)."
        if cb and cb != agent:
            return f"Ya claimeada por '{cb}' (tarea {tid[:8]}). Pedi un handoff en vez de doble-tomar."
        cur.execute("UPDATE tasks SET claimed_by=%s, claimed_at=now(), status='in_progress', "
                    "updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(claimed_by, '')=%s RETURNING id::text AS id", (agent, tid, st, cb))
        if not cur.fetchone():
            raise ToolError(f"Conflicto concurrente al tomar {tid[:8]}; la tarea cambio despues de leerla.")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'claim',%s,'in_progress',%s)", (tid, agent, st, f"claimed by {agent}"))
        conn.commit()
    _audit(agent, "task_claim", f"task:{tid}")
    return f"Tarea {tid[:8]} tomada por {agent} -> in_progress (claimed_at=now)."


@mcp.tool()
async def task_comment(task_id: str, body: str, ctx: Context = None) -> str:
    """Agrega un comentario al hilo de actividad de una tarea (no cambia el status).
    Atribuido al owner de tu key. Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_comment')
    if _deny:
        return _deny
    if not (body or "").strip():
        return "Falta el body del comentario."
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        _immutable_check(cur, tid)
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'comment',%s)", (tid, agent, body))
        conn.commit()
    _audit(agent, "task_comment", f"task:{tid}")
    return f"Comentario agregado a {tid[:8]} por {agent}."


@mcp.tool()
async def task_handoff(task_id: str, to_agent: str, note: str = "", ctx: Context = None) -> str:
    """Deriva (handoff) una tarea a otro agente/lider: setea assignee=to_agent y libera
    el claim (claimed_by=''). Registra evento 'handoff' con la nota. Es la delegacion
    cruzada (Saruman->Merlin, Rias->Radagast, etc.). Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_handoff')
    if _deny:
        return _deny
    if not (to_agent or "").strip():
        return "Falta to_agent (a quien derivar)."
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("UPDATE tasks SET assignee=%s, claimed_by='', updated_at=now() "
                    "WHERE id::text = %s", (to_agent, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'handoff',%s)", (tid, agent, note or f"handoff -> {to_agent}"))
        cur.execute("SELECT title FROM tasks WHERE id::text = %s", (tid,))
        _row = cur.fetchone()
        _htitle = _row["title"] if _row else ""
        conn.commit()
    # Sprint 5: hook bus - si el destinatario del handoff es un lider, notificar.
    # DELEGADO a common/bus_notify.py. Antes el gate era `to_agent in LEADERS`, asi
    # que derivarle una tarea a un SUBAGENTE no avisaba a nadie (rudeus-pursena: 16
    # handoffs, 0 avisos). Ahora se avisa al lider de su equipo.
    bus_notify.notify_assignment(_pg, agent, to_agent,
                                 f"Tarea recibida por handoff: {_htitle} (de {agent})", tid)
    _audit(agent, "task_handoff", f"task:{tid} -> {to_agent}")
    return f"Tarea {tid[:8]} derivada de {agent} a '{to_agent}' (claim liberado)."


@mcp.tool()
async def task_review(task_id: str, verdict: str, note: str = "", ctx: Context = None) -> str:
    """Gate de QA (mapea a Grima). Solo opera sobre tareas en in_review o submitted.
    verdict='pass' -> in_review->done. verdict='needs_changes' -> in_review->in_progress.
    Registra evento 'review' con nota. Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_review')
    if _deny:
        return _deny
    v = (verdict or "").strip().lower()
    if v not in ("pass", "needs_changes"):
        return "verdict invalido: usa 'pass' o 'needs_changes'."
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT status, submitted_by, claimed_by FROM tasks WHERE id::text = %s", (tid,))
        _row = cur.fetchone()
        st = _row["status"]
        if st not in ("in_review", "submitted"):
            return f"Solo se revisa en in_review/submitted (status actual={st}, tarea {tid[:8]})."
        # INVARIANTE RELACIONAL (2026-08-05, tarea 26e7e659): quien ENTREGO no firma.
        # FUENTE UNICA con tasks_core.review (tarea c6b61bd1): la condicion y el
        # mensaje viven en tasks_rbac. Va aca porque esta tool habla DIRECTO a
        # Postgres y no pasa por tasks_core: un control en un solo transporte no esta
        # aplicado (leccion del agujero de revocacion, que tenia dos puertas).
        # 'otixe' esta exento por decision suya (es el humano); los 5 LIDERES NO.
        if tasks_rbac.self_review_denied(agent, _row.get("submitted_by")):
            return tasks_rbac.self_review_message(agent, tid)
        to = "done" if v == "pass" else "in_progress"
        submitted_by = _row.get("submitted_by") or ""
        claimed_by = _row.get("claimed_by") or ""
        cur.execute("UPDATE tasks SET status=%s, updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(submitted_by, '')=%s AND COALESCE(claimed_by, '')=%s "
                    "RETURNING id::text AS id", (to, tid, st, submitted_by, claimed_by))
        if not cur.fetchone():
            raise ToolError(f"Conflicto concurrente al revisar {tid[:8]}; la tarea cambio despues de leerla.")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'review',%s,%s,%s)", (tid, agent, st, to, f"{v}: {note}"))
        conn.commit()
    _audit(agent, "task_review", f"task:{tid} {v}")
    return f"Review de {tid[:8]} por {agent}: {v} -> status={to}."


@mcp.tool()
async def task_submit(task_id: str, note: str = "", ctx: Context = None) -> str:
    """Marca una tarea como 'submitted' (in_progress -> submitted): el executor la entrega
    para auditoria del gate. Luego el auditor usa task_review (pass->done / needs_changes->in_progress).
    Soporta el ciclo pending->in_progress->submitted->approve/reject->done. Acepta id o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_submit')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT status, assignee, title FROM tasks WHERE id::text = %s", (tid,))
        _row_s = cur.fetchone()
        st = _row_s["status"]
        _asg_s = _row_s["assignee"] or ""
        _stitle = _row_s["title"] or ""
        if not _can_transition(st, "submitted"):
            return f"No se puede pasar a submitted desde status={st} (tarea {tid[:8]})."
        cur.execute("UPDATE tasks SET status='submitted', submitted_by=%s, updated_at=now() "
                    "WHERE id::text = %s", (agent, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'status_change',%s,'submitted',%s)", (tid, agent, st, note))
        conn.commit()
    # 2026-08-10: hook bus - avisarle a quien tiene que FIRMAR que hay una entrega.
    # MISMO patron que task_create/task_handoff de este archivo (no se inventa uno nuevo).
    # Guarda anti-duplicado: solo si el status cambio de verdad (_can_transition admite
    # frm==to, asi que un re-submit mandaria un segundo mensaje identico).
    # DELEGADO a common/bus_notify.py. La guarda `st != "submitted"` SE CONSERVA aca
    # (no se mueve al modulo): es especifica de esta operacion, porque
    # _can_transition admite frm==to y un re-submit mandaria un mensaje duplicado.
    if st != "submitted":
        bus_notify.notify_assignment(_pg, agent, _asg_s,
                                     f"Tarea entregada para tu firma: {_stitle} (de {agent})", tid)
    _audit(agent, "task_submit", f"task:{tid}")
    return f"Tarea {tid[:8]} -> submitted por {agent} (lista para auditoria del gate)."


@mcp.tool()
async def task_block(task_id: str, reason: str = "", ctx: Context = None) -> str:
    """Marca una tarea como blocked con un motivo (dependency|external|info|resource u otro).
    Registra evento 'block'. Rechaza transiciones invalidas (SDD secc.4). Acepta id o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_block')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT status FROM tasks WHERE id::text = %s", (tid,))
        st = cur.fetchone()["status"]
        if not _can_transition(st, "blocked"):
            return f"No se puede bloquear desde status={st} (tarea {tid[:8]})."
        cur.execute("UPDATE tasks SET status='blocked', blocked_reason=%s, updated_at=now() "
                    "WHERE id::text = %s", (reason, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'block',%s,'blocked',%s)", (tid, agent, st, reason))
        conn.commit()
    _audit(agent, "task_block", f"task:{tid}")
    return f"Tarea {tid[:8]} -> blocked (reason={reason or '-'}) por {agent}."


@mcp.tool()
async def task_unblock(task_id: str, note: str = "", ctx: Context = None) -> str:
    """Desbloquea una tarea (blocked -> in_progress), limpia blocked_reason y registra
    evento 'unblock'. Acepta id completo o prefijo."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'task_unblock')
    if _deny:
        return _deny
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT status FROM tasks WHERE id::text = %s", (tid,))
        st = cur.fetchone()["status"]
        if st != "blocked":
            return f"No esta blocked (status={st}, tarea {tid[:8]})."
        cur.execute("UPDATE tasks SET status='in_progress', blocked_reason='', updated_at=now() "
                    "WHERE id::text = %s", (tid,))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'unblock','blocked','in_progress',%s)", (tid, agent, note))
        conn.commit()
    _audit(agent, "task_unblock", f"task:{tid}")
    return f"Tarea {tid[:8]} desbloqueada -> in_progress por {agent}."


@mcp.tool()
async def tasks_inbox(agent: str = "", ctx: Context = None) -> str:
    """Tu bandeja: tareas asignadas a un agente (assignee), default = owner de tu key.
    Incluye status en {todo,in_progress,in_review,blocked,submitted} (excluye
    done/cancelled/deferred), ordenadas por prioridad. Marca 'ready' (todas sus deps en
    done) reusando la logica de tasks_ready.

    Las 'submitted' se listan desde 2026-08-10 con el marcador '⚠ ESPERA TU FIRMA' y el
    autor de la entrega: estaban invisibles para quien tiene que firmarlas. Para una tarea
    submitted el flag ready/deps no dice nada util (lo accionable es firmar o rechazar),
    asi que el marcador de firma va PRIMERO; el campo ready se sigue calculando igual.
    Aparecer aca NO habilita claim (claim sigue siendo solo todo|in_review|blocked).
    """
    owner, _k = _require_agent(ctx)
    who = agent or owner
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.status, t.priority, t.claimed_by, "
            "t.submitted_by, "
            "NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done') AS ready "
            "FROM tasks t WHERE t.assignee = %s AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND t.status IN ('todo','in_progress','in_review','blocked','submitted') "
            "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.created_at",
            (who,))
        rows = cur.fetchall()
    if not rows:
        return f"Inbox de '{who}' vacio (sin todo/in_progress/in_review/blocked/submitted)."

    def _fmt(r):
        # Precedencia visual: si espera TU firma, eso es lo accionable y va primero.
        # El flag ready/deps se conserva para el resto (no se rompe el campo, solo
        # no se muestra donde no significa nada: una submitted se firma o se rechaza).
        if r["status"] == "submitted":
            by = r.get("submitted_by") or "?"
            auto = " (AUTO-FIRMA: la entregaste vos)" if by == who else ""
            head = f"[submitted] ({r['priority']}) ⚠ ESPERA TU FIRMA — entregada por {by}{auto}"
        else:
            head = f"[{r['status']}] ({r['priority']}) {'ready' if r['ready'] else 'deps'}"
        return (f"{head} {r['title']} "
                f"— proj:{r['project'] or '-'} claim:{r['claimed_by'] or '-'} | id:{r['id'][:8]}")

    return "\n".join(_fmt(r) for r in rows)


@mcp.tool()
async def task_activity(task_id: str, ctx: Context = None) -> str:
    """Hilo cronologico de eventos de una tarea: created|comment|status_change|claim|
    handoff|review|block|unblock. Acepta id completo o prefijo."""
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_visible_id(cur, task_id)
        if err:
            return err
        cur.execute("SELECT agent, kind, from_status, to_status, body, created_at "
                    "FROM task_activity WHERE task_id::text = %s ORDER BY created_at", (tid,))
        rows = cur.fetchall()
    if not rows:
        return f"Sin actividad registrada para {tid[:8]}."
    out = [f"Actividad de {tid[:8]}:"]
    for r in rows:
        trans = ""
        if r["from_status"] or r["to_status"]:
            trans = f" [{r['from_status'] or '?'}->{r['to_status'] or '?'}]"
        body = f" — {r['body']}" if r["body"] else ""
        out.append(f"  {r['created_at']:%Y-%m-%d %H:%M} ({r['agent']}) {r['kind']}{trans}{body}")
    return chr(10).join(out)


# ---------------- CHECKPOINTS DE TAREAS (Sprint 3) ----------------
@mcp.tool()
async def checkpoint_save(task_id: str, step_index: int, label: str = "", state: str = "{}", ctx: Context = None) -> str:
    """Guarda un checkpoint de progreso en una tarea larga: un snapshot del paso 'step_index'
    para poder retomar tras un fallo. 'label' describe el paso; 'state' es un JSON (string) con
    el estado arbitrario del paso (se parsea a jsonb). Atribuido al owner de tu key. Acepta id
    completo o prefijo de tarea."""
    agent, _k = _require_agent(ctx)
    _deny = _rbac(agent, 'checkpoint_save')
    if _deny:
        return _deny
    try:
        parsed = json.loads(state) if (state or "").strip() else {}
    except (ValueError, TypeError) as e:
        return f"state no es JSON valido: {e}"
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_id(cur, task_id)
        if err:
            return err
        _immutable_check(cur, tid)
        cur.execute(
            "INSERT INTO task_checkpoints (task_id, step_index, label, state) "
            "VALUES (%s,%s,%s,%s) RETURNING id, created_at",
            (tid, step_index, label, json.dumps(parsed)))
        row = cur.fetchone()
        conn.commit()
    _audit(agent, "checkpoint_save", f"task:{tid} step={step_index}")
    return (f"Checkpoint guardado: {str(row['id'])[:8]} tarea={tid[:8]} step={step_index} "
            f"label={label or '-'} por {agent}")


@mcp.tool()
async def checkpoint_list(task_id: str, ctx: Context = None) -> str:
    """Lista los checkpoints de una tarea ordenados por step_index (y created_at). Muestra
    el estado JSON de cada paso para poder retomar. Acepta id completo o prefijo."""
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_visible_id(cur, task_id)
        if err:
            return err
        cur.execute(
            "SELECT id, step_index, label, state, created_at FROM task_checkpoints "
            "WHERE task_id::text = %s ORDER BY step_index, created_at", (tid,))
        rows = cur.fetchall()
    if not rows:
        return f"Sin checkpoints para {tid[:8]}."
    out = [f"Checkpoints de {tid[:8]}:"]
    for r in rows:
        st = json.dumps(r["state"], ensure_ascii=False)
        out.append(f"  [step {r['step_index']}] {r['label'] or '-'} "
                   f"({r['created_at']:%Y-%m-%d %H:%M}) id:{str(r['id'])[:8]} state={st}")
    return chr(10).join(out)


@mcp.tool()
async def checkpoint_latest(task_id: str, ctx: Context = None) -> str:
    """Devuelve el checkpoint mas avanzado de una tarea (mayor step_index; desempata por
    created_at mas reciente). Es 'donde retomar'. Acepta id completo o prefijo."""
    with _pg() as conn, conn.cursor() as cur:
        tid, err = _resolve_visible_id(cur, task_id)
        if err:
            return err
        cur.execute(
            "SELECT id, step_index, label, state, created_at FROM task_checkpoints "
            "WHERE task_id::text = %s ORDER BY step_index DESC, created_at DESC LIMIT 1", (tid,))
        r = cur.fetchone()
    if not r:
        return f"Sin checkpoints para {tid[:8]}."
    st = json.dumps(r["state"], ensure_ascii=False)
    return (f"Ultimo checkpoint de {tid[:8]}: step {r['step_index']} "
            f"[{r['label'] or '-'}] ({r['created_at']:%Y-%m-%d %H:%M}) id:{str(r['id'])[:8]}\n"
            f"state={st}")


# ---------------- BUS DE NOTIFICACIONES (Sprint 5) ----------------
@mcp.tool()
async def bus_publish(recipients: str, content: str, performative: str = "inform",
                      refs_task: str = "", conversation_id: str = "", ctx: Context = None) -> str:
    """Publica un mensaje en el bus de notificaciones del cerebro. El 'sender' se deriva
    SIEMPRE de tu API key (anti-spoof): cualquier remitente que mande el cliente se ignora.
    recipients: coma-separado (uno o varios agentes). performative: request|inform.
    refs_task: id de tarea referida (opcional). Crea el bus_message y una bus_delivery
    'pending' por cada recipient. Devuelve el id del mensaje y a quien se entrego.

    RETIRADO (orden de Otixe, 2026-09-15; lecturas retiradas en F9, 2026-10-05): el bus
    viejo ya no es un canal. Para escribir usa correspondencia_publicar. El tool
    NO se borra (un tool que desaparece del registro rompe a los clientes que lo tienen
    cacheado y no explica nada); responde el MISMO texto que el REST, con el codigo
    410. Reversible por BUS_LEGACY_PUBLISH (default: cerrado)."""
    sender, _k = _require_agent(ctx)
    if bus_legacy.publish_closed():
        # Se audita el intento rechazado con la identidad real. `_audit` abre su propia
        # conexion y jamas propaga, asi que esto no puede convertir un rechazo limpio en
        # un error opaco. audit_log no es bus_messages: el bus NO recibe ninguna fila.
        _audit(sender, "bus_publish",
               f"RECHAZADO {bus_legacy.STATUS}: bus viejo en solo-lectura")
        return bus_legacy.MESSAGE
    recs = [r.strip() for r in (recipients or "").split(",") if r.strip()]
    if not recs:
        return "Falta recipients (uno o varios, coma-separado)."
    perf = (performative or "inform").strip().lower()
    if perf not in ("request", "inform"):
        perf = "inform"
    with _pg() as conn, conn.cursor() as cur:
        # FIX 3 (2026-08-04, tarea 89fea402): refs_task aceptaba SOLO uuid completo.
        # Un prefijo (el formato que los propios listados imprimen, ej '89fea402')
        # llegaba crudo al INSERT y Postgres devolvia
        #   'invalid input syntax for type uuid' -> HTTP 500 incomprensible.
        # Ahora se resuelve con el mismo _resolve_id de las cadenas de tareas:
        # prefijo unico -> uuid; ambiguo o inexistente -> error CLARO, y NO se
        # publica el mensaje (una referencia rota es peor que un error visible).
        ref_tid = None
        if (refs_task or "").strip():
            ref_tid, err = _resolve_id(cur, refs_task.strip())
            if err:
                return f"refs_task invalido: {err}"
        cur.execute(
            "INSERT INTO bus_messages (sender, recipients, performative, content, conversation_id, refs_task) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (sender, recs, perf, content, conversation_id or "", ref_tid))
        mid = cur.fetchone()["id"]
        for r in recs:
            cur.execute(
                "INSERT INTO bus_deliveries (message_id, recipient, status) VALUES (%s,%s,'pending')",
                (mid, r))
        conn.commit()
    _audit(sender, "bus_publish", f"msg:{mid} -> {','.join(recs)}")
    return f"Mensaje {str(mid)[:8]} publicado por {sender} [{perf}] -> {', '.join(recs)}."


@mcp.tool()
async def bus_inbox(ctx: Context = None) -> str:
    """Tu bandeja del bus (METADATA + preview): mensajes con delivery pending/delivered
    dirigidos a VOS (recipient = owner de tu key), excluyendo los ya 'acked'. Al leerlos,
    los 'pending' pasan a 'delivered' (delivered_at=now()). Devuelve el id de mensaje para
    poder hacer bus_ack.

    RESPONDE '¿QUE TENGO PENDIENTE?', asi que NO devuelve cuerpos completos: por mensaje van
    remitente, performative, fecha, hilo, refs_task, el LARGO del cuerpo y un PREVIEW de 120
    chars — suficiente para triar. Para leer un mensaje completo: bus_thread /
    bus_group con include_content=true (esa lectura queda auditada). Medido 2026-08-18: la
    bandeja de saruman pesaba 463 kB (158 msgs) y truncaba la respuesta del tool; con
    metadata baja a ~29 kB (16x)."""
    agent, _k = _require_agent(ctx)
    if bus_legacy.read_closed(agent):
        return bus_legacy.READ_MESSAGE
    with _pg() as conn, conn.cursor() as cur:
        # El CUERPO no se selecciona: viajan su largo y un preview de PREVIEW_CHARS con los
        # espacios colapsados (una linea por mensaje). Que el content no salga de Postgres
        # es lo que hace que 'no filtrar' no dependa de acordarse de no imprimirlo.
        cur.execute(
            "SELECT d.id AS delivery_id, d.status, m.id AS message_id, m.sender, "
            "       m.performative, m.refs_task, m.ts, m.conversation_id, "
            "       length(m.content) AS content_len, "
            "       left(regexp_replace(m.content, '\\s+', ' ', 'g'), %s) AS preview "
            "FROM bus_deliveries d JOIN bus_messages m ON m.id = d.message_id "
            "WHERE d.recipient = %s AND d.status <> 'acked' "
            "ORDER BY m.ts", (_BUS_PREVIEW_CHARS, agent))
        rows = cur.fetchall()
        cur.execute(
            "UPDATE bus_deliveries SET status='delivered', delivered_at=now() "
            "WHERE recipient = %s AND status = 'pending'", (agent,))
        conn.commit()
    if not rows:
        return f"Inbox de {agent}: sin mensajes pendientes."
    total = sum(int(r["content_len"] or 0) for r in rows)
    out = [f"Inbox de {agent} ({len(rows)} mensaje(s), {total} chars de cuerpo NO incluidos "
           f"— preview {_BUS_PREVIEW_CHARS}c; el cuerpo se lee con "
           f"bus_thread/bus_group include_content=true):"]
    for r in rows:
        clen = int(r["content_len"] or 0)
        prev = (r["preview"] or "") + ("…" if clen > _BUS_PREVIEW_CHARS else "")
        ref = f" t:{str(r['refs_task'])[:8]}" if r["refs_task"] else ""
        cid = f" c:{r['conversation_id'][:8]}" if (r["conversation_id"] or "").strip() else ""
        out.append(
            f"  [{r['performative']}] {r['sender']} {prev} "
            f"| msg:{str(r['message_id'])[:8]} ({r['status']}) {clen}c{ref}{cid}")
    return chr(10).join(out)


@mcp.tool()
async def bus_ack(message_id: str, ctx: Context = None) -> str:
    """Confirma (ack) la recepcion/lectura consciente de un mensaje del bus dirigido a VOS.
    Marca tu bus_delivery como 'acked' (ack_at=now()); el mensaje deja de aparecer en tu
    bus_inbox. Acepta id completo o prefijo del mensaje."""
    agent, _k = _require_agent(ctx)
    if bus_legacy.read_closed(agent):
        return bus_legacy.READ_MESSAGE
    # FIX 2026-08-05: guarda de entrada vacia. Mismo patron que el bug de task_id:
    # con message_id='' el `LIKE '%'` marcaria como acked TODOS los mensajes
    # pendientes del agente de una. Esta acotado por `recipient` (el daño no cruza
    # agentes) pero igual es un ack masivo silencioso.
    if not (message_id or "").strip():
        return "falta message_id"
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE bus_deliveries SET status='acked', ack_at=now() "
            "WHERE recipient = %s AND (message_id::text = %s OR message_id::text LIKE %s) "
            "RETURNING message_id",
            (agent, message_id, message_id + "%"))
        rows = cur.fetchall()
        conn.commit()
    if not rows:
        return f"No habia mensaje pendiente {message_id[:8]} para {agent} (o ya estaba acked)."
    _audit(agent, "bus_ack", f"msg:{message_id}")
    return f"Mensaje {message_id[:8]} marcado como acked por {agent}."


# ---------------- CORRESPONDENCIA (bus v2; REST via gateway) ----------------
@mcp.tool()
async def correspondencia_publicar(destinatarios: str, asunto: str, contenido: str,
                                    prioridad: str = "medio", tipo: str = "inform",
                                    refs_task: str = "", ref_vault: str = "",
                                    ctx: Context = None) -> str:
    """Publica Correspondencia cuando necesitás un aviso ``inform`` cerrado o un
    ``request`` conversable. La identidad/remitente se deriva SIEMPRE de tu API key.
    ``destinatarios`` son buzones coma-separados: ``@saruman/saruman`` es el buzón
    canónico del líder, ``@saruman`` su alias compatible, ``@saruman/*`` todo el
    equipo y ``@saruman/lurtz`` un subagente; prioridad
    urgente|medio|bajo. Devuelve código corto, raíz, buzones resueltos y
    cantidad de entregas; ese código sirve para responder, OK o reacción expresiva."""
    agent, k = _require_agent(ctx)
    recs = [r.strip() for r in (destinatarios or "").split(",") if r.strip()]
    if not recs:
        return "Falta destinatarios (uno o varios buzones, coma-separados)."
    payload = {"destinatarios": recs, "asunto": asunto, "contenido": contenido,
               "prioridad": prioridad, "tipo": tipo,
               "refs_task": refs_task or None, "ref_vault": ref_vault or None}
    try:
        data = await _call("POST", "/correspondencia/publicar", k, payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "correspondencia_publicar", f"codigo:{data.get('codigo')}")
    return (f"Correspondencia {data.get('codigo')} publicada por {agent} "
            f"[{tipo}] -> {', '.join(data.get('destinatarios', []))}; "
            f"entregas={data.get('entregas')} abierto={data.get('abierto')}.")


def _corr_body_render(item: dict) -> str:
    """Rendiza el cuerpo de UN item de la bandeja. Si el gateway no mando el cuerpo
    (incluir_contenido=False) imprime el preview + el LARGO real: el lector ve que hay
    texto y CUANTO sin que el cuerpo pase por aca. El '…' y el contador son la senal de
    que falta contenido -- un preview cortado no se puede confundir con un mensaje corto.
    Mismo criterio que _bus_msg_line, para que las dos ventanas no divergan."""
    if item.get("contenido") is not None:
        return f"contenido={item.get('contenido')}"
    largo = int(item.get("contenido_len") or 0)
    prev = (item.get("contenido_preview") or "") + ("…" if item.get("contenido_truncado") else "")
    return f"preview={prev} ({largo}c)"


@mcp.tool()
async def correspondencia_bandeja(archivo: bool = False, incluir_contenido: bool = False,
                                  ctx: Context = None) -> str:
    """Lee TU bandeja de Correspondencia (identidad derivada de la key), no la de
    otro agente. Úsala para ver pendientes o ``archivo=True`` para archivados.
    Devuelve mensajes recibidos y enviados; en cada uno muestra reacciones activas
    (quién hizo OK o reaccionó expresivamente) y ``sin_reaccion`` para que el remitente
    sepa quién aún no acusó, sin mandar otro mensaje.

    EL CUERPO ES EXPLÍCITO (F11): por defecto NO trae los mensajes completos, sólo su
    LARGO y un preview de 120 chars. Para leer UNO entero usá ``correspondencia_leer``
    (acepta prefijo del código y audita la lectura). ``incluir_contenido=True`` trae
    TODOS los cuerpos y es lo que hacía esta tool siempre: medido 2026-09-18 en la
    bandeja de saruman daba 719.538 chars para 7 recibidos + 204 enviados **y la
    respuesta se TRUNCABA**, así que el mensaje que buscabas podía quedar fuera del
    corte. Si tu predicado es "qué tengo pendiente" o "de qué se trata", el preview
    alcanza; pedí el cuerpo cuando lo vas a leer."""
    agent, k = _require_agent(ctx)
    # ── LÍNEA VIEJA (F11) — se conserva marcada, NO se borra ──────────────────
    # path = "/correspondencia/bandeja" + ("?archivo=true" if archivo else "")
    # Traía el cuerpo SIEMPRE porque el default del REST es incluir_contenido=true (y
    # sigue siéndolo: el panel de Sinapsis renderiza `contenido` directo de ahí). El
    # flip del default vive ACÁ, en la ventana del agente, que es la que se truncaba.
    qs = [f"archivo={'true' if archivo else 'false'}"]
    if not incluir_contenido:  # opt-out explícito; el default del REST es traerlo
        qs.append("incluir_contenido=false")
    path = "/correspondencia/bandeja?" + "&".join(qs)
    try:
        data = await _call("GET", path, k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    cabecera = (f"Correspondencia de {agent} — {data.get('vista')} {data.get('count')} recibidos; "
                f"{data.get('enviados_count', 0)} enviados")
    if not data.get("contenido_incluido", True):
        cabecera += (f" [METADATA: cuerpos NO incluidos, preview "
                     f"{data.get('preview_chars') or _CORR_PREVIEW_CHARS}c; "
                     f"correspondencia_leer(codigo) para UNO entero]")
    lines = [cabecera + ":"]
    for label, items in (("RECIBIDO", data.get("items", [])),
                         ("ENVIADO", data.get("enviados", []))):
        for item in items:
            reacts = ", ".join(
                f"{r.get('buzon')}:{'OK' if r.get('gesto') == 'ok' else r.get('emoji')}"
                for r in item.get("reacciones", [])) or "ninguna"
            missing = ",".join(item.get("sin_reaccion", [])) or "nadie"
            lines.append(f"  {label} {item.get('codigo')} [{item.get('tipo')}] "
                         f"abierto={item.get('abierto')} {item.get('asunto')} | "
                         f"{_corr_body_render(item)} | "
                         f"reacciones={reacts} | sin_reaccion={missing}")
    return chr(10).join(lines)


@mcp.tool()
async def correspondencia_leer(codigo: str, ctx: Context = None) -> str:
    """Lee UN mensaje de Correspondencia COMPLETO por su código y marca leída tu
    entrega. Acepta PREFIJO del código (como todas las tools del tablero); si el prefijo
    matchea más de un mensaje te dice cuáles, no falla en genérico.

    Es la contraparte de ``correspondencia_bandeja``, que devuelve metadata + preview de
    120 chars: la bandeja te dice QUÉ hay, esto te da el cuerpo de UNO. Existe porque
    antes la única puerta al contenido era volcar la bandeja entera (719.538 chars
    medidos, con truncado) — traer 200 mensajes para leer 1.

    El gateway registra el efecto canónico de cuerpo entregado, con la cantidad de
    chars. MCP registra aparte el intento de este transporte como
    ``mcp_correspondencia_leer``: una lectura por comodidad deja de ser
    indistinguible de una necesaria, sin contar el mismo efecto dos veces.
    Funciona también sobre mensajes que MANDASTE vos (no hay entrega tuya que marcar;
    lo dice en la salida)."""
    agent, k = _require_agent(ctx)
    code = (codigo or "").strip().lower()
    if not code:
        return "Falta codigo (el de la bandeja; acepta prefijo)."
    try:
        data = await _call("POST", "/correspondencia/leer", k,
                           {"codigo": code, "via_lectura": "mcp"})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    m = data.get("mensaje") or {}
    # La fila canónica `correspondencia_leer` ya la escribe el middleware del
    # gateway tras entregar el cuerpo. Esta traza identifica sólo el transporte
    # MCP; ocurre exclusivamente después de un REST exitoso.
    _audit(agent, "mcp_correspondencia_leer",
           f"codigo:{data.get('codigo')} contenido_chars:{data.get('contenido_chars')} "
           f"marcado_leido:{data.get('marcado_leido')}")
    reacts = ", ".join(
        f"{r.get('buzon')}:{'OK' if r.get('gesto') == 'ok' else r.get('emoji')}"
        for r in m.get("reacciones", [])) or "ninguna"
    missing = ",".join(m.get("sin_reaccion", [])) or "nadie"
    adj = ", ".join(f"{a.get('nombre')}({a.get('tamano')}B)" for a in m.get("adjuntos", [])) or "ninguno"
    ref = f" refs_task={str(m.get('refs_task'))[:8]}" if m.get("refs_task") else ""
    marca = "leído" if data.get("marcado_leido") else (
        "sin entrega tuya (sos el remitente)" if data.get("es_remitente") else "sin entrega tuya")
    lines = [
        f"Correspondencia {data.get('codigo')} [{m.get('tipo')}] prioridad={m.get('prioridad')} "
        f"abierto={m.get('abierto')} raíz={m.get('raiz')}{ref}",
        f"  de {m.get('remitente')} · {m.get('creado_en')} · estado={data.get('estado')} ({marca})",
        f"  asunto: {m.get('asunto')}",
        f"  reacciones={reacts} | sin_reaccion={missing} | adjuntos={adj}",
        f"  audiencia_respuesta={', '.join(m.get('audiencia_respuesta') or []) or 'nadie'}",
        f"  ── cuerpo COMPLETO ({data.get('contenido_chars')} chars) "
        f"───────────────────────────",
        str(m.get("contenido") or ""),
    ]
    return chr(10).join(lines)


@mcp.tool()
async def correspondencia_responder(codigo: str, contenido: str,
                                     tipo: str = "inform", prioridad: str = "",
                                     refs_task: str = "", ref_vault: str = "",
                                     motivo_revision: str = "",
                                     ctx: Context = None) -> str:
    """Responde dentro de un hilo de Correspondencia y devuelve el nuevo código y
    todos sus destinatarios. Un ``inform`` cerrado rechaza respuesta directa. Si tenés
    una corrección (skill duplicada, error o hallazgo contradictorio), pasá
    ``motivo_revision`` OBLIGATORIO: primero solicita revisión, registra autor+motivo y
    abre el hilo; recién entonces responde. Todos los destinatarios originales ven la
    respuesta. No uses esto como acuse: para procesado usá correspondencia_ok sin emoji."""
    agent, k = _require_agent(ctx)
    if (motivo_revision or "").strip():
        try:
            await _call("POST", "/correspondencia/solicitar-revision", k,
                        {"codigo": codigo, "motivo": motivo_revision.strip()})
        except httpx.HTTPStatusError as e:
            return f"Error al solicitar revisión {e.response.status_code}: {_err_detail(e)}"
    payload = {"codigo": codigo, "contenido": contenido, "tipo": tipo,
               "prioridad": prioridad or None, "refs_task": refs_task or None,
               "ref_vault": ref_vault or None}
    try:
        data = await _call("POST", "/correspondencia/responder", k, payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "correspondencia_responder", f"codigo:{data.get('codigo')}")
    return (f"Respuesta {data.get('codigo')} en raíz {data.get('raiz')} -> "
            f"{', '.join(data.get('destinatarios', []))}; entregas={data.get('entregas')}.")


@mcp.tool()
async def correspondencia_ok(codigo: str, reaccion_expresiva: str = "",
                             retirar_reaccion: bool = False,
                             ctx: Context = None) -> str:
    """Dos gestos distintos, elegidos explícitamente. Sin ``reaccion_expresiva`` hace
    OK DE FLUJO: significa procesado y ARCHIVA tu entrega. Con un emoji en
    ``reaccion_expresiva`` hace una REACCIÓN EXPRESIVA: aporta tono y NO archiva;
    ``retirar_reaccion=True`` la retira marcando fecha, sin borrar historia.
    Ambos son idempotentes y NO crean mensajes. Devuelve cuál gesto ocurrió y si archiva."""
    agent, k = _require_agent(ctx)
    expressive = (reaccion_expresiva or "").strip()
    if expressive:
        path = "/correspondencia/reaccion"
        payload = {"codigo": codigo, "emoji": expressive,
                   "retirar": bool(retirar_reaccion)}
    else:
        if retirar_reaccion:
            return "retirar_reaccion requiere reaccion_expresiva; un OK de flujo no se retira."
        path = "/correspondencia/ok"
        payload = {"codigo": codigo, "emoji": "ok"}
    try:
        data = await _call("POST", path, k, payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if expressive:
        _audit(agent, "correspondencia_reaccion", f"codigo:{codigo} emoji:{expressive}")
        return (f"REACCIÓN EXPRESIVA {expressive!r} en {codigo}: NO archiva; "
                f"creadas={data.get('creadas')} retiradas={data.get('retiradas')}.")
    _audit(agent, "correspondencia_ok", f"codigo:{codigo}")
    return (f"OK DE FLUJO en {codigo}: procesado y ARCHIVADO; "
            f"archivados={data.get('archivados')}.")


@mcp.tool()
async def correspondencia_ack(codigos: str = "", raiz: str = "",
                              ctx: Context = None) -> str:
    """OK DE FLUJO masivo en Correspondencia: archiva tus entregas por códigos
    coma-separados y/o por raíz completa. Úsalo cuando ya procesaste varios mensajes;
    NO es aprobación ni reacción expresiva y NO crea mensajes. Idempotente: repetir
    devuelve cero archivados. Devuelve cantidad y códigos efectivamente archivados."""
    agent, k = _require_agent(ctx)
    codes = [c.strip() for c in (codigos or "").split(",") if c.strip()]
    if not codes and not (raiz or "").strip():
        return "Indica codigos y/o raiz."
    try:
        data = await _call("POST", "/correspondencia/ack", k,
                           {"codigos": codes, "raiz": (raiz or "").strip() or None,
                            "emoji": "ok"})
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "correspondencia_ack", f"archivados:{data.get('archivados')}")
    return (f"OK DE FLUJO masivo: archivados={data.get('archivados')}; "
            f"códigos={','.join(data.get('codigos', [])) or '-'}.")



@mcp.tool()
async def correspondencia_resumen(ctx: Context = None) -> str:
    """Teaser BARATO de TU Correspondencia (identidad derivada de la key): cuántas
    entregas no archivadas tenés, cuántas urgentes, cuántas urgentes ya escaladas por
    falta de atención, y hasta 3 códigos de urgentes SIN LEER para abrir primero. NO
    trae asunto ni contenido: es un aviso, no la bandeja — para leer los mensajes usá
    correspondencia_bandeja o correspondencia_digest. Si no tenés pendientes lo dice en
    una línea: el silencio es la señal correcta.

    Los conteos y los códigos NO son del mismo universo y la línea lo dice: los
    conteos son de entregas no archivadas (incluye leídas y no leídas) y los códigos
    son sólo de urgentes sin leer, que es lo que conviene abrir primero."""
    agent, k = _require_agent(ctx)
    try:
        data = await _call("GET", "/correspondencia/resumen", k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    pendientes = int(data.get("pendientes") or 0)
    if pendientes == 0:
        return f"Correspondencia de {agent}: sin pendientes."
    urgentes = int(data.get("urgentes") or 0)
    urgentes_sin_leer = int(data.get("urgentes_sin_leer") or 0)
    escalados = int(data.get("urgentes_escalados") or 0)
    partes = []
    if urgentes:
        # `sin leer` sólo se nombra cuando difiere del total: si son iguales, decirlo
        # dos veces sugiere dos poblaciones donde hay una.
        detalle_urgentes = (f"{urgentes_sin_leer} sin leer"
                            if urgentes_sin_leer != urgentes else "")
        extra = ", ".join(x for x in (detalle_urgentes,
                                      f"{escalados} escalado(s)" if escalados else "")
                          if x)
        partes.append(f"{urgentes} urgente(s)" + (f" ({extra})" if extra else ""))
    for etiqueta, clave in (("medio(s)", "medios"), ("bajo(s)", "bajos")):
        valor = int(data.get(clave) or 0)
        if valor:
            partes.append(f"{valor} {etiqueta}")
    detalle = f" ({'; '.join(partes)})" if partes else ""
    codigos = ", ".join(data.get("urgentes_sin_leer_codigos") or [])
    cola = f" Urgente(s) sin leer: {codigos}." if codigos else ""
    return (f"Correspondencia de {agent}: {pendientes} pendiente(s){detalle}.{cola} "
            f"Buzón {data.get('buzon')}; sin cuerpos (pull con correspondencia_bandeja "
            f"para el listado con preview, o correspondencia_leer para UNO entero).")


@mcp.tool()
async def correspondencia_digest(buzon: str = "", ctx: Context = None) -> str:
    """Digest de TU Correspondencia AGRUPADO POR PRIORIDAD (urgente/medio/bajo), una
    línea por mensaje con código, asunto, remitente y marca [escalado:causa] si un
    urgente tuyo lleva sin atender. NO expande contenido ni reacciones: para eso está
    correspondencia_bandeja. Es SIEMPRE pull — nadie te lo empuja ni te despierta.
    ``buzon`` sólo lo puede usar una identidad humana admin para mirar otro buzón."""
    agent, k = _require_agent(ctx)
    target = (buzon or "").strip()
    path = "/correspondencia/digest" + (f"?buzon={quote(target)}" if target else "")
    try:
        data = await _call("GET", path, k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    total = int(data.get("count") or 0)
    if total == 0:
        return f"Correspondencia de {agent} — digest de {data.get('buzon')}: sin pendientes."
    lines = [f"Correspondencia de {agent} — digest de {data.get('buzon')}: "
             f"{total} entrega(s) no archivada(s), agrupadas por prioridad "
             f"(sin contenido; pull con correspondencia_bandeja para el listado con "
             f"preview, o correspondencia_leer(codigo) para UNO entero):"]
    for priority in (data.get("prioridades") or ["urgente", "medio", "bajo"]):
        group = data.get(priority) or {}
        items = group.get("items") or []
        escalados = int(group.get("escalados") or 0)
        marca = f", {escalados} escalado(s)" if escalados else ""
        lines.append(f"  [{priority.upper()}] {int(group.get('count') or 0)}{marca}"
                     + ("" if items else " — ninguno"))
        for item in items:
            causa = item.get("escalado_causa")
            flag = f" [escalado:{causa}]" if item.get("escalada_en") else ""
            lines.append(f"    {item.get('codigo')}{flag} {item.get('asunto')} "
                         f"— de {item.get('remitente')} ({item.get('creado_en')})")
    return chr(10).join(lines)


# ---------------- GRAFO DE NOTAS (determinista) ----------------
@mcp.tool()
async def cerebro_graph_query(node: str, direction: str = "both", ctx: Context = None) -> str:
    """Consulta estructural del grafo de NOTAS del vault: backlinks/outlinks de una nota (determinista, no semantico).
    node: stem o path parcial de la nota (case-insensitive, ej 50 Agents/Saruman/... o solo el titulo).
    direction: out (a quien apunta) | in (quien la apunta) | both (default).
    Devuelve la nota resuelta + sus outlinks y backlinks. No alucina: lee el grafo real de [[wikilinks]]."""
    try:
        data = await _call("GET",
                           f"/graph/query?node={quote(node)}&direction={quote(direction or "both")}",
                           _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if data.get("error"):
        return f"No resuelto: {data.get('error')}"
    d = (direction or "both").lower()
    lines = [f"Nota: {data.get('resolved')}  ({data.get('path','')})"]
    if d in ("out", "both"):
        out = data.get("out", [])
        lines.append(f"Outlinks ({data.get("out_count", len(out))}): " + (", ".join(out) if out else "(ninguno)"))
    if d in ("in", "both"):
        inl = data.get("in", [])
        lines.append(f"Backlinks ({data.get("in_count", len(inl))}): " + (", ".join(inl) if inl else "(ninguno)"))
    return chr(10).join(lines)


@mcp.tool()
async def cerebro_semantic_graph_query(source: str, direction: str = "out", min_score: float = None, limit: int = 20, ctx: Context = None) -> str:
    """Consulta el grafo SEMANTICO del cerebro: notas relacionadas por SIMILITUD vectorial
    (coseno sobre embeddings), NO por [[wikilinks]]. Complementa cerebro_graph_query: este
    infiere relaciones que no estan escritas como links. Precomputado por batch (k-NN Qdrant).
    source: source_path de la nota (exacto o substring, ej '50 Agents/Saruman/...').
    direction: out (notas similares a esta) | in (notas para las que esta es similar) | both.
    min_score: umbral coseno [0..1] (default = el del rebuild). limit: max relacionadas por lado.
    Devuelve notas relacionadas con su score decreciente. Determinista, no alucina: lee semantic_edges."""
    from urllib.parse import quote as _q
    qs = f"/graph/semantic-query?source={_q(source)}&direction={_q(direction or 'out')}&limit={int(limit)}"
    if min_score is not None:
        qs += f"&min_score={float(min_score)}"
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if data.get("error"):
        return f"No resuelto: {data.get('error')}"
    d = (direction or "out").lower()
    lines = [f"Nota: {data.get('resolved')}  (grafo semantico, min_score={data.get('min_score')})"]
    if d in ("out", "both"):
        out = data.get("out", [])
        lines.append(f"Similares ({data.get('out_count', len(out))}):")
        for r in out:
            lines.append(f"  [{r['score']:.3f}] {r['source']}")
        if not out:
            lines.append("  (ninguna)")
    if d in ("in", "both"):
        inl = data.get("in", [])
        lines.append(f"Referida-por ({data.get('in_count', len(inl))}):")
        for r in inl:
            lines.append(f"  [{r['score']:.3f}] {r['source']}")
        if not inl:
            lines.append("  (ninguna)")
    return chr(10).join(lines)


@mcp.tool()
async def cerebro_agent_registry(action: str = "list", name: str = "", ctx: Context = None) -> str:
    """Registro declarativo de la flota (fuente de verdad = frontmatter de los .md del vault).
    action='list' (default) lista todos los agentes; action='get' + name devuelve uno.
    Determinista, no semantico. Devuelve modelo/tools/thinking/runtime por agente."""
    act = (action or "list").lower()
    try:
        if act == "get":
            if not name or not name.strip():
                return "Error: action='get' requiere name"
            data = await _call("GET", f"/agents/registry?name={quote(name)}", _read_key(ctx))
        else:
            data = await _call("GET", "/agents/registry", _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if isinstance(data, dict) and data.get("error"):
        return f"No resuelto: {data.get('error')}"
    if act == "get":
        d = data
        tools = d.get("tools") or []
        ext = d.get("extension_tools") or []
        runtime = d.get("runtime") or []
        lines = [
            f"Agente: {d.get('name')}",
            f"  role:      {d.get('role')}",
            f"  model:     {d.get('model')}",
            f"  thinking:  {d.get('thinking')}",
            f"  temperature: {d.get('temperature')}",
            f"  max_turns: {d.get('max_turns')}",
            f"  team:      {d.get('team')}",
            f"  runtime:   {', '.join(runtime) if runtime else '(ninguno)'}",
            f"  active:    {d.get('active')}",
            f"  tools ({len(tools)}): {', '.join(tools) if tools else '(ninguno)'}",
            f"  extension_tools ({len(ext)}): {', '.join(ext) if ext else '(ninguno)'}",
            f"  source:    {d.get('source_path')}",
        ]
        return chr(10).join(lines)
    ags = data.get("agents", []) if isinstance(data, dict) else []
    lines = [f"Flota: {data.get('count', len(ags))} agentes"]
    for a in ags:
        rt = ", ".join(a.get("runtime") or []) or "-"
        ntools = len(a.get("tools") or [])
        lines.append(f"  {a.get('name')} \u00b7 {a.get('role') or '-'} \u00b7 {a.get('model') or '-'} \u00b7 {rt} \u00b7 {ntools} tools")
    return chr(10).join(lines)


# ---------------- HARVEST LIBRE + TELEMETRIA DE VIDA (Gap 2a Cerebro v2) ----------------
@mcp.tool()
async def checkpoint_agent_save(session_ref: str = "", step_index: int = 0, label: str = "",
                                kind: str = "progress", state: str = "{}",
                                ctx: Context = None) -> str:
    """Guarda un checkpoint/harvest LIBRE (sin task_id): snapshot de progreso o cosecha
    atado a tu agente + 'session_ref' (id de invocacion/sesion, libre; '' si no aplica).
    Sirve para que un subagente sin tarea formal deje progreso o cosecha antes de morir.
    'kind' in progress|harvest|pitfall|landmark (default progress). 'state' es un JSON
    (string) que se parsea a jsonb. El agente se deriva SIEMPRE de tu key (anti-spoof)."""
    agent, k = _require_agent(ctx)
    try:
        parsed = json.loads(state) if (state or "").strip() else {}
    except (ValueError, TypeError) as e:
        return f"state no es JSON valido: {e}"
    if not isinstance(parsed, dict):
        return "state debe ser un objeto JSON (dict)"
    payload = {"session_ref": session_ref or "", "step_index": step_index,
               "label": label or "", "kind": (kind or "progress"), "state": parsed}
    try:
        data = await _call("POST", "/checkpoint/agent-save", k, payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    _audit(agent, "checkpoint_agent_save", f"{agent} kind={data.get('kind')} session={session_ref or '-'}")
    return (f"Checkpoint libre guardado: {str(data.get('id'))[:8]} agente={data.get('agent')} "
            f"kind={data.get('kind')} session={session_ref or '-'} step={step_index}")


@mcp.tool()
async def checkpoint_agent_latest(agent: str, session_ref: str = "", ctx: Context = None) -> str:
    """Ultimo checkpoint LIBRE de un agente. Si 'session_ref' dado, el ultimo de esa sesion;
    si no, el ultimo del agente por fecha. Devuelve metadata siempre; el 'state' completo
    solo si sos el propio agente o un lider (la metadata de vida no es sensible, el
    contenido si)."""
    k = _read_key(ctx)
    path = f"/checkpoint/agent-latest?agent={quote(agent)}"
    if session_ref:
        path += f"&session_ref={quote(session_ref)}"
    try:
        data = await _call("GET", path, k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if not data.get("found"):
        return f"Sin checkpoints libres para {agent}."
    lines = [
        f"Ultimo checkpoint libre de {data.get('agent')}: id:{str(data.get('id'))[:8]}",
        f"  kind={data.get('kind')} step={data.get('step_index')} "
        f"session={data.get('session_ref') or '-'} label={data.get('label') or '-'} "
        f"({data.get('created_at')})",
    ]
    if "state" in data:
        lines.append(f"  state={json.dumps(data['state'], ensure_ascii=False)}")
    else:
        lines.append("  state=(oculto: solo el propio agente o un lider lo ven)")
    return chr(10).join(lines)


@mcp.tool()
async def agent_liveness(agent: str, window_secs: int = 600, ctx: Context = None) -> str:
    """Telemetria de vida de un agente: cuando fue su ultimo checkpoint y su ultima cosecha
    (harvest/pitfall/landmark O cualquier memory_add), y si cosecho dentro de la ventana
    'window_secs'. Metadata no sensible: cualquier key valida puede consultarla. La usa un
    proceso externo (harness) para decidir si forzar la cosecha antes de matar a un subagente."""
    k = _read_key(ctx)
    try:
        data = await _call("GET",
                           f"/checkpoint/agent-liveness?agent={quote(agent)}&window_secs={int(window_secs)}",
                           k)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    return (f"Vida de {data.get('agent')} (ventana {data.get('window_secs')}s):\n"
            f"  ultimo checkpoint: {data.get('last_checkpoint_at') or '(nunca)'} "
            f"(hace {data.get('seconds_since_checkpoint')}s)\n"
            f"  ultima cosecha:    {data.get('last_harvest_at') or '(nunca)'} "
            f"(hace {data.get('seconds_since_harvest')}s)\n"
            f"  cosecho en ventana: {data.get('harvested_in_window')} "
            f"| checkpoints en ventana: {data.get('checkpoints_in_window')}")


# ==================== OLA 1 CEREBRO V2 (2026-07-21) ====================
@mcp.tool()
async def retrieval_eval_run(k: int = 5, neg_threshold: float = None,
                             ctx: Context = None) -> str:
    """Corre la EVALUACION de calidad de retrieval sobre el golden set del Cerebro y guarda
    el run. Para cada query curada corre memory_search y calcula precision@k, recall@k y MRR
    contra las fuentes esperadas. CARA -> SOLO LIDER (si tu key no es lider, 403). k = corte
    del top-k (default 5). Devuelve las metricas globales + el run_id persistido."""
    # TIMEOUT PROPIO (2026-08-17, saruman) + captura de TODA excepcion.
    # BUG MEDIDO, reportado por @monica (tarea f5bc6dc3): el cron del 16-ago recibio
    # "Error executing tool retrieval_eval_run: " con mensaje VACIO, mientras el run SI
    # se habia persistido correctamente (09:00:18 en retrieval_eval_runs). O sea reportaba
    # ERROR sobre una operacion EXITOSA -- el peor modo de falla de un canal de error,
    # porque no distingue "fallo" de "no supe leer la respuesta".
    # CAUSA RAIZ, dos defectos que se suman:
    #  1. El eval tarda MAS que HTTP_TIMEOUT (120s): journal del cron -> pedido 09:00:02,
    #     respuesta 09:03:44 = 222s. El request se cortaba por timeout del cliente.
    #  2. El `except` capturaba SOLO httpx.HTTPStatusError. Un ReadTimeout NO es esa clase,
    #     asi que escapaba y el framework de FastMCP la reportaba con str(e) VACIO
    #     (httpx.ReadTimeout("") no lleva mensaje).
    # ⇒ El fix son las dos cosas: timeout propio holgado Y captura amplia con causa legible.
    # El servidor sigue trabajando aunque el cliente corte, y de ahi venia la contradiccion
    # "error en el cliente / exito en la base": es la leccion de @rias, un TimeoutError NO
    # informa nada sobre el resultado de la operacion.
    try:
        # neg_threshold ahora es alcanzable desde MCP (2026-08-17). Antes la tool mandaba
        # SOLO {"k":k}, asi que el umbral de los controles negativos existia en el REST y
        # era invisible/inusable por MCP: "la capacidad existe pero solo por REST con body
        # explicito" (@saruman-grima, 15-ago). Se omite del payload si es None para no
        # pisar el default del servidor.
        _payload = {"k": int(k)}
        if neg_threshold is not None:
            _payload["neg_threshold"] = float(neg_threshold)
        data = await _call("POST", "/eval/retrieval-run", _read_key(ctx), _payload,
                           timeout=EVAL_TIMEOUT)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    except httpx.TimeoutException as e:
        # NO se afirma que fallo: el server pudo haber terminado igual. Se dice que NO SE SUPO.
        return (f"TIMEOUT del cliente a los {EVAL_TIMEOUT:.0f}s esperando /eval/retrieval-run "
                f"({type(e).__name__}). ⚠️ El run PUEDE haberse guardado igual: el servidor "
                f"sigue trabajando aunque el cliente corte. VERIFICAR con retrieval_eval_stats "
                f"antes de reintentar -- un reintento a ciegas duplica el run y gasta embeddings.")
    except Exception as e:
        # Cualquier otra: nunca devolver vacio. Un error sin causa no es un error, es ruido.
        return f"Error inesperado en retrieval_eval_run: {type(e).__name__}: {e or '(sin mensaje)'}"
    return chr(10).join([
        f"Eval retrieval @k={data.get('k')}  (n_queries={data.get('n_queries')}, "
        f"dur={data.get('duration')}s, run_id={data.get('run_id')})",
        f"  precision@k = {data.get('precision_at_k')}",
        f"  recall@k    = {data.get('recall_at_k')}",
        f"  MRR         = {data.get('mrr')}",
        f"  specificity = {data.get('specificity')} "
        f"({data.get('n_negative')} controles negativos)",
    ])


def _fmt_breakdown(bd) -> str:
    """Formatea {"positive":24,"negative":8} como "(24 positivas + 8 negativas)".
    Existe para que la diferencia entre evalset_size y n_queries sea autoexplicativa:
    n_queries cuenta SOLO las positivas (las negativas se miden con specificity)."""
    if not isinstance(bd, dict) or not bd:
        return ""
    pos, neg = bd.get("positive"), bd.get("negative")
    if pos is None and neg is None:
        return "(" + ", ".join(f"{v} {k}" for k, v in sorted(bd.items())) + ")"
    return f"({pos or 0} positivas + {neg or 0} negativas)"


def _fmt_procedencia(data) -> str:
    """Procedencia del promedio historico: sobre cuantos runs se calculo y cuantos se
    excluyeron por estar marcados invalid (tarea 2e479a7a). Devuelve p.ej.
    "(29/30 runs; 1 excluido por invalid)". Si el gateway es viejo y no manda los campos,
    devuelve "" -- degrada a la salida anterior en vez de mentir con un 0 inventado."""
    n_avg = data.get("runs_in_avg")
    n_exc = data.get("runs_excluded_invalid")
    if n_avg is None or n_exc is None:
        return ""
    total = data.get("total_runs")
    plural = "excluido" if n_exc == 1 else "excluidos"
    # El espacio separador va ACA y no en el f-string del llamador: si va alla, la via de
    # degradacion (helper -> "") imprime "Promedio historico : " con un espacio huerfano.
    return f" ({n_avg}/{total} runs; {n_exc} {plural} por invalid)"


@mcp.tool()
async def retrieval_eval_stats(ctx: Context = None) -> str:
    """Estado de las metricas de retrieval del Cerebro: ultimo run (precision@k/recall@k/MRR),
    agregados historicos y tamano del golden set. Barato (solo lee). Sirve para ver la linea
    base de calidad de busqueda y compararla tras cambios (grafo semantico, consolidacion)."""
    try:
        data = await _call("GET", "/eval/retrieval-stats", _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    lr = data.get("last_run")
    lines = [
        # El desglose va EN LA MISMA LINEA que el total: @monica leyo "32 queries" y
        # "n=24" en el mismo reporte y concluyo que faltaban 8. Sin el desglose, los dos
        # numeros que no cierran estan visibles y el que los reconcilia no.
        f"Golden set: {data.get('evalset_size')} queries "
        f"{_fmt_breakdown(data.get('evalset_breakdown'))} | "
        f"runs historicos: {data.get('total_runs')}",
        # PROCEDENCIA DEL PROMEDIO (2026-08-24, lurtz, tarea 2e479a7a). El promedio EXCLUYE
        # los runs marcados detail.invalid=true (ej. 09e0faca, corrido con el bug de
        # _load_evalset). Si el numero cambia y el reporte no dice por que, se cambia un
        # agregado sesgado por otro sin procedencia. Por eso la cuenta va SIEMPRE, incluso
        # en 0: un campo que solo aparece a veces no es verificable por el que lee.
        # Y por eso va aca y no solo en el REST: esta es la via que lee la flota -- el pitfall
        # de esta misma tarea es "por cada campo nuevo que se persista, verificar que aparece
        # en TODAS las vias de lectura".
        f"Promedio historico{_fmt_procedencia(data)}: "
        f"p={data.get('historical_avg_precision')} "
        f"r={data.get('historical_avg_recall')} mrr={data.get('historical_avg_mrr')} "
        f"spec={data.get('historical_avg_specificity')}",
    ]
    if lr:
        lines.append(
            f"Ultimo run ({lr.get('run_at')}, por {lr.get('triggered_by')}): "
            f"@k={lr.get('k')} p={lr.get('precision_at_k')} r={lr.get('recall_at_k')} "
            f"mrr={lr.get('mrr')} spec={lr.get('specificity')} "
            f"(n={lr.get('n_queries')} positivas + {lr.get('n_negative')} negativas)")
    else:
        lines.append("Sin runs todavia.")
    return chr(10).join(lines)


@mcp.tool()
async def consolidation_analyze(dup_threshold: float = None, decay_months: int = None,
                                top_n: int = None, max_chunks: int = None,
                                persist: bool = True, ctx: Context = None) -> str:
    """Corre el 'sueno' (SOLO LIDER): detecta near-dups, decay y densidad. Hace merge
    por identidad logica: no borra candidatos ni memoria. Ausencia en corrida limitada no
    invalida; ``stale`` solo aparece tras scan completo de decay y no es un dismissal.
    max_chunks acota el origen (bajo para pruebas; full es caro). persist=False = dry-run."""
    payload = {"dup_threshold": dup_threshold, "decay_months": decay_months,
               "top_n": top_n, "max_chunks": max_chunks, "persist": persist}
    payload = {kk: vv for kk, vv in payload.items() if vv is not None}
    try:
        data = await _call("POST", "/consolidation/analyze", _read_key(ctx), payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    c = data.get("counts", {})
    g = data.get("signal_density", {}).get("global", {})
    return chr(10).join([f"Consolidacion (sueno) corrida en {data.get('duration')}s:",
             f"  near-dup pairs: {c.get('near_dup_pairs')}",
             f"  decay candidates: {c.get('decay_candidates')}",
             f"  persisted (proposed): {c.get('persisted')}",
             f"  signal_ratio global: {g.get('signal_ratio')} (active={g.get('active')}, soft_deleted={g.get('soft_deleted')})"])


@mcp.tool()
async def consolidation_candidates(kind: str = "", status: str = "proposed",
                                   limit: int = 100, ctx: Context = None) -> str:
    """Lista candidatos de consolidacion (barato). kind: dedup | decay. status: proposed |
    applied | dismissed | stale. ``stale`` es opt-in: una corrida completa de decay ya no
    detecto la regla; no es decision humana ni borrado. Default proposed conserva triage."""
    qs = f"/consolidation/candidates?status={quote(status or 'proposed')}&limit={int(limit)}"
    if kind:
        qs += f"&kind={quote(kind)}"
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    items = data.get("candidates", [])
    lines = [f"Candidatos ({data.get('count', len(items))})  summary={data.get('summary')}:"]
    for it in items[:50]:
        if it["kind"] == "dedup":
            lines.append(f"  [dedup {it['score']:.3f}] {it['src_chunk'][:8]}~{(it['dst_chunk'] or '')[:8]}  {it['source_path']}")
        else:
            lines.append(f"  [decay] {it['src_chunk'][:8]}  {it['source_path']}")
    if not items:
        lines.append("  (ninguno)")
    return chr(10).join(lines)


def _bus_msg_line(m: dict) -> str:
    """Renderiza UN mensaje del replay (bus_thread / bus_group). Fuente unica del formato
    para que las dos vistas no puedan divergir.

    Si el gateway no mando el cuerpo (include_content=False) imprime el preview + el largo
    real: el lector ve que hay texto y CUANTO, sin que el cuerpo pase por aca. El sufijo
    '…' y el contador son la senal de que falta contenido — un preview cortado no se
    puede confundir con un mensaje corto."""
    ref = f" refs_task={m['refs_task'][:8]}" if m.get("refs_task") else ""
    head = (f"[{m['timestamp']}] ({m['sender']} -> {', '.join(m.get('recipients') or [])}) "
            f"[{m['performative']}]")
    if m.get("content") is not None:
        return f"{head} {m['content']}{ref}"
    clen = int(m.get("content_len") or 0)
    prev = (m.get("content_preview") or "") + ("…" if m.get("content_truncated") else "")
    return f"{head} {prev} ({clen}c){ref}"


@mcp.tool()
async def bus_thread(conversation_id: str, include_content: bool = False,
                     ctx: Context = None) -> str:
    """Replay de un hilo del bus entre pares: reconstruye TODOS los mensajes de una
    conversacion (conversation_id) ordenados cronologicamente, con sender, recipients,
    performative (request/inform), refs_task y timestamp. Es la vista 'historia del
    hilo de equipo', ortogonal a tu bus_inbox. Solo lectura, determinista.

    EL CUERPO ES EXPLICITO: por defecto NO trae los mensajes completos, solo su largo y un
    preview de 120 chars (alcanza para ubicar de que se hablo). Pedi include_content=True
    SOLO si necesitas el texto: esa lectura queda registrada en audit_log como deliberada.
    Si tu predicado es un count, una fecha o quien participo, el cuerpo no hace falta."""
    agent, _k = _require_agent(ctx)
    if bus_legacy.read_closed(agent):
        return bus_legacy.READ_MESSAGE
    qs = f"/bus/thread?conversation_id={quote(conversation_id)}"
    if include_content:  # se omite en el default para no pisar el del servidor
        qs += "&include_content=true"
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if data.get("error"):
        return f"No resuelto: {data.get('error')}"
    lines = [f"Hilo {data['conversation_id']} — {data['count']} mensaje(s), "
             f"participantes: {', '.join(data.get('participants', []))}"
             + ("" if data.get("content_included") else
                f" [METADATA: cuerpos NO incluidos, preview {_BUS_PREVIEW_CHARS}c; "
                f"include_content=True para leerlos]")]
    for m in data.get("messages", []):
        lines.append("  " + _bus_msg_line(m))
    return chr(10).join(lines)


@mcp.tool()
async def bus_threads(agent: str = "", include_unthreaded: bool = False,
                      project: str = "", since: str = "", until: str = "",
                      q: str = "", ctx: Context = None) -> str:
    """Lista hilos del bus (conversaciones con conversation_id) en los que participo un agente
    (sender o recipient); vacio = TODOS. Por hilo: count de mensajes, participantes y ultimo
    mensaje. Complementa bus_thread (que da el replay completo de uno).

    include_unthreaded=True suma las conversaciones IMPLICITAS: los mensajes publicados SIN
    conversation_id (la mayoria del bus), agrupados por conjunto canonico de participantes
    (ej. 'merlin|rias'). Se marcan con [sin hilo] y se leen con bus_group(participants=...).

    Filtros opcionales (AND): project (proyecto de la tarea referenciada; los mensajes sin
    refs_task no tienen proyecto y quedan fuera), since/until ('YYYY-MM-DD'; until incluye el
    dia completo) y q (coincidencia de TEXTO en contenido/id/participantes, NO semantica)."""
    principal, _k = _require_agent(ctx)
    if bus_legacy.read_closed(principal):
        return bus_legacy.READ_MESSAGE
    params = {}
    if (agent or "").strip():
        params["agent"] = agent.strip()
    # Se omite cada parametro cuando vale su default, para no pisar el comportamiento
    # historico del servidor (include_unthreaded=False => respuesta identica a la de antes).
    if include_unthreaded:
        params["include_unthreaded"] = "true"
    for k, v in (("project", project), ("since", since), ("until", until), ("q", q)):
        if (v or "").strip():
            params[k] = v.strip()
    qs = "/bus/threads"
    if params:
        qs += "?" + "&".join(f"{k}={quote(v)}" for k, v in params.items())
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    threads = data.get("threads", [])
    if not threads:
        return "Sin hilos" + (f" para {agent}" if agent else "") + "."
    lines = [f"Hilos ({data.get('count', len(threads))}):"]
    for t in threads:
        lm = t.get("last_message") or {}
        last = f" | ultimo: ({lm.get('sender')}) {lm.get('content','')[:60]}" if lm else ""
        # El backend decide el tipo con `kind` (solo viene con include_unthreaded): NO se
        # infiere de "tiene conversation_id?", porque el campo se guarda como cadena vacia.
        if t.get("kind") == "implicit":
            ident = f"[sin hilo] {t.get('participants_key')}"
        else:
            ident = t.get("conversation_id") or ""
        lines.append(f"  {ident} — {t['messages']} msg, "
                     f"[{', '.join(t.get('participants', []))}]{last}")
    return chr(10).join(lines)


@mcp.tool()
async def bus_group(participants: str, limit: int = 200, project: str = "",
                    since: str = "", until: str = "", q: str = "",
                    include_content: bool = False,
                    ctx: Context = None) -> str:
    """Replay de una conversacion IMPLICITA del bus: los mensajes publicados SIN
    conversation_id (o sea, sin hilo declarado) entre un conjunto de participantes,
    ordenados cronologicamente. `participants` es la clave canonica que devuelve
    bus_threads(include_unthreaded=True): los agentes deduplicados y ordenados
    alfabeticamente unidos por '|' (ej. 'merlin|rias').

    Es el equivalente de bus_thread para lo que NO tiene conversation_id. Solo devuelve
    mensajes sin hilo: los que ya pertenecen a un hilo se leen con bus_thread, nunca aca.
    Acepta los mismos filtros opcionales que bus_threads. Solo lectura, determinista.

    EL CUERPO ES EXPLICITO: por defecto metadata + preview de 120 chars.
    include_content=True trae los textos completos y AUDITA la lectura. Aca ocurrio la
    fuga que origino esta regla: una llamada para verificar `count == 12` devolvio los 12
    mensajes completos de una conversacion privada, que no hacian falta para el predicado."""
    agent, _k = _require_agent(ctx)
    if bus_legacy.read_closed(agent):
        return bus_legacy.READ_MESSAGE
    params = {"participants": participants}
    if limit != 200:  # se omite el default para no pisar el del servidor
        params["limit"] = str(limit)
    for k, v in (("project", project), ("since", since), ("until", until), ("q", q)):
        if (v or "").strip():
            params[k] = v.strip()
    if include_content:
        params["include_content"] = "true"
    qs = "/bus/group?" + "&".join(f"{k}={quote(v)}" for k, v in params.items())
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    if data.get("error"):
        return f"No resuelto: {data.get('error')}"
    lines = [f"Conversacion implicita (sin conversation_id) {data['participants_key']} — "
             f"{data['count']} mensaje(s), "
             f"participantes: {', '.join(data.get('participants', []))}"
             + ("" if data.get("content_included") else
                f" [METADATA: cuerpos NO incluidos, preview {_BUS_PREVIEW_CHARS}c; "
                f"include_content=True para leerlos]")]
    for m in data.get("messages", []):
        lines.append("  " + _bus_msg_line(m))
    return chr(10).join(lines)



@mcp.tool()
async def skill_mine(window_days: int = None, min_corroboration: int = None,
                     sim_threshold: float = None, max_items: int = None,
                     persist: bool = True, ctx: Context = None) -> str:
    """Destila los harvests de la flota en PROPUESTAS de skill (SOLO LIDER, es caro).
    Agrupa memorias (failure|insight|correction|tool-quirk) + checkpoints (pitfall|landmark)
    recientes por similitud y propone skill NUEVA o UPDATE cuando >= min_corroboration
    agentes DISTINTOS corroboran el patron (k>=2, anti prior-poisoning). SOLO PROPONE:
    jamas crea/edita skills. max_items acota para pruebas. persist=False = dry-run.
    Distinto de skill_add (crear una) y skill_search (buscar existentes)."""
    payload = {"window_days": window_days, "min_corroboration": min_corroboration,
               "sim_threshold": sim_threshold, "max_items": max_items, "persist": persist}
    payload = {kk: vv for kk, vv in payload.items() if vv is not None}
    try:
        data = await _call("POST", "/skills/mine", _read_key(ctx), payload)
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    c = data.get("counts", {})
    return chr(10).join([f"Skill-mining corrido en {data.get('duration')}s:",
             f"  senales: {c.get('signals')}  clusters: {data.get('clusters_found')}",
             f"  propuestas: {c.get('proposals')} (new={c.get('new')}, update={c.get('update')})",
             f"  persisted (proposed): {c.get('persisted')}  |  0 skills creadas (SOLO PROPONE)"])


@mcp.tool()
async def skill_proposals(status: str = "proposed", kind: str = "",
                          limit: int = 100, ctx: Context = None) -> str:
    """Lista las PROPUESTAS de skill generadas por skill_mine (barato, lee tabla).
    status: proposed | accepted | dismissed. kind: new | update (vacio = ambos).
    Cada propuesta trae corroboracion (# agentes distintos), evidencia (senales reales) y,
    si es update, la skill relacionada. Es lo que revisa Monica/Otixe antes de crear/editar.
    Distinto de skill_search (que busca skills YA existentes)."""
    qs = f"/skills/proposals?status={quote(status or 'proposed')}&limit={int(limit)}"
    if kind:
        qs += f"&kind={quote(kind)}"
    try:
        data = await _call("GET", qs, _read_key(ctx))
    except httpx.HTTPStatusError as e:
        return f"Error {e.response.status_code}: {_err_detail(e)}"
    items = data.get("proposals", [])
    lines = [f"Propuestas de skill ({data.get('count', len(items))})  summary={data.get('summary')}:"]
    for it in items[:50]:
        rel = f" -> {it.get('related_skill')}" if it.get("related_skill") else ""
        lines.append(f"  #{it['id']} [{it['kind']}{rel}] k={it['corroboration']} sim={it.get('avg_sim')}: {(it.get('title') or '')[:80]}")
    return chr(10).join(lines)


# Shared error translation for direct-SQL generic task writers. psycopg's
# connection context rolls back the failed transaction before this escapes.
def _install_generic_task_guard_errors():
    import functools
    for name in ("task_create", "task_update", "task_link", "task_unlink", "task_cancel",
                 "task_claim", "task_comment", "task_handoff", "task_review", "task_submit",
                 "task_block", "task_unblock", "checkpoint_save"):
        tool = mcp._tool_manager._tools[name]
        original = tool.fn

        @functools.wraps(original)
        async def guarded(*args, __original=original, **kwargs):
            try:
                return await __original(*args, **kwargs)
            except psycopg.Error as exc:
                if exc.sqlstate == "PZ101":
                    raise ToolError("Error 409: solicitud de aprobación: use el comando tipado; no se permiten cambios genéricos") from exc
                raise

        tool.fn = guarded


_install_generic_task_guard_errors()


# S3b: typed approval interface. Both MCP transports serve this exact registry.
# The flag is sampled at import/boot by common; no generic task behavior changes.
def _approval_check(ctx):
    if not approval.approval_enabled():
        raise ToolError('Error 503: approval_disabled')
    try:
        name, _key = _require_agent(ctx)
    except AuthError as exc:
        raise ToolError('Error 401: unauthorized') from exc
    return approval.trusted_principal(name, 'agent',
                                      'mcp_sse' if os.environ.get('MCP_TRANSPORT') == 'sse' else 'mcp')


def _approval_adapter():
    return approval.ApprovalCommands(lambda: psycopg.connect(autocommit=False, **PG))


def _approval_scope(data):
    if type(data) is not dict or set(data) != {'action_type', 'resource_kind', 'resource_id', 'constraints', 'scope_version'}:
        raise ValueError('invalid scope shape')
    constraints = data['constraints']
    if type(constraints) is not dict or any(type(v) is not str for k, v in constraints.items() if k != 'backup_refs'):
        raise ValueError('invalid constraints')
    if 'backup_refs' in constraints and (type(constraints['backup_refs']) is not list or
                                       any(type(v) is not str for v in constraints['backup_refs'])):
        raise ValueError('invalid backup_refs')
    return approval.Scope(**data)


def _approval_cas(version, digest):
    import re
    if type(version) is not int or version < 0 or type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('invalid CAS')


def _approval_title(title, description):
    if type(title) is not str or not 1 <= len(title) <= 500 or type(description) not in (str, type(None)) or (description is not None and len(description) > 10000):
        raise ValueError('invalid title/description')


def _approval_evidence(value):
    if type(value) is not str or not 1 <= len(value) <= 10000:
        raise ValueError('invalid evidence')


def _approval_result(fn):
    try:
        return fn()
    except approval.OutcomeUnknown as exc:
        raise ToolError('Error 503: outcome_unknown; resultado incierto; no repetir automáticamente; reconciliar según §5 (UUID propio en request/supersede, tupla de estado en los demás) antes de una nueva decisión') from exc
    except approval.ApprovalError as exc:
        raise ToolError(f'Error {exc.status}: {exc.code}') from exc
    except (ValueError, TypeError) as exc:
        raise ToolError('Error 422: approval_invalid_input') from exc
    except Exception as exc:
        raise ToolError('Error 500: approval_error') from exc


@mcp.tool()
def approval_request(request_id: str, title: str, scope: dict, description: str | None = None, ctx: Context = None) -> dict:
    """Create an agent-owned approval request using your own canonical UUID v4."""
    p = _approval_check(ctx)
    def run():
        _approval_title(title, description)
        return _approval_adapter().request(p, str(approval._uuid(request_id, True)), title, description, _approval_scope(scope))
    return _approval_result(run)


@mcp.tool()
def approval_get(id: str, ctx: Context = None) -> dict:
    """Get only your own approval request detail."""
    p = _approval_check(ctx)
    return _approval_result(lambda: _approval_adapter().get(p, str(approval._uuid(id))))


@mcp.tool()
def approval_mine(limit: int = 50, cursor: str | None = None, status: str | None = None, ctx: Context = None) -> dict:
    """List only your approval requests, optionally filtered by state."""
    p = _approval_check(ctx)
    def run():
        approval._limit(limit)
        if status is not None and (type(status) is not str or status not in approval._STATUSES):
            raise ValueError('invalid status')
        if cursor is not None:
            approval.decode_cursor(cursor)
        return _approval_adapter().list_mine(p, limit, cursor, status)
    return _approval_result(run)


@mcp.tool()
def approval_finish(id: str, expected_version: int, expected_hash: str, evidence: str, ctx: Context = None) -> dict:
    """Finish an approved request owned by your agent, with evidence."""
    p = _approval_check(ctx)
    def run():
        _approval_cas(expected_version, expected_hash); _approval_evidence(evidence)
        return _approval_adapter().finish(p, str(approval._uuid(id)), expected_version, expected_hash, evidence)
    return _approval_result(run)


@mcp.tool()
def approval_withdraw(id: str, expected_version: int, expected_hash: str, ctx: Context = None) -> dict:
    """Withdraw an owned request with current CAS."""
    p = _approval_check(ctx)
    def run():
        _approval_cas(expected_version, expected_hash)
        return _approval_adapter().withdraw(p, str(approval._uuid(id)), expected_version, expected_hash)
    return _approval_result(run)


@mcp.tool()
def approval_supersede(id: str, expected_version: int, expected_hash: str, new_request_id: str,
                       title: str, scope: dict, description: str | None = None, ctx: Context = None) -> dict:
    """Atomically replace your request using a new client-generated UUID v4."""
    p = _approval_check(ctx)
    def run():
        _approval_cas(expected_version, expected_hash); _approval_title(title, description)
        return _approval_adapter().supersede(p, str(approval._uuid(id)), expected_version, expected_hash,
                                             str(approval._uuid(new_request_id, True)), title, description, _approval_scope(scope))
    return _approval_result(run)


@mcp.tool()
def approval_record_effect(id: str, expected_version: int, expected_hash: str,
                           occurred_at: str, evidence: str, ctx: Context = None) -> dict:
    """Record an assertion of effect, not proof of physical execution."""
    p = _approval_check(ctx)
    def run():
        _approval_cas(expected_version, expected_hash); _approval_evidence(evidence)
        return _approval_adapter().record_effect(p, str(approval._uuid(id)), expected_version, expected_hash,
                                                  approval._time(occurred_at), evidence)
    return _approval_result(run)


@mcp.tool()
def approval_approve(id: str, expected_version: int, expected_hash: str, ctx: Context = None) -> dict:
    """Denied for agents: only Otixe may decide through BFF."""
    _approval_check(ctx)
    raise ToolError('Error 403: approval_forbidden; sólo Otixe humano en BFF')


@mcp.tool()
def approval_revoke(id: str, expected_version: int, expected_hash: str, reason: str, ctx: Context = None) -> dict:
    """Denied for agents: only Otixe may decide through BFF."""
    _approval_check(ctx)
    raise ToolError('Error 403: approval_forbidden; sólo Otixe humano en BFF')


@mcp.tool()
def approval_request_changes(id: str, expected_version: int, expected_hash: str, reason: str, ctx: Context = None) -> dict:
    """Denied for agents: only Otixe may decide through BFF."""
    _approval_check(ctx)
    raise ToolError('Error 403: approval_forbidden; sólo Otixe humano en BFF')


# FastMCP 1.28 generated models otherwise ignore extras. Install strict
# per-tool metadata as in the pre-existing S3a helper, never globally.
def _install_approval_boundary():
    import functools
    import inspect
    for name in ('approval_request', 'approval_get', 'approval_mine', 'approval_finish',
                 'approval_withdraw', 'approval_supersede', 'approval_record_effect',
                 'approval_approve', 'approval_revoke', 'approval_request_changes'):
        tool = mcp._tool_manager._tools[name]
        original = tool.fn
        signature = inspect.signature(original)
        fields = {key: (parameter.annotation, ... if parameter.default is inspect.Parameter.empty else parameter.default)
                  for key, parameter in signature.parameters.items() if key != 'ctx'}

        class _Arguments(ArgModelBase):
            model_config = ConfigDict(extra='forbid', strict=True)

        arguments = create_model(name + 'Arguments', __base__=_Arguments, **fields)

        class _Metadata(FuncMetadata):
            async def call_fn_with_arg_validation(self, *args, **kwargs):
                if not approval.approval_enabled():
                    raise ToolError('Error 503: approval_disabled')
                try:
                    return await super().call_fn_with_arg_validation(*args, **kwargs)
                except ValidationError as exc:
                    raise ToolError('Error 422: approval_invalid_input') from None

        tool.fn_metadata = _Metadata(
            arg_model=arguments, output_schema=tool.fn_metadata.output_schema,
            output_model=tool.fn_metadata.output_model, wrap_output=tool.fn_metadata.wrap_output,
        )
        tool.parameters = arguments.model_json_schema(by_alias=True)

        # §3.3 v7.10: handler whitelist cannot see extras after FastMCP
        # validation; effective protection is the ArgModel extra='forbid'.
        @functools.wraps(original)
        def guarded(*args, __original=original, __allowed=frozenset(fields), **kwargs):
            if not approval.approval_enabled():
                raise ToolError('Error 503: approval_disabled')
            if set(kwargs) - (__allowed | {'ctx'}):
                raise ToolError('Error 422: approval_invalid_input')
            return __original(*args, **kwargs)
        tool.fn = guarded


_install_approval_boundary()

if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
