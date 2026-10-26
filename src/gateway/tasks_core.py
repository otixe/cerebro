"""tasks_core - logica CANONICA del dominio de tareas del Cerebro.

CANONICO. Fase B: migrar `services/mcp/mcp_server.py` a este modulo (hoy mantiene
su propia copia de este mismo SQL; la unificacion mcp->tasks_core se hace con Otixe).
Mientras las dos copias coexistan, cualquier cambio de comportamiento debe aplicarse
a AMBAS o divergen (que es exactamente la deuda que este modulo viene a cerrar:
la spec 50 Agents/Saruman/Specs/spec-rbac-task-rest-h3-2026-07-28.md documenta 3
implementaciones independientes y divergidas sobre la misma tabla `tasks`).

Origen: portado 1:1 (mismo SQL) desde services/mcp/mcp_server.py (2026-07-28).
Diferencias deliberadas respecto de la copia MCP:
  - Devuelve **dicts** JSON-serializables, no strings de tool. La presentacion
    (texto para el LLM) queda del lado del caller.
  - Los errores de dominio son `TaskError(status, detail)` en vez de strings.
    El caller REST los mapea a HTTPException; el caller MCP los formatea a texto.
  - NO escribe `audit_log`: en el gateway lo hace el audit_middleware via
    `request.state.audit`. Auditar aca duplicaria filas.
  - NO hace authz: la RBAC (`_require_execution_class`) vive en la superficie
    (app.py). Este modulo asume que el caller YA autentico y autorizo.

Tablas: tasks, task_activity, task_deps, task_checkpoints, bus_messages,
bus_deliveries. DDL de task_checkpoints (verificado en el LXC 2026-07-28):
  id uuid PK, task_id uuid FK -> tasks(id), step_index int, label text,
  state jsonb, created_at timestamptz.
"""
import datetime as _dt
import json
import sanitize  # reparacion de surrogates huerfanos
import os
import uuid as _uuid

# FUENTE UNICA de la notificacion tablero->bus (common/, horneado por el Dockerfile)
import bus_notify
import agent_config
from datetime import date, datetime

import psycopg
from psycopg.rows import dict_row

import tasks_rbac

PG = dict(
    host=os.environ.get("POSTGRES_HOST", "postgres"),
    port=int(os.environ.get("POSTGRES_PORT_INTERNAL", "5432")),
    user=os.environ.get("POSTGRES_USER"),
    password=os.environ.get("POSTGRES_PASSWORD"),
    dbname=os.environ.get("POSTGRES_DB"),
)

# Sprint 5 (2026-07-17): lideres que reciben notificacion automatica en el bus
# cuando se les asigna una tarea (create / handoff). Paridad literal con mcp_server.py.
# DEPRECADA (2026-08-11, tarea 2929a26a): ya NO gobierna la notificacion del bus.
# La verdad de "quien es lider" la da `agents.is_lead` en la DB, resuelta por
# common/bus_notify.py. Esta copia hardcodeada EXCLUIA a monica, que es lider
# real (is_lead=true, team=monica, orchestrator), asi que sus 10 handoffs no
# avisaban a nadie. Se deja SIN USO (nadie la importa, verificado) en vez de
# borrarla, para no mezclar una limpieza con un cambio de comportamiento en el
# mismo pase: si hay que revertir el cableado, se revierte solo.
LEADERS = agent_config.leaders()  # legacy fallback only; notifications use DB rows

# Maquina de estados (SDD secc.4). needs_changes/failed = eventos, no estados.
VALID_STATUSES = {"todo", "in_progress", "blocked", "in_review", "submitted",
                  "done", "deferred", "cancelled"}
TRANSITIONS = {
    "todo": {"in_progress", "blocked", "deferred", "cancelled"},
    "in_progress": {"in_review", "submitted", "done", "blocked", "deferred", "cancelled"},
    "in_review": {"done", "in_progress", "cancelled"},
    "submitted": {"done", "in_progress", "cancelled"},
    "blocked": {"in_progress", "deferred", "cancelled"},
    "deferred": {"todo", "in_progress", "cancelled"},
    "done": {"todo", "in_progress"},
    "cancelled": set(),
}


