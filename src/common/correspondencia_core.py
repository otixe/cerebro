"""FUENTE ÚNICA del resolvedor de buzones de Correspondencia (extraído, no reescrito).

Este módulo NO es una copia a mano: sus definiciones son los bytes EXACTOS de
`gateway/correspondencia.py` (sha256 f1fb6f20164abaf2e79087ce5ff31f2fb906e48bf0f6d21d137c42be131f6b69), extraídos por AST con
`build_common_core.py`. Existe porque el aviso automático de asignación de tablero
(`correspondencia_notify.py`) corre en TRES puertas — gateway, mcp y el BFF de
Sinapsis — y `gateway/correspondencia.py` sólo está horneado en la primera.

Sin este módulo la única salida era duplicar el resolvedor en `common/`, que es el
defecto que `bus_notify.py` documenta en su propia cabecera: «una constante
duplicada es una copia que se pudre» (la constante LEADERS hardcodeada excluía a
mónica y nadie lo vio durante meses).

`gateway/correspondencia.py` REBINDEA estos nombres desde acá, así que sigue
existiendo UNA sola implementación: si esta cambia, cambia para las cuatro puertas.

Única transformación respecto del fuente desplegado, declarada y asertada por el
build script: `_new_root` lanzaba `HTTPExc`+`eption(503, ...)`. Acá lanza
`RootAllocationError`; el gateway lo traduce al mismo 503 para no cambiar su
contrato REST. Motivo: este archivo se hornea en imágenes donde fastapi no es
parte del grafo de import (mcp, sinapsis-bff).

Dependencias: sólo stdlib (`uuid`, `typing`). Deliberado: cualquier import de
TERCEROS rompería el build de las imágenes de mcp y sinapsis.
"""
import logging
import json
import os
import uuid
from typing import Callable

# `logging` es stdlib => NO viola la restricción de dependencias del header (que
# prohíbe TERCEROS, para no romper el build de las imágenes de mcp y sinapsis).
log = logging.getLogger("correspondencia_core")


class RootAllocationError(RuntimeError):
    """No se pudo asignar un código raíz único (sustituye al 503 del gateway)."""



ROOT_HEX_LENGTH = 16


ACTIVE_IDENTITY_ALIASES = json.loads(os.environ.get("CORRESPONDENCIA_IDENTITY_ALIASES", "{}"))
if (not isinstance(ACTIVE_IDENTITY_ALIASES, dict) or any(
        not isinstance(k, str) or not k.startswith('@') or
        not isinstance(v, str) or not v for k, v in ACTIVE_IDENTITY_ALIASES.items())):
    raise ValueError("Invalid CORRESPONDENCIA_IDENTITY_ALIASES")


def individual_mailbox(row: dict) -> str:
    """Deriva el buzón personal canónico desde el principal activo.

    Los líderes agentes usan ``@equipo/líder``; ``@equipo`` es sólo un alias
    compatible de lectura/input. Los humanos sin equipo (hoy Otixe) conservan
    ``@nombre``: no se inventa un namespace que su modelo no tiene.
    """
    name = str(row["name"]).strip().lower()
    team = str(row.get("team") or "").strip().lower()
    kind = str(row.get("kind") or "agent").strip().lower()
    if kind == "human":
        if team:
            raise ValueError("un humano no debe tener equipo")
        return f"@{name}"
    if not team:
        raise ValueError(f"agente sin team: {name}")
    if bool(row.get("is_lead")):
        return f"@{team}/{name}"
    prefix = f"{team}-"
    local = name[len(prefix):] if name.startswith(prefix) else name
    return f"@{team}/{local}"


def _agent_rows(cur, lock: bool = False) -> list[dict]:
    cur.execute(
        "SELECT name, team, is_lead, kind, role FROM agents "
        "WHERE active=true ORDER BY name" + (" FOR SHARE" if lock else "")
    )
    return [dict(row) for row in cur.fetchall()]


