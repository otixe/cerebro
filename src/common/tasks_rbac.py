"""tasks_rbac - FUENTE UNICA de la politica de tareas, compartida por los DOS transportes.

Por que existe este archivo
---------------------------
`gateway/` y `mcp/` son dos build contexts de Docker distintos (`build: ./gateway` y
`build: ./mcp`). Un `COPY ../common` es IMPOSIBLE: Docker no puede copiar fuera del
contexto. Esa restriccion -- y no el descuido -- es la razon por la que estas tres
logicas vivieron duplicadas. Se resuelve con `additional_contexts` de Compose
(verificado en Docker 29.6.1 / Compose v5.2.0): cada Dockerfile trae este archivo
con `COPY --from=common tasks_rbac.py ./` sin ampliar su contexto principal.

Regla de division (contrato con los callers)
--------------------------------------------
Aca vive la LOGICA y la POLITICA, en forma NEUTRAL al transporte: sin FastAPI, sin
MCP, sin excepciones de dominio, sin strings de tool. Solo stdlib -- tiene que poder
importarse tanto en la imagen del gateway como en la del MCP.

La TRADUCCION a la firma de cada transporte se queda en cada lado:
  · gateway: levanta `TaskError` / `HTTPException`
  · MCP:     devuelve strings de error / tuplas `(valor, error)`

Asi no hay que reescribir los ~30 call-sites de cada arbol.

NO agregar aca: acceso a la tabla `agents`, resolucion de identidad por key, ni nada
que dependa de una conexion propia. Los callers pasan el cursor que ya tienen.
"""

import re
import os


# ---------------------------------------------------------------------------
# 1) RBAC de tareas por execution_class
#
# `execution_class` = poder de EJECUCION. Es ORTOGONAL a `agents.role`
# (admin|member|guest), que es scope de VAULT.
#
# Anti-spoofing: la clase se deriva SIEMPRE de la key autenticada, JAMAS del body.
# Fail-closed: sin clase declarada => 'read-only'.
#
# Spec: 50 Agents/Saruman/Specs/spec-rbac-task-rest-h3-2026-07-28.md (secc.1-3)
# ---------------------------------------------------------------------------
EXECUTION_CLASSES = {"read-only", "writer", "executor", "auditor", "orchestrator"}

# Conjuntos de la matriz (secc.2 de la spec)
CLS_ALL = frozenset(EXECUTION_CLASSES)                                    # lectura/comment/checkpoint
CLS_WRITER_PLUS = frozenset({"writer", "executor", "auditor", "orchestrator"})  # claim/submit/block/...
CLS_REVIEW = frozenset({"auditor", "orchestrator"})                       # INVARIANTE: gate de QA
CLS_ORCH = frozenset({"orchestrator"})                                    # (reservado)
# GOBIERNO DEL TABLERO: auditor + orchestrator (POLITICA OTIXE 2026-08-05, textual:
# "el auditor debe poder crear, modificar, cambiar los estados a voluntad, ya que un
# auditor detecta tareas duplicadas y podria querer consolidar en una nueva").
# ANTES era solo orchestrator, y eso dejaba al auditor sin poder REGISTRAR lo que
# encontraba: el rol que existe para hallar problemas era el unico que no podia
# abrirlos. Lo pidieron @rudeus (su auditora Pursena no pudo abrir la tarea de su
# propio hallazgo y la tuvo que crear el, degradando la atribucion justo donde mas
# importa que sea del auditor) y el caso de NERO (auditor de Monica), que debe
# derivar al lider de cada grupo lo que no puede verificar -- y derivar exige crear.
CLS_GOBIERNO = frozenset({"auditor", "orchestrator"})

# Matriz operacion -> clases permitidas. FUENTE UNICA: la usan los endpoints REST,
# las tools del MCP y los tests.
# INVARIANTE: 'task_review' NUNCA incluye 'executor' (el ejecutor no se auto-aprueba);
# vale incluso en warn-only, para cuando se active el enforcement.
TASK_RBAC = {
    # lectura: cualquier key valida
    "task_list": CLS_ALL,
    "task_get": CLS_ALL,
    "task_deps": CLS_ALL,
    "tasks_ready": CLS_ALL,
    "tasks_inbox": CLS_ALL,
    "task_activity": CLS_ALL,
    "project_summary": CLS_ALL,
    "checkpoint_list": CLS_ALL,
    "checkpoint_latest": CLS_ALL,
    # colaboracion basica: cualquier key valida
    "task_comment": CLS_ALL,
    "checkpoint_save": CLS_ALL,
    # ciclo de vida del trabajo: writer o superior
    "task_claim": CLS_WRITER_PLUS,
    "task_submit": CLS_WRITER_PLUS,
    "task_block": CLS_WRITER_PLUS,
    "task_unblock": CLS_WRITER_PLUS,
    "task_handoff": CLS_WRITER_PLUS,
    # gate de QA: SOLO auditor/orchestrator (invariante de seguridad)
    "task_review": CLS_REVIEW,
    # gobierno del tablero: auditor + orchestrator (politica Otixe 2026-08-05)
    # El auditor consolida duplicados: para eso necesita CREAR la tarea nueva,
    # CANCELAR las redundantes, ENCADENAR dependencias y CAMBIAR estados.
    "task_create": CLS_GOBIERNO,
    "task_update": CLS_GOBIERNO,
    "task_cancel": CLS_GOBIERNO,
    "task_link": CLS_GOBIERNO,
    "task_unlink": CLS_GOBIERNO,
}