class TaskError(Exception):
    """Error de dominio de tareas. `status` es el codigo HTTP sugerido:
    400 (input invalido), 404 (no existe), 409 (ambiguo / estado incompatible)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _pg():
    return psycopg.connect(row_factory=dict_row, **PG)


def can_transition(frm: str, to: str) -> bool:
    if frm == to:
        return True
    return to in TRANSITIONS.get(frm, set())


def _ser(v):
    """Normaliza a algo JSON-serializable (uuid/datetime/date -> str)."""
    if isinstance(v, _uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _row(r):
    return {k: _ser(v) for k, v in dict(r).items()} if r is not None else None


def _rows(rs):
    return [_row(r) for r in rs]


def _resolve_id(cur, tid):
    """Resuelve un id completo o prefijo a un uuid unico (texto). Levanta TaskError.

    La LOGICA vive en tasks_rbac.resolve_id_core (fuente unica compartida con el MCP,
    tarea c6b61bd1). Aca queda SOLO la traduccion a la firma de este transporte:
    excepcion de dominio en vez de tupla. No dupliques el SQL aca."""
    tid_ok, status, detail = tasks_rbac.resolve_id_core(cur, tid)
    if detail:
        raise TaskError(status, detail)
    return tid_ok


def _visible_id(cur, tid):
    """Resolve ordinary IDs without exposing approval IDs even by prefix."""
    if not (tid or "").strip():
        raise TaskError(400, "falta task_id")
    cur.execute("SELECT id::text AS id FROM tasks WHERE request_kind IS DISTINCT FROM 'approval_request' "
                "AND (id::text = %s OR id::text LIKE %s) LIMIT 2", (tid, tid + "%"))
    rows = cur.fetchall()
    if not rows:
        raise TaskError(404, f"No existe tarea '{tid}'.")
    if len(rows) > 1:
        raise TaskError(409, f"Ambiguo: varias tareas coinciden con '{tid}'. Usa el id completo.")
    return rows[0]["id"]


def _immutable_check(cur, *ids):
    """Lock both endpoints in deterministic order until the transaction ends."""
    cur.execute("SELECT id, request_kind FROM tasks WHERE id = ANY(%s::uuid[]) "
                "ORDER BY id FOR SHARE", (list(ids),))
    if any(row["request_kind"] == "approval_request" for row in cur.fetchall()):
        raise TaskError(409, "approval_request_immutable")


def _bus_notify_leader(cur, sender, recipient, content, refs_task):  # DEPRECADA: ver common/bus_notify.py
    # DEPRECADA (2026-08-11, tarea 2929a26a): sin callers. La escritura al bus
    # vive ahora en common/bus_notify.py, compartida por las TRES puertas
    # (gateway, MCP y el BFF de Sinapsis, que no notificaba en absoluto).
    """Sprint 5: bus_message performative='request' + bus_delivery pending para
    avisarle a un lider que se le asigno/derivo una tarea. Corre dentro del cursor
    de la operacion; el caller lo envuelve en try/except (un fallo del bus NUNCA
    debe romper el flujo de tareas)."""
    cur.execute(
        "INSERT INTO bus_messages (sender, recipients, performative, content, refs_task) "
        "VALUES (%s,%s,'request',%s,%s) RETURNING id",
        (sender, [recipient], content, refs_task))
    mid = cur.fetchone()["id"]
    cur.execute(
        "INSERT INTO bus_deliveries (message_id, recipient, status) VALUES (%s,%s,'pending')",
        (mid, recipient))
    return mid


def _notify_if_leader(actor, target, content, tid):
    """Hook bus fuera de la transaccion principal, best-effort (nunca propaga).

    DELEGA a common/bus_notify.py (FUENTE UNICA, tarea 2929a26a 2026-08-11).
    Antes esta funcion tenia su propia copia de la logica y el gate era
    `target in LEADERS`, con dos consecuencias medidas:
      - un SUBAGENTE nunca recibia aviso (rudeus-pursena: 16 handoffs, 0 avisos;
        grima: 2 y 0). Ahora se avisa al LIDER DE SU EQUIPO, que persiste entre
        sesiones -- un subagente muere al terminar su turno.
      - la constante hardcodeada EXCLUIA a monica, que es lider real
        (is_lead=true, team=monica, orchestrator). Ahora la verdad la da
        `agents.is_lead`, no una copia que se pudre.
    Se conserva el NOMBRE y la FIRMA para no tocar los 3 call sites.
    Devuelve bool por compatibilidad con el campo `bus_notified` de la respuesta.
    """
    return bool(bus_notify.notify_assignment(_pg, actor, target, content, tid))


# ---------------- CRUD / TABLERO ----------------
def create(agent, title, *, description="", project="", priority="medium",
           assignee="", subagent="", tags=None, parent_id="", due_date="",
           organization=""):
    """Crea una tarea (status inicial 'todo') + evento 'created'. Notifica al bus
    si el assignee es un lider distinto del creador.

    `organization` (2026-08-04) es OPCIONAL y retrocompatible. Se manda siempre
    como NULLIF: el trigger `trg_tasks_derive_organization` trata NULL, '' y
    columna-omitida de forma IDENTICA (verificado por INSERT en transaccion con
    ROLLBACK, 2026-08-04), por lo que no hace falta armar el INSERT condicional.
    Vacio -> el trigger deriva la org del `project`. Explicito -> gana lo pasado."""
    if not (title or "").strip():
        raise TaskError(400, "falta title")
    if isinstance(tags, str):
        taglist = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        taglist = [str(t).strip() for t in (tags or []) if str(t).strip()]
    with _pg() as conn, conn.cursor() as cur:
        # Padre opcional: UUID exacto o prefijo hex >=8. El helper no permite que
        # un UUID completo inexistente se "corrija" como prefijo.
        parent_resuelto = None
        if parent_id not in (None, ""):
            parent_resuelto, status, detail = tasks_rbac.resolve_parent_id_core(cur, parent_id)
            if detail:
                raise TaskError(status, detail)
            _immutable_check(cur, parent_resuelto)
        cur.execute(
            "INSERT INTO tasks (title, description, project, priority, assignee, subagent, creator, tags, parent_id, due_date, organization) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, organization",
            (title, description, project, priority, assignee, subagent, agent, taglist,
             parent_resuelto, due_date or None, organization or None))
        _new = cur.fetchone()
        tid = _new["id"]
        org_final = _new["organization"]
        cur.execute(
            "INSERT INTO task_activity (task_id, agent, kind, to_status, body) "
            "VALUES (%s,%s,'created','todo',%s)",
            (tid, agent, f"creada: {title}"))
        conn.commit()
    notified = _notify_if_leader(agent, assignee, f"Nueva tarea asignada: {title} (de {agent})", tid)
    return {"id": str(tid), "title": title, "status": "todo", "project": project,
            "organization": org_final,
            "priority": priority, "assignee": assignee, "subagent": subagent,
            "creator": agent, "tags": taglist, "bus_notified": notified}


def list_tasks(*, status="", project="", assignee="", include_done=False, limit=30,
               organization=""):
    """Lista tareas del tablero. Por defecto excluye done/cancelled.
    `organization` (2026-08-04) es un FILTRO opcional; vacio = todas."""
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
    out = _rows(rows)
    return {"tasks": out, "count": len(out)}


def get(task_id):
    """Detalle completo de una tarea (id o prefijo) + subtareas."""
    with _pg() as conn, conn.cursor() as cur:
        # FIX 2026-08-05: resolvedor UNIFICADO en vez de SQL inline. La copia inline
        # replicaba el patron LIKE sin pasar por la guarda comun; era una de las 6
        # copias que el dedup de c6b61bd1 no habia alcanzado (cubrio _resolve_id
        # pero no los SQL inline de get/update/list).
        tid = _visible_id(cur, task_id)
        cur.execute("SELECT * FROM tasks WHERE id::text = %s", (tid,))
        rows = cur.fetchall()
        if not rows:
            raise TaskError(404, f"No existe tarea '{task_id}'.")
        t = rows[0]
        cur.execute("SELECT id,title,status FROM tasks WHERE parent_id=%s AND request_kind IS DISTINCT FROM 'approval_request'", (t["id"],))
        subs = cur.fetchall()
    out = _row(t)
    out["subtasks"] = _rows(subs)
    return out


def update(agent, task_id, *, status="", assignee="", subagent="", priority="",
           project="", title="", description="", organization="", due_date="", parent_id=None):
    """Actualiza campos de una tarea. Registra 'status_change' si cambia el status
    y reporta las tareas que quedan desbloqueadas al pasarla a done.

    `organization` (2026-08-04) es opcional: si se pasa, se persiste explicita
    (gana sobre la derivacion del trigger). Si se pasa SOLO `project`, el trigger
    re-deriva la organization del nuevo project.

    `due_date` (2026-08-12) YYYY-MM-DD; "" = no tocar; "-"/"none"/"null"/"clear" = NULL
    (desagendar). Pedido de @rias: task_create la aceptaba y task_update no, asi que
    `parent_id` es ternario: omitir/JSON null no cambia el padre, ``"-"`` lo
    limpia explícitamente y un UUID/prefijo válido lo reasigna. Ningún otro
    alias textual significa NULL.
    """
    fields, params = [], []
    for col, val in [("status", status), ("assignee", assignee), ("subagent", subagent),
                     ("priority", priority), ("project", project), ("title", title),
                     ("description", description), ("organization", organization)]:
        if val:
            fields.append(f"{col} = %s"); params.append(val)
    # due_date (2026-08-12, saruman): va SEPARADO del loop de arriba porque necesita
    # tres estados y no dos. Para el resto de los campos "" significa "no tocar" y no
    # hay forma de expresar "borrar"; una fecha SI tiene que poder limpiarse (desagendar).
    # Centinelas "-"/"none"/"null" => NULL. Se valida el formato ANTES de llegar al
    # UPDATE: postgres rechazaria una fecha mal formada con un error de driver crudo,
    # que llega al agente como 500 en vez de un 400 que le diga QUE esta mal.
    parent_clear = parent_id == "-"
    if parent_id is not None:
        if parent_clear:
            fields.append("parent_id = NULL")
        else:
            # Se resuelve dentro de la misma transacción antes del UPDATE y de
            # cualquier actividad: no hay mutación parcial ante padre inválido.
            parent_resolved_marker = parent_id
    else:
        parent_resolved_marker = None
    if due_date:
        _dd = due_date.strip().lower()
        if _dd in ("-", "none", "null", "clear"):
            fields.append("due_date = NULL")
        else:
            try:
                _parsed = _dt.datetime.strptime(due_date.strip(), "%Y-%m-%d").date()
            except ValueError:
                raise TaskError(400, f"due_date invalida '{due_date}'; formato YYYY-MM-DD "
                                     f"(o '-' para limpiarla).")
            fields.append("due_date = %s"); params.append(_parsed.isoformat())
    if not fields and parent_id is None:
        raise TaskError(400, "Nada para actualizar (pasa al menos un campo).")
    if status and status not in VALID_STATUSES:
        raise TaskError(400, f"status invalido '{status}'; validos: {sorted(VALID_STATUSES)}")
    # FIX 2026-08-05: resolver a UN id ANTES de construir el UPDATE. Antes el WHERE
    # usaba `LIKE task_id + '%'`: con task_id='' eso es `LIKE '%'` y el UPDATE
    # alcanzaba TODO el tablero. Aca habia un rollback defensivo si matcheaba >1,
    # pero es mejor no llegar nunca a ejecutar el UPDATE: se resuelve primero.
    with _pg() as conn, conn.cursor() as cur:
        # The shared xact lock is intentionally before either child or ancestor
        # read. Under the default READ COMMITTED transaction, those reads see
        # the commit made by the preceding hierarchy writer.
        if parent_id is not None:
            tasks_rbac.acquire_parent_write_guard(cur)
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        if parent_id is not None and not parent_clear:
            resolved_parent, parent_status, parent_detail = tasks_rbac.resolve_parent_id_core(
                cur, parent_resolved_marker, child_id=tid, guard_held=True)
            if parent_detail:
                raise TaskError(parent_status, parent_detail)
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
        if not rows:
            conn.rollback()
            raise TaskError(404, f"No se encontro tarea con id '{task_id}'.")
        if len(rows) > 1:
            conn.rollback()
            raise TaskError(409, f"Ambiguo: {len(rows)} tareas coinciden con '{task_id}'. Usa el id completo.")
        if status:
            for _r in rows:
                _frm = prev.get(str(_r["id"]))
                if _frm != status:
                    cur.execute(
                        "INSERT INTO task_activity (task_id, agent, kind, from_status, to_status) "
                        "VALUES (%s,%s,'status_change',%s,%s)",
                        (_r["id"], agent, _frm, status))
        unblocked = []
        if status == "done":
            cur.execute(
                "SELECT DISTINCT t.id::text AS id, t.title, t.project, t.organization "
                "FROM tasks t JOIN task_deps d ON d.task_id = t.id "
                "WHERE d.depends_on_id = %s AND t.status = 'todo' AND t.request_kind IS DISTINCT FROM 'approval_request' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_deps d2 JOIN tasks dt ON dt.id = d2.depends_on_id "
                "  WHERE d2.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done')",
                (rows[0]["id"],))
            unblocked = cur.fetchall()
        conn.commit()
    r = rows[0]
    return {"id": str(r["id"]), "title": r["title"], "status": r["status"],
            "organization": r.get("organization"),
            "unblocked": _rows(unblocked)}


def cancel(agent, task_id):
    """Cancela una tarea de forma SOFT (status='cancelled'). NUNCA borra (Regla de Oro)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("UPDATE tasks SET status='cancelled', updated_at=now() WHERE id::text = %s "
                    "RETURNING title", (tid,))
        row = cur.fetchone()
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, to_status, body) "
                    "VALUES (%s,%s,'status_change','cancelled',%s)",
                    (tid, agent, "cancelled (soft)"))
        conn.commit()
    return {"id": tid, "status": "cancelled", "title": row["title"] if row else None}