def _registry(cur, lock: bool = False) -> tuple[dict[str, dict], dict[str, list[str]], dict[str, str]]:
    rows = _agent_rows(cur, lock=lock)
    individuals: dict[str, dict] = {}
    teams: dict[str, list[str]] = {}
    aliases: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for row in rows:
        try:
            mailbox = individual_mailbox(row)
        except ValueError as exc:
            # DEGRADA EN VEZ DE CAERSE. Antes esta ValueError subía y tumbaba el
            # registro COMPLETO: UNA sola fila inconsistente dejaba a la flota
            # entera sin avisos, de una vez. Con el bus encendido eso quedaba
            # TAPADO (el aviso salía igual por el canal viejo); en canal puro es
            # silencio total y sin síntoma. Se saltea la fila, se registra QUIÉN y
            # CUÁL es el defecto, y el barrido sigue con las demás.
            _name = str(row.get("name") or "?")
            _team = str(row.get("team") or "").strip().lower()
            _kind = str(row.get("kind") or "agent").strip().lower()
            # El defecto se NOMBRA derivandolo de la FILA, no del texto de la
            # excepción, por dos razones medidas:
            #  1. `individual_mailbox` incluye el `name` en UNA sola de sus dos
            #     ramas -- "un humano no debe tener equipo" NO lo lleva --, así que
            #     loguear `str(exc)` a secas dejaría un aviso que no dice DE QUIÉN.
            #  2. El mensaje de una excepción es prosa: clasificar parseandolo
            #     haría que reescribir la frase cambie la clasificación.
            # ⚠️ "Malformada" NO es "team vacío": son DOS inconsistencias
            # `kind`x`team` distintas, y para `kind='human'` el team vacío es el
            # estado REQUERIDO (otixe), no un defecto.
            if _kind == "human" and _team:
                _defecto = "kind='human' CON team=%r" % _team
            elif _kind != "human" and not _team:
                _defecto = "kind=%r SIN team" % _kind
            else:
                _defecto = "inconsistencia kind/team no clasificada"
            log.warning("fila de `agents` SALTEADA del registro de buzones: "
                        "name=%r defecto=%s (%s)", _name, _defecto, exc)
            continue
        individuals[mailbox] = row
        by_name[str(row["name"]).strip().lower()] = mailbox
        if row.get("kind") == "agent":
            team = str(row.get("team") or "").lower()
            teams.setdefault(f"@{team}/*", []).append(mailbox)
            if bool(row.get("is_lead")):
                aliases[f"@{team}"] = mailbox
    # Alias de IDENTIDAD. Aditivo sobre `aliases`, que es la ÚNICA fuente de la que
    # `normalize_mailbox()` y `equivalent_mailboxes()` derivan todo ⇒ se propaga solo
    # a resolver / publicar / responder / hilos / bandeja / acuses, sin tocar ninguno.
    for alias, target_name in ACTIVE_IDENTITY_ALIASES.items():
        key = (alias or "").strip().lower()
        mailbox = by_name.get(str(target_name or "").strip().lower())
        if not mailbox:
            # FAIL-CLOSED: si el agente destino no está activo, el alias NO existe.
            # Un alias colgado resolvería a un buzón sin dueño y crearía entregas
            # que nadie puede leer ni drenar.
            continue
        if key in individuals or key in aliases or f"{key}/*" in teams:
            # No tapar nada real: un buzón individual homónimo, un alias de equipo ya
            # existente o un equipo que se llame igual GANAN sobre el alias. Sin esta
            # guarda, dar de alta un agente llamado `ariel` desviaría su correo.
            continue
        aliases[key] = mailbox
    return (individuals,
            {key: sorted(set(value)) for key, value in teams.items()},
            aliases)


def mailbox_normalizer(cur) -> Callable[[str], str]:
    """UN snapshot del registro -> normalizador puro, apto para usar dentro de un loop.

    Existe porque `normalize_mailbox` relee `agents` en CADA llamada y se la invocaba
    POR FILA: la vista Archivo de la bandeja hacía ~2.900 `SELECT ... FROM agents` en
    UNA sola request (medido 2026-09-16, con el control `/resumen` en 4 que hace
    informativo al número grande), y con Postgres en contención el mismo endpoint
    tardó 120 s. No era una consulta lenta: eran ~2.900 viajes secuenciales.

    EL ALCANCE DEL SNAPSHOT ES LA REQUEST, JAMÁS EL PROCESO. Una cache global o con
    TTL volvería invisible la revocación de un agente entre requests, o sea fail-open
    en control de identidad. Y un solo snapshot por request es MÁS consistente que
    releer 2.900 veces, no menos: releyendo, dos filas de la MISMA respuesta pueden
    normalizarse contra registros distintos.
    """
    _, _, aliases = _registry(cur)

    def normalize(raw: str) -> str:
        value = (raw or "").strip().lower()
        return aliases.get(value, value)

    return normalize


def normalize_mailbox(cur, raw: str) -> str:
    """Normaliza alias personal antes de validar, comparar o persistir.

    Reexpresada SOBRE `mailbox_normalizer` para que la regla de alias
    (`strip().lower()` + lookup) siga viviendo en UN solo lugar. Mismo contrato y
    mismo costo que antes (un snapshot por llamada): los call sites de una sola
    fila no cambian de comportamiento.
    """
    return mailbox_normalizer(cur)(raw)


def _new_root(cur) -> tuple[uuid.UUID, str]:
    for _ in range(16):
        message_id = uuid.uuid4()
        root = message_id.hex[:ROOT_HEX_LENGTH]
        cur.execute(
            "SELECT 1 FROM correspondencia_mensajes WHERE codigo=%s", (root,)
        )
        if cur.fetchone() is None:
            return message_id, root
    raise RootAllocationError("no se pudo asignar un código raíz único")


def _insert_deliveries(cur, message_id: uuid.UUID, mailboxes: list[str]) -> None:
    for mailbox in mailboxes:
        cur.execute(
            "INSERT INTO correspondencia_entregas "
            "(id, mensaje_id, buzon, estado) VALUES (%s,%s,%s,'pendiente')",
            (uuid.uuid4(), message_id, mailbox),
        )