# NOTA SOBRE EL INVARIANTE QUE SIGUE EN PIE:
# Ampliar el gobierno del tablero al auditor NO debilita el invariante central de
# Harness 4.1 ("reviewer != submitter sobre la misma tarea"), porque ese control es
# RELACIONAL y vive aparte, en self_review_denied(): un auditor puede crear y
# cancelar tareas, pero sigue sin poder APROBAR una que el mismo entrego.
# Y 'executor' sigue FUERA de task_review: el ejecutor no se auto-aprueba.


def normalize_execution_class(cls):
    """Normaliza una clase cruda de DB a una del enum. FAIL-CLOSED:
    None / '' / desconocida -> 'read-only'. Nunca devuelve None."""
    cls = (cls or "").strip().lower()
    return cls if cls in EXECUTION_CLASSES else "read-only"


def is_allowed(cls, op):
    """True si la clase `cls` puede ejecutar la operacion `op`.
    Operacion no listada en la matriz -> True (no hay politica que la restrinja:
    los callers ya la trataban asi y cambiarlo seria un cambio de politica)."""
    allowed = TASK_RBAC.get(op)
    if allowed is None:
        return True
    return cls in allowed


def allowed_txt(op):
    """Clases permitidas de `op`, ordenadas y en texto -- para el mensaje de error."""
    return ",".join(sorted(TASK_RBAC.get(op, ())))


# ---------------------------------------------------------------------------
# 2) Resolucion de id de tarea por prefijo
#
# Nucleo NEUTRAL: devuelve `(id, status, detail)`. En exito -> (id, None, None).
# En error -> (None, status_http, detalle). Cada transporte traduce:
#   · gateway/tasks_core._resolve_id -> raise TaskError(status, detail)
#   · mcp/_resolve_id                -> return (None, detail)
# ---------------------------------------------------------------------------
_RESOLVE_SQL = ("SELECT id::text AS id, title FROM tasks "
                "WHERE id::text = %s OR id::text LIKE %s")
_PARENT_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8,35}$")
_PARENT_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Un único candado cooperativo para ESCRITURAS de jerarquía.  No se toma en
# lecturas ni en operaciones de tareas sin parent_id.  Es deliberadamente un
# número constante (y no ids ordenados): así no existe un orden de locks que
# pueda invertir una carrera A→B/B→A.
_PARENT_WRITE_LOCK_NAMESPACE = 0x43524250  # "CRBP"


def acquire_parent_write_guard(cur):
    """Serializa create-with-parent y reparent dentro de la transacción actual.

    PostgreSQL libera ``pg_advisory_xact_lock`` al commit/rollback, incluso si el
    caller levanta una excepción. Los callers usan el aislamiento por defecto de
    psycopg/PostgreSQL, READ COMMITTED; por eso las consultas de ancestros que
    siguen al lock ven el último commit anterior a su adquisición. No se ofrece
    garantía equivalente para una sesión configurada externamente REPEATABLE READ.
    """
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_PARENT_WRITE_LOCK_NAMESPACE,))


def resolve_id_core(cur, tid):
    """Resuelve un id completo o prefijo a un uuid unico (texto).

    Devuelve (id, None, None) | (None, status, detail).

    La guarda de entrada vacia es OBLIGATORIA y va ANTES del SQL: con tid=''
    el patron LIKE '%' matchea la tabla ENTERA, asi que sin guarda el error
    que sale es 'Ambiguo: <N> tareas' -- N = total del tablero -- en vez de
    'falta task_id'. Medido 2026-08-05: 994 de 994 filas."""
    if not (tid or "").strip():
        return None, 400, "falta task_id"
    cur.execute(_RESOLVE_SQL, (tid, tid + "%"))
    rows = cur.fetchall()
    if not rows:
        return None, 404, "No existe tarea '%s'." % tid
    if len(rows) > 1:
        return None, 409, ("Ambiguo: %d tareas coinciden con '%s'. Usa el id completo."
                           % (len(rows), tid))
    return rows[0]["id"], None, None