def project_summary(project="", organization=""):
    """Conteo de tareas por (organizacion, proyecto, estado). Vacio = todo.

    Devuelve DOS vistas de los mismos datos:
      - `projects`: lista plana [{project, by_status, total}] — forma HISTORICA,
        intacta para no romper a ningun caller viejo.
      - `organizations`: jerarquia [{org_id, org_name, color, total,
        by_status, projects:[{project, by_status, total}]}] (2026-08-04).
    """
    # M1 (auditoria 2026-07-28): los fragmentos de WHERE son literales HARDCODEADOS
    # del fuente (allowlist), nunca texto del input. Los valores van parametrizados.
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
            "       o.name AS org_name, o.color AS org_color, o.sort_order AS org_sort "
            "FROM tasks t LEFT JOIN organizations o ON o.id = t.organization "
            f"{where} "
            "GROUP BY t.project, t.organization, t.status, o.name, o.color, o.sort_order "
            "ORDER BY o.sort_order NULLS LAST, t.organization, t.project", params)
        rows = cur.fetchall()
    agg = {}
    orgs = {}
    for r in rows:
        proj = r["project"] or "(sin proyecto)"
        agg.setdefault(proj, {})[r["status"]] = r["n"]
        oid = r["organization"] or "(sin organizacion)"
        o = orgs.setdefault(oid, {
            "org_id": r["organization"], "org_name": r["org_name"] or oid,
            "color": r["org_color"] or "", "sort_order": r["org_sort"],
            "by_status": {}, "_p": {}})
        o["by_status"][r["status"]] = o["by_status"].get(r["status"], 0) + r["n"]
        o["_p"].setdefault(proj, {})[r["status"]] = r["n"]
    projects = [{"project": p, "by_status": st, "total": sum(st.values())}
                for p, st in agg.items()]
    organizations = []
    for o in sorted(orgs.values(),
                    key=lambda x: (x["sort_order"] is None, x["sort_order"] or 0,
                                   x["org_name"] or "")):
        plist = [{"project": p, "by_status": st, "total": sum(st.values())}
                 for p, st in sorted(o.pop("_p").items())]
        o["projects"] = plist
        o["total"] = sum(o["by_status"].values())
        organizations.append(o)
    return {"projects": projects, "count": len(projects),
            "organizations": organizations, "org_count": len(organizations)}


# ---------------- CADENAS / DEPENDENCIAS ----------------
def link(agent, task_id, depends_on_id):
    """'task_id' queda BLOQUEADA POR 'depends_on_id'. Rechaza auto-dependencia y
    ciclo directo (si la relacion inversa ya existe)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        did = _resolve_id(cur, depends_on_id)
        _immutable_check(cur, tid, did)
        if tid == did:
            raise TaskError(400, "Auto-dependencia no permitida: una tarea no puede depender de si misma.")
        cur.execute("SELECT 1 FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s",
                    (did, tid))
        if cur.fetchone():
            raise TaskError(409, "Ciclo directo: la dependencia inversa ya existe. No se creo la relacion.")
        cur.execute("INSERT INTO task_deps (task_id, depends_on_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (tid, did))
        conn.commit()
    return {"task_id": tid, "depends_on_id": did, "linked": True}


def unlink(agent, task_id, depends_on_id):
    """Quita SOLO la relacion de dependencia. NO borra tareas."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        did = _resolve_id(cur, depends_on_id)
        _immutable_check(cur, tid, did)
        cur.execute("DELETE FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s "
                    "RETURNING task_id::text AS task_id", (tid, did))
        row = cur.fetchone()
        conn.commit()
    return {"task_id": tid, "depends_on_id": did, "removed": bool(row)}