def resolve_parent_id_core(cur, parent_id, child_id=None, *, guard_held=False):
    """Resuelve un padre sin adivinar una intención contradictoria.

    Un UUID completo se busca *solo* por igualdad exacta: si no existe, no se
    degrada a prefijo. Un prefijo debe ser hexadecimal y tener al menos ocho
    caracteres; los demás textos (incluidos controles) se rechazan antes de
    consultar. El resultado siempre es el UUID canónico almacenado.

    Si ``child_id`` está presente, también impide auto-parent y ciclos de
    ancestros. La CTE guarda el camino ya visitado, por lo que una corrupción
    histórica preexistente no puede volverla infinita.
    """
    # El lock se toma ANTES de todo lookup de padre/ancestros y permanece hasta
    # que el caller haga INSERT/UPDATE + commit. `guard_held` permite al update
    # tomarlo aun antes de resolver el id del hijo, sin volver a bloquear.
    if not guard_held:
        acquire_parent_write_guard(cur)
    return _resolve_parent_id_after_guard(cur, parent_id, child_id)


def _resolve_parent_id_after_guard(cur, parent_id, child_id=None):
    if not isinstance(parent_id, str):
        return None, 400, "parent_id invalido"
    value = parent_id.strip()
    if not value:
        return None, 400, "parent_id invalido"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None, 400, "parent_id invalido"
    if _PARENT_UUID_RE.fullmatch(value):
        cur.execute("SELECT id::text AS id FROM tasks WHERE id::text = %s", (value.lower(),))
    elif _PARENT_PREFIX_RE.fullmatch(value):
        cur.execute("SELECT id::text AS id FROM tasks WHERE id::text LIKE %s", (value.lower() + "%",))
    else:
        return None, 400, "parent_id invalido"
    rows = cur.fetchall()
    if not rows:
        return None, 404, "parent_id no existe"
    if len(rows) > 1:
        return None, 422, "parent_id ambiguo"
    resolved = rows[0]["id"]
    if child_id is None:
        return resolved, None, None
    if resolved == str(child_id):
        return None, 409, "una tarea no puede ser su propia subtarea"
    cur.execute(
        "WITH RECURSIVE ancestors(id, parent_id, path) AS ("
        " SELECT id, parent_id, ARRAY[id] FROM tasks WHERE id::text = %s"
        " UNION ALL"
        " SELECT t.id, t.parent_id, a.path || t.id FROM tasks t"
        " JOIN ancestors a ON t.id = a.parent_id"
        " WHERE NOT t.id = ANY(a.path)"
        ") SELECT 1 FROM ancestors WHERE id::text = %s LIMIT 1",
        (resolved, str(child_id)),
    )
    if cur.fetchone():
        return None, 409, "parent_id crea un ciclo"
    return resolved, None, None


# ---------------------------------------------------------------------------
# 3) Gate de auto-firma (invariante relacional): quien ENTREGO no APRUEBA
#
# Tarea 26e7e659 (2026-08-05). Va DESPUES del lookup de la fila, no en la capa de
# authz: el RBAC por clase se evalua ANTES de conocer la tarea y no puede ver
# `submitted_by`. Un enum de execution_class no expresa una RELACION.
# Medicion que lo funda: 97 auto-firmas, TODAS de clase orchestrator (los 5
# lideres), CERO de auditores.
# ---------------------------------------------------------------------------
# Identidades EXENTAS del invariante "reviewer != submitter" (Otixe 2026-08-05:
# "a mi me dejas fuera de esa regla"). Es el humano: puede cerrar cualquier cosa
# sin que el sistema lo frene. Los 5 LIDERES **NO** estan exentos: exceptuarlos
# vaciaria el control, porque son justamente ellos los que lo violan.
SELF_REVIEW_EXEMPT = {n.strip().lower() for n in os.environ.get('SELF_REVIEW_EXEMPT_AGENTS', '').split(',') if n.strip()}


def self_review_denied(agent, submitted_by):
    """True si `agent` no puede auditar esta entrega porque el la entrego.
    submitted_by vacio/None -> False (no hay entrega registrada que atribuir)."""
    if (agent or "").lower() in SELF_REVIEW_EXEMPT:
        return False
    submitter = (submitted_by or "").strip()
    return bool(submitter) and submitter == agent


def self_review_message(agent, tid):
    """Mensaje de rechazo de auto-firma. Identico en los dos transportes."""
    return ("no podes auditar tu propia entrega: la tarea %s la entregaste "
            "vos ('%s'). Que la firme otro lider o un auditor." % (tid[:8], agent))