def deps(task_id):
    """blocked_by (deps que deben estar done) + blocks (tareas que dependen de esta)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _visible_id(cur, task_id)
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
                    "WHERE d.task_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' ORDER BY dt.status, dt.title", (tid,))
        blocked_by = cur.fetchall()
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.task_id "
                    "WHERE d.depends_on_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' ORDER BY dt.status, dt.title", (tid,))
        blocks = cur.fetchall()
    return {"task_id": tid, "blocked_by": _rows(blocked_by), "blocks": _rows(blocks)}


def ready(*, project="", assignee="", organization=""):
    """Tareas en 'todo' LISTAS para empezar: sin deps, o con TODAS sus deps en done.
    `organization` (2026-08-04) es un FILTRO opcional; vacio = todas."""
    proj = project or None
    who = assignee or None
    org = organization or None
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.organization, t.priority, t.assignee "
            "FROM tasks t WHERE t.status = 'todo' AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND (%s::text IS NULL OR t.project = %s) "
            "AND (%s::text IS NULL OR t.organization = %s) "
            "AND (%s::text IS NULL OR t.assignee = %s) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done') "
            "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.project, t.title",
            (proj, proj, org, org, who, who))
        rows = cur.fetchall()
    out = _rows(rows)
    return {"tasks": out, "count": len(out)}


# ---------------- CICLO DE VIDA / COLABORACION ----------------
def claim(agent, task_id):
    """Toma la tarea: claimed_by=agent, status=in_progress. Solo desde
    todo|in_review|blocked y si no esta tomada por OTRO agente."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("SELECT status, claimed_by FROM tasks WHERE id::text = %s", (tid,))
        t = cur.fetchone()
        st = t["status"]
        cb = t["claimed_by"] or ""
        if st not in ("todo", "in_review", "blocked"):
            raise TaskError(409, f"No se puede tomar {tid[:8]}: status={st} (solo todo|in_review|blocked).")
        if cb and cb != agent:
            raise TaskError(409, f"Ya claimeada por '{cb}' (tarea {tid[:8]}). Pedi un handoff en vez de doble-tomar.")
        # El SELECT anterior solo decide el contrato y permisos; no reserva la fila.
        # Revalidar el snapshot en el UPDATE evita que dos lectores acepten el mismo claim.
        cur.execute("UPDATE tasks SET claimed_by=%s, claimed_at=now(), status='in_progress', "
                    "updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(claimed_by, '')=%s RETURNING id::text AS id",
                    (agent, tid, st, cb))
        if not cur.fetchone():
            raise TaskError(409, f"Conflicto concurrente al tomar {tid[:8]}; la tarea cambio despues de leerla.")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'claim',%s,'in_progress',%s)", (tid, agent, st, f"claimed by {agent}"))
        conn.commit()
    return {"id": tid, "claimed_by": agent, "from_status": st, "status": "in_progress"}


def comment(agent, task_id, body):
    """Comentario en el hilo de actividad (no cambia el status)."""
    if not (body or "").strip():
        raise TaskError(400, "Falta el body del comentario.")
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'comment',%s)", (tid, agent, body))
        conn.commit()
    return {"id": tid, "agent": agent, "kind": "comment"}


def handoff(agent, task_id, to_agent, note=""):
    """Deriva la tarea: assignee=to_agent y libera el claim. Notifica al bus si
    el destinatario es un lider."""
    if not (to_agent or "").strip():
        raise TaskError(400, "Falta to_agent (a quien derivar).")
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("UPDATE tasks SET assignee=%s, claimed_by='', updated_at=now() "
                    "WHERE id::text = %s", (to_agent, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'handoff',%s)", (tid, agent, note or f"handoff -> {to_agent}"))
        cur.execute("SELECT title FROM tasks WHERE id::text = %s", (tid,))
        _row_t = cur.fetchone()
        htitle = _row_t["title"] if _row_t else ""
        conn.commit()
    notified = _notify_if_leader(agent, to_agent,
                                f"Tarea recibida por handoff: {htitle} (de {agent})", tid)
    return {"id": tid, "from": agent, "to": to_agent, "claim_released": True,
            "bus_notified": notified}


# Identidades EXENTAS del invariante "reviewer != submitter": REEXPORT de la fuente
# unica (tasks_rbac.SELF_REVIEW_EXEMPT, tarea c6b61bd1). Estaba duplicado con el MCP.
# Editar la politica en common/tasks_rbac.py, NO aca.
SELF_REVIEW_EXEMPT = tasks_rbac.SELF_REVIEW_EXEMPT


def review(agent, task_id, verdict, note=""):
    """Gate de QA. Solo sobre in_review|submitted. pass -> done;
    needs_changes -> in_progress.

    INVARIANTE de seguridad (spec secc.2): esta operacion es de {auditor,orchestrator}.
    El chequeo de CLASE no vive aca (este modulo no hace authz) sino en la superficie
    (_require_execution_class en app.py) y debe valer en TODAS las superficies.

    INVARIANTE RELACIONAL (2026-08-05, tarea 26e7e659): quien ENTREGO una tarea no
    puede APROBARLA. Este chequeo SI vive aca, y no en la capa de authz, por una
    razon estructural: el RBAC por clase se evalua ANTES del lookup de la tarea y no
    conoce la fila, mientras que este necesita `submitted_by` ⇒ va DESPUES del
    lookup. Un enum de execution_class no puede expresar una relacion.
    Medicion que lo funda: 97 auto-firmas, TODAS de clase orchestrator (los 5
    lideres), CERO de auditores."""
    v = (verdict or "").strip().lower()
    if v not in ("pass", "needs_changes"):
        raise TaskError(400, "verdict invalido: usa 'pass' o 'needs_changes'.")
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("SELECT status, submitted_by, claimed_by FROM tasks WHERE id::text = %s", (tid,))
        _row = cur.fetchone()
        st = _row["status"]
        if st not in ("in_review", "submitted"):
            raise TaskError(409, f"Solo se revisa en in_review/submitted (status actual={st}, tarea {tid[:8]}).")
        # --- Gate relacional: quien entrego no firma (fuente unica: tasks_rbac) ---
        if tasks_rbac.self_review_denied(agent, _row.get("submitted_by")):
            raise TaskError(403, tasks_rbac.self_review_message(agent, tid))
        to = "done" if v == "pass" else "in_progress"
        # submitted_by/claimed_by participated in the reviewed authorization snapshot;
        # include them so a re-entry into the same status cannot approve another row version.
        submitted_by = _row.get("submitted_by") or ""
        claimed_by = _row.get("claimed_by") or ""
        cur.execute("UPDATE tasks SET status=%s, updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(submitted_by, '')=%s AND COALESCE(claimed_by, '')=%s "
                    "RETURNING id::text AS id", (to, tid, st, submitted_by, claimed_by))
        if not cur.fetchone():
            raise TaskError(409, f"Conflicto concurrente al revisar {tid[:8]}; la tarea cambio despues de leerla.")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'review',%s,%s,%s)", (tid, agent, st, to, f"{v}: {note}"))
        conn.commit()
    return {"id": tid, "reviewer": agent, "verdict": v, "from_status": st, "status": to}


def submit(agent, task_id, note=""):
    """in_progress -> submitted: el executor entrega para auditoria del gate.

    Registra `submitted_by` (2026-08-05): es el dato que le falta a la plataforma
    para poder evaluar el invariante relacional en `review`. Sin esto el sistema no
    tiene con que saber quien entrego.

    Notifica al bus (2026-08-10) al ASSIGNEE, que es quien tiene que FIRMAR: es el
    analogo exacto de lo que ya hacian `create` (avisa al assignee) y `handoff` (avisa
    al to_agent) con el MISMO helper `_notify_if_leader`. Antes de esto una entrega no
    avisaba a nadie: la tarea quedaba esperando una firma en silencio.
    Solo notifica si el status CAMBIO (st != 'submitted'): can_transition admite
    frm==to, asi que un re-submit sin esta guarda mandaria un mensaje duplicado."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("SELECT status, assignee, title FROM tasks WHERE id::text = %s", (tid,))
        _row_s = cur.fetchone()
        st = _row_s["status"]
        asg = _row_s["assignee"] or ""
        stitle = _row_s["title"] or ""
        if not can_transition(st, "submitted"):
            raise TaskError(409, f"No se puede pasar a submitted desde status={st} (tarea {tid[:8]}).")
        cur.execute("UPDATE tasks SET status='submitted', submitted_by=%s, updated_at=now() "
                    "WHERE id::text = %s", (agent, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'status_change',%s,'submitted',%s)", (tid, agent, st, note))
        conn.commit()
    notified = False
    if st != "submitted":
        notified = _notify_if_leader(agent, asg,
                                    f"Tarea entregada para tu firma: {stitle} (de {agent})", tid)
    return {"id": tid, "agent": agent, "submitted_by": agent, "from_status": st,
            "status": "submitted", "bus_notified": notified}


def block(agent, task_id, reason=""):
    """Marca blocked con motivo. Rechaza transiciones invalidas (SDD secc.4)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("SELECT status FROM tasks WHERE id::text = %s", (tid,))
        st = cur.fetchone()["status"]
        if not can_transition(st, "blocked"):
            raise TaskError(409, f"No se puede bloquear desde status={st} (tarea {tid[:8]}).")
        cur.execute("UPDATE tasks SET status='blocked', blocked_reason=%s, updated_at=now() "
                    "WHERE id::text = %s", (reason, tid))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'block',%s,'blocked',%s)", (tid, agent, st, reason))
        conn.commit()
    return {"id": tid, "agent": agent, "from_status": st, "status": "blocked",
            "blocked_reason": reason}


def unblock(agent, task_id, note=""):
    """blocked -> in_progress, limpia blocked_reason."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute("SELECT status FROM tasks WHERE id::text = %s", (tid,))
        st = cur.fetchone()["status"]
        if st != "blocked":
            raise TaskError(409, f"No esta blocked (status={st}, tarea {tid[:8]}).")
        cur.execute("UPDATE tasks SET status='in_progress', blocked_reason='', updated_at=now() "
                    "WHERE id::text = %s", (tid,))
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'unblock','blocked','in_progress',%s)", (tid, agent, note))
        conn.commit()
    return {"id": tid, "agent": agent, "from_status": "blocked", "status": "in_progress"}


def inbox(agent, *, organization=""):
    """Tareas asignadas a `agent` en {todo,in_progress,in_review,blocked,submitted},
    con flag 'ready' (todas sus deps en done). `organization` filtra opcionalmente
    (2026-08-04).

    'submitted' se INCLUYE desde 2026-08-10: una tarea entregada para que alguien la
    firme estaba INVISIBLE en la bandeja de quien tiene que firmarla — justo el estado
    que existe para pedir una firma era el que la bandeja no listaba (10 tareas mudas al
    momento del fix). Se agrega `submitted_by` al SELECT porque es el dato que necesita
    quien firma (y si submitted_by == assignee es el limbo de auto-firma).
    CAMBIO ADITIVO: no se renombra ni se quita ningun campo del JSON (hay consumidores).
    OJO: aparecer en la bandeja NO habilita claim — `claim` sigue aceptando solo
    todo|in_review|blocked, a proposito (son cosas distintas).
    """
    if not (agent or "").strip():
        raise TaskError(400, "falta agent")
    org = organization or None
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.organization, t.status, t.priority, t.claimed_by, "
            "t.submitted_by, "
            "NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' AND dt.status <> 'done') AS ready "
            "FROM tasks t WHERE t.assignee = %s AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND (%s::text IS NULL OR t.organization = %s) "
            "AND t.status IN ('todo','in_progress','in_review','blocked','submitted') "
            "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.created_at",
            (agent, org, org))
        rows = cur.fetchall()
    out = _rows(rows)
    return {"agent": agent, "tasks": out, "count": len(out)}


def activity(task_id):
    """Hilo cronologico de eventos: created|comment|status_change|claim|handoff|
    review|block|unblock."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _visible_id(cur, task_id)
        cur.execute("SELECT agent, kind, from_status, to_status, body, created_at "
                    "FROM task_activity WHERE task_id::text = %s ORDER BY created_at", (tid,))
        rows = cur.fetchall()
    out = _rows(rows)
    return {"task_id": tid, "events": out, "count": len(out)}


# ---------------- CHECKPOINTS DE TAREAS (Sprint 3) ----------------
# DDL confirmado: task_checkpoints(id uuid, task_id uuid FK, step_index int,
# label text, state jsonb, created_at timestamptz).
def checkpoint_save(agent, task_id, step_index, label="", state=None):
    """Snapshot del paso `step_index` de una tarea larga. `state` acepta dict o
    string JSON (se persiste como jsonb)."""
    if isinstance(state, str):
        try:
            parsed = json.loads(state) if state.strip() else {}
        except (ValueError, TypeError) as e:
            raise TaskError(400, f"state no es JSON valido: {e}")
    else:
        parsed = state or {}
    # SURROGATES HUERFANOS (hermano de 30e09724, desplegado en app.py el 2026-09-07):
    # `task_checkpoints.state` es jsonb y Postgres es el UNICO que valida pares surrogate
    # (RFC 8259) => un surrogate huerfano revienta el INSERT y se pierde el checkpoint
    # COMPLETO. Toda la cadena Python es permisiva: json.loads/json.dumps solo re-escapan.
    # `label` es `text`: NO revienta, pero se guardaria roto, asi que tambien se repara.
    # Se reusa la MISMA funcion ya auditada y en produccion: repara al HUERFANO y CONSERVA
    # los pares validos (un emoji bien formado no se toca; verificado por digest).
    # Estado al aplicar: 0 fallos historicos en audit_log por esta ruta => era LATENTE.
    ck_state, _surr_state = sanitize.repair_surrogates_deep(parsed)
    ck_label, _surr_label = sanitize.repair_surrogates(label)
    with _pg() as conn, conn.cursor() as cur:
        tid = _resolve_id(cur, task_id)
        _immutable_check(cur, tid)
        cur.execute(
            "INSERT INTO task_checkpoints (task_id, step_index, label, state) "
            "VALUES (%s,%s,%s,%s) RETURNING id, created_at",
            (tid, step_index, ck_label, json.dumps(ck_state)))
        row = cur.fetchone()
        conn.commit()
    return {"id": str(row["id"]), "task_id": tid, "step_index": step_index,
            "label": ck_label, "agent": agent, "created_at": _ser(row["created_at"])}


def checkpoint_list(task_id):
    """Checkpoints de una tarea ordenados por step_index (y created_at)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _visible_id(cur, task_id)
        cur.execute(
            "SELECT id, step_index, label, state, created_at FROM task_checkpoints "
            "WHERE task_id::text = %s ORDER BY step_index, created_at", (tid,))
        rows = cur.fetchall()
    out = _rows(rows)
    return {"task_id": tid, "checkpoints": out, "count": len(out)}


def checkpoint_latest(task_id):
    """Checkpoint mas avanzado (mayor step_index; desempata por created_at)."""
    with _pg() as conn, conn.cursor() as cur:
        tid = _visible_id(cur, task_id)
        cur.execute(
            "SELECT id, step_index, label, state, created_at FROM task_checkpoints "
            "WHERE task_id::text = %s ORDER BY step_index DESC, created_at DESC LIMIT 1", (tid,))
        r = cur.fetchone()
    if not r:
        return {"task_id": tid, "found": False}
    out = _row(r)
    out["task_id"] = tid
    out["found"] = True
    return out


# ---------------- ORGANIZACIONES / PROYECTOS (jerarquia org -> proyecto -> tarea) ----------------
# Agregado 2026-08-04 (tarea 5ae5afd9). El esquema (organizations, projects,
# tasks.organization + trigger trg_tasks_derive_organization) YA estaba migrado y
# verificado en prod ANTES de este codigo; aca solo se EXPONE.
#
# Regla de Oro #1: NO existe borrado. Archivar un proyecto = status='archived'.
# Desactivar una organizacion = active=false. Nunca DELETE.
_PROJECT_STATUSES = {"active", "archived", "paused"}


def organizations_list(*, include_inactive=False):
    """Organizaciones con conteo de proyectos (no archivados) y de tareas abiertas.

    'tareas abiertas' = status NOT IN ('done','cancelled'), misma semantica que
    list_tasks() por defecto, para que los numeros del panel sean coherentes.
    """
    where = "" if include_inactive else "WHERE o.active"
    assert where in ("", "WHERE o.active")  # invariante: fragmento de allowlist
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT o.id, o.name, o.color, o.sort_order, o.active, "
            "  (SELECT count(*) FROM projects p "
            "     WHERE p.org_id = o.id AND p.status <> 'archived') AS project_count, "
            "  (SELECT count(*) FROM projects p WHERE p.org_id = o.id) AS project_count_all, "
            "  (SELECT count(*) FROM tasks t WHERE t.organization = o.id AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "     AND t.status NOT IN ('done','cancelled')) AS open_task_count, "
            "  (SELECT count(*) FROM tasks t WHERE t.organization = o.id AND t.request_kind IS DISTINCT FROM 'approval_request') AS task_count "
            f"FROM organizations o {where} ORDER BY o.sort_order, o.id")
        rows = cur.fetchall()
    out = _rows(rows)
    return {"organizations": out, "count": len(out)}


def projects_list(*, org="", include_archived=False):
    """Proyectos con su org_id, status y conteos de tareas.
    Por defecto EXCLUYE los archivados (include_archived=True para verlos)."""
    frags, params = [], []
    if org:
        frags.append("p.org_id = %s"); params.append(org)
    if not include_archived:
        frags.append("p.status <> 'archived'")
    assert all(f in ("p.org_id = %s", "p.status <> 'archived'") for f in frags)
    where = ("WHERE " + " AND ".join(frags)) if frags else ""
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, p.org_id, p.name, p.status, p.sort_order, "
            "  o.name AS org_name, o.color AS org_color, "
            "  (SELECT count(*) FROM tasks t WHERE t.project = p.id AND t.request_kind IS DISTINCT FROM 'approval_request') AS task_count, "
            "  (SELECT count(*) FROM tasks t WHERE t.project = p.id AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "     AND t.status NOT IN ('done','cancelled')) AS open_task_count "
            "FROM projects p LEFT JOIN organizations o ON o.id = p.org_id "
            f"{where} ORDER BY o.sort_order NULLS LAST, p.org_id, p.sort_order, p.id",
            params)
        rows = cur.fetchall()
    out = _rows(rows)
    return {"projects": out, "count": len(out)}


def organization_create(agent, org_id, name, *, color="", sort_order=100):
    """Crea una organizacion. IDEMPOTENTE por id: si ya existe, NO la modifica
    (devuelve created=False con la fila vigente). Para cambiarla: organization_update."""
    oid = (org_id or "").strip()
    if not oid:
        raise TaskError(400, "falta org_id")
    if not (name or "").strip():
        raise TaskError(400, "falta name")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, color, sort_order, active FROM organizations WHERE id = %s", (oid,))
        pre = cur.fetchone()
        if pre:
            return {"organization": _row(pre), "created": False,
                    "detail": f"La organizacion '{oid}' ya existia (idempotente, sin cambios)."}
        cur.execute(
            "INSERT INTO organizations (id, name, color, sort_order) VALUES (%s,%s,%s,%s) "
            "RETURNING id, name, color, sort_order, active",
            (oid, name.strip(), color or "", int(sort_order)))
        row = cur.fetchone()
        conn.commit()
    return {"organization": _row(row), "created": True}


def organization_update(agent, org_id, *, name="", color="", sort_order=None, active=None):
    """Actualiza una organizacion. Solo los campos pasados. NO borra (usar active=false)."""
    oid = (org_id or "").strip()
    if not oid:
        raise TaskError(400, "falta org_id")
    fields, params = [], []
    for col, val in [("name", name), ("color", color)]:
        if val:
            fields.append(f"{col} = %s"); params.append(val)
    if sort_order is not None:
        fields.append("sort_order = %s"); params.append(int(sort_order))
    if active is not None:
        fields.append("active = %s"); params.append(bool(active))
    if not fields:
        raise TaskError(400, "Nada para actualizar (pasa al menos un campo).")
    assert all(f.split(" = ")[0] in ("name", "color", "sort_order", "active") for f in fields)
    fields.append("updated_at = now()")
    params.append(oid)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE organizations SET {', '.join(fields)} WHERE id = %s "
                    "RETURNING id, name, color, sort_order, active", params)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise TaskError(404, f"No existe la organizacion '{oid}'.")
        conn.commit()
    return {"organization": _row(row), "updated": True}


def project_create(agent, project_id, org_id, name="", *, status="active", sort_order=100):
    """Crea un proyecto dentro de una organizacion. IDEMPOTENTE por id: si ya existe
    NO lo modifica (devuelve created=False). Valida que la org exista (404 si no).

    OJO: `projects.id` es el MISMO string que vive en `tasks.project` (decision de la
    migracion). Por eso el id lo elige el llamador y no es un uuid."""
    pid = (project_id or "").strip()
    oid = (org_id or "").strip()
    if not pid:
        raise TaskError(400, "falta project_id")
    if not oid:
        raise TaskError(400, "falta org_id")
    st = (status or "active").strip()
    if st not in _PROJECT_STATUSES:
        raise TaskError(400, f"status invalido '{st}'; validos: {sorted(_PROJECT_STATUSES)}")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM organizations WHERE id = %s", (oid,))
        if not cur.fetchone():
            raise TaskError(404, f"No existe la organizacion '{oid}'. Creala antes (organization_create).")
        cur.execute("SELECT id, org_id, name, status, sort_order FROM projects WHERE id = %s", (pid,))
        pre = cur.fetchone()
        if pre:
            return {"project": _row(pre), "created": False,
                    "detail": f"El proyecto '{pid}' ya existia (idempotente, sin cambios)."}
        cur.execute(
            "INSERT INTO projects (id, org_id, name, status, sort_order) VALUES (%s,%s,%s,%s,%s) "
            "RETURNING id, org_id, name, status, sort_order",
            (pid, oid, (name or pid).strip(), st, int(sort_order)))
        row = cur.fetchone()
        conn.commit()
    return {"project": _row(row), "created": True}


def project_update(agent, project_id, *, org_id="", name="", status="", sort_order=None,
                   reassign_tasks=True):
    """Actualiza un proyecto. El caso central es REASIGNARLO a otra organizacion
    (org_id), que es como Otixe corrige una clasificacion mal derivada.

    Al cambiar org_id, por defecto ARRASTRA las tareas del proyecto a la nueva org
    (reassign_tasks=True): si no, `tasks.organization` quedaria mintiendo respecto de
    `projects.org_id`. Es un UPDATE de clasificacion, NUNCA un borrado.
    """
    pid = (project_id or "").strip()
    if not pid:
        raise TaskError(400, "falta project_id")
    st = (status or "").strip()
    if st and st not in _PROJECT_STATUSES:
        raise TaskError(400, f"status invalido '{st}'; validos: {sorted(_PROJECT_STATUSES)}")
    fields, params = [], []
    for col, val in [("org_id", (org_id or "").strip()), ("name", name), ("status", st)]:
        if val:
            fields.append(f"{col} = %s"); params.append(val)
    if sort_order is not None:
        fields.append("sort_order = %s"); params.append(int(sort_order))
    if not fields:
        raise TaskError(400, "Nada para actualizar (pasa al menos un campo).")
    assert all(f.split(" = ")[0] in ("org_id", "name", "status", "sort_order") for f in fields)
    new_org = (org_id or "").strip()
    with _pg() as conn, conn.cursor() as cur:
        if new_org:
            cur.execute("SELECT 1 FROM organizations WHERE id = %s", (new_org,))
            if not cur.fetchone():
                raise TaskError(404, f"No existe la organizacion '{new_org}'.")
        fields.append("updated_at = now()")
        cur.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = %s "
                    "RETURNING id, org_id, name, status, sort_order", params + [pid])
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise TaskError(404, f"No existe el proyecto '{pid}'.")
        moved = 0
        if new_org and reassign_tasks:
            cur.execute("UPDATE tasks SET organization = %s, updated_at = now() "
                        "WHERE project = %s AND coalesce(organization,'') <> %s",
                        (new_org, pid, new_org))
            moved = cur.rowcount
        conn.commit()
    return {"project": _row(row), "updated": True, "tasks_reassigned": moved}
