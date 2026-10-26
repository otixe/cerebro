import threading
import time
import os, re, secrets, tempfile
import errno
import stat
from contextlib import contextmanager
import json
import base64
import binascii
import logging
# FUENTE UNICA de la notificacion tablero->bus (common/, horneado por el Dockerfile)
import bus_notify
import tasks_rbac
import shutil, hashlib, asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
import bcrypt
import psycopg
from psycopg.rows import dict_row

# slowapi: rate-limit por IP (mismo patron que el gateway). Anti brute-force en
# /login y /change-password (auditoria  H2).
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

VAULT = Path(os.environ.get("VAULT_ROOT", "/vault/obsidian-vault"))
# H1 (auditoria ): el fallback de "sesion de emergencia" por password
# global del env (SINAPSIS_PASSWORD/VISOR_PASSWORD) fue ELIMINADO. La unica via de
# autenticacion es el login humano real: bcrypt contra `agents` + sesion en
# `sessions` (TTL, revocable, auditable). No leemos mas esas env vars.
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
def _read_required_secret_file(env_name: str) -> str:
    """Read a required secret without ever placing its value in Config.Env."""
    secret_path = os.environ.get(env_name, "").strip()
    if not secret_path:
        raise RuntimeError(f"{env_name} is required; refusing to start")
    try:
        value = Path(secret_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"cannot read required secret file configured by {env_name}: {exc}"
        ) from exc
    if not value:
        raise RuntimeError(f"secret file configured by {env_name} is empty; refusing to start")
    return value


# cambio de implementación: Sinapsis authenticates as itself. There is deliberately no fallback
# to another service credential: a missing secret must stop the BFF, never change its identity.
SEARCH_KEY = _read_required_secret_file("SINAPSIS_API_KEY_FILE")
# Nombre de usuario admin por default (parametrizable). Solo se usa como etiqueta
# de atribucion en audit_log / task_activity cuando el principal no tiene name.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
# Directorio del build estatico del SPA (React/Vite). Servido por este mismo FastAPI.
DIST = os.environ.get("DIST_DIR", "/app/web/dist")
PG = dict(host=os.environ.get("POSTGRES_HOST", "postgres"),
          port=int(os.environ.get("POSTGRES_PORT_INTERNAL", "5432")),
          user=os.environ.get("POSTGRES_USER"), password=os.environ.get("POSTGRES_PASSWORD"),
          dbname=os.environ.get("POSTGRES_DB"))
EXCLUDE = ("node_modules", ".venv", "venv", ".git", "site-packages", ".deno", ".cache", "__pycache__")

# --- M5 (auditoria ): carpetas del vault PROHIBIDAS para Sinapsis ---
# El indexer aplica politica metadata-only sobre '95 Secrets' (SECRETS_METADATA_ONLY),
# pero Sinapsis sirve el vault DIRECTO del FS: sin este exclude, cualquier principal
# con scope raiz (admin incluido) leeria los valores en claro. Sinapsis NUNCA lee
# estas rutas: ni via /api/node ni /api/image_bytes, ni las lista en el arbol.
# Configurable por env (coma-separado); default alineado con SECRETS_METADATA_ONLY.
SECRETS_DENY = tuple(
    p.strip().strip("/") for p in os.environ.get(
        "SINAPSIS_SECRETS_DENY", "95 Secrets").split(",") if p.strip()
)

# --- M2 (auditoria ): CORS explicito ---
# SINAPSIS_ALLOWED_ORIGINS = lista coma-separada de origins permitidos
# (ej. "https://sinapsis.example,http://localhost:5173"). Default RESTRICTIVO:
# lista VACIA => el navegador solo permite same-origin (el SPA se sirve desde este
# mismo FastAPI, asi que no necesita CORS). Declarar origins solo si hay un frontend
# en otro host/puerto (ej. `vite dev`).
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("SINAPSIS_ALLOWED_ORIGINS", "").split(",") if o.strip()]

# Limites de rate para los endpoints de credenciales (H2). Estrictos: bcrypt es caro
# y un brute-force sin limite es DoS + ataque de password a la vez.
RATE_LIMIT_LOGIN = os.environ.get("RATE_LIMIT_LOGIN", "10/minute")
RATE_LIMIT_PASSWORD = os.environ.get("RATE_LIMIT_PASSWORD", "10/minute")

# --- Fase A: gestión de archivos ---
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".webp"}
MAX_UPLOAD = 25 * 1024 * 1024  # 25 MB

# Endurecimiento de adjuntos de Correspondencia (F4). El tope de borde acompaña
# al del gateway, que es la autoridad; se lee del entorno con el mismo nombre para
# que no puedan quedar desalineados por accidente. Un valor legible y > 0 mueve
# este tope (arriba o abajo), que es justo el punto de leer la MISMA variable que
# el gateway. Un valor ilegible o <= 0 SÍ cae al default MAX_UPLOAD: acá no puede
# AMPLIAR el tope, y esa misma config le da 500 al gateway, que es quien decide.
_ADJUNTO_UINT_RE = re.compile(r"[0-9]{1,19}")
_ADJUNTO_TIPO_INERTE = "application/octet-stream"
_ADJUNTO_CSP = ("default-src 'none'; sandbox; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'; object-src 'none'; "
                "script-src 'none'")


def _adjunto_max_bytes() -> int:
    raw = (os.environ.get("CORRESPONDENCIA_ADJUNTO_MAX_BYTES") or "").strip()
    if raw and _ADJUNTO_UINT_RE.fullmatch(raw) and int(raw) > 0:
        return int(raw)
    return MAX_UPLOAD


_ADJUNTO_MAX_BYTES = _adjunto_max_bytes()
TRASH_DIR = ".trash"
# Dirs a excluir al listar archivos (además de EXCLUDE)
SKIP_DIRS = set(EXCLUDE) | {".trash", ".smart-env"}
# El indexer monta VAULT_ROOT=/vault; el visor /vault/obsidian-vault → los archivos del
# visor quedan indexados con source_path prefijado por "obsidian-vault/".
INDEX_PREFIX = "obsidian-vault/"

app = FastAPI(title="SINAPSIS")

# --- Rate limiter (slowapi, in-memory; key = IP del cliente) ---
# En Sinapsis los endpoints protegidos son PRE-auth (/login) o de rotacion de
# credencial (/change-password), asi que la key es la IP remota, no el agente.
limiter = Limiter(key_func=get_remote_address, headers_enabled=False)


async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    resp = JSONResponse(status_code=429,
                        content={"error": "rate_limit_exceeded", "detail": str(exc.detail)})
    if "Retry-After" not in resp.headers:
        resp.headers["Retry-After"] = "60"
    return resp


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- CORS explicito (M2) --- allow_origins declarado, jamas '*' con credenciales.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# H1: no hay tokens en memoria. Toda sesion vive en la tabla `sessions`.
_cache = None


def _pg():
    return psycopg.connect(row_factory=dict_row, **PG)


def _hash_password(pw: str) -> str:
    """Hash bcrypt de una contraseña (str utf-8). bcrypt trunca a 72 bytes."""
    return bcrypt.hashpw(pw.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


@app.on_event("startup")
def _sinapsis_bootstrap():
    # Idempotent: optional first human admin (schema is in src/postgres/init.sql). See bootstrap.py.
    import bootstrap
    bootstrap.run(_pg, _hash_password, ADMIN_USER)



def _verify_password(pw: str, hashed) -> bool:
    """Verifica una contraseña contra un hash bcrypt. False si el hash no es bcrypt
    o está vacío (ej. placeholder salted-sha256 de Fase 0)."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(pw.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _resolve_session(token: str):
    """Devuelve el principal de una sesión válida (no revocada, no expirada) o None.
    La validez se resuelve SIEMPRE contra la DB (tabla sessions), no en memoria."""
    if not token:
        return None
    token_hash = _sha256_hex(token)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.name, a.role, a.kind, s.id AS session_id "
            "FROM sessions s JOIN agents a ON a.id = s.principal_id "
            "WHERE s.token_hash=%s AND s.revoked_at IS NULL AND s.expires_at > now()",
            (token_hash,))
        return cur.fetchone()


def _check(authorization):
    """Valida el Bearer token y devuelve el principal de la sesión.
    UNICA via de auth (H1, auditoria ): sesión real en la tabla
    `sessions` (no revocada, no expirada). El fallback de emergencia por password
    global del env fue eliminado: un token que no está en `sessions` es 401.
    Devuelve un dict con al menos {id, name, role, kind}."""
    tok = (authorization or "").replace("Bearer ", "").strip()
    if not tok:
        raise HTTPException(401, "no autorizado")
    principal = _resolve_session(tok)
    if not principal:
        raise HTTPException(401, "no autorizado")
    return principal


def _require_admin(authorization):
    """Valida sesión Y que el principal sea role='admin'. 403 en caso contrario.
    Base de autorización de todos los endpoints /admin/*."""
    principal = _check(authorization)
    if principal.get("role") != "admin":
        raise HTTPException(403, "requiere rol admin")
    return principal


# ==== Lectura del BUS desde Sinapsis: guard PROPIO, MAS estricto que _require_admin ====
# NO se reusa _require_admin, y es deliberado. Medido en Postgres ():
# role='admin' lo tienen SIETE principals y SEIS son AGENTES (assistant-a, assistant-g, assistant-c,
# assistant-d, assistant-c, assistant-b); el unico kind='human' es `admin`. Que HOY _require_admin
# equivalga a "solo admin" es una COINCIDENCIA del estado de la base -- el login exige
# kind='human' AND active=true y solo admin tiene password_hash -- y NO un diseno.
# El dia que se cree otro usuario humano con role='admin' (POST /admin/users acepta ese
# rol) heredaria EN SILENCIO el acceso al contenido intimo de assistant-a y assistant-d. Ese error no
# tiene rollback: una fuga de lectura vence con el PRIMER GET (lo leido esta leido).
# admin lo dijo textual sobre quien puede leer el bus: "sobre quien puede solo yo".
# Por eso el permiso es una ALLOWLIST NOMINAL por entorno: ampliarla tiene que ser un
# acto DELIBERADO (editar BUS_HUMAN_READERS y reiniciar el servicio), jamas una herencia
# automatica de un rol que comparte con seis agentes.
BUS_HUMAN_READERS = tuple(
    n.strip().lower()
    for n in os.environ.get("BUS_HUMAN_READERS", ADMIN_USER).split(",")
    if n.strip()
)


def _is_bus_reader(principal) -> bool:
    """True si `principal` cumple las TRES condiciones de autorización del bus
    (aparte de tener sesión válida): role='admin' Y kind='human' Y name en la
    allowlist BUS_HUMAN_READERS. Fuente ÚNICA del predicado: la usa el guard
    _require_bus_reader (que lanza 403) y GET /me (que solo lo informa), para que
    la UI y el enforcement no puedan divergir."""
    if not principal:
        return False
    if (principal.get("role") or "").strip().lower() != "admin":
        return False
    if (principal.get("kind") or "").strip().lower() != "human":
        return False
    name = (principal.get("name") or "").strip().lower()
    return bool(name) and name in BUS_HUMAN_READERS


def _require_bus_reader(authorization):
    """Sesión válida Y kind='human' Y role='admin' Y name ∈ BUS_HUMAN_READERS.

    ACUMULATIVO y fail-closed: si falta CUALQUIERA de las cuatro condiciones =>
    HTTPException(403). Nunca devuelve una lista vacía con 200: un `200 []` es
    indistinguible de "no hay hilos" y de "no podés verlos", y esa ambigüedad se
    lee después como "no había nada que proteger".

    Reusa _check para la resolución de sesión (no duplica el SELECT sobre
    `sessions`) y _is_bus_reader para el predicado de autorización."""
    principal = _check(authorization)
    if not _is_bus_reader(principal):
        raise HTTPException(403, "no autorizado a leer el bus")
    return principal


# ==== PUBLICACION del BUS: tercer permiso nominal, independiente de lectura/chat ====
# Permisos distintos => allowlists distintas: quien puede LEER el bus no hereda el
# derecho a PUBLICAR en él. Tampoco se reusa _require_admin: ese rol lo comparten
# agentes que jamás deben poder hablar con la voz humana delegada por el panel.
BUS_HUMAN_PUBLISHERS = tuple(
    n.strip().lower()
    for n in os.environ.get("BUS_HUMAN_PUBLISHERS", ADMIN_USER).split(",")
    if n.strip()
)


def _is_bus_publisher(principal) -> bool:
    """Fuente única del predicado usado por el guard y GET /me."""
    if not principal:
        return False
    if (principal.get("role") or "").strip().lower() != "admin":
        return False
    if (principal.get("kind") or "").strip().lower() != "human":
        return False
    name = (principal.get("name") or "").strip().lower()
    return bool(name) and name in BUS_HUMAN_PUBLISHERS


def _require_bus_publisher(authorization):
    """Sesión válida AND human AND admin AND nombre nominalmente autorizado."""
    try:
        principal = _check(authorization)
    except HTTPException:
        raise HTTPException(403, "no autorizado a publicar en el bus")
    if not _is_bus_publisher(principal):
        raise HTTPException(403, "no autorizado a publicar en el bus")
    return principal


# ==== ENVIO de CHAT a un agente desde Sinapsis: guard HERMANO del de bus ====
# Mismo razonamiento que BUS_HUMAN_READERS, pero el objeto protegido es PEOR: leer el
# bus es una FUGA (lo leido esta leido); un POST /api/chat/send es EJECUCION DE CODIGO
# POR DELEGACION -- inyecta un turno en un proceso pi vivo, que puede escribir al
# Cerebro, tocar archivos y correr bash con la identidad del agente. El rollback de
# esta feature (retirar la ruta + rebuild) VENCE con el primer mensaje inyectado por un
# principal != admin: retirar la ruta no deshace un turno que ya produjo efectos.
# Por eso NO se reusa _require_admin (role='admin' lo comparten SIETE principals y SEIS
# son AGENTES) ni _require_bus_reader (permiso distinto => allowlist distinta: quien
# puede LEER el bus no hereda el derecho a HABLARLE a un agente). Allowlist NOMINAL
# propia por entorno: ampliarla exige editar CHAT_HUMAN_SENDERS y reiniciar el
# servicio, jamas una herencia automatica de un rol compartido con seis agentes.
CHAT_HUMAN_SENDERS = tuple(
    n.strip().lower()
    for n in os.environ.get("CHAT_HUMAN_SENDERS", ADMIN_USER).split(",")
    if n.strip()
)


def _is_chat_sender(principal) -> bool:
    """True si `principal` cumple las TRES condiciones de autorizacion del chat
    (aparte de tener sesion valida): role='admin' Y kind='human' Y name en la
    allowlist CHAT_HUMAN_SENDERS. Fuente UNICA del predicado: la usa el guard
    _require_chat_sender (que lanza 403) y GET /me (que solo lo informa), para que
    la UI y el enforcement no puedan divergir. Mismo contrato que _is_bus_reader."""
    if not principal:
        return False
    if (principal.get("role") or "").strip().lower() != "admin":
        return False
    if (principal.get("kind") or "").strip().lower() != "human":
        return False
    name = (principal.get("name") or "").strip().lower()
    return bool(name) and name in CHAT_HUMAN_SENDERS


def _require_chat_sender(authorization):
    """Sesion valida Y kind='human' Y role='admin' Y name en CHAT_HUMAN_SENDERS.

    ACUMULATIVO y fail-closed: si falta CUALQUIERA de las cuatro condiciones =>
    HTTPException(403). Nunca degrada a un 200: sobre un endpoint de ENVIO un 200
    mentiroso es peor que en la lectura del bus, porque el cliente concluye que el
    mensaje se entrego.

    Reusa _check para la resolucion de sesion (no duplica el SELECT sobre
    `sessions`) y _is_chat_sender para el predicado de autorizacion."""
    principal = _check(authorization)
    if not _is_chat_sender(principal):
        raise HTTPException(403, "no autorizado a enviar mensajes de chat")
    return principal


# ==== FASE 3a authz: enforcement de scope (SOLO SINAPSIS / humanos) ====
# scopes_of/path_allowed resuelven el scope EFECTIVO del principal a partir de
# scope_grants activos. admin => scope raiz ('') = todo. El resultado
# se cachea en el propio dict del principal (vive por request) para no repetir
# el SELECT en endpoints que filtran muchos items.
def scopes_of(principal):
    """Lista de (path_prefix, can_write) permitidos del principal.
    admin => [('', True)] (todo, con escritura). Cacheado por request
    en principal['_scopes']."""
    cached = principal.get("_scopes")
    if cached is not None:
        return cached
    if principal.get("role") == "admin":
        result = [("", True)]
    else:
        pid = principal.get("id")
        if not pid:
            result = []
        else:
            with _pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT path_prefix, can_write FROM scope_grants "
                    "WHERE principal_id=%s AND revoked_at IS NULL", (pid,))
                result = [(r["path_prefix"], bool(r["can_write"])) for r in cur.fetchall()]
    principal["_scopes"] = result
    return result


def _norm_scope_path(source_path: str) -> str:
    """Normaliza un path para chequeo de scope: quita el prefijo de indice
    'obsidian-vault/' si aplica y las '/' iniciales."""
    sp = source_path or ""
    if sp.startswith(INDEX_PREFIX):
        sp = sp[len(INDEX_PREFIX):]
    return sp.lstrip("/")


def path_allowed(principal, source_path, write: bool = False) -> bool:
    """True si algun prefijo del scope efectivo del principal cubre source_path.
    prefix '' cubre todo. Un prefijo 'Personal/' cubre 'Personal/x.md' y la propia
    carpeta 'Personal'. Para write=True exige que el grant que cubre tenga can_write."""
    sp = _norm_scope_path(source_path)
    for pref, can_write in scopes_of(principal):
        if write and not can_write:
            continue
        if pref == "" or sp == pref.rstrip("/") or sp.startswith(pref):
            return True
    return False


def _is_secrets_path(rel: str) -> bool:
    """True si `rel` (path RELATIVO al vault) cae bajo una carpeta de SECRETS_DENY.

    M5 (auditoria ): Sinapsis sirve el vault crudo del FS y no aplicaba la
    politica metadata-only que el indexer usa para '95 Secrets' -> un principal con
    scope raiz podia leer los VALORES en claro. Este helper es la unica verdad del
    deny: se aplica en LECTURA (_safe_path, api_node, api_image_bytes,
    file_save/...) y en el LISTADO del arbol (build_tree).

    Coincide con la carpeta misma y con todo su contenido, case-insensitive, y tras
    normalizar el prefijo de indice 'obsidian-vault/' y las '/' iniciales.
    """
    sp = _norm_scope_path(rel or "").replace("\\", "/").strip("/").lower()
    if not sp:
        return False
    for deny in SECRETS_DENY:
        d = deny.lower()
        if sp == d or sp.startswith(d + "/"):
            return True
    return False


def _deny_if_secrets(rel: str):
    """403 si `rel` cae bajo SECRETS_DENY. Mismo mensaje para existente/inexistente
    (no filtra si el secreto existe)."""
    if _is_secrets_path(rel):
        raise HTTPException(403, "ruta no accesible desde Sinapsis")


def _prune_tree_to_scope(node, principal):
    """Devuelve una copia del arbol conservando solo archivos en scope y los
    directorios que aun tengan hijos vivos (asi las ramas fuera de scope se podan
    y las carpetas ancestro de una rama permitida se conservan para navegar).
    None si el nodo entero queda fuera de scope."""
    if node.get("type") == "file":
        return dict(node) if path_allowed(principal, node.get("path") or "") else None
    # directorio
    kept = []
    for c in node.get("children", []):
        pruned = _prune_tree_to_scope(c, principal)
        if pruned is not None:
            kept.append(pruned)
    # raiz del vault (path=='') siempre se devuelve (aunque vacia); un subdir sin
    # hijos vivos se poda salvo que este el mismo cubierto por un scope explicito.
    if node.get("path") == "":
        out = dict(node)
        out["children"] = kept
        return out
    if kept or path_allowed(principal, node.get("path") or ""):
        out = dict(node)
        out["children"] = kept
        return out
    return None


def _audit(cur, agent: str, action: str, detail: str = "", success: bool = True):
    """Registra una acción administrativa en audit_log (columnas agent/action/query).

    `success` es un PARAMETRO LIGADO (%s), nunca un literal ni un f-string: un
    RECHAZO (403/401) tiene que quedar con success=false para que
    `WHERE success=false` encuentre los intentos fallidos. El default True
    preserva el comportamiento de los call sites de EXITO preexistentes."""
    cur.execute(
        "INSERT INTO audit_log (agent, action, query, success) VALUES (%s,%s,%s,%s)",
        (agent or ADMIN_USER, action, detail or None, bool(success)),
    )


def _audit_fs_begin(agent: str, action: str, detail: str = "") -> int:
    """Registra durablemente un intento filesystem antes de su efecto.

    `success=false` significa "intento no confirmado", NO que el efecto falló:
    el proceso puede morir después del efecto filesystem y antes de confirmarlo.
    """
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (agent, action, query, success) "
            "VALUES (%s,%s,%s,false) RETURNING id",
            (agent or ADMIN_USER, action, detail or None),
        )
        audit_id = cur.fetchone()["id"]
        conn.commit()
    return audit_id


def _audit_fs_commit(audit_id: int):
    """Confirma un intento filesystem solo después de su efecto.

    `success=false` significa "intento no confirmado", NO que el efecto falló:
    el proceso puede morir después del efecto filesystem y antes de confirmarlo.
    """
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE audit_log SET success=true WHERE id=%s AND success=false RETURNING id",
            (audit_id,),
        )
        confirmed = cur.fetchall()
        if len(confirmed) != 1:
            raise RuntimeError(f"filesystem audit {audit_id} was not confirmable")
        conn.commit()


def _safe_path(path: str, must_be_md=True):
    """Resuelve una ruta relativa DENTRO del vault. Bloquea traversal y rutas fuera.
    Deniega tambien las rutas de SECRETS_DENY ('95 Secrets', M5): se chequea DESPUES
    de resolver contra el FS, asi un traversal que aterrice en la carpeta prohibida
    tampoco pasa."""
    rel = path[len("obsidian-vault/"):] if path.startswith("obsidian-vault/") else path
    rel = rel.lstrip("/")
    p = (VAULT / rel).resolve()
    vroot = VAULT.resolve()
    if vroot != p and vroot not in p.parents:
        raise HTTPException(403, "fuera del vault")
    if must_be_md and p.suffix.lower() != ".md":
        raise HTTPException(400, "solo archivos .md")
    # M5: deny de secrets sobre el path RESUELTO (no sobre el crudo) -> no se puede
    # eludir con '..' ni con prefijos redundantes.
    resolved_rel = str(p.relative_to(vroot)) if p != vroot else ""
    _deny_if_secrets(resolved_rel)
    return rel, p


def _sanitize_filename(name: str) -> str:
    """Nombre plano seguro: sin separadores, sin ocultos, sin traversal."""
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    name = name.strip().lstrip(".")
    return name or "upload"


def _fs_http_error(exc: OSError):
    """Fail closed; never fall back to path-following IO or cross-device copy."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return HTTPException(403, "symlink o componente no permitido")
    if exc.errno == errno.ENOENT:
        return HTTPException(404, "no existe")
    if exc.errno == errno.EEXIST:
        return HTTPException(409, "ya existe")
    if exc.errno == errno.EXDEV:
        return HTTPException(409, "movimiento entre dispositivos no permitido; no se copió")
    return HTTPException(500, f"operación filesystem no confirmada (errno={exc.errno})")


@contextmanager
def _vault_dir(rel_dir: str, create=False):
    """Pin the vault root and each directory; never traverse a symlink.

    _safe_path/path_allowed remain the policy layer. This is the IO layer:
    open the ORIGINAL relative path, not its earlier resolved absolute path.
    Every mkdir/open is relative to an already pinned parent, including .trash.
    """
    parts = Path(rel_dir).parts
    if Path(rel_dir).is_absolute() or ".." in parts:
        raise HTTPException(403, "fuera del vault")
    fds = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fds.append(os.open(VAULT, flags))
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=fds[-1])
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fds[-1])
                except FileExistsError:
                    pass  # open below still rejects a symlink created by a racer.
                child = os.open(part, flags, dir_fd=fds[-1])
            fds.append(child)
        yield fds[-1]
    except OSError as exc:
        raise _fs_http_error(exc) from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


@contextmanager
def _vault_parent(rel: str, create=False):
    path = Path(rel)
    if path.name in ("", ".", ".."):
        raise HTTPException(403, "hoja no permitida")
    with _vault_dir(str(path.parent), create=create) as dir_fd:
        yield dir_fd, path.name


@contextmanager
def _vault_leaf(dir_fd: int, name: str, missing_ok=False, directory_error=None):
    """Read/fstat only a regular leaf opened with O_NOFOLLOW; no FIFO blocking.

    An ordinary directory may have a caller-specific 409/404 contract. Symlinks
    still fail at O_NOFOLLOW; all other non-regular leaves keep the default 403.
    """
    fd = None
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=dir_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise
        if fd is not None:
            mode = os.fstat(fd).st_mode
            if directory_error is not None and stat.S_ISDIR(mode):
                raise HTTPException(*directory_error)
            if not stat.S_ISREG(mode):
                raise HTTPException(403, "hoja no es un archivo regular")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _leaf_stat(dir_fd: int, name: str, missing_ok=False, directory_error=None):
    with _vault_leaf(dir_fd, name, missing_ok=missing_ok, directory_error=directory_error) as fd:
        return os.fstat(fd) if fd is not None else None


def _sync_dir(dir_fd: int):
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise HTTPException(
            500, "resultado incierto: el efecto pudo aplicarse; falló la durabilidad del directorio"
        ) from exc


def _atomic_write_bytes(dir_fd: int, name: str, data: bytes, *, create=False):
    """O_TMPFILE + fsync + fd-relative publication on the same filesystem.

    Create links directly to the final name: EEXIST is an atomic 409, not a
    check/replace race. Save/upload link a private staging name then replace.
    O_TMPFILE support is required (measured on ZFS); no path-based fallback.
    An unlinked anonymous inode disappears on close. If replace fails after
    staging publication, retain that private staging entry; NEVER unlink it.
    A directory-fsync failure means an uncertain already-visible result.
    """
    previous = _leaf_stat(dir_fd, name, missing_ok=True,
                          directory_error=(409, "ya existe") if create else None)
    if create and previous is not None:
        raise HTTPException(409, "ya existe")
    fd = os.open(".", os.O_TMPFILE | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=dir_fd)
    try:
        if previous is not None:
            try:
                os.fchown(fd, previous.st_uid, previous.st_gid)
                os.fchmod(fd, previous.st_mode & 0o777)
            except OSError as exc:
                raise HTTPException(500, "no se puede preservar el ownership; no se guardó") from exc
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if not written:
                raise OSError(errno.EIO, "short write")
            view = view[written:]
        os.fsync(fd)
        if create:
            os.link(f"/proc/self/fd/{fd}", name, dst_dir_fd=dir_fd, follow_symlinks=True)
        else:
            staging = f".{name}.tmp-{secrets.token_hex(16)}"
            os.link(f"/proc/self/fd/{fd}", staging, dst_dir_fd=dir_fd, follow_symlinks=True)
            os.replace(staging, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        _sync_dir(dir_fd)
    finally:
        os.close(fd)


def _current_index_hash(source_path: str):
    """source_hash de los chunks activos del archivo en el índice (o None si no está).

    Replica la lógica de _get_indexed_state del indexer: agrupa por source_hash y
    devuelve el del grupo con más chunks activos.
    """
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""SELECT source_hash, COUNT(*) AS n
                       FROM chunks_meta
                       WHERE source_path=%s AND deleted_at IS NULL
                       GROUP BY source_hash
                       ORDER BY n DESC LIMIT 1""", (source_path,))
        row = cur.fetchone()
        return row["source_hash"] if row else None


def build_graph():
    nodes, links = {}, []
    files = [p for p in VAULT.rglob("*.md")
             if not any(x in str(p) for x in EXCLUDE)
             and not _is_secrets_path(str(p.relative_to(VAULT)))]  # M5: nunca '95 Secrets'
    for p in files:
        stem = p.stem
        if stem not in nodes:
            rel = str(p.relative_to(VAULT))
            nodes[stem] = {"id": stem, "path": rel, "group": rel.split("/")[0], "type": "note", "val": 1}
    for p in files:
        stem = p.stem
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.findall(r"\[\[([^\]|#]+)", txt):
            tgt = m.strip().split("/")[-1].strip()
            if tgt in nodes and tgt != stem:
                links.append({"source": stem, "target": tgt})
                nodes[tgt]["val"] += 1
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,title,project,status FROM tasks WHERE status NOT IN ('done','cancelled') AND request_kind IS DISTINCT FROM 'approval_request'")
            for t in cur.fetchall():
                tid = "task:" + str(t["id"])[:8]
                nodes[tid] = {"id": tid, "path": None, "group": "TAREAS", "type": "task",
                              "status": t["status"], "title": t["title"], "val": 1}
                if t["project"]:
                    pid = "proj:" + t["project"]
                    if pid not in nodes:
                        nodes[pid] = {"id": pid, "path": None, "group": "PROYECTOS", "type": "project",
                                      "title": t["project"], "val": 4}
                    links.append({"source": pid, "target": tid})
    except Exception as e:
        print("tasks graph err:", e)
    # agentes + su memoria (interaccion real agente<->dato, de chunks_meta.agent_owner)
    try:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT agent_owner, source_path, title FROM chunks_meta "
                        "WHERE agent_owner IS NOT NULL AND agent_owner NOT IN ('indexer','') "
                        "AND deleted_at IS NULL")
            for r in cur.fetchall():
                aid = "agent:" + r["agent_owner"]
                if aid not in nodes:
                    nodes[aid] = {"id": aid, "path": None, "group": "AGENTES", "type": "agent",
                                  "title": r["agent_owner"], "val": 6}
                mid = "mem:" + r["source_path"]
                if mid not in nodes:
                    nodes[mid] = {"id": mid, "path": None, "group": "MEMORIA", "type": "memory",
                                  "title": r["title"] or r["source_path"], "val": 1}
                links.append({"source": aid, "target": mid})
    except Exception as e:
        print("agent graph err:", e)
    return {"nodes": list(nodes.values()), "links": links}


def build_tree():
    root = {"name": "vault", "path": "", "type": "dir", "children": []}
    for p in sorted(VAULT.rglob("*.md")):
        if any(x in str(p) for x in EXCLUDE):
            continue
        rel_str = str(p.relative_to(VAULT))
        # M5: '95 Secrets' NO se lista en el arbol de Sinapsis (ni la carpeta ni su
        # contenido): lo que no se muestra no se pide.
        if _is_secrets_path(rel_str):
            continue
        parts = rel_str.split("/")
        node = root
        for i, part in enumerate(parts):
            sub = "/".join(parts[:i + 1])
            is_file = (i == len(parts) - 1)
            child = next((c for c in node["children"] if c["name"] == part), None)
            if not child:
                child = {"name": part, "path": sub, "type": "file" if is_file else "dir"}
                if not is_file:
                    child["children"] = []
                node["children"].append(child)
            node = child

    def srt(n):
        if "children" in n:
            n["children"].sort(key=lambda c: (c["type"] == "file", c["name"].lower()))
            for c in n["children"]:
                srt(c)
    srt(root)
    return root


# --- Ola 3a: frontmatter → propiedades (parser puro, sin PyYAML) ---
# El container del visor no trae PyYAML; se implementa un parser line-based que
# cubre los estilos usados en el vault: escalares (con/ sin comillas), listas
# inline `[a, b]`, listas en bloque (`- item`) y mapas anidados (1 nivel). Lo
# que no entiende lo ignora sin romper. Devuelve tipos JSON-friendly.
_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
_FM_KEY_RE = re.compile(r"^([A-Za-z0-9_][\w .-]*?):(?:[ \t]+(.*))?$")
_FM_ITEM_RE = re.compile(r"^[ \t]+-[ \t]*(.*)$")
_FM_INDENT_RE = re.compile(r"^[ \t]+\S")
_FM_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_FM_INT_RE = re.compile(r"^-?\d+$")
_FM_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _fm_unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    return s


def _fm_scalar(raw: str):
    """Convierte un escalar YAML a tipo JSON-friendly (str/int/float/bool/None)."""
    s = raw.strip()
    if s == "":
        return None
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if _FM_INT_RE.match(s):
        try:
            return int(s)
        except ValueError:
            return s
    if _FM_FLOAT_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return s
    return s  # str (incluye fechas ISO, que quedan como texto)


def _fm_split_inline(inner: str):
    """Divide `a, b, "c, d"` respetando comillas. Devuelve lista de escalares."""
    out, cur, quote = [], "", None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ",":
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip() != "" or out:
        out.append(cur.strip())
    return [_fm_unquote(x) for x in out if x.strip() != ""]


def _parse_frontmatter(text: str) -> dict:
    """Extrae y parsea el bloque frontmatter `---...---`. Devuelve {} si no hay."""
    m = _FM_RE.match(text)
    if not m:
        return {}
    lines = m.group(1).split("\n")
    props: dict = {}
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i].rstrip("\r")
        km = _FM_KEY_RE.match(line)
        if not km:
            i += 1
            continue
        key = km.group(1).strip()
        rest = (km.group(2) or "").strip()

        # lista en bloque: valor vacío + siguientes líneas `- item`
        if rest == "" and i + 1 < n and _FM_ITEM_RE.match(lines[i + 1].rstrip("\r")):
            items = []
            j = i + 1
            while j < n:
                im = _FM_ITEM_RE.match(lines[j].rstrip("\r"))
                if not im:
                    break
                v = _fm_unquote(im.group(1).strip())
                if v != "":
                    items.append(v)
                j += 1
            props[key] = items
            i = j
            continue

        # mapa anidado (1 nivel): valor vacío + siguientes líneas indentadas
        if rest == "" and i + 1 < n and _FM_INDENT_RE.match(lines[i + 1].rstrip("\r")):
            sub: dict = {}
            j = i + 1
            while j < n:
                cur = lines[j].rstrip("\r")
                if _FM_INDENT_RE.match(cur):
                    skm = _FM_KEY_RE.match(cur.strip())
                    if skm:
                        sub[skm.group(1).strip()] = _fm_scalar(skm.group(2) or "")
                    j += 1
                elif cur.strip() == "":
                    j += 1
                else:
                    break
            props[key] = sub
            i = j
            continue

        # lista inline `[...]`
        if rest.startswith("[") and rest.endswith("]"):
            props[key] = _fm_split_inline(rest[1:-1])
            i += 1
            continue

        # escalar
        props[key] = _fm_scalar(rest)
        i += 1
    return props


@app.post("/login")
@limiter.limit(lambda: RATE_LIMIT_LOGIN)
async def login(request: Request):
    """Login humano real (Fase 1 authz v2): {username, password}.
    Busca el principal humano activo, verifica con bcrypt y crea una sesión en DB
    (token opaco, token_hash sha256, method='password', TTL 12h).

    H1 (auditoria ): el fallback de emergencia por password global del
    env fue ELIMINADO. Esta es la unica via de login: usuario real + bcrypt.

    H2 (auditoria ): rate-limit por IP (RATE_LIMIT_LOGIN, def 10/minute)
    contra brute-force sobre bcrypt. El parametro se llama `request` (no `req`)
    PORQUE slowapi lo busca por ese nombre exacto en kwargs; renombrarlo rompe el
    endpoint en runtime (py_compile no lo detecta)."""
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    # Login contra la DB (unica via).
    if username and password:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, password_hash, must_change_password FROM agents "
                        "WHERE lower(name)=lower(%s) AND kind='human' AND active=true",
                        (username,))
            row = cur.fetchone()
        if row and _verify_password(password, row["password_hash"]):
            token = secrets.token_urlsafe(32)
            token_hash = _sha256_hex(token)
            with _pg() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO sessions (principal_id, token_hash, method, expires_at) "
                            "VALUES (%s, %s, 'password', now() + interval '12 hours')",
                            (row["id"], token_hash))
                conn.commit()
            return {"token": token,
                    "must_change_password": bool(row.get("must_change_password"))}
    raise HTTPException(401, "credenciales inválidas")


@app.post("/logout")
async def logout(authorization: str = Header(None)):
    """Revoca la sesión actual (sessions.revoked_at=now()). Idempotente."""
    _check(authorization)
    tok = (authorization or "").replace("Bearer ", "").strip()
    if tok:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("UPDATE sessions SET revoked_at=now() "
                        "WHERE token_hash=%s AND revoked_at IS NULL", (_sha256_hex(tok),))
            conn.commit()
    return {"ok": True}


@app.post("/change-password")
@limiter.limit(lambda: RATE_LIMIT_PASSWORD)
async def change_password(request: Request, authorization: str = Header(None)):
    """Rota la contraseña del principal de la sesión: valida old con bcrypt,
    hashea new con bcrypt y actualiza agents.password_hash. Toda sesión válida
    tiene principal en DB (H1: no hay más sesiones sin principal).

    H2: rate-limit por IP (RATE_LIMIT_PASSWORD, def 10/minute) — old_password es
    un oraculo de password igual que /login. El parametro se llama `request` (no
    `req`) PORQUE slowapi lo busca por ese nombre exacto en kwargs; renombrarlo
    rompe el endpoint en runtime (py_compile no lo detecta)."""
    principal = _check(authorization)
    b = await request.json()
    old_pw = b.get("old_password") or ""
    new_pw = b.get("new_password") or ""
    if not old_pw or not new_pw:
        raise HTTPException(400, "old_password y new_password requeridos")
    if len(new_pw) < 8:
        raise HTTPException(400, "la nueva contraseña debe tener al menos 8 caracteres")
    pid = principal.get("id")
    if not pid:
        raise HTTPException(400, "sesión sin principal asociado")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM agents WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row or not _verify_password(old_pw, row["password_hash"]):
            raise HTTPException(401, "contraseña actual incorrecta")
        cur.execute("UPDATE agents SET password_hash=%s, must_change_password=false WHERE id=%s",
                    (_hash_password(new_pw), pid))
        conn.commit()
    return {"ok": True}


@app.get("/me")
def me(authorization: str = Header(None)):
    """Info del principal de la sesión actual (Fase 1.5): name, kind, role, email,
    must_change_password y sus scope_grants activos. Puebla la vista "Mi cuenta".
    H1: toda sesión válida tiene su principal en `agents`; no hay admin sintético."""
    principal = _check(authorization)
    pid = principal.get("id")
    if not pid:
        raise HTTPException(401, "no autorizado")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, kind, role, email, team, is_lead, "
                    "COALESCE(must_change_password, false) AS must_change_password "
                    "FROM agents WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "principal no encontrado")
        cur.execute("SELECT path_prefix, can_write FROM scope_grants "
                    "WHERE principal_id=%s AND revoked_at IS NULL "
                    "ORDER BY path_prefix", (pid,))
        grants = cur.fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "role": row["role"],
        "is_admin": row["kind"] == "human" and row["name"] == ADMIN_USER,
        "admin_user": ADMIN_USER,
        "email": row["email"],
        "team": row["team"],
        "is_lead": bool(row["is_lead"]),
        "must_change_password": bool(row["must_change_password"]),
        # El frontend NO puede derivar este permiso del rol: 'admin' lo comparten
        # seis AGENTES y solo un humano de la allowlist puede leer el bus. Se
        # informa con el MISMO predicado que gatea /api/bus/* (_is_bus_reader), asi
        # la UI no puede ofrecer una entrada que el BFF va a rechazar con 403 (ni
        # esconder una que si esta permitida). Es informativo: el permiso real lo
        # hace cumplir _require_bus_reader en cada request.
        "can_read_bus": _is_bus_reader(row),
        # Tercer permiso nominal de la familia: PUBLICAR no se hereda de leer.
        # Mismo predicado que gatea POST /api/bus/publish.
        "can_publish_bus": _is_bus_publisher(row),
        # Permiso HERMANO y SEPARADO del anterior: leer el bus y poder HABLARLE a un
        # agente son dos autorizaciones distintas con allowlists distintas
        # (CHAT_HUMAN_SENDERS). Se informa con el MISMO predicado que gatea
        # /api/chat/* (_is_chat_sender), asi la UI no ofrece una entrada que el BFF
        # va a rechazar con 403. Es informativo: el permiso real lo hace cumplir
        # _require_chat_sender en CADA request.
        "can_use_chat": _is_chat_sender(row),
        "scope_grants": [
            {"path_prefix": g["path_prefix"], "can_write": bool(g["can_write"])}
            for g in grants
        ],
    }


# ==== FASE 2 authz: panel de administración (config; el enforcement es Fase 3) ====
# Todos los /admin/* exigen role=admin del principal de la sesión (_require_admin).

_PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def _gen_temp_password(n: int = 16) -> str:
    """Contraseña temporal aleatoria fuerte (alfabeto sin caracteres ambiguos)."""
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(n))


def _valid_vault_folder(path_prefix: str) -> bool:
    """True si path_prefix (con trailing slash) es una carpeta real del vault.
    Bloquea traversal. '' (raíz) se considera válido (scope raíz = todo)."""
    pref = (path_prefix or "").strip()
    if pref == "":
        return True
    if not pref.endswith("/"):
        return False
    rel = pref.rstrip("/")
    p = (VAULT / rel).resolve()
    vroot = VAULT.resolve()
    if vroot != p and vroot not in p.parents:
        return False
    return p.is_dir()


@app.post("/admin/users")
async def admin_create_user(req: Request, authorization: str = Header(None)):
    """Crea un usuario humano. Devuelve la contraseña temporal UNA vez (nunca se
    loguea). El INSERT necesita api_key_hash NOT NULL: se usa un placeholder
    sha256 de un token random descartable (los humanos no usan api_key)."""
    admin = _require_admin(authorization)
    b = await req.json()
    username = (b.get("username") or "").strip()
    role = (b.get("role") or "member").strip()
    email = (b.get("email") or "").strip() or None
    if not username:
        raise HTTPException(400, "username requerido")
    if role not in ("admin", "member", "guest"):
        raise HTTPException(400, "rol inválido (admin|member|guest)")
    temp_pw = _gen_temp_password()
    pw_hash = _hash_password(temp_pw)
    # placeholder NOT NULL para api_key_hash (los humanos no usan api_key)
    api_key_placeholder = _sha256_hex("human:" + secrets.token_urlsafe(32))
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE lower(name)=lower(%s)", (username,))
        if cur.fetchone():
            raise HTTPException(409, "ya existe un principal con ese nombre")
        cur.execute(
            "INSERT INTO agents (name, api_key_hash, kind, role, password_hash, email, "
            "must_change_password, active, created_by) "
            "VALUES (%s,%s,'human',%s,%s,%s,true,true,%s) RETURNING id",
            (username, api_key_placeholder, role, pw_hash, email, admin.get("name") or ADMIN_USER),
        )
        new_id = cur.fetchone()["id"]
        _audit(cur, admin.get("name") or ADMIN_USER, "admin_create_user",
               f"user={username} role={role} id={new_id}")
        conn.commit()
    # temp_pw se devuelve UNA vez; jamás se loguea.
    return {"ok": True, "id": new_id, "name": username, "role": role,
            "temp_password": temp_pw}


@app.get("/admin/principals")
def admin_principals(authorization: str = Header(None)):
    """Lista todos los principals (humanos + agentes) con owner y scope_grants
    activos. Puebla la vista de administración."""
    _require_admin(authorization)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.name, a.kind, a.role, a.team, a.active, "
            "a.owner_principal_id, o.name AS owner_name "
            "FROM agents a LEFT JOIN agents o ON o.id = a.owner_principal_id "
            "ORDER BY a.kind, lower(a.name)")
        principals = cur.fetchall()
        cur.execute(
            "SELECT id, principal_id, path_prefix, can_write "
            "FROM scope_grants WHERE revoked_at IS NULL ORDER BY path_prefix")
        grants = cur.fetchall()
    by_principal: dict = {}
    for g in grants:
        by_principal.setdefault(g["principal_id"], []).append(
            {"id": g["id"], "path_prefix": g["path_prefix"], "can_write": bool(g["can_write"])})
    out = []
    for p in principals:
        out.append({
            "id": p["id"], "name": p["name"], "kind": p["kind"], "role": p["role"],
            "team": p["team"], "active": bool(p["active"]),
            "owner_principal_id": p["owner_principal_id"], "owner_name": p["owner_name"],
            "scope_grants": by_principal.get(p["id"], []),
        })
    return {"principals": out}


@app.post("/admin/grants")
async def admin_create_grant(req: Request, authorization: str = Header(None)):
    """Crea un scope_grant (principal_id, path_prefix con trailing slash, can_write).
    Valida que path_prefix sea una carpeta real del vault. Loguea en audit_log."""
    admin = _require_admin(authorization)
    b = await req.json()
    principal_id = b.get("principal_id")
    path_prefix = (b.get("path_prefix") or "").strip()
    can_write = bool(b.get("can_write"))
    if not principal_id:
        raise HTTPException(400, "principal_id requerido")
    if not path_prefix:
        raise HTTPException(400, "path_prefix requerido")
    if not path_prefix.endswith("/"):
        path_prefix += "/"
    if not _valid_vault_folder(path_prefix):
        raise HTTPException(400, f"no es una carpeta válida del vault: {path_prefix}")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM agents WHERE id=%s", (principal_id,))
        pr = cur.fetchone()
        if not pr:
            raise HTTPException(404, "principal no existe")
        cur.execute(
            "INSERT INTO scope_grants (principal_id, path_prefix, can_write, granted_by) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (principal_id, path_prefix, can_write,
             admin.get("id") if admin.get("id") else None))
        grant_id = cur.fetchone()["id"]
        _audit(cur, admin.get("name") or ADMIN_USER, "admin_grant_add",
               f"principal={pr['name']}({principal_id}) prefix={path_prefix} write={can_write} grant={grant_id}")
        conn.commit()
    return {"ok": True, "id": grant_id, "principal_id": principal_id,
            "path_prefix": path_prefix, "can_write": can_write}


@app.post("/admin/grants/revoke")
async def admin_revoke_grant(req: Request, authorization: str = Header(None)):
    """Revoca un scope_grant (set revoked_at=now()). Loguea en audit_log."""
    admin = _require_admin(authorization)
    b = await req.json()
    grant_id = b.get("grant_id")
    if not grant_id:
        raise HTTPException(400, "grant_id requerido")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE scope_grants SET revoked_at=now() "
            "WHERE id=%s AND revoked_at IS NULL "
            "RETURNING id, principal_id, path_prefix", (grant_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "grant no encontrado o ya revocado")
        _audit(cur, admin.get("name") or ADMIN_USER, "admin_grant_revoke",
               f"grant={grant_id} principal={row['principal_id']} prefix={row['path_prefix']}")
        conn.commit()
    return {"ok": True, "grant_id": grant_id}


@app.post("/admin/assign-agent")
async def admin_assign_agent(req: Request, authorization: str = Header(None)):
    """Asocia un agente a un usuario humano dueño (owner_principal_id). Pasar
    owner_principal_id=null desasocia. Loguea en audit_log."""
    admin = _require_admin(authorization)
    b = await req.json()
    agent_id = b.get("agent_id")
    owner_id = b.get("owner_principal_id")
    if not agent_id:
        raise HTTPException(400, "agent_id requerido")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, kind FROM agents WHERE id=%s", (agent_id,))
        ag = cur.fetchone()
        if not ag:
            raise HTTPException(404, "agente no existe")
        if ag["kind"] != "agent":
            raise HTTPException(400, "el principal a asociar debe ser un agente")
        if owner_id:
            cur.execute("SELECT id, name, kind FROM agents WHERE id=%s", (owner_id,))
            owner = cur.fetchone()
            if not owner:
                raise HTTPException(404, "usuario dueño no existe")
            if owner["kind"] != "human":
                raise HTTPException(400, "el dueño debe ser un usuario humano")
        cur.execute("UPDATE agents SET owner_principal_id=%s WHERE id=%s",
                    (owner_id or None, agent_id))
        _audit(cur, admin.get("name") or ADMIN_USER, "admin_assign_agent",
               f"agent={ag['name']}({agent_id}) owner={owner_id}")
        conn.commit()
    return {"ok": True, "agent_id": agent_id, "owner_principal_id": owner_id or None}


@app.get("/api/graph")
def api_graph(authorization: str = Header(None)):
    principal = _check(authorization)
    global _cache
    if _cache is None:
        _cache = build_graph()
    # admin (scope raiz) ve el grafo completo sin copia.
    if any(pref == "" for pref, _ in scopes_of(principal)):
        return _cache
    # Filtro por scope: se conservan solo los nodos-nota cuyo path esta permitido,
    # ademas de los nodos sin path (tareas/proyectos/agentes/memoria). Se descartan
    # los links que apunten a un nodo removido para no dejar aristas colgadas.
    kept_ids = set()
    nodes = []
    for n in _cache["nodes"]:
        p = n.get("path")
        if p and n.get("type") == "note" and not path_allowed(principal, p):
            continue
        kept_ids.add(n["id"])
        nodes.append(n)
    links = [l for l in _cache["links"]
             if l["source"] in kept_ids and l["target"] in kept_ids]
    return {"nodes": nodes, "links": links}


@app.post("/api/refresh")
def api_refresh(authorization: str = Header(None)):
    _check(authorization)
    global _cache
    _cache = build_graph()
    return {"nodes": len(_cache["nodes"]), "links": len(_cache["links"])}


@app.get("/api/tree")
def api_tree(authorization: str = Header(None)):
    principal = _check(authorization)
    tree = build_tree()
    if any(pref == "" for pref, _ in scopes_of(principal)):
        return tree
    pruned = _prune_tree_to_scope(tree, principal)
    return pruned if pruned is not None else {"name": "vault", "path": "", "type": "dir", "children": []}


@app.get("/api/node")
def api_node(path: str, authorization: str = Header(None)):
    """Contenido de una nota .md del vault. Scope enforced (path_allowed) y deny de
    '95 Secrets' via _safe_path (M5)."""
    principal = _check(authorization)
    rel, p = _safe_path(path)
    if not path_allowed(principal, rel):
        raise HTTPException(403, "fuera de tu scope")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "no es un archivo del vault")
    data = p.read_bytes()
    try:
        content = data.decode("utf-8")
        lossy = False
    except UnicodeDecodeError:
        content = data.decode("utf-8", errors="ignore")
        lossy = True
    # lossy informa una lectura tolerante; file_save rechaza su round-trip con 422.
    return {"path": rel, "content": content, "version": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "lossy": lossy}


@app.post("/api/file_save")
async def api_file_save(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    content = b.get("content")
    if content is None:
        raise HTTPException(400, "falta content")
    rel, p = _safe_path(b.get("path") or "")
    if not path_allowed(principal, rel, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    if "base_version" not in b:
        raise HTTPException(409, "Falta el token de version: recarga la pagina y la nota antes de guardar (editor desactualizado).")
    data = content.encode("utf-8")
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "file_save", rel)
    with _vault_parent(rel, create=True) as (dir_fd, name):
        with _vault_leaf(dir_fd, name, missing_ok=True) as fd:
            if fd is None:
                current_data, current_version = None, None
            else:
                with os.fdopen(os.dup(fd), "rb") as current:
                    current_data = current.read()
                current_version = hashlib.sha256(current_data).hexdigest()
        base_version = b["base_version"]
        if base_version is None and current_version is not None:
            raise HTTPException(409, "Se esperaba un archivo nuevo y ya existe: recarga la nota antes de guardar.")
        if base_version != current_version:
            raise HTTPException(409, "La nota cambio en disco desde que la abriste (o ya no existe): recarga la nota antes de guardar.")
        if current_version is not None:
            try:
                current_data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(422, "UTF-8 invalido (lossy): no se puede guardar esta nota sin perder bytes. Repara la codificacion por una via que preserve el original.")
        # Still NOT a writer lock: the version check and replace are not a CAS.
        _atomic_write_bytes(dir_fd, name, data)
    _audit_fs_commit(_audit_id)
    return {"ok": True, "path": rel, "bytes": len(data),
            "version": hashlib.sha256(data).hexdigest()}


@app.post("/api/file_create")
async def api_file_create(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    path = b.get("path") or ""
    if not path.endswith(".md"):
        path += ".md"
    rel, p = _safe_path(path)
    if not path_allowed(principal, rel, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "file_create", rel)
    with _vault_parent(rel, create=True) as (dir_fd, name):
        _atomic_write_bytes(dir_fd, name, b.get("content", "").encode("utf-8"), create=True)
    _audit_fs_commit(_audit_id)
    return {"ok": True, "path": rel}


@app.post("/api/file_rename")
async def api_file_rename(req: Request, authorization: str = Header(None)):
    """Renombra/mueve un .md dentro del vault. 409 si el destino existe."""
    principal = _check(authorization)
    b = await req.json()
    src = b.get("path") or ""
    dst = b.get("new_path") or ""
    if not src or not dst:
        raise HTTPException(400, "faltan path/new_path")
    if not dst.endswith(".md"):
        dst += ".md"
    rel_from, p_from = _safe_path(src)
    rel_to, p_to = _safe_path(dst)
    if not path_allowed(principal, rel_from, write=True) or not path_allowed(principal, rel_to, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "file_rename", f"{rel_from} -> {rel_to}")
    with _vault_parent(rel_from) as (src_dir_fd, src_name):
        _leaf_stat(src_dir_fd, src_name, directory_error=(404, "origen no existe"))
        with _vault_parent(rel_to, create=True) as (dst_dir_fd, dst_name):
            if _leaf_stat(dst_dir_fd, dst_name, missing_ok=True,
                          directory_error=(409, "destino ya existe")) is not None:
                raise HTTPException(409, "destino ya existe")
            os.rename(src_name, dst_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            _sync_dir(src_dir_fd)
            _sync_dir(dst_dir_fd)
    _audit_fs_commit(_audit_id)
    return {"ok": True, "from": rel_from, "to": rel_to}


@app.post("/api/file_delete")
async def api_file_delete(req: Request, authorization: str = Header(None)):
    """SOFT delete: mueve a .trash/<UTC>__<nombre-plano>. NUNCA os.remove (Regla de Oro)."""
    principal = _check(authorization)
    b = await req.json()
    rel, p = _safe_path(b.get("path") or "")
    if not path_allowed(principal, rel, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "file_delete", rel)
    with _vault_parent(rel) as (src_dir_fd, src_name):
        _leaf_stat(src_dir_fd, src_name, directory_error=(404, "no existe"))
        with _vault_dir(TRASH_DIR, create=True) as dst_dir_fd:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            flat = rel.replace("/", "__")
            dest_name = f"{ts}__{flat}"
            if _leaf_stat(dst_dir_fd, dest_name, missing_ok=True) is not None:
                dest_name = f"{ts}__{secrets.token_hex(3)}__{flat}"
            os.rename(src_name, dest_name, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            _sync_dir(src_dir_fd)
            _sync_dir(dst_dir_fd)
    _audit_fs_commit(_audit_id)
    return {"ok": True, "trashed_to": f"{TRASH_DIR}/{dest_name}"}


# Distinct caps: reject after 500 by default, but count exactly up to 5000.
DIR_DELETE_MAX_FILES = max(0, int(os.environ.get("DIR_DELETE_MAX_FILES", "500")))
DIR_COUNT_CAP = max(DIR_DELETE_MAX_FILES, int(os.environ.get("DIR_COUNT_CAP", "5000")))


def _dir_delete_target(principal, path: str, write: bool):
    # _safe_path denies 95 Secrets with 403 on the RESOLVED path first;
    # the explicit guard below only handles a path that passed that first layer.
    rel, p = _safe_path(path, must_be_md=False)
    if not path_allowed(principal, rel, write=write):
        raise HTTPException(403, "fuera de tu scope de escritura" if write else "fuera de tu scope de lectura")
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, "carpeta no existe")
    vroot = VAULT.resolve()
    if rel == "" or p == vroot:
        raise HTTPException(400, "no se puede eliminar la raíz del vault")
    resolved_rel = str(p.relative_to(vroot))
    if resolved_rel == TRASH_DIR or resolved_rel.startswith(TRASH_DIR + "/"):
        raise HTTPException(400, "no se puede eliminar .trash desde este endpoint")
    if _is_secrets_path(resolved_rel):
        raise HTTPException(400, "no se puede eliminar 95 Secrets")
    # Check EVERY requested path component, not contents: A/B -> C/D within
    # vault would let textual scope A/B/x authorize access to C/D/x. An internal
    # symlink inside the selected directory is allowed and counted, not followed.
    component = VAULT
    for segment in Path(rel).parts:
        component = component / segment
        if component.is_symlink():
            raise HTTPException(400, "no se puede eliminar una carpeta symlink ni atravesar symlinks")
    if not path_allowed(principal, resolved_rel, write=write):
        raise HTTPException(403, "fuera de tu scope de escritura" if write else "fuera de tu scope de lectura")
    return rel, p


def _dir_stats(p: Path):
    files = symlinks = 0
    # scandir does not follow symlinked directories, including external links.
    pending = [p]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.is_symlink():
                    symlinks += 1
                    files += 1
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                else:
                    files += 1
                if files > DIR_COUNT_CAP:
                    return DIR_COUNT_CAP, symlinks, True
    return files, symlinks, False


@app.get("/api/dir_stats")
def api_dir_stats(path: str, authorization: str = Header(None)):
    principal = _check(authorization)
    _, p = _dir_delete_target(principal, path, write=False)
    files, symlinks, truncated = _dir_stats(p)
    return {"files": files, "symlinks": symlinks, "truncated": truncated,
            "max_files": DIR_DELETE_MAX_FILES, "allowed": not truncated and files <= DIR_DELETE_MAX_FILES}


@app.post("/api/dir_delete")
async def api_dir_delete(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    rel, p = _dir_delete_target(principal, b.get("path") or "", write=True)
    files, symlinks, truncated = _dir_stats(p)
    if truncated:
        raise HTTPException(409, f"La carpeta contiene más de {DIR_COUNT_CAP} archivos; el tope es {DIR_DELETE_MAX_FILES}. No se movió.")
    if files > DIR_DELETE_MAX_FILES:
        raise HTTPException(409, f"La carpeta contiene {files} archivos; el tope es {DIR_DELETE_MAX_FILES}. No se movió.")
    trash = VAULT / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    # Preflight only: same-device gives a clearer error before rename might raise EXDEV;
    # it does not guarantee that the rename will succeed.
    if trash.is_symlink() or trash.stat().st_dev != p.stat().st_dev:
        raise HTTPException(409, ".trash no está en el mismo filesystem o es symlink")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    flat = rel.replace("/", "__")
    dest = trash / f"{ts}__{flat}"
    if os.path.lexists(dest):
        dest = trash / f"{ts}__{secrets.token_hex(3)}__{flat}"
    if os.path.lexists(dest):
        raise HTTPException(409, "el destino en .trash ya existe; no se movió")
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "dir_delete", rel)
    # Bare os.rename ONLY: shutil.move falls back to copytree + rmtree on rename
    # failure, even on the same device; never copy or delete a directory here.
    try:
        os.rename(p, dest)
    except OSError as exc:
        raise HTTPException(500, f"No se pudo mover la carpeta de forma segura (errno={exc.errno}); no se intentó copiar ni borrar") from exc
    _audit_fs_commit(_audit_id)
    return {"ok": True, "trashed_to": str(dest.relative_to(VAULT)), "files": files, "symlinks": symlinks}


@app.post("/api/file_upload")
async def api_file_upload(file: UploadFile = File(...), dir: str = Form(""),
                          authorization: str = Header(None)):
    """Sube un adjunto NO-md (png/jpg/gif/svg/pdf/webp). Cap 25 MB."""
    principal = _check(authorization)
    raw = await file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "archivo supera 25 MB")
    name = _sanitize_filename(file.filename or "upload")
    ext = os.path.splitext(name)[1].lower()
    if ext not in UPLOAD_EXTS:
        raise HTTPException(400, f"extensión no permitida: {ext or '(sin extensión)'}")
    rel_dir, p_dir = _safe_path(dir or "", must_be_md=False)
    dest_rel = (rel_dir + "/" + name) if rel_dir else name
    if not path_allowed(principal, dest_rel, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    # Resolve only for the existing containment policy. IO must keep the original
    # basename so an internal leaf symlink cannot turn into another file name.
    resolved_dest = (p_dir / name).resolve()
    vroot = VAULT.resolve()
    if vroot not in resolved_dest.parents:
        raise HTTPException(403, "fuera del vault")
    dest = p_dir / name
    if dest.exists():
        stem, e = os.path.splitext(name)
        dest = p_dir / f"{stem}-{secrets.token_hex(3)}{e}"
    final_rel = str(dest.relative_to(VAULT))
    _audit_id = _audit_fs_begin(principal.get("name") or ADMIN_USER, "file_upload", final_rel)
    with _vault_dir(rel_dir, create=True) as dir_fd:
        _leaf_stat(dir_fd, name, missing_ok=True)  # also reject an existing symlink before suffixing.
        _atomic_write_bytes(dir_fd, dest.name, raw)
    _audit_fs_commit(_audit_id)
    return {"ok": True, "path": final_rel}


@app.get("/api/files")
def api_files(ext: str = "", authorization: str = Header(None)):
    """Lista archivos. Con ?ext=png,jpg filtra por extensiones; sin ext lista los NO-md.
    Excluye EXCLUDE + .trash + .smart-env."""
    principal = _check(authorization)
    exts = None
    if ext.strip():
        exts = {("." + e.strip().lstrip(".")).lower() for e in ext.split(",") if e.strip()}
    out = []
    for p in VAULT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(VAULT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        if _is_secrets_path(str(rel)):  # M5
            continue
        if not path_allowed(principal, str(rel)):
            continue
        e = p.suffix.lower()
        if exts is None:
            if e == ".md":
                continue
        elif e not in exts:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append({"path": str(rel), "ext": e, "size": size})
    return {"files": out}


@app.get("/api/recent")
def api_recent(limit: int = 20, authorization: str = Header(None)):
    """Archivos del vault ordenados por mtime desc (últimas ediciones/subidas).

    Incluye .md (notas editadas) y adjuntos (subidos). Excluye EXCLUDE + .trash +
    .smart-env (vía SKIP_DIRS). Cada item: {path, mtime, ext, is_md}. mtime = epoch s.
    """
    principal = _check(authorization)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    out = []
    for p in VAULT.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(VAULT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        if _is_secrets_path(str(rel)):  # M5
            continue
        if not path_allowed(principal, str(rel)):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        e = p.suffix.lower()
        out.append({"path": str(rel), "mtime": mtime, "ext": e, "is_md": e == ".md"})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"recent": out[:limit]}


@app.get("/api/collection")
def api_collection(dir: str = "", recursive: int = 0, authorization: str = Header(None)):
    """Trata una carpeta como base de datos (fusión Notion): cada `.md` es una fila
    y su frontmatter son las propiedades.

    `dir` = carpeta relativa dentro del vault ('' = raíz). `recursive=1` desciende a
    subcarpetas. Devuelve {items:[{path, title, mtime, properties:{...}}], ...}.
    Excluye EXCLUDE + .trash + .smart-env. Auth Bearer. Valida `dir` dentro del vault.
    """
    principal = _check(authorization)
    rel_dir, p_dir = _safe_path(dir or "", must_be_md=False)
    if not p_dir.exists() or not p_dir.is_dir():
        raise HTTPException(404, "no es una carpeta del vault")
    it = p_dir.rglob("*.md") if recursive else p_dir.glob("*.md")
    items = []
    for p in it:
        if not p.is_file():
            continue
        rel = p.relative_to(VAULT)
        if set(rel.parts) & SKIP_DIRS:
            continue
        if _is_secrets_path(str(rel)):  # M5: recursive=1 desde la raiz no debe leer secrets
            continue
        if not path_allowed(principal, str(rel)):
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
            mtime = p.stat().st_mtime
        except OSError:
            continue
        props = _parse_frontmatter(txt)
        # Título: propiedad `title` → primer H1 del cuerpo → nombre del archivo.
        title = None
        t = props.get("title")
        if isinstance(t, str) and t.strip():
            title = t.strip()
        if not title:
            body = txt[_FM_RE.match(txt).end():] if _FM_RE.match(txt) else txt
            hm = _FM_H1_RE.search(body)
            title = hm.group(1).strip() if hm else p.stem
        items.append({"path": str(rel), "title": title, "mtime": mtime, "properties": props})
    items.sort(key=lambda x: x["title"].lower())
    return {"dir": rel_dir, "recursive": bool(recursive), "count": len(items), "items": items}


@app.post("/api/reindex")
async def api_reindex(req: Request, authorization: str = Header(None)):
    """Confirma (síncrono) que el índice del cerebro refleja el archivo tras editarlo.

    Hallazgo: el indexer usa un watchdog (inotify) continuo sobre el mismo bind-mount
    que escribe el visor, con dedup por source_hash (sha256 de bytes) e IDs de chunk
    determinísticos. Por eso las ediciones se reindexan solas (~1s) — un re-chunk manual
    sería redundante. Este endpoint solo dispara/espera y confirma.
    """
    _check(authorization)
    b = await req.json()
    rel, p = _safe_path(b.get("path") or "")
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "no existe")
    source_path = INDEX_PREFIX + rel
    file_hash = hashlib.sha256(p.read_bytes()).hexdigest()

    # 1. ya está al día
    if _current_index_hash(source_path) == file_hash:
        return {"ok": True, "status": "current", "source_path": source_path}

    # 2. esperar a que el watchdog del indexer alcance
    for _ in range(16):
        await asyncio.sleep(0.5)
        if _current_index_hash(source_path) == file_hash:
            return {"ok": True, "status": "reindexed", "source_path": source_path}

    # 3. nudge: re-escribir bytes idénticos (hash intacto) para re-disparar el watchdog
    p.write_bytes(p.read_bytes())
    for _ in range(16):
        await asyncio.sleep(0.5)
        if _current_index_hash(source_path) == file_hash:
            return {"ok": True, "status": "reindexed_after_nudge", "source_path": source_path}

    # 4. no alcanzó dentro del timeout
    return {"ok": False, "status": "pending", "source_path": source_path,
            "detail": "el indexer no confirmó el reindex dentro del timeout"}


@app.get("/api/search")
def api_search(q: str, authorization: str = Header(None)):
    principal = _check(authorization)
    try:
        # AUTHZ T1: mismo builder de headers. `/memory/search` NO esta en el default de
        # DELEGATION_ROUTES => hoy esto emite exactamente los headers historicos. El
        # mecanismo queda cableado para que habilitarlo sea un cambio de env, no de codigo.
        r = httpx.post(f"{GATEWAY}/memory/search", headers=_gw_headers(principal, "/memory/search"),
                       json={"query": q, "top_k": 25}, timeout=30)
        r.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(504, "el gateway no respondió /memory/search en 30s")
    except httpx.HTTPStatusError as exc:
        detail = (exc.response.text or "").strip().replace("\n", " ")[:500]
        suffix = f": {detail}" if detail else ""
        raise HTTPException(
            503, f"gateway /memory/search respondió HTTP {exc.response.status_code}{suffix}"
        )
    except httpx.RequestError as exc:
        raise HTTPException(502, f"no se pudo consultar el gateway (/memory/search): {exc}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise HTTPException(502, "gateway /memory/search devolvió JSON inválido") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
        raise HTTPException(502, "gateway /memory/search devolvió un contrato inválido")

    out = []
    for x in payload.get("results", []):
        sp = x.get("source_path") or ""
        # Filtro de scope sobre el source_path devuelto por el gateway (Fase 3a:
        # SINAPSIS filtra la respuesta; el filtro en el gateway es Fase 3b).
        if _is_secrets_path(sp):  # M5: ni el hit metadata-only de '95 Secrets'
            continue
        if not path_allowed(principal, sp):
            continue
        # FIX 95943baa (): el gateway devuelve DOS scores y hay que
        # exponer el interpretable. `score` en modo hibrido es **RRF**, cuyo techo
        # matematico es ~0.0328 => leido contra la escala de coseno (>0.7 muy
        # relevante ... <0.3 irrelevante) hacia que TODO resultado del Cerebro
        # pareciera irrelevante. `score_cosine` es el que se puede interpretar.
        # Se manda el coseno como `score` (lo que la UI ya pinta) y se conservan
        # ambos crudos para no perder informacion de ranking.
        _cos = x.get("score_cosine")
        _rrf = x.get("score")
        out.append({"path": sp[len("obsidian-vault/"):] if sp.startswith("obsidian-vault/") else sp,
                    "title": x.get("title"),
                    "score": round(_cos if _cos is not None else (_rrf or 0), 3),
                    "score_cosine": None if _cos is None else round(_cos, 3),
                    "score_rrf": None if _rrf is None else round(_rrf, 4),
                    "score_kind": "cosine" if _cos is not None else "rrf",
                    "owner": x.get("agent_owner"), "snippet": (x.get("text") or "")[:240]})
    return {"results": out}


# ==== Lectura del BUS (solo el lector autorizado): PROXY de solo-lectura ====
# El gateway YA tiene la lectura hecha (GET /bus/threads, GET /bus/thread, GET /bus/group)
# y YA esta blindado contra el "hilo fantasma": conversations.list_threads filtra
# `conversation_id <> '' AND IS NOT NULL` para los hilos declarados y agrupa los mensajes
# SIN hilo (`IS NULL OR = ''`) por conjunto canonico de participantes en vez de por el campo
# crudo; /bus/thread y /bus/group rechazan el identificador vacio con 422 (min_length=1).
# Aca NO se reimplementa NADA de eso: solo se proxya y se propaga (incluidos los filtros
# agent/project/since/until/q, que se resuelven en SQL del lado del gateway).
# Las rutas GET siguen siendo sólo lectura y conservan _require_bus_reader. La única
# escritura nueva es POST /api/bus/publish, con guard nominal propio. bus_notify NO se toca.
_BUS_TIMEOUT = 15.0


# ==== AUTHZ T1 (): EMISION DE LA IDENTIDAD DEL HUMANO AL GATEWAY ====
# EL DEFECTO QUE CIERRA: este BFF autentica al humano por SESION (bcrypt + tabla
# `sessions`) y después habla con el gateway con la credencial propia de `sinapsis`
# (SEARCH_KEY). Antes de T0 usaba la de `assistant-b`, por lo que T1 descartaba la
# delegación y atribuía las lecturas al principal equivocado.
#
# NO se resuelve identidad aca: ya esta resuelta. `_resolve_session` devuelve
# {id, name, role, kind} y `_check` la exige viva. Esto solo TRANSMITE un valor ya
# calculado, y lo hace siguiendo el precedente explicito de `_bff_actor` (fix H3):
# EL PRINCIPAL SALE SIEMPRE DE LA SESION, NUNCA DEL BODY NI DE UN QUERY PARAM.
# Un header poblado desde entrada del cliente seria el agujero, no el fix.
DELEGATION_HEADER = "X-Cerebro-On-Behalf-Of"

# ---- FLAG POR RUTA -------------------------------------------------------------
# EL FLAG NO PUEDE IR POR CALL-SITE. `_bus_proxy` es un proxy GENERICO
# (`httpx.get(f"{GATEWAY}{path}")`): UNA linea que sirve N rutas (/bus/threads,
# /bus/thread, /bus/group ... y cualquiera que se agregue). Si el flag se decidiera por
# call-site, activar "solo /bus/*" activaria TAMBIEN todo lo que pase por esa linea
# -- exactamente lo que el flag existe para evitar. => El filtro se evalua sobre el
# `path`, DENTRO del proxy.
#
# DEFAULT: '/bus/' en ON, TODO LO DEMAS en OFF. Un disparador nuevo nace deshabilitado
# (Harness v4.1 sec.5). El motivo de que /memory/search y /vault/read arranquen en OFF NO
# es cautela generica: migrarlos hace que esas lecturas se resuelvan con el scope de
# `admin` en vez del de `assistant-b` => CAMBIA EL RESULTADO de las busquedas del panel, y eso
# lo tiene que medir T2 antes. El flag permite avanzar sin apostar.
#
# Formato: rutas separadas por coma. Sin slash final = igualdad exacta; con slash
# final = prefijo deliberado. Cadena vacia => delegacion COMPLETAMENTE apagada.
# Así /bus/ conserva su semántica histórica y una ruta nueva no nace delegada.
DELEGATION_ROUTES = tuple(
    p.strip() for p in os.environ.get("DELEGATION_ROUTES", "/bus/").split(",")
    if p.strip()
)


def _delegation_enabled_for(path: str) -> bool:
    """True si la delegacion esta habilitada para ESTE path del gateway.

    Se evalua sobre el `path` y no sobre el call-site: ver el comentario de
    DELEGATION_ROUTES. Una entrada terminada en '/' es prefijo deliberado; cualquier
    otra exige igualdad exacta. Ambos lados se normalizan a leading slash."""
    p = (path or "").strip()
    if not p:
        return False
    if not p.startswith("/"):
        p = "/" + p
    for configured in DELEGATION_ROUTES:
        route = configured if configured.startswith("/") else "/" + configured
        if (route.endswith("/") and p.startswith(route)) or p == route:
            return True
    return False


def _gw_headers(principal, path: str) -> dict:
    """Headers para hablarle al gateway: la credencial del BFF + (si la ruta esta
    habilitada) la delegacion del principal HUMANO de la sesion.

    El nombre sale de `principal.get("name")` -- la sesion ya resuelta --, mismo origen
    que usa `_bff_actor`. Si el principal no es kind='human' NO se emite: el gateway lo
    rechazaria igual (exige kind='human'), y emitirlo seria mandar un header que sabemos
    invalido. Doble validacion deliberada: el emisor no manda basura Y el receptor no
    confia en el emisor."""
    h = {"X-Merlin-Key": SEARCH_KEY}
    if not _delegation_enabled_for(path):
        return h
    if not isinstance(principal, dict):
        return h
    name = (principal.get("name") or "").strip()
    if not name:
        return h
    if (principal.get("kind") or "").strip().lower() != "human":
        return h
    h[DELEGATION_HEADER] = name
    return h


def _bus_proxy(path: str, params: dict, principal=None) -> Response:
    """GET {GATEWAY}{path} con la key que ya usa el BFF; propaga status y cuerpo.

    AUTHZ T1: si se pasa `principal` (el de la sesion, ya resuelto por el guard) y la
    ruta esta habilitada en DELEGATION_ROUTES, se emite ademas el header de delegacion
    para que el gateway atribuya la lectura al HUMANO y no a la credencial del BFF.
    `principal=None` => comportamiento historico exacto (sin delegacion).

    El status del gateway viaja TAL CUAL (422/404/500 llegan al cliente). Traducir un
    error del gateway a `200 []` haria indistinguible "no hay hilos" de "no pudimos
    leer". Timeout explicito; si vence => 504 con causa legible, nunca vacio."""
    try:
        # AUTHZ T1: el flag se resuelve DENTRO de esta linea, sobre el `path`, porque
        # esta unica llamada sirve N rutas distintas.
        r = httpx.get(f"{GATEWAY}{path}", headers=_gw_headers(principal, path),
                      params=params, timeout=_BUS_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el gateway no respondió {path} en {_BUS_TIMEOUT:.0f}s")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"no se pudo consultar el gateway ({path}): {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


def _normalize_bus_recipients(raw) -> list[str]:
    """Acepta array o coma-separado; normaliza trim/lower/dedup preservando orden."""
    parts = raw if isinstance(raw, list) else str(raw or "").split(",")
    out = []
    for value in parts:
        if not isinstance(value, str):
            raise HTTPException(422, "recipients debe ser array de strings o string coma-separado")
        name = value.strip().lower()
        if name and name not in out:
            out.append(name)
    if not out:
        raise HTTPException(422, "recipients debe contener al menos un agente")
    return sorted(out)


def _bus_publish_denied_audit(authorization, recipients, content_len: int, status: int):
    """Audita el 403 con sentinela explícito; nunca atribuye anónimo a ADMIN_USER."""
    who = _resolve_session((authorization or "").replace("Bearer ", "").strip())
    recs = ",".join(recipients)[:256] if recipients else "-"
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, (who or {}).get("name") or "desconocido",
               "sinapsis_bus_publish_denied",
               f"status={status} recipients={recs} content_len={content_len}",
               success=False)
        conn.commit()


@app.post("/api/bus/publish")
async def api_bus_publish(req: Request, authorization: str = Header(None)):
    """Publica como el HUMANO de la sesión, nunca como el BFF.

    Auditoría obligatoria antes del gateway: sólo longitud y destinatarios, jamás texto.
    Si auditar falla, no se llama al gateway. La delegación T1 para /bus/ hace que
    verify_agent derive sender='admin' (o el humano nominal autorizado).
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Se extrae metadata mínima antes del guard exclusivamente para auditar un rechazo.
    raw_recipients = body.get("recipients")
    try:
        audit_recipients = _normalize_bus_recipients(raw_recipients)
    except HTTPException:
        audit_recipients = []
    raw_content = body.get("content")
    audit_content_len = len(raw_content) if isinstance(raw_content, str) else 0
    try:
        principal = _require_bus_publisher(authorization)
    except HTTPException as e:
        _bus_publish_denied_audit(
            authorization, audit_recipients, audit_content_len, e.status_code)
        raise

    recipients = _normalize_bus_recipients(raw_recipients)
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise HTTPException(422, "content no puede estar vacío")
    payload = {
        "recipients": ",".join(recipients),
        "content": raw_content.strip(),
        "performative": str(body.get("performative") or "inform").strip().lower(),
        "refs_task": str(body.get("refs_task") or "").strip(),
        "conversation_id": str(body.get("conversation_id") or "").strip(),
        "idempotency_key": body.get("idempotency_key"),
    }

    # Fail-closed: este commit debe completar ANTES de cualquier efecto en el bus.
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or "desconocido", "sinapsis_bus_publish",
               f"recipients={','.join(recipients)[:256]} content_len={len(payload['content'])}")
        conn.commit()

    path = "/bus/publish"
    try:
        r = httpx.post(f"{GATEWAY}{path}", headers=_gw_headers(principal, path),
                       json=payload, timeout=_BUS_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, "no sabemos si se envió: el gateway no respondió a tiempo")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"no sabemos si se envió: error de transporte al gateway: {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


@app.get("/api/bus/threads")
def api_bus_threads(agent: str = "", limit: int = Query(50, ge=1, le=500),
                    include_unthreaded: bool = False,
                    project: str = "",
                    since: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                    until: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                    q: str = "",
                    authorization: str = Header(None)):
    """Hilos del bus (conversation_id no vacío): count, participantes, último mensaje.

    `include_unthreaded=true` suma las conversaciones IMPLÍCITAS (mensajes SIN
    conversation_id agrupados por participantes); en ese modo cada item trae `kind`
    ('thread' | 'implicit'), que decide el GATEWAY. Sin el parámetro la respuesta es
    idéntica a la histórica: cambio aditivo, cero regresión.

    Filtros opcionales combinables (AND), todos propagados al gateway: project (vía
    refs_task → tasks.project; los mensajes sin refs_task no tienen proyecto y quedan
    FUERA), since/until ('YYYY-MM-DD'; `until` incluye el día completo) y q
    (coincidencia de TEXTO, no búsqueda semántica: el bus no está indexado en Qdrant).

    Gate: _require_bus_reader (NO _require_admin — ver el comentario del guard).

    AUDITORIA (detector de fuga): se registra en audit_log el acceso atribuido al
    principal HUMANO que devuelve el guard; el gateway recibe la credencial propia
    de Sinapsis y la delegación verificada. Se audita TAMBIÉN el rechazo
    ('bus_threads_denied'): un 403 es la senal de que alguien INTENTO leer el bus.
    Se audita ANTES de consultar el gateway: asi el contenido de los mensajes no
    existe todavia en este scope y no puede filtrarse a audit_log ni por error.
    Si el INSERT de auditoria falla, la request falla y NO se sirve nada
    (fail-closed: una lectura que no podemos atribuir no se entrega).

    Los filtros se auditan TRUNCADOS (64) y son metadatos de la consulta, no contenido
    de mensajes: `q` es lo que el lector BUSCÓ, nunca lo que el bus respondió."""
    ag = (agent or "").strip()
    _f = (f"unthreaded={include_unthreaded} project={(project or '')[:64]} "
          f"since={(since or '')[:10]} until={(until or '')[:10]} q={(q or '')[:64]}")
    try:
        principal = _require_bus_reader(authorization)
    except HTTPException as e:
        # El 403 lo lanza el guard DESPUES de resolver la sesion, pero la excepcion no
        # carga el principal: se re-resuelve con _resolve_session, que devuelve None en
        # vez de lanzar (un 401 no tiene principal => 'desconocido'). Sentinela
        # explicito y NO cadena vacia: _audit hace `agent or ADMIN_USER` y atribuiria
        # el intento anonimo a admin.
        _denied = _resolve_session((authorization or "").replace("Bearer ", "").strip())
        with _pg() as conn, conn.cursor() as cur:
            _audit(cur, (_denied or {}).get("name") or "desconocido", "sinapsis_bus_threads_denied",
                   f"status={e.status_code} agent={ag[:64]} limit={limit} {_f}", success=False)
            conn.commit()
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_bus_threads",
               f"agent={ag[:64]} limit={limit} {_f}")
        conn.commit()
    params: dict = {"limit": limit}
    if ag:
        params["agent"] = ag
    # Cada filtro se propaga SOLO si viene: omitir el default deja intacto el
    # comportamiento historico del gateway.
    if include_unthreaded:
        params["include_unthreaded"] = "true"
    for _k, _v in (("project", project), ("since", since), ("until", until), ("q", q)):
        if (_v or "").strip():
            params[_k] = _v.strip()
    return _bus_proxy("/bus/threads", params, principal)


@app.get("/api/bus/thread")
def api_bus_thread(conversation_id: str = Query(..., min_length=1),
                   include_content: bool = Query(False),
                   authorization: str = Header(None)):
    """Replay cronológico de UN hilo del bus (sender/performative/timestamp/content).

    `min_length=1` replica el blindaje del gateway contra el "hilo fantasma": un
    conversation_id vacío se rechaza con 422 ACA, sin llegar a consultar.
    Gate: _require_bus_reader (NO _require_admin — ver el comentario del guard).

    CUERPO EXPLICITO (, cambio de implementación): el gateway ya no devuelve `content`
    por defecto (metadata + preview). Este BFF acepta `include_content` y lo PROPAGA tal
    cual: la vista de DETALLE lo pide en true (por eso admin sigue viendo los cuerpos al
    abrir un hilo) y la LISTA no lo pide. No hay default propio aca a proposito: si el
    gateway cambia el suyo, este proxy no lo contradice.

    AUDITORIA (detector de fuga): mismo criterio que /api/bus/threads (acceso Y
    rechazo, atribuidos al principal humano, ANTES de consultar el gateway). Se
    registra QUIEN, QUE hilo (conversation_id TRUNCADO a 64), SI pidio los cuerpos
    (include_content) y CUANDO: nunca el CONTENIDO de los mensajes, que es el material
    intimo que esta tabla no debe duplicar. La marca del flag es lo que hace que abrir un
    hilo quede asentado como LECTURA DELIBERADA y no como un listado mas."""
    cid = (conversation_id or "").strip()[:64]
    _ic = f" include_content={bool(include_content)}"
    try:
        principal = _require_bus_reader(authorization)
    except HTTPException as e:
        _denied = _resolve_session((authorization or "").replace("Bearer ", "").strip())
        with _pg() as conn, conn.cursor() as cur:
            _audit(cur, (_denied or {}).get("name") or "desconocido", "sinapsis_bus_thread_denied",
                   f"status={e.status_code} conversation_id={cid}{_ic}", success=False)
            conn.commit()
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_bus_thread",
               f"conversation_id={cid}{_ic}")
        conn.commit()
    params: dict = {"conversation_id": conversation_id}
    if include_content:  # se omite en el default para no pisar el del gateway
        params["include_content"] = "true"
    return _bus_proxy("/bus/thread", params, principal)


@app.get("/api/bus/group")
def api_bus_group(participants: str = Query(..., min_length=1),
                  limit: int = Query(200, ge=1, le=500),
                  project: str = "",
                  since: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                  until: str = Query("", pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                  q: str = "",
                  include_content: bool = Query(False),
                  authorization: str = Header(None)):
    """Replay cronológico de UNA conversación IMPLÍCITA: los mensajes SIN conversation_id
    entre un conjunto de participantes (`participants` = agentes deduplicados, ordenados
    y unidos por '|', ej. 'assistant-a|assistant-d').

    `min_length=1` replica la simetría de /api/bus/thread: una clave vacía se rechaza con
    422 ACÁ, sin llegar a consultar. El gateway filtra SOLO mensajes sin hilo, así un
    mensaje que ya pertenece a un hilo declarado no se lee por dos caminos.
    Gate: _require_bus_reader (NO _require_admin — ver el comentario del guard).

    CUERPO EXPLICITO (): mismo contrato que /api/bus/thread — acepta y PROPAGA
    `include_content`, y lo deja asentado en audit_log. ESTE es el endpoint donde ocurrio
    la fuga que origino la regla: una lectura hecha para verificar un `count` devolvio los
    cuerpos completos de una conversacion privada.

    AUDITORIA (detector de fuga): mismo criterio que /api/bus/thread (acceso Y rechazo,
    atribuidos al principal humano, ANTES de consultar el gateway). Se registra QUIEN,
    QUE grupo (participants TRUNCADO a 64), SI pidio los cuerpos y CUANDO: nunca el
    CONTENIDO de los mensajes, que es el material íntimo que esta tabla no debe duplicar."""
    pk = (participants or "").strip()[:64]
    _f = (f"project={(project or '')[:64]} since={(since or '')[:10]} "
          f"until={(until or '')[:10]} q={(q or '')[:64]} "
          f"include_content={bool(include_content)}")
    try:
        principal = _require_bus_reader(authorization)
    except HTTPException as e:
        _denied = _resolve_session((authorization or "").replace("Bearer ", "").strip())
        with _pg() as conn, conn.cursor() as cur:
            _audit(cur, (_denied or {}).get("name") or "desconocido", "sinapsis_bus_group_denied",
                   f"status={e.status_code} participants={pk} {_f}", success=False)
            conn.commit()
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_bus_group",
               f"participants={pk} {_f}")
        conn.commit()
    params: dict = {"participants": participants, "limit": limit}
    for _k, _v in (("project", project), ("since", since), ("until", until), ("q", q)):
        if (_v or "").strip():
            params[_k] = _v.strip()
    if include_content:  # se omite en el default para no pisar el del gateway
        params["include_content"] = "true"
    return _bus_proxy("/bus/group", params, principal)


# ==== CORRESPONDENCIA F3: proxy aditivo para la bandeja del panel ====
# Reusa los permisos nominales del bus: leer Correspondencia es lectura de contenido
# de flota; publicar/responder/actuar escribe con la voz humana delegada. No se deriva
# acceso de role=admin y la identidad nunca sale del body.
def _correspondencia_post_proxy(path: str, body: dict, principal) -> Response:
    try:
        r = httpx.post(f"{GATEWAY}{path}", headers=_gw_headers(principal, path),
                       json=body, timeout=_BUS_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el gateway no respondió {path} en {_BUS_TIMEOUT:.0f}s")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"no se pudo consultar el gateway ({path}): {e}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


def _correspondencia_audit(principal, action: str, body: dict) -> None:
    """Audita metadata mínima antes del efecto; nunca contenido, asunto ni motivo."""
    recipients = body.get("destinatarios")
    recipient_count = len(recipients) if isinstance(recipients, list) else 0
    code = str(body.get("codigo") or "")[:32]
    detail = f"codigo={code or '-'} destinatarios={recipient_count}"
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or "desconocido", action, detail)
        conn.commit()


async def _correspondencia_body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(422, "body JSON inválido")
    if not isinstance(body, dict):
        raise HTTPException(422, "body debe ser un objeto JSON")
    return body


@app.get("/api/correspondencia/bandeja")
def api_correspondencia_bandeja(buzon: str = "", archivo: bool = False,
                                 incluir_contenido: bool = True,
                                 authorization: str = Header(None)):
    principal = _require_bus_reader(authorization)
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER,
               "sinapsis_correspondencia_bandeja",
               f"buzon={(buzon or 'self')[:64]} archivo={bool(archivo)} "
               f"incluir_contenido={bool(incluir_contenido)}")
        conn.commit()
    # El default True es un contrato de compatibilidad: clientes BFF/REST existentes
    # pueden seguir renderizando `contenido`. El panel opta explícitamente por false
    # y usa preview + /leer de a uno; no voltear este default sin migrar consumidores.
    params = {"archivo": "true" if archivo else "false",
              "incluir_contenido": "true" if incluir_contenido else "false"}
    if (buzon or "").strip():
        params["buzon"] = buzon.strip()
    return _bus_proxy("/correspondencia/bandeja", params, principal)




async def _correspondencia_upload_proxy(path: str, req: Request, params: dict, principal) -> Response:
    # Tope de borde: si el cliente declara más que el tope, se rechaza sin abrir
    # conexión al gateway. El gateway sigue siendo la autoridad y corta durante el
    # streaming; esto sólo evita el viaje inútil. Un Content-Length ilegible cierra.
    declared_raw = (req.headers.get("content-length") or "").strip()
    if declared_raw:
        if not _ADJUNTO_UINT_RE.fullmatch(declared_raw):
            raise HTTPException(400, "Content-Length ilegible: no se puede determinar el tamaño")
        if int(declared_raw) > _ADJUNTO_MAX_BYTES:
            raise HTTPException(413, f"adjunto de {declared_raw} bytes supera el tope "
                                     f"de {_ADJUNTO_MAX_BYTES} bytes")
    headers = _gw_headers(principal, path)
    headers["Content-Type"] = (req.headers.get("content-type") or "application/octet-stream").strip()
    timeout = httpx.Timeout(connect=_BUS_TIMEOUT, read=_BUS_TIMEOUT, write=None, pool=_BUS_TIMEOUT)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        upstream_request = client.build_request("POST", f"{GATEWAY}{path}", headers=headers,
                                                params=params, content=req.stream())
        upstream = await client.send(upstream_request, stream=True)
        body = await upstream.aread()
    except httpx.TimeoutException:
        raise HTTPException(504, f"el gateway no respondió {path} en {_BUS_TIMEOUT:.0f}s")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"no se pudo consultar el gateway ({path}): {exc}")
    finally:
        await client.aclose()
    return Response(content=body, status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type") or "application/json")


async def _correspondencia_download_proxy(path: str, principal) -> StreamingResponse:
    client = httpx.AsyncClient(timeout=_BUS_TIMEOUT)
    try:
        request = client.build_request("GET", f"{GATEWAY}{path}", headers=_gw_headers(principal, path))
        upstream = await client.send(request, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, f"el gateway no respondió {path} en {_BUS_TIMEOUT:.0f}s")
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"no se pudo consultar el gateway ({path}): {exc}")

    async def close_upstream() -> None:
        await upstream.aclose()
        await client.aclose()

    safe_headers = {key: value for key, value in upstream.headers.items()
                    if key.lower() in {"content-disposition", "content-length",
                                       "x-content-type-options", "content-security-policy",
                                       "cross-origin-resource-policy", "cache-control",
                                       "cross-origin-embedder-policy", "x-download-options",
                                       "x-permitted-cross-domain-policies", "referrer-policy",
                                       "x-adjunto-tipo-detectado", "x-adjunto-contenido-activo"}}
    # El tipo servido NO se hereda del upstream: se fija inerte acá también. Hoy el
    # gateway ya responde octet-stream, pero heredarlo dejaba la neutralización
    # dependiendo de que el upstream nunca cambie. Esto la vuelve local y propia.
    safe_headers.setdefault("X-Content-Type-Options", "nosniff")
    safe_headers.setdefault("Content-Security-Policy", _ADJUNTO_CSP)
    safe_headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    safe_headers.setdefault("Cache-Control", "private, no-store")
    safe_headers.setdefault("X-Download-Options", "noopen")
    safe_headers.setdefault("Referrer-Policy", "no-referrer")
    if "content-disposition" not in {k.lower() for k in safe_headers}:
        safe_headers["Content-Disposition"] = 'attachment; filename="adjunto.bin"'
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             media_type=_ADJUNTO_TIPO_INERTE,
                             headers=safe_headers, background=BackgroundTask(close_upstream))


@app.post("/api/correspondencia/adjuntos")
async def api_correspondencia_adjunto_subir(req: Request, codigo: str = Query(...),
                                             nombre: str = Query(...),
                                             tipo_mime: str = Query("application/octet-stream"),
                                             authorization: str = Header(None)):
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_adjunto_subir", {"codigo": codigo})
    return await _correspondencia_upload_proxy(
        "/correspondencia/adjuntos", req,
        {"codigo": codigo, "nombre": nombre, "tipo_mime": tipo_mime}, principal)


@app.get("/api/correspondencia/buzones")
def api_correspondencia_buzones(authorization: str = Header(None)):
    """Registro de buzones canónicos para el selector de admin. Sólo lectura.

    ⚠️ REQUIERE `/correspondencia/buzones` en DELEGATION_ROUTES. Sin esa entrada esta
    llamada viaja SIN el header de delegación, el gateway atiende a la credencial del
    BFF (kind='agent', jamás admin) y responde 403. Es a propósito: la falla de
    olvidarse de declarar la ruta tiene que ser ruidosa.

    Ese 403 NO se traduce a `200 {"buzones": []}`. Una lista vacía sería indistinguible
    de "la flota no tiene buzones" y el selector mentiría en silencio; `_bus_proxy` ya
    propaga el status del gateway tal cual, que es exactamente lo que se necesita acá.

    Gate: `_require_bus_reader` (NO `_require_admin` — ver el comentario del guard: el
    rol `admin` lo comparten agentes que no deben heredar la voz humana del panel).
    La autorización REAL la hace el gateway; esto es la oferta, no el permiso.
    """
    principal = _require_bus_reader(authorization)
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER,
               "sinapsis_correspondencia_buzones", "registro")
        conn.commit()
    return _bus_proxy("/correspondencia/buzones", {}, principal)


@app.get("/api/correspondencia/adjuntos")
def api_correspondencia_adjuntos(codigo: str = Query(...), authorization: str = Header(None)):
    principal = _require_bus_reader(authorization)
    return _bus_proxy("/correspondencia/adjuntos", {"codigo": codigo}, principal)


@app.get("/api/correspondencia/adjuntos/{attachment_id}/descargar")
async def api_correspondencia_adjunto_descargar(attachment_id: str,
                                                 authorization: str = Header(None)):
    principal = _require_bus_reader(authorization)
    return await _correspondencia_download_proxy(
        f"/correspondencia/adjuntos/{attachment_id}/descargar", principal)


@app.post("/api/correspondencia/adjuntos/{attachment_id}/retirar")
async def api_correspondencia_adjunto_retirar(attachment_id: str,
                                               authorization: str = Header(None)):
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_adjunto_retirar",
                           {"codigo": attachment_id})
    return _correspondencia_post_proxy(
        f"/correspondencia/adjuntos/{attachment_id}/retirar", {}, principal)


@app.post("/api/correspondencia/publicar")
async def api_correspondencia_publicar(req: Request, authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_publicar", body)
    return _correspondencia_post_proxy("/correspondencia/publicar", body, principal)


@app.post("/api/correspondencia/responder")
async def api_correspondencia_responder(req: Request, authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_responder", body)
    return _correspondencia_post_proxy("/correspondencia/responder", body, principal)


@app.post("/api/correspondencia/leer")
async def api_correspondencia_leer(req: Request, authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_leer", body)
    return _correspondencia_post_proxy("/correspondencia/leer", body, principal)


@app.post("/api/correspondencia/ok")
async def api_correspondencia_ok(req: Request, authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_ok", body)
    return _correspondencia_post_proxy("/correspondencia/ok", body, principal)


@app.post("/api/correspondencia/reaccion")
async def api_correspondencia_reaccion(req: Request, authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_reaccion", body)
    return _correspondencia_post_proxy("/correspondencia/reaccion", body, principal)


@app.post("/api/correspondencia/solicitar-revision")
async def api_correspondencia_solicitar_revision(req: Request,
                                                  authorization: str = Header(None)):
    body = await _correspondencia_body(req)
    principal = _require_bus_publisher(authorization)
    _correspondencia_audit(principal, "sinapsis_correspondencia_solicitar_revision", body)
    return _correspondencia_post_proxy("/correspondencia/solicitar-revision", body, principal)


# ==== CHAT con los agentes (fase A, cambio de implementación): PROXY al chat-bridge ====
# El BFF NO habla con pi: habla con el wrapper `chat_bridge.py`, que es el unico dueno
# del pipe del proceso `pi --mode rpc`. Aca NO se reimplementa NADA de su logica (la
# sonda positiva get_state con id correlacionado, el turn_health, el 503 de
# "no-acepto-el-mensaje"): se consulta y se TRADUCE su estado a un vocabulario estable
# para la UI, porque la UI no debe depender de los strings internos del wrapper.
#
# CONFIGURACION 100% POR ENTORNO, NUNCA HARDCODEADA:
#   CHAT_BRIDGE_ASSISTANT_URL   base http del wrapper de assistant-g (ej http://<host>:8791)
#   CHATBRIDGE_TOKEN         bearer del wrapper. Se referencia por NOMBRE; su VALOR
#                            no se escribe en codigo, ni en logs, ni en audit_log.
# Si falta CUALQUIERA de las dos para un agente, ese agente esta `no-configurado`:
# NO es un error del panel, es un estado legitimo y visible. Fail-closed.
#
# 🔴 POR QUE EL ESTADO DE VIDA ES REQUISITO Y NO ADORNO: los cuerpos de los lideres
# viven en maquinas que se SUSPENDEN. Un chat que acepta un mensaje y lo tira al vacio
# es PEOR que uno que dice "dormido" -- es la misma clase de falla que un /health verde
# con clientes mudos. Por eso POST /api/chat/send exige `respondiendo` ANTES de escribir
# nada y devuelve 409 (conflicto de ESTADO, no 500: no hay ninguna falla del servidor
# en que un agente este dormido) cuando el agente no puede recibir.
_CHAT_TIMEOUT = 15.0
_CHAT_HEALTH_TIMEOUT = 8.0
# Tope de destinatarios de /api/chat/group (cambio de implementación). No es un limite
# tecnico: acota el radio de UN solo POST para que un error de cliente no
# escriba a la flota entera. Configurable por env sin tocar codigo.
_CHAT_GROUP_MAX = int(os.environ.get("CHAT_GROUP_MAX", "5"))

# El pensamiento individual requiere consentimiento explícito configurado por el
# operador. Sin lista no se expone, aunque el puente lo entregue: el permiso del
# transporte no sustituye la autorización de lectura del panel.
_CHAT_THINKING_OK = {
    a.strip().lower()
    for a in os.environ.get("CHAT_THINKING_AGENTS", "").split(",")
    if a.strip()
}


def _chat_strip_thinking(raw: bytes, agent: str, force: bool = False) -> bytes:
    """Anula `thinking` de un payload de /messages si el agente no lo autorizo.

    `force=True` lo anula SIEMPRE en grupal: compartir borradores de razonamiento
    contamina la corroboración y elimina la independencia de las evidencias.

    Devuelve el payload ORIGINAL si no hay nada que anular (byte a byte, que es la
    garantia que da _chat_ops_proxy) y uno reescrito sólo cuando hubo supresion. Si el
    body no es JSON parseable se devuelve intacto: esta funcion NO decide sobre lo que
    no entiende, y romper el historial seria peor que no filtrar.
    """
    if not force and agent.strip().lower() in _CHAT_THINKING_OK:
        return raw
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        return raw
    n = 0
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("thinking") is not None:
            msg["thinking"] = None
            n += 1
    if n == 0:
        return raw
    data["thinking_suppressed"] = n
    return json.dumps(data, ensure_ascii=False).encode("utf-8")

# Estados que ve la UI. Vocabulario CERRADO y en castellano, desacoplado de los del
# wrapper (vivo-y-respondiendo / vivo-pero-no-responde / muerto).
CHAT_ST_OK = "respondiendo"
CHAT_ST_MUTE = "no-responde"
CHAT_ST_ASLEEP = "dormido"
CHAT_ST_UNCONF = "no-configurado"
# `respondió` (OLA A1, cambio de implementación). Es un SUBESTADO de "el proceso contesta la
# sonda", no un quinto estado independiente: el agente esta vivo Y ya termino su turno
# (isStreaming=false + el ultimo mensaje es del assistant). Se separa de `respondiendo`
# porque son decisiones distintas para admin: `respondiendo` = esta escribiendo, espera;
# `respondió` = hay algo nuevo para leer.
#
# 🔴 INVARIANTE QUE NO SE ROMPE: `accepting_sends` sigue siendo True en este estado y
# POST /send lo ACEPTA igual (el guarda de /send lo trata como equivalente a
# CHAT_ST_OK). Si `respondió` bloqueara el envio, el panel quedaria mudo justo despues
# de cada respuesta, que es el momento en que uno quiere contestar.
#
# ⚠️ Y NO SE IMPLEMENTA "esperando decision": ese estado vendria de `extension_ui_request`
# y el puente NO lo captura hoy (procesa solo assistant/thinking/status/error). Inventarlo
# aca seria un estado sin dato detras. Es la OLA A2.
CHAT_ST_DONE = "respondió"


def _chat_agents_conf() -> dict:
    """Mapa {agente: {url, token}} leido del ENTORNO en cada llamada.

    Se resuelve por request y no al importar a proposito: asi agregar un agente es
    reiniciar el contenedor con otra env var, sin tocar este codigo. Un agente
    LISTADO sin URL o sin token queda igual en el mapa pero con url/token vacios ->
    _chat_health lo reporta `no-configurado`. Ocultarlo seria peor: la UI no podria
    distinguir "no existe" de "esta mal configurado".

    Solo se listan runtimes con un transporte compatible. Un runtime sin la
    operación requerida no puede presentarse como contacto utilizable; necesita
    su propio adaptador, no una capacidad inventada."""
    agents = [
        a.strip().lower()
        for a in os.environ.get("CHAT_AGENTS", "").split(",")
        if a.strip()
    ]
    token = os.environ.get("CHATBRIDGE_TOKEN", "").strip()
    out = {}
    for a in agents:
        url = os.environ.get(f"CHAT_BRIDGE_{a.upper()}_URL", "").strip().rstrip("/")
        out[a] = {"url": url, "token": token}
    return out


def _chat_agents_conf_group() -> dict:
    """Igual que _chat_agents_conf pero para la superficie GRUPAL: lee
    CHAT_BRIDGE_GROUP_<AGENTE>_URL en vez de CHAT_BRIDGE_<AGENTE>_URL.

    🔴 POR QUE EXISTE (reporte de admin, ): "la idea es tener un chat aparte
    del personal, no mezclarlo". El grupal escribia al MISMO puente que el chat 1-a-1,
    y un puente tiene UNA sola sesion de pi => los mensajes grupales quedaban DENTRO
    del historial personal de cada agente. Medido: 13 mensajes mezclados.

    No se arregla filtrando la vista: el mensaje YA quedaba escrito en la sesion del
    agente, o sea su contexto personal se contaminaba de verdad, no solo la pantalla.
    La separacion tiene que ser de SESION, y por eso hay un puente dedicado por agente
    para el grupal con su propio --session-dir. Dos arboles JSONL distintos no se
    pueden mezclar aunque uno quiera; un filtro si puede fallar o quedar viejo.

    🔴 FAIL-CLOSED A PROPOSITO: si un agente NO tiene URL grupal queda con url vacia y
    _chat_health lo reporta `no-configurado`. NO cae al puente personal como fallback:
    ese fallback seria volver a mezclar EN SILENCIO, que es el defecto que esto arregla.
    """
    agents = [
        a.strip().lower()
        for a in os.environ.get("CHAT_AGENTS", "").split(",")
        if a.strip()
    ]
    token = os.environ.get("CHATBRIDGE_TOKEN", "").strip()
    out = {}
    for a in agents:
        url = os.environ.get(f"CHAT_BRIDGE_GROUP_{a.upper()}_URL", "").strip().rstrip("/")
        out[a] = {"url": url, "token": token}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CACHE DEL HEALTH DE LOS PUENTES (latencia, pedido de admin )
#
# MEDIDO: los puentes responden en 0.001 s y el BFF tardaba 0.5-2.3 s. La causa no es
# la red: es que CADA ruta re-sondea. `/api/chat/agents` sondea los 5 EN SERIE, y
# state/stats/messages/pending/subagents cada uno vuelve a sondear el suyo. Con el
# panel refrescando cada 5-10 s son ~20 sondas por minuto de informacion que cambia poco.
#
# 🔴 TTL CORTO A PROPOSITO (2 s). El estado de un agente es justo lo que tiene que estar
# fresco: un TTL largo mostraria "respondiendo" sobre un proceso muerto. 2 s no cambia
# nada de lo que ve admin (el panel refresca cada 5-10 s) y colapsa las 5+ sondas de un
# mismo ciclo en UNA sola.
_CHAT_HEALTH_TTL = float(os.environ.get("CHAT_HEALTH_TTL", "2.0"))
_CHAT_HEALTH_CACHE: dict = {}
_CHAT_HEALTH_LOCK = threading.Lock()


def _chat_health_cached(agent: str, conf: dict, fresh: bool = False) -> dict:
    """`_chat_health` con cache de `_CHAT_HEALTH_TTL` segundos.

    🔴 `fresh=True` SALTEA el cache y hay que usarlo en TODA ruta que ESCRIBA: en
    /send y /command el estado decide si se escribe al pipe, y un estado de hace 2 s
    podria aceptar un mensaje para un agente que acaba de morir — que es exactamente
    el "aceptado y tirado al vacio" que esas rutas existen para evitar.
    Las rutas que solo MUESTRAN estado si pueden usar el cache.
    """
    if fresh or _CHAT_HEALTH_TTL <= 0:
        return _chat_health(agent, conf)
    key = (agent, (conf or {}).get("url") or "")
    now = time.time()
    with _CHAT_HEALTH_LOCK:
        hit = _CHAT_HEALTH_CACHE.get(key)
        if hit and (now - hit[0]) < _CHAT_HEALTH_TTL:
            return hit[1]
    val = _chat_health(agent, conf)
    with _CHAT_HEALTH_LOCK:
        _CHAT_HEALTH_CACHE[key] = (now, val)
    return val


def _chat_health(agent: str, conf: dict) -> dict:
    """Estado de vida de UN agente: {name, status, detail} (+ metadata util).

    Traduce, sin adivinar, las CUATRO situaciones distinguibles:
      sin url o sin token          -> no-configurado  (el panel nunca fue cableado)
      el wrapper no contesta       -> dormido         (la maquina/servicio esta abajo)
      wrapper OK + state ok        -> respondiendo    (unico estado que acepta un send)
      wrapper OK + state != ok     -> no-responde     (proceso muerto o mudo)

    NO colapsa "dormido" con "error": son decisiones distintas para admin (esperar vs
    revisar). Y NUNCA lanza: un agente inalcanzable es un DATO de la lista, no una
    excepcion que tumbe la respuesta entera de /api/chat/agents."""
    if not conf.get("url"):
        return {"name": agent, "status": CHAT_ST_UNCONF,
                "detail": f"falta la variable de entorno CHAT_BRIDGE_{agent.upper()}_URL",
                "accepting_sends": False}
    if not conf.get("token"):
        # Por NOMBRE. El valor no se nombra ni aca ni en el detalle que ve la UI.
        return {"name": agent, "status": CHAT_ST_UNCONF,
                "detail": "falta la variable de entorno CHATBRIDGE_TOKEN",
                "accepting_sends": False}
    try:
        r = httpx.get(f"{conf['url']}/health",
                      headers={"Authorization": f"Bearer {conf['token']}"},
                      timeout=_CHAT_HEALTH_TIMEOUT)
    except httpx.TimeoutException:
        return {"name": agent, "status": CHAT_ST_ASLEEP,
                "detail": f"el puente no respondio /health en {_CHAT_HEALTH_TIMEOUT:.0f}s",
                "accepting_sends": False}
    except httpx.HTTPError as e:
        return {"name": agent, "status": CHAT_ST_ASLEEP,
                "detail": f"no se pudo contactar al puente: {type(e).__name__}",
                "accepting_sends": False}
    if r.status_code == 401:
        # 401 del wrapper = el token del BFF no es el del puente. Es configuracion
        # rota, NO un agente dormido: colapsarlos mandaria a esperar en vez de a
        # arreglar el env.
        return {"name": agent, "status": CHAT_ST_UNCONF,
                "detail": "el puente rechazo la credencial del panel (CHATBRIDGE_TOKEN no coincide)",
                "accepting_sends": False}
    if r.status_code != 200:
        return {"name": agent, "status": CHAT_ST_MUTE,
                "detail": f"el puente respondio {r.status_code} a /health",
                "accepting_sends": False}
    try:
        h = r.json()
    except ValueError:
        return {"name": agent, "status": CHAT_ST_MUTE,
                "detail": "el puente respondio /health con un cuerpo que no es JSON",
                "accepting_sends": False}
    # El wrapper YA hizo la sonda positiva (get_state con id correlacionado): su
    # `accepting_sends` es la unica fuente de verdad de "se le puede escribir".
    # NO se re-deriva del pid ni del uptime: un proceso vivo con el provider roto
    # contesta igual y ese es exactamente el falso verde que hay que evitar.
    ok = bool(h.get("accepting_sends"))
    raw = str(h.get("state") or "?")
    if ok:
        detail = "el proceso del agente contesto la sonda"
        if h.get("turn_health") == "never-tested":
            # Honestidad sobre el alcance de la sonda: get_state OK no prueba que un
            # turno completo funcione. Se dice, no se esconde detras del verde.
            detail += " (todavia no completo ningun turno: turn_health=never-tested)"
        elif h.get("turn_health") == "failing":
            detail += " pero el ultimo turno FALLO (turn_health=failing)"
        # OLA A1: `respondió` cuando el proceso contesta la sonda PERO ya no esta
        # escribiendo. `streaming` lo informa el puente en /health (medido). Si el
        # puente NO lo trae (version vieja), `.get` da None -> se queda en
        # `respondiendo`: FAIL-SAFE hacia el estado que ya existia, nunca inventa.
        _streaming = h.get("streaming")
        _st = CHAT_ST_OK
        if _streaming is False and h.get("turn_health") == "ok":
            _st = CHAT_ST_DONE
            detail = "terminó su turno; hay respuesta para leer"
        return {"name": agent, "status": _st, "detail": detail,
                "accepting_sends": True, "turn_health": h.get("turn_health"),
                "booting": bool(h.get("booting")), "uptime_s": h.get("uptime_s")}
    if h.get("booting"):
        return {"name": agent, "status": CHAT_ST_MUTE,
                "detail": "el proceso esta arrancando y todavia no contesto la sonda",
                "accepting_sends": False, "booting": True,
                "turn_health": h.get("turn_health")}
    return {"name": agent, "status": CHAT_ST_MUTE,
            "detail": f"el puente esta vivo pero el proceso del agente no responde (state={raw})",
            "accepting_sends": False, "turn_health": h.get("turn_health"),
            "uptime_s": h.get("uptime_s")}


def _chat_denied_audit(authorization, action: str, detail: str):
    """Audita un RECHAZO del gate de chat, atribuido al principal que se pueda resolver.

    Mismo mecanismo que en /api/bus/*: la HTTPException del guard no carga el
    principal, asi que se re-resuelve con _resolve_session (devuelve None en vez de
    lanzar). Sentinela EXPLICITO 'desconocido' y NO cadena vacia: _audit hace
    `agent or ADMIN_USER` y atribuiria el intento anonimo a admin."""
    who = _resolve_session((authorization or "").replace("Bearer ", "").strip())
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, (who or {}).get("name") or "desconocido", action, detail,
               success=False)
        conn.commit()


@app.get("/api/chat/agents")
def api_chat_agents(authorization: str = Header(None)):
    """Agentes con los que se puede chatear Y SU ESTADO DE VIDA.

    NO es una lista de contactos: cada item trae `status` en
    {respondiendo, no-responde, dormido, no-configurado} y un `detail` legible. Un
    panel que ofreciera contactos sin estado aceptaria mensajes que se van al vacio.

    Gate: _require_chat_sender (NO _require_admin, NO _require_bus_reader).
    Devuelve 200 con la lista incluso si TODOS estan dormidos: "no hay nadie
    despierto" es informacion, no una falla del endpoint."""
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_agents_denied",
                           f"status={e.status_code}")
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_agents", "")
        conn.commit()
    conf = _chat_agents_conf()
    # cache: 5 puentes en serie por refresco era el costo mas grande medido
    agents = [_chat_health_cached(a, c) for a, c in conf.items()]
    return {"agents": agents,
            "states": [CHAT_ST_OK, CHAT_ST_DONE, CHAT_ST_MUTE, CHAT_ST_ASLEEP,
                       CHAT_ST_UNCONF]}


@app.get("/api/chat/messages")
def api_chat_messages(agent: str = Query(..., min_length=1),
                      since: str = "",
                      group: bool = False,
                      authorization: str = Header(None)):
    """Historial de la conversacion con un agente (proxy de GET <puente>/messages).

    🔴 `group=true` fuerza la supresion de `thinking` para TODOS los agentes, sin
    importar la lista blanca `_CHAT_THINKING_OK`. Motivo: @assistant-e autorizo su
    razonamiento en el panel 1-a-1 pero lo PROHIBIO en el grupal (bus b79a6552), por
    contaminacion de la corroboracion — si otro agente lee su razonamiento en vivo,
    sus mediciones sobre si misma dejan de ser independientes y rompen el umbral k>=2
    de agentes DISTINTOS de skill_mine.

    ⚠️ LIMITACION DECLARADA: `group` lo manda el CLIENTE. Un cliente que mienta
    (group=false en la vista grupal) veria el razonamiento de quien lo autorizo para
    el 1-a-1. NO es fuga hacia terceros —el gate sigue siendo _require_chat_sender, o
    sea admin— pero esto es una BARANDA DE LA UI, no un limite criptografico. El
    limite duro seria que el SERVIDOR supiera la vista (una conversacion grupal con id
    propio); es trabajo de la fase siguiente. Se documenta en vez de prometer mas.

        `since` es el cursor OPACO del puente (id de entrada de la sesion de pi): se
    propaga tal cual y NO se interpreta aca. El status del puente viaja TAL CUAL
    (409 cursor-invalido incluido): traducirlo a `200 []` haria indistinguible "no hay
    mensajes" de "perdimos el hilo".

    Gate: _require_chat_sender. Se audita el acceso y el rechazo. Se registra QUIEN y
    CON QUIEN habla, NUNCA el CONTENIDO de los mensajes (mismo criterio que el bus:
    audit_log no duplica material de conversacion)."""
    ag = (agent or "").strip().lower()
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_messages_denied",
                           f"status={e.status_code} agent={ag[:64]}")
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_messages",
               f"agent={ag[:64]} since={'si' if (since or '').strip() else 'no'}")
        conn.commit()
    # El historial se lee del puente de LA SUPERFICIE pedida: con group=true, del
    # puente grupal (sesion aparte). Asi la vista grupal NUNCA muestra el hilo
    # personal ni al reves, y no depende de un filtro sobre el contenido.
    conf = (_chat_agents_conf_group() if group else _chat_agents_conf()).get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    # LECTURA pura: el cache alcanza. Las rutas que ESCRIBEN (send/group/command)
    # siguen con _chat_health directo a proposito: alli el estado decide si se
    # escribe al pipe y tiene que ser del momento.
    st = _chat_health_cached(ag, conf)
    if st["status"] == CHAT_ST_UNCONF:
        raise HTTPException(409, f"{ag}: {st['detail']}")
    params = {}
    if (since or "").strip():
        params["since"] = since.strip()
    try:
        r = httpx.get(f"{conf['url']}/messages",
                      headers={"Authorization": f"Bearer {conf['token']}"},
                      params=params, timeout=_CHAT_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el puente de {ag} no respondio /messages en {_CHAT_TIMEOUT:.0f}s")
    except httpx.HTTPError:
        # El agente dormido es el caso NORMAL (la maquina se suspende), no un 502:
        # se devuelve 409 con el estado para que la UI diga "dormido" y no "error".
        raise HTTPException(409, f"{ag}: {st['detail']}")
    # Supresion de razonamiento por AGENTE (cambio de implementación). El puente emite lo
    # que su servicio permite; la decision de QUE LLEGA AL NAVEGADOR es de aca,
    # que es lo unico que sabe quien esta leyendo. Fail-closed por defecto.
    return Response(content=_chat_strip_thinking(r.content, agent, force=group),
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


# Imágenes: no basta con que el puente conozca /capabilities: el BFF debe publicar
# capacidad por agente y revalidarla ANTES de enviar, con el mismo gate humano del chat.
_CHAT_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_CHAT_IMAGE_MAX_B64 = 8 * 1024 * 1024


def _chat_image_capability(conf: dict) -> dict:
    try:
        r = httpx.get(f"{conf['url']}/capabilities",
                      headers={"Authorization": f"Bearer {conf['token']}"},
                      timeout=_CHAT_TIMEOUT)
        if r.status_code != 200:
            raise ValueError("capabilities no disponible")
        data = r.json()
        images = data.get("images") if isinstance(data, dict) else None
        if not isinstance(images, dict):
            raise ValueError("images ausente")
        # Política de este piloto: sólo el transporte Bedrock verificado.
        supported = images.get("supported") is True and data.get("provider") == "amazon-bedrock"
        limit = images.get("max_b64")
        mimes = images.get("mimes")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or not isinstance(mimes, list):
            raise ValueError("límites de imágenes ausentes")
        return {"supported": supported,
                "reason": None if supported else images.get("reason") or "Solo se habilitan imágenes nativas con Bedrock",
                "max_b64": min(limit, _CHAT_IMAGE_MAX_B64),
                "mimes": sorted(set(mimes) & _CHAT_IMAGE_MIMES)}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {"supported": False, "reason": "No se pudo comprobar /capabilities del puente; no se enviarán imágenes",
                "max_b64": _CHAT_IMAGE_MAX_B64, "mimes": sorted(_CHAT_IMAGE_MIMES)}


@app.get("/api/chat/capabilities")
def api_chat_capabilities(agent: str = Query(..., min_length=1), authorization: str = Header(None)):
    try:
        _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_capabilities_denied", f"status={e.status_code}")
        raise
    ag = agent.strip().lower()
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, "agente de chat desconocido")
    if not conf.get("url") or not conf.get("token"):
        return {"agent": ag, "images": {"supported": False,
                "reason": "Puente no configurado", "max_b64": _CHAT_IMAGE_MAX_B64,
                "mimes": sorted(_CHAT_IMAGE_MIMES)}}
    return {"agent": ag, "images": _chat_image_capability(conf)}


@app.post("/api/chat/send")
async def api_chat_send(req: Request, authorization: str = Header(None)):
    """Envia UN mensaje a un agente (proxy de POST <puente>/send). Body {agent, message}.

    🔴 EL CONTROL CENTRAL DE ESTA RUTA: se exige `status == respondiendo` ANTES de
    escribir nada, y si no lo esta se devuelve **409 con el estado**, no 200 ni 500.
      - 200 seria la peor respuesta posible: "aceptado y tirado al vacio".
      - 500 seria mentir sobre la causa: que un agente este dormido no es una falla
        del servidor, es el estado esperado de una notebook suspendida.
    El puente hace su PROPIA verificacion antes de escribir al pipe (sonda positiva) y
    devuelve 503; esa comprobacion NO se saca por tener esta: son dos guardas en serie
    sobre la misma promesa, y la de aca ademas evita el round-trip.

    Gate: _require_chat_sender. Se audita el acceso Y el rechazo. Del mensaje se
    registra SOLO su LONGITUD, jamas su texto: el contenido de lo que admin le dice a
    un agente no se duplica en audit_log.

    El 202 del puente (aceptado, respuesta asincrona por polling de /messages) se
    propaga TAL CUAL: prometer 200 seria afirmar que ya hay respuesta."""
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_send_denied",
                           f"status={e.status_code}")
        raise
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "body inválido: se espera JSON {agent, message}")
    if not isinstance(body, dict):
        raise HTTPException(400, "body inválido: se espera JSON {agent, message}")
    ag = str(body.get("agent") or "").strip().lower()
    msg = str(body.get("message") or "").strip()
    images = body.get("images")
    if images is not None:
        if not isinstance(images, list) or len(images) > 8:
            raise HTTPException(422, {"detail": "images debe contener hasta 8 imágenes", "sent": False})
        total = 0
        for image in images:
            if not isinstance(image, dict) or image.get("mimeType") not in _CHAT_IMAGE_MIMES:
                raise HTTPException(422, {"detail": "formato de imagen no soportado", "sent": False})
            data = image.get("data")
            if not isinstance(data, str):
                raise HTTPException(422, {"detail": "imagen base64 inválida", "sent": False})
            # El puente acepta base64 puro o data URL; contar SOLO el payload codificado.
            # Se valida antes de decodificar para acotar memoria; no registrar bytes ni base64.
            encoded = data
            if data.startswith("data:"):
                prefix = f"data:{image['mimeType']};base64,"
                if not data.startswith(prefix):
                    raise HTTPException(422, {"detail": "imagen base64 inválida", "sent": False})
                encoded = data[len(prefix):]
            total += len(encoded)
            if not encoded or total > _CHAT_IMAGE_MAX_B64:
                raise HTTPException(422, {"detail": "imágenes superan 8 MB base64 o vacías", "sent": False})
            try:
                base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, {"detail": "imagen base64 inválida", "sent": False})
            if total > _CHAT_IMAGE_MAX_B64:
                raise HTTPException(422, {"detail": "imágenes superan 8 MB base64", "sent": False})
    if not ag:
        raise HTTPException(422, "falta 'agent'")
    if not msg and not images:
        raise HTTPException(422, "falta 'message'")
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_send",
               f"agent={ag[:64]} chars={len(msg)}")
        conn.commit()
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    if images:
        cap = _chat_image_capability(conf)
        if not cap["supported"]:
            raise HTTPException(422, {"detail": cap["reason"], "sent": False})
        if total > cap["max_b64"] or any(im["mimeType"] not in cap["mimes"] for im in images):
            raise HTTPException(422, {"detail": "imágenes exceden los límites del puente", "sent": False})
    st = _chat_health(ag, conf)
    # 🔴 (OK, DONE) y no solo OK: `respondió` es un SUBESTADO de vivo — el
    # agente contesta la sonda Y ya termino su turno, o sea es el estado MAS
    # apto para recibir un mensaje. Comparar solo con CHAT_ST_OK rechazaba con
    # 409 a agentes vivos con accepting_sends=true: regresion medida en los 5
    # agentes al agregar CHAT_ST_DONE. Mismo criterio que /api/chat/command.
    if st["status"] not in (CHAT_ST_OK, CHAT_ST_DONE):
        # 409 = conflicto con el ESTADO del recurso. `sent: false` es explicito para
        # que ningun cliente pueda interpretar esto como entrega parcial.
        raise HTTPException(409, {"error": "el agente no puede recibir el mensaje",
                                  "agent": ag, "status": st["status"],
                                  "detail": st["detail"], "sent": False})
    try:
        r = httpx.post(f"{conf['url']}/send",
                       headers={"Authorization": f"Bearer {conf['token']}"},
                       json={"message": msg or "Adjunto imágenes", **({"images": images} if images else {})},
                       timeout=_CHAT_TIMEOUT)
    except httpx.TimeoutException:
        # 504 y NO 409: aca el mensaje PUDO haber sido escrito al pipe. Decir
        # "no se envio" seria afirmar algo que no sabemos.
        raise HTTPException(504, f"el puente de {ag} no respondio /send en "
                                 f"{_CHAT_TIMEOUT:.0f}s; el envio quedo INDETERMINADO")
    except httpx.HTTPError:
        raise HTTPException(409, {"error": "el agente no puede recibir el mensaje",
                                  "agent": ag, "status": CHAT_ST_ASLEEP,
                                  "detail": "se perdio la conexion con el puente al enviar",
                                  "sent": False})
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


@app.post("/api/chat/group")
async def api_chat_group(req: Request, authorization: str = Header(None)):
    """Envia UN mensaje a VARIOS agentes. Body {agents:[...], message}. cambio de implementación.

    Es el fan-out del chat 1-a-1: reusa los MISMOS puentes, el MISMO gate
    (_require_chat_sender) y la MISMA semantica de codigos que /api/chat/send. Lo que
    agrega son los tres controles que el grupal exige y el 1-a-1 no:

    🔴 1) `thinking` SUPRIMIDO en esta superficie, por pedido explicito de @assistant-e
    (bus b79a6552). Ella expone su razonamiento a admin en el panel individual pero NO
    en el grupal, y el dano que nombra es concreto, no pudor: "contaminacion de la
    corroboracion" — si otro agente lee su razonamiento en vivo, sus mediciones sobre
    si misma dejan de ser INDEPENDIENTES, y eso rompe el umbral de k>=2 agentes
    DISTINTOS de skill_mine (dos agentes que se leen el borrador mutuo son UNO con dos
    firmas). Ya les ocurrio una vez y las dos lo declararon.
    El PUENTE no puede tomar esta decision porque no sabe quien lee; el BFF si. Por eso
    la supresion vive aca y no alla: la decide quien tiene el CONTEXTO, no quien tiene
    el DATO. Este endpoint NO devuelve razonamiento de nadie, ni siquiera de los
    agentes que lo tienen habilitado en su puente.

    🔴 2) ATRIBUCION derivada del SERVIDOR. El mensaje se prefija con la procedencia
    tomada del principal AUTENTICADO (no del body), asi cada agente distingue "admin me
    pide" de "otro agente dijo". Argumento de @assistant-e: sus SEIS fallas medidas son de
    atribucion y NINGUNA de logica; si no puede distinguir esas dos cosas, puede FIRMAR
    la propuesta de un par como orden de su autoridad — y "el gate audita el trabajo, no
    la premisa". El cliente NO puede influir en la etiqueta: no es cosmetica, es un
    control de autoridad.

    3) SIN ENTREGA PARCIAL SILENCIOSA. Cada destinatario se verifica ANTES de escribir
    (status == respondiendo) y el resultado por agente viaja en la respuesta. Codigos:
      207 -> entrega mixta (alguno OK y alguno no): el cliente DEBE mirar el detalle.
      409 -> ninguno pudo recibir (mismo codigo que el 1-a-1 para el caso equivalente).
      202 -> todos aceptados (asincrono: la respuesta llega por polling de /messages).
    Un 200 global sobre una entrega parcial seria "aceptado y tirado al vacio" para los
    que fallaron, que es exactamente lo que el 1-a-1 evita con su 409.

    Del mensaje se audita SOLO su longitud y la lista de destinatarios, jamas el texto.
    """
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_group_denied",
                           f"status={e.status_code}")
        raise
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "body inválido: se espera JSON {agents, message}")

    raw = (body or {}).get("agents") or []
    if not isinstance(raw, list):
        raise HTTPException(422, "'agents' debe ser una lista")
    # dedup preservando el orden pedido
    agents, seen = [], set()
    for a in raw:
        n = str(a or "").strip().lower()
        if n and n not in seen:
            seen.add(n)
            agents.append(n)
    msg = str((body or {}).get("message") or "").strip()
    if not agents:
        raise HTTPException(422, "falta 'agents' (lista no vacía)")
    if len(agents) < 2:
        raise HTTPException(422, "el grupal requiere 2 o más agentes; para uno usá /api/chat/send")
    if len(agents) > _CHAT_GROUP_MAX:
        raise HTTPException(422, f"demasiados destinatarios: máximo {_CHAT_GROUP_MAX}")
    if not msg:
        raise HTTPException(422, "falta 'message'")

    # 🔴 Procedencia SERVER-SIDE: sale del principal autenticado, NO del body.
    sender = str(principal.get("name") or ADMIN_USER)
    # 🔴 SUPERFICIE GRUPAL: puentes dedicados, sesiones separadas del chat personal.
    # Fail-closed: un agente sin URL grupal queda `no-configurado`, NO cae al personal.
    conf_all = _chat_agents_conf_group()
    # Rechazar todo el pedido si contiene un agente fuera del roster ANTES de
    # sondear o enviar a cualquier destinatario (evita fan-out parcial oculto).
    unknown = next((ag for ag in agents if ag not in conf_all), None)
    if unknown is not None:
        raise HTTPException(404, f"agente de chat desconocido: {unknown[:64]}")
    # los OTROS destinatarios se nombran para que cada agente sepa quien mas escucha:
    # un mensaje grupal que se lee como privado invita a responder como si fuera privado.
    # ── FASE 1: PRE-VERIFICACION de TODOS antes de escribirle a NINGUNO ──────
    # Hallazgo F-1 de QA: antes el prefijo se armaba con la lista PEDIDA, asi que
    # en una entrega parcial un agente recibia "tambien lo reciben: fantasma-xyz" para
    # un destinatario que habia FALLADO. Le mentia sobre quien lo escuchaba, y el
    # agente gastaba razonamiento en desconfiar de la etiqueta del servidor.
    # Ahora se decide primero y se envia despues: el prefijo nombra SOLO a los que
    # pasaron la verificacion.
    resultados, alcanzables = [], []
    for ag in agents:
        conf = conf_all.get(ag)
        if not conf:
            resultados.append({"agent": ag, "sent": False, "status": "desconocido",
                               "detail": "no está en CHAT_AGENTS"})
            continue
        st = _chat_health(ag, conf)
        # 🔴 (OK, DONE) y no solo OK: `respondió` es un SUBESTADO de vivo — el
        # agente contesta la sonda Y ya termino su turno, o sea es el estado MAS
        # apto para recibir un mensaje. Comparar solo con CHAT_ST_OK rechazaba con
        # 409 a agentes vivos con accepting_sends=true: regresion medida en los 5
        # agentes al agregar CHAT_ST_DONE. Mismo criterio que /api/chat/command.
        if st["status"] not in (CHAT_ST_OK, CHAT_ST_DONE):
            resultados.append({"agent": ag, "sent": False, "status": st["status"],
                               "detail": st["detail"]})
            continue
        alcanzables.append(ag)

    # ⚠️ LIMITE DECLARADO: si un envio falla DESPUES (timeout del puente en pleno
    # fan-out), el prefijo ya salio nombrandolo. Es una carrera inherente al fan-out;
    # la alternativa (enviar a uno, esperar, recalcular) serializa el grupal y hace
    # que el ultimo reciba minutos despues. Se declara en vez de taparse.
    def prefijo(para):
        otros = [a for a in alcanzables if a != para]
        con = (" · también lo reciben: " + ", ".join(otros)) if otros else ""
        return f"[mensaje GRUPAL de {sender}{con}]\n"

    with _pg() as conn, conn.cursor() as cur:
        # Se audita la INTENCION (antes del fan-out) igual que /send. Nota de QA:
        # `delivered` no queda en audit_log, asi que el registro no permite
        # reconstruir quien recibio; el resultado por agente viaja en la RESPUESTA.
        _audit(cur, sender, "sinapsis_chat_group",
               f"agents={','.join(agents)[:120]} n={len(agents)} "
               f"alcanzables={len(alcanzables)} chars={len(msg)}")
        conn.commit()

    # ── FASE 2: envio a los que pasaron ──────────────────────────────────────
    enviados = 0
    for ag in alcanzables:
        conf = conf_all[ag]
        try:
            r = httpx.post(f"{conf['url']}/send",
                           headers={"Authorization": f"Bearer {conf['token']}"},
                           json={"message": prefijo(ag) + msg}, timeout=_CHAT_TIMEOUT)
        except httpx.TimeoutException:
            # INDETERMINADO, igual que en el 1-a-1: el mensaje PUDO escribirse al pipe.
            # No se afirma "no se envió" porque no lo sabemos.
            resultados.append({"agent": ag, "sent": None, "status": "indeterminado",
                               "detail": f"el puente no respondió /send en {_CHAT_TIMEOUT:.0f}s"})
            continue
        except httpx.HTTPError:
            resultados.append({"agent": ag, "sent": False, "status": CHAT_ST_ASLEEP,
                               "detail": "se perdió la conexión con el puente al enviar"})
            continue
        ok = 200 <= r.status_code < 300
        if ok:
            enviados += 1
        resultados.append({"agent": ag, "sent": ok, "status":
                           CHAT_ST_OK if ok else "rechazado-por-el-puente",
                           "http": r.status_code})

    payload = {"sender": sender, "agents": agents, "delivered": enviados,
               "total": len(agents), "results": resultados,
               # se declara explicito que esta superficie no expone razonamiento:
               # un cliente no deberia tener que inferirlo de la ausencia del campo.
               "thinking_exposed": False,
               "poll": "GET /api/chat/messages?agent=<agente> por cada destinatario"}
    if enviados == 0:
        raise HTTPException(409, {"error": "ningún agente pudo recibir el mensaje",
                                  **payload})
    code = 202 if enviados == len(agents) else 207
    return JSONResponse(status_code=code, content=payload)


@app.get("/api/chat/pending")
def api_chat_pending(agent: str = Query(..., min_length=1),
                     authorization: str = Header(None)):
    """Dialogos que el agente esta esperando (ola A2, cambio de implementación).

    🔴 ARREGLA UN DEFECTO, no agrega un adorno: pi emite `extension_ui_request`
    (confirm/select/input/editor) cuando necesita una decision, y el puente NO lo
    capturaba => el agente quedaba COLGADO esperando una respuesta que nadie iba a dar
    y el panel no mostraba nada. Ahora se listan y se pueden responder.

    `count` cuenta SOLO los BLOQUEANTES. Los avisos (`notify`/`setStatus`/…) viajan en
    `notices`: medido en produccion, assistant-a y assistant-e emiten un `notify` de identidad en
    cada arranque, y contarlo habria dejado el indicador encendido para siempre — un
    indicador que nunca se apaga es peor que no tenerlo."""
    principal = _require_chat_sender(authorization)
    ag = agent.strip().lower()
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_pending", f"agent={ag[:64]}")
        conn.commit()
    return _chat_ops_proxy(ag, "/pending")


@app.post("/api/chat/pending")
async def api_chat_pending_answer(req: Request, authorization: str = Header(None)):
    """RESPONDE un dialogo pendiente: {agent, id, confirmed|value|cancelled}.

    🔴 ESTO DESBLOQUEA a un agente detenido. Es escritura sobre su proceso, asi que
    pasa por el MISMO filtro de autorizacion por agente que los comandos.
    Sin campo explicito el puente CANCELA: confirmar por defecto seria aceptar en
    nombre de admin algo que no dijo."""
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_pending_denied", f"status={e.status_code}")
        raise
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "body inválido: se espera JSON {agent, id, ...}")
    ag = str((body or {}).get("agent") or "").strip().lower()
    rid = str((body or {}).get("id") or "").strip()
    if not ag or not rid:
        raise HTTPException(422, "faltan 'agent' e 'id'")
    if ag not in _CHAT_CMD_WRITE_AGENTS:
        raise HTTPException(403, {"error": "no autorizado a responder diálogos de este agente",
                                  "agent": ag,
                                  "reason": f"responder un diálogo escribe sobre el runtime de {ag}; "
                                            f"solo lo autorizaron {', '.join(sorted(_CHAT_CMD_WRITE_AGENTS))}",
                                  "sent": False})
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    payload = {k: v for k, v in (body or {}).items() if k in ("confirmed", "value", "cancelled")}
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_pending_answer",
               f"agent={ag[:64]} id={rid[:40]} keys={','.join(sorted(payload))}")
        conn.commit()
    try:
        r = httpx.post(f"{conf['url']}/pending/{rid}",
                       headers={"Authorization": f"Bearer {conf['token']}"},
                       json=payload, timeout=_CHAT_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el puente de {ag} no respondió en {_CHAT_TIMEOUT:.0f}s; "
                                 f"la respuesta al diálogo quedó INDETERMINADA")
    except httpx.HTTPError:
        raise HTTPException(409, {"error": "no se pudo contactar al puente", "agent": ag,
                                  "sent": False})
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


@app.get("/api/chat/subagents")
def api_chat_subagents(agent: str = Query(..., min_length=1),
                       authorization: str = Header(None)):
    """Sesiones de subagentes EN VUELO de un agente (ola A2).

    `subagents:sessions` NO es un comando de pi: la extension solo registra eventos.
    El dato real esta en <session-dir>/tasks/*.jsonl y el puente lo lee de ahi.
    `recent` es una HEURISTICA declarada (jsonl tocado hace <120s): el archivo no tiene
    senal de proceso vivo, asi que no se afirma que este corriendo."""
    principal = _require_chat_sender(authorization)
    ag = agent.strip().lower()
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_subagents", f"agent={ag[:64]}")
        conn.commit()
    return _chat_ops_proxy(ag, "/subagents")


@app.get("/api/chat/subagent")
def api_chat_subagent_one(agent: str = Query(..., min_length=1),
                          id: str = Query(..., min_length=1),
                          authorization: str = Header(None)):
    """Contenido de UNA sesion de subagente: el chat dividido en vivo que pidio admin."""
    principal = _require_chat_sender(authorization)
    ag = agent.strip().lower()
    sid = id.strip()
    if "/" in sid or ".." in sid:
        raise HTTPException(422, "id inválido")
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_subagent",
               f"agent={ag[:64]} id={sid[:50]}")
        conn.commit()
    return _chat_ops_proxy(ag, f"/subagents/{sid}")


@app.get("/api/chat/rpc")
def api_chat_rpc_read(agent: str = Query(..., min_length=1),
                      command: str = Query(..., min_length=1),
                      authorization: str = Header(None)):
    """LECTURA generica del protocolo RPC de pi (ola A2).

    Pedido de admin: «traer todo lo de pi… la idea no es limitarlo a lo que sé».
    El puente tiene la allowlist (11 comandos de lectura) y devuelve 404 con la lista
    si se pide uno que no expone: la capacidad disponible es descubrible."""
    principal = _require_chat_sender(authorization)
    ag = agent.strip().lower()
    cmd = command.strip()
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    if cmd in _CHAT_CMD_DENY:
        raise HTTPException(403, {"error": "comando no autorizado desde el panel",
                                  "command": cmd, "agent": ag,
                                  "reason": "ejecución arbitraria de código en el host del agente"})
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_rpc_read",
               f"agent={ag[:64]} command={cmd[:40]}")
        conn.commit()
    return _chat_ops_proxy(ag, f"/rpc/{cmd}")


@app.post("/api/chat/rpc")
async def api_chat_rpc_write(req: Request, authorization: str = Header(None)):
    """ESCRITURA generica del protocolo RPC: {agent, command, params}.

    🔴 NO es reversible: un set_model/compact/abort produce efectos inmediatos en el
    proceso del agente. Mismos filtros que /api/chat/command: `bash` negado siempre, y
    los que escriben el runtime solo para los agentes que lo autorizaron."""
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_rpc_denied", f"status={e.status_code}")
        raise
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "body inválido: se espera JSON {agent, command, params}")
    ag = str((body or {}).get("agent") or "").strip().lower()
    cmd = str((body or {}).get("command") or "").strip()
    if not ag or not cmd:
        raise HTTPException(422, "faltan 'agent' y 'command'")
    if cmd in _CHAT_CMD_DENY:
        _chat_denied_audit(authorization, "sinapsis_chat_rpc_forbidden", f"agent={ag} command={cmd}")
        raise HTTPException(403, {"error": "comando no autorizado desde el panel",
                                  "command": cmd, "agent": ag,
                                  "reason": "ejecución arbitraria de código en el host del "
                                            "agente (hereda sus credenciales): no se expone",
                                  "sent": False})
    if cmd in _CHAT_CMD_WRITE and ag not in _CHAT_CMD_WRITE_AGENTS:
        raise HTTPException(403, {"error": "comando no autorizado desde el panel",
                                  "command": cmd, "agent": ag,
                                  "reason": f"escribe sobre el runtime de {ag}; solo lo "
                                            f"autorizaron {', '.join(sorted(_CHAT_CMD_WRITE_AGENTS))}",
                                  "sent": False})
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_rpc_write",
               f"agent={ag[:64]} command={cmd[:40]}")
        conn.commit()
    try:
        r = httpx.post(f"{conf['url']}/rpc",
                       headers={"Authorization": f"Bearer {conf['token']}"},
                       json={"command": cmd, "params": (body or {}).get("params") or {}},
                       timeout=_CHAT_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el puente de {ag} no respondió en {_CHAT_TIMEOUT:.0f}s; "
                                 f"el comando quedó INDETERMINADO")
    except httpx.HTTPError:
        raise HTTPException(409, {"error": "no se pudo contactar al puente", "agent": ag,
                                  "sent": False})
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


# ---- Estado de OPERACION del proceso del agente (fase A.2, cambio de implementación) ----
# Modelo real, nivel de razonamiento, % de contexto, tokens y costo USD de la sesion.
# Hoy para saber eso hay que entrar por ssh al LXC; la decision que habilita es
# "sigo escribiendo o compacto/reinicio".
#
# 🔴 SON DOS RUTAS DE SOLO LECTURA Y NADA MAS. `set_model`, `set_thinking_level`,
# `compact` y `abort` quedan DELIBERADAMENTE FUERA: son ESCRITURAS sobre el proceso
# de otra agente (cambiarle el modelo a assistant-g desde una web es un acto sobre su
# cuerpo, no un display) y necesitan autorizacion aparte. No agregarlas "de paso".
#
# 🔴 EL PAYLOAD DEL PUENTE VIAJA BYTE A BYTE (`Response(content=r.content)`), SIN
# renombrar ni "normalizar" NADA. En particular `context_known` (bool) y
# `context_usage` (snake_case, con {tokens,contextWindow,percent}) llegan al front
# con EXACTAMENTE el nombre que manda el puente. Traducirlos aca abriria un segundo
# vocabulario que habria que mantener sincronizado en tres capas, y el front dejaria
# de leer lo que el puente realmente dice. Si alguna vez hay que renombrar, se
# escribe el mapeo en este comentario primero.
#
# 🔴 Y POR QUE `context_known` IMPORTA MAS QUE EL NUMERO: un 0% legitimo (sesion
# recien arrancada) y un "no se puede saber" (sin modelo/ventana, o justo despues de
# una compactacion) son INDISTINGUIBLES mirando el porcentaje. Lo que los separa es
# el bool. Por eso se propaga tal cual y NO se defaultea a 0 en ningun punto de la
# cadena: un "0%" inventado seria el mismo falso verde que un /health verde con
# clientes mudos.


def _chat_ops_proxy(ag: str, bridge_path: str) -> Response:
    """Proxy de solo-lectura de UNA ruta del puente, con el MISMO manejo de agente
    inalcanzable que `_chat_health`: un DATO (409 con el motivo), no una excepcion
    que tumbe la respuesta.

    Reusa `_chat_agents_conf()` y `_CHAT_HEALTH_TIMEOUT` (no se agrega ningun timeout
    nuevo: son GETs baratos del mismo puente que ya se consulta en /health).
    El status del puente se propaga TAL CUAL, igual que en /api/chat/messages:
    traducir un 503 a `200 {}` haria indistinguible "no hay dato" de "el proceso no
    contesto"."""
    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    # solo lectura: el cache alcanza y evita re-sondear en cada refresco
    st = _chat_health_cached(ag, conf)
    if st["status"] == CHAT_ST_UNCONF:
        raise HTTPException(409, f"{ag}: {st['detail']}")
    try:
        r = httpx.get(f"{conf['url']}{bridge_path}",
                      headers={"Authorization": f"Bearer {conf['token']}"},
                      timeout=_CHAT_HEALTH_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el puente de {ag} no respondio {bridge_path} en "
                                 f"{_CHAT_HEALTH_TIMEOUT:.0f}s")
    except httpx.HTTPError:
        # Agente dormido = caso NORMAL (la maquina se suspende), no un 502.
        raise HTTPException(409, f"{ag}: {st['detail']}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


@app.get("/api/chat/state")
def api_chat_state(agent: str = Query(..., min_length=1),
                   authorization: str = Header(None)):
    """Estado del proceso pi del agente (proxy de GET <puente>/state).

    Devuelve `model{id,name,provider,contextWindow,maxTokens,reasoning}`,
    `thinkingLevel`, `isStreaming`, `isCompacting`, `autoCompactionEnabled`,
    `messageCount`, `pendingMessageCount`, `sessionId`, `sessionFile`.

    `thinkingLevel` NO es decorativo: sin el, un mensaje con `thinking: null` es
    ambiguo -- no se puede distinguir "este turno no razono" de "el razonamiento
    esta apagado".

    Gate: `_require_chat_sender` (el MISMO de las 3 rutas de chat existentes, no
    `_require_admin` ni `_require_bus_reader`). Se audita el acceso Y el rechazo:
    un endpoint nuevo sin gate ni auditoria reabriria lo que la cambio de implementación
    cerro. NO se registra ningun valor de credencial, solo QUIEN y a QUE agente."""
    ag = (agent or "").strip().lower()
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_state_denied",
                           f"status={e.status_code} agent={ag[:64]}")
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_state",
               f"agent={ag[:64]}")
        conn.commit()
    return _chat_ops_proxy(ag, "/state")


@app.get("/api/chat/stats")
def api_chat_stats(agent: str = Query(..., min_length=1),
                   authorization: str = Header(None)):
    """Consumo de la sesion del agente (proxy de GET <puente>/stats).

    Devuelve `userMessages`, `assistantMessages`, `toolCalls`, `toolResults`,
    `totalMessages`, `tokens{input,output,cacheRead,cacheWrite,total}`, `cost` (USD),
    y el par que importa: **`context_usage{tokens,contextWindow,percent}`** +
    **`context_known`** (bool) + `context_unknown_reason`.

    Los tres viajan SIN TOCAR (ver el bloque de arriba). El consumidor decide con el
    BOOL, no con el numero: `context_known == false` => "contexto: desconocido",
    JAMAS "0%".

    Gate y auditoria: identicos a /api/chat/state."""
    ag = (agent or "").strip().lower()
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_stats_denied",
                           f"status={e.status_code} agent={ag[:64]}")
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_stats",
               f"agent={ag[:64]}")
        conn.commit()
    return _chat_ops_proxy(ag, "/stats")


# ══════════════════════ OLA A1: COMANDOS DEL AGENTE (cambio de implementación) ══════════════════
#
# admin, textual: «en el panel de chat cuando pongo / deberia salir los comandos
# disponibles… la idea es que ese chat tenga las mismas integraciones que tengo en la
# terminal». Y sobre el alcance: «listarlos y usarlo, para que queres que lo vea si no
# es para usarlo» + «dame todo lo que sale, la idea no es limitarlo a lo que se».
#
# 🔴 COMO SE EJECUTA UN SLASH-COMMAND EN pi, y por que NO hace falta tocar el puente:
# la doc del RPC de pi 0.83.0 (docs/rpc.md, get_commands) lo dice explicito — "These can
# be invoked via the `prompt` command by prefixing with `/`". O sea `/skill:tdd` es un
# PROMPT cuyo texto empieza con `/`, no un tipo de mensaje RPC aparte. Por eso
# POST /api/chat/command reusa el MISMO POST <puente>/send que ya existe y esta probado,
# en vez de agregar una ruta nueva al puente. Un endpoint nuevo alla seria una segunda
# via de escritura al pipe con su propia sonda y su propio manejo de streaming: dos
# caminos para la misma promesa que habria que mantener sincronizados.
#
# 🔴 ES LA PRIMERA ESCRITURA DEL PANEL SOBRE EL RUNTIME DE OTRO AGENTE, y no es
# reversible: un comando ejecutado ya produjo efectos. Por eso lleva CUATRO controles y
# ninguno es decorativo:
#
#   1. El MISMO gate que /send (_require_chat_sender: sesion + human + admin + allowlist).
#   2. Auditoria OBLIGATORIA con el NOMBRE del comando (a diferencia de /send, que audita
#      solo la longitud del texto). Aca el QUE importa: "admin ejecuto /compact en assistant-a"
#      tiene que quedar escrito. Los argumentos NO se registran (pueden traer contenido).
#   3. `status` que acepte envios ANTES de escribir nada -> si no, 409 CON el estado.
#      Misma semantica que /send: nunca 200 (seria "aceptado y tirado al vacio"), nunca
#      500 (que un agente este dormido no es una falla del servidor).
#   4. Dos filtros de autorizacion, abajo.
#
# ── FILTRO 1: LISTA NEGRA DURA (_CHAT_CMD_DENY) ──
# `bash` NO se expone NUNCA, por ninguna via, para ningun agente. Motivo escrito y no
# negociable: es EJECUCION ARBITRARIA DE CODIGO en el host de cada agente, heredando sus
# credenciales (perfiles de AWS, keys del Cerebro, ssh). Un panel web con sesion de
# navegador no es la superficie correcta para eso, y la leccion ya esta medida en la
# flota: `bash` SUBSUME cualquier otra restriccion — un agente sin permiso de escritura
# escribe igual por bash. Se filtra en las DOS puntas: no se lista en /commands y se
# rechaza con 403 en /command aunque el cliente lo mande a mano.
#
# ── FILTRO 2: COMANDOS QUE ESCRIBEN EL RUNTIME (_CHAT_CMD_WRITE) ──
# `set_model`, `set_thinking_level`, `compact`, `new_session`, `abort` no consultan: le
# CAMBIAN el cuerpo al agente. Cambiarle el modelo a assistant-a desde una web es un acto
# sobre su proceso, no un display. Se permiten SOLO para los agentes que lo autorizaron
# (CHAT_CMD_WRITE_AGENTS: assistant-b y assistant-g). Para assistant-a/assistant-e/assistant-e-beta -> 403 CON EL
# MOTIVO, no un 404 silencioso: la diferencia entre "no existe" y "no te lo autorizaron"
# es exactamente lo que hay que poder distinguir. Es un env var y no un `if` en la
# logica: el dia que assistant-e autorice, se agrega su nombre y se reinicia, sin tocar codigo.
#
# ⚠️ Y LO QUE ESTA LISTA NO ES: no es una allowlist de comandos. El catalogo se sirve
# COMPLETO (pedido explicito de admin: «dame todo lo que sale»), menos la lista negra.
# Un comando desconocido se ejecuta: pi es el que sabe si existe, y el puente propaga su
# respuesta. Filtrar contra una lista blanca escondida haria invisible cualquier skill
# nueva que un agente instale.

_CHAT_CMD_DENY = {
    c.strip().lower()
    for c in os.environ.get("CHAT_CMD_DENY", "bash").split(",")
    if c.strip()
}

_CHAT_CMD_WRITE = {
    c.strip().lower()
    for c in os.environ.get(
        "CHAT_CMD_WRITE",
        "set_model,set_thinking_level,compact,new_session,abort").split(",")
    if c.strip()
}

_CHAT_CMD_WRITE_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("CHAT_CMD_WRITE_AGENTS", "").split(",")
    if a.strip()
}

# Longitud maxima del nombre + argumentos. No es paranoia de inyeccion (el puente manda
# el texto por un pipe JSON, no por un shell): acota lo que entra al historial del agente
# y lo que se escribe en audit_log.
_CHAT_CMD_MAX = int(os.environ.get("CHAT_CMD_MAX", "4000"))

# Nombre de comando ADMISIBLE. pi los nombra `llama`, `skill:tdd`, `session-name`: letras,
# digitos, `:`, `_`, `-`, `.`. Se valida el nombre y NO los argumentos (el argumento de un
# comando es texto libre para el agente, igual que un mensaje).
_CHAT_CMD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.\-]{0,79}$")


def _chat_cmd_denied(cmd: str, agent: str):
    """Motivo de rechazo de un comando, o None si esta permitido.

    FUENTE UNICA del predicado: la usan la ruta de EJECUCION (que lanza 403) y la de
    LISTADO (que marca cada comando con `allowed`/`denied_reason`). Si divergieran, la UI
    ofreceria un boton que el BFF rechaza — el mismo defecto que _is_chat_sender existe
    para evitar en /me."""
    c = (cmd or "").strip().lower().lstrip("/")
    if c in _CHAT_CMD_DENY:
        return ("ejecucion arbitraria de codigo en el host del agente (hereda sus "
                "credenciales): no se expone desde el panel")
    if c in _CHAT_CMD_WRITE and agent.strip().lower() not in _CHAT_CMD_WRITE_AGENTS:
        return (f"escribe sobre el runtime de {agent}; solo lo autorizaron "
                f"{', '.join(sorted(_CHAT_CMD_WRITE_AGENTS)) or '(ninguno)'}")
    return None


def _chat_commands_annotate(raw: bytes, agent: str) -> bytes:
    """Anota el catalogo del puente con la decision del BFF y SACA la lista negra.

    Dos cosas distintas y a proposito:
      · los de _CHAT_CMD_DENY se ELIMINAN de la lista (no se muestran ni en gris): que
        `bash` figure en un desplegable invita a intentarlo, y el rechazo llegaria recien
        al tap. `denied_hard` lleva la cuenta para que la ausencia sea VERIFICABLE y no
        se confunda con "el agente no lo tiene".
      · los de _CHAT_CMD_WRITE no autorizados se DEJAN con `allowed: false` y su motivo:
        ahi la informacion util es que el comando EXISTE pero ese agente no lo autorizo.
        Esconderlos haria indistinguible "no lo tiene" de "no te lo permiten".

    Si el body no es JSON parseable se devuelve INTACTO, igual que _chat_strip_thinking:
    esta funcion no decide sobre lo que no entiende."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if not isinstance(data, dict):
        return raw
    cmds = data.get("commands")
    if not isinstance(cmds, list):
        return raw
    kept = []
    hard = 0
    for c in cmds:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        low = name.lower().lstrip("/")
        if low in _CHAT_CMD_DENY:
            hard += 1
            continue
        why = _chat_cmd_denied(name, agent)
        out = dict(c)
        out["allowed"] = why is None
        if why:
            out["denied_reason"] = why
        kept.append(out)
    data["commands"] = kept
    data["count"] = len(kept)
    data["denied_hard"] = hard
    data["executable"] = sum(1 for c in kept if c.get("allowed"))
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


@app.get("/api/chat/commands")
def api_chat_commands(agent: str = Query(..., min_length=1),
                      authorization: str = Header(None)):
    """Catalogo de slash-commands del agente (proxy de GET <puente>/commands).

    Devuelve `{agent, count, commands:[{name, description, source, location, path,
    allowed, denied_reason?}], denied_hard, executable, source}`.

    `count` es EXPLICITO y viaja aunque sea 0: un consumidor tiene que poder distinguir
    "este agente no tiene comandos" (200 con count 0 — medido: assistant-g da exactamente eso)
    de "no pude preguntarle" (que es un 409/503, nunca una lista vacia).

    Gate y auditoria: IDENTICOS a /api/chat/state (mismo _require_chat_sender, misma
    auditoria del acceso Y del rechazo). Lo unico que agrega sobre `_chat_ops_proxy` es
    la anotacion de autorizacion, que es una decision del BFF y no del puente: el puente
    no sabe quien lee ni que autorizo cada agente."""
    ag = (agent or "").strip().lower()
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_commands_denied",
                           f"status={e.status_code} agent={ag[:64]}")
        raise
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_commands",
               f"agent={ag[:64]}")
        conn.commit()
    r = _chat_ops_proxy(ag, "/commands")
    if r.status_code != 200:
        return r
    return Response(content=_chat_commands_annotate(r.body, ag),
                    status_code=r.status_code,
                    media_type="application/json")


@app.post("/api/chat/command")
async def api_chat_command(req: Request, authorization: str = Header(None)):
    """EJECUTA un slash-command en el proceso del agente. Body {agent, command, args?}.

    Autorizado por admin: «listarlos y usarlo, para que queres que lo vea si no es para
    usarlo». Los cuatro controles y su motivo estan en el bloque de arriba; aca queda el
    ORDEN, que importa: gate -> validacion -> autorizacion -> auditoria -> sonda de
    estado -> envio. La auditoria va ANTES del envio a proposito: si el puente muere en
    el intento, el registro de que se INTENTO tiene que existir igual. Un audit_log que
    solo tiene los exitos no sirve para reconstruir que paso.

    Codigos:
      403 -> el comando esta en la lista negra, o escribe el runtime de un agente que no
             lo autorizo. CON EL MOTIVO en el detalle.
      404 -> agente que el panel no conoce.
      409 -> el agente no puede recibir (dormido/mudo). `sent: false` explicito.
      422 -> falta `agent` o `command`, o el nombre no tiene forma de comando.
      504 -> el puente no contesto: el envio queda INDETERMINADO (no se afirma que no
             se ejecuto — pudo haberse escrito al pipe).
      2xx -> lo que devuelva el puente, TAL CUAL (202 = aceptado, respuesta por polling).
    """
    try:
        principal = _require_chat_sender(authorization)
    except HTTPException as e:
        _chat_denied_audit(authorization, "sinapsis_chat_command_denied",
                           f"status={e.status_code}")
        raise
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "body inválido: se espera JSON {agent, command, args?}")

    ag = str((body or {}).get("agent") or "").strip().lower()
    cmd = str((body or {}).get("command") or "").strip().lstrip("/")
    args = str((body or {}).get("args") or "").strip()
    if not ag:
        raise HTTPException(422, "falta 'agent'")
    if not cmd:
        raise HTTPException(422, "falta 'command'")
    if not _CHAT_CMD_RE.match(cmd):
        raise HTTPException(422, "el nombre del comando no tiene forma de comando de pi "
                                 "(letras, dígitos, ':', '_', '-', '.')")

    # Autorizacion ANTES de la sonda de estado: un comando prohibido se rechaza con su
    # motivo aunque el agente este dormido. Si el orden fuera al reves, el mismo pedido
    # daria 409 o 403 segun el humor de la maquina del otro lado.
    why = _chat_cmd_denied(cmd, ag)
    if why:
        with _pg() as conn, conn.cursor() as cur:
            _audit(cur, principal.get("name") or ADMIN_USER,
                   "sinapsis_chat_command_forbidden",
                   f"agent={ag[:64]} command={cmd[:80]}", success=False)
            conn.commit()
        raise HTTPException(403, {"error": "comando no autorizado desde el panel",
                                 "agent": ag, "command": cmd, "reason": why,
                                 "sent": False})

    text = f"/{cmd}" + (f" {args}" if args else "")
    if len(text) > _CHAT_CMD_MAX:
        raise HTTPException(422, f"el comando y sus argumentos superan {_CHAT_CMD_MAX} "
                                 f"caracteres")

    # 🔴 Se registra el NOMBRE del comando (a diferencia de /send, que solo audita la
    # longitud): esto es una ESCRITURA sobre el runtime de otro agente y "quien ejecuto
    # QUE en quien" es justo lo que hay que poder reconstruir. Los ARGUMENTOS no se
    # registran: pueden traer contenido de la conversacion, y de eso audit_log no es dueño.
    with _pg() as conn, conn.cursor() as cur:
        _audit(cur, principal.get("name") or ADMIN_USER, "sinapsis_chat_command",
               f"agent={ag[:64]} command={cmd[:80]} args_chars={len(args)}")
        conn.commit()

    conf = _chat_agents_conf().get(ag)
    if not conf:
        raise HTTPException(404, f"agente de chat desconocido: {ag[:64]}")
    st = _chat_health(ag, conf)
    # CHAT_ST_DONE acepta envios igual que CHAT_ST_OK: los dos significan "el proceso
    # contesto la sonda". Bloquear en `respondió` dejaria el panel mudo justo despues de
    # cada respuesta.
    if st["status"] not in (CHAT_ST_OK, CHAT_ST_DONE):
        raise HTTPException(409, {"error": "el agente no puede ejecutar el comando",
                                  "agent": ag, "command": cmd,
                                  "status": st["status"], "detail": st["detail"],
                                  "sent": False})
    try:
        r = httpx.post(f"{conf['url']}/send",
                       headers={"Authorization": f"Bearer {conf['token']}"},
                       json={"message": text}, timeout=_CHAT_TIMEOUT)
    except httpx.TimeoutException:
        raise HTTPException(504, f"el puente de {ag} no respondio /send en "
                                f"{_CHAT_TIMEOUT:.0f}s; la ejecución de /{cmd} quedó "
                                f"INDETERMINADA")
    except httpx.HTTPError:
        raise HTTPException(409, {"error": "el agente no puede ejecutar el comando",
                                  "agent": ag, "command": cmd,
                                  "status": CHAT_ST_ASLEEP,
                                  "detail": "se perdio la conexion con el puente al enviar",
                                  "sent": False})
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


# Approval BFF v1: no human request/finish routes; service reads and isolated approver writes.
import approval_commands as _approval
from uuid import UUID as _ApprovalUUID
from urllib.parse import urlsplit as _approval_urlsplit

_APPROVAL_ON = os.environ.get("APPROVAL_API_ENABLED") == "true"
_APPROVAL_STATUSES = set(_approval._STATUSES) | {"all"}
_APPROVAL_BEARER = re.compile(r"Bearer [^\s,]+\Z", re.ASCII)

def _approval_boundary(req: Request, *, decision=False):
    if not _APPROVAL_ON:
        raise HTTPException(503, "approval_disabled")
    if req.method == "POST":
        content_type = req.headers.get("content-type", "")
        if not re.fullmatch(r"application/json(?:\s*;\s*charset=(?:utf-8|UTF-8))?", content_type):
            raise HTTPException(415, "approval_content_type_required")
    origins = req.headers.getlist("origin")
    if len(origins) > 1:
        raise HTTPException(403, "approval_origin_forbidden")
    if origins:
        origin = origins[0]
        parsed = _approval_urlsplit(origin)
        public = f"{req.url.scheme}://{req.url.netloc}"
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or
                parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.query or parsed.fragment or
                origin not in (public, *ALLOWED_ORIGINS)):
            raise HTTPException(403, "approval_origin_forbidden")
    headers = [v.decode("latin-1") for k, v in req.scope["headers"] if k.lower() == b"authorization"]
    if len(headers) != 1 or not _APPROVAL_BEARER.fullmatch(headers[0]):
        raise HTTPException(401, "no autorizado")
    principal = _check(headers[0])
    # _check queries session validity, but does not test agents.active. This is
    # auth-only SQL, excluded from approval statement count per contract section 0.8.
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT active FROM agents WHERE id=%s", (principal["id"],))
        active = cur.fetchone()
    if not active or not active["active"]:
        raise HTTPException(401, "no autorizado")
    if principal["kind"] != "human" or principal["name"] != ADMIN_USER:
        raise HTTPException(403, "approval_forbidden")
    return _approval.trusted_principal(principal["name"], principal["kind"], "bff")

_APPROVAL_OUTCOME_UNKNOWN_DETAIL = "outcome_unknown; resultado incierto; no repetir automáticamente; reconciliar según §5 (UUID propio en request/supersede, tupla de estado en los demás) antes de una nueva decisión"

def _approval_bff_failure(exc):
    if isinstance(exc, _approval.ApprovalError):
        if exc.code == "outcome_unknown":
            raise HTTPException(503, _APPROVAL_OUTCOME_UNKNOWN_DETAIL) from exc
        raise HTTPException(exc.status, exc.code) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(422, "approval_invalid_input") from exc
    # Never serialize DB diagnostics/DSN/credentials into the response.
    logging.exception("approval_bff_failure")
    raise HTTPException(500, "approval_error") from exc

def _approval_service_connect():
    return _pg()

def _approval_approver_password():
    # Lazy, one-time read; no approver material is initialized for GET or flag OFF.
    path = os.environ.get("APPROVER_DB_CREDENTIAL_FILE", "")
    if not path:
        raise _approval.ApprovalError("approval_unavailable", 503)
    try:
        pwd = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise _approval.ApprovalError("approval_unavailable", 503) from exc
    if not pwd:
        raise _approval.ApprovalError("approval_unavailable", 503)
    return pwd

from functools import lru_cache as _approval_lru_cache
_approval_approver_password = _approval_lru_cache(maxsize=1)(_approval_approver_password)

def _approval_approver_connect():
    config = {k: v for k, v in PG.items() if k not in ("user", "password")}
    try:
        return psycopg.connect(user="cerebro_approver", password=_approval_approver_password(), **config)
    except psycopg.OperationalError as exc:
        raise _approval.ApprovalError("approval_unavailable", 503) from exc

def _approval_query(req, keys):
    params = req.query_params
    if any(k not in keys or len(params.getlist(k)) != 1 for k in params.keys()):
        raise ValueError("invalid query")
    raw = params.get("limit", "50")
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= 200:
        raise ValueError("invalid limit")
    return int(raw), params.get("cursor")

def _approval_inbox_cursor_decode(value, status):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("invalid inbox cursor")
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip('=') != value.rstrip('='):
            raise ValueError("noncanonical cursor")
        def pairs(entries):
            result = {}
            for k, v in entries:
                if k in result: raise ValueError("duplicate cursor key")
                result[k] = v
            return result
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NaN")))
        if type(data) is not dict or set(data) != {"v", "created_at", "id", "f"} or type(data["f"]) is not str or data["f"] not in _APPROVAL_STATUSES or data["f"] != status:
            raise ValueError("cursor filter mismatch")
        # Validate the ORIGINAL timestamp/UUID/version through the shared decoder.
        # Re-encoding a normalized timestamp before validation would hide non-UTC or
        # non-canonical wire representations supplied by the client.
        inner = _approval_encode_three(data)
        _approval.decode_cursor(inner)
        return inner
    except (ValueError, UnicodeError, TypeError, KeyError) as exc:
        raise ValueError("invalid inbox cursor") from exc

def _approval_encode_three(data):
    raw = json.dumps({k: data[k] for k in ("v", "created_at", "id")}, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')

def _approval_inbox_cursor_encode(cursor, status):
    if cursor is None: return None
    raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
    data = json.loads(raw)
    data['f'] = status
    return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode('utf-8')).decode().rstrip('=')

def _approval_path_id(id):
    return str(_approval._uuid(id))

def _approval_body(body, reason=False):
    required = {"expected_version", "expected_hash"} | ({"reason"} if reason else set())
    if type(body) is not dict or set(body) != required or type(body["expected_version"]) is not int or body["expected_version"] < 0 or type(body["expected_hash"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", body["expected_hash"]):
        raise ValueError("invalid approval body")
    if reason:
        _approval._text(body["reason"])
        if len(body["reason"].encode("utf-8")) > 4096: raise ValueError("reason too long")
    return body

@app.get("/api/approval/inbox")
def api_approval_inbox(req: Request):
    p = _approval_boundary(req)
    try:
        limit, cursor = _approval_query(req, {"limit", "cursor", "status"})
        status = req.query_params.get("status", "pending_approval")
        if status not in _APPROVAL_STATUSES: raise ValueError("invalid status")
        cursor = _approval_inbox_cursor_decode(cursor, status)
        result = _approval.ApprovalCommands(_approval_service_connect).list_inbox(p, limit, cursor, status=status)
        result["next_cursor"] = _approval_inbox_cursor_encode(result["next_cursor"], status)
        return result
    except Exception as exc: _approval_bff_failure(exc)

@app.get("/api/approval/mine")
def api_approval_mine(req: Request):
    _approval_boundary(req)
    try:
        limit, cursor = _approval_query(req, {"limit", "cursor", "status"})
        status = req.query_params.get("status")
        if status is not None and status not in _approval._STATUSES: raise ValueError("invalid status")
        if cursor is not None: _approval.decode_cursor(cursor)
        return {"items": [], "next_cursor": None}
    except Exception as exc: _approval_bff_failure(exc)

@app.get("/api/approval/requests/{id}/review")
def api_approval_review(req: Request, id: str):
    p = _approval_boundary(req)
    try: return _approval.ApprovalCommands(_approval_service_connect).get_for_review(p, _approval_path_id(id))
    except Exception as exc: _approval_bff_failure(exc)

@app.get("/api/approval/requests/{id}")
def api_approval_detail(req: Request, id: str):
    p = _approval_boundary(req)
    try: return _approval.ApprovalCommands(_approval_service_connect).get(p, _approval_path_id(id))
    except Exception as exc: _approval_bff_failure(exc)

async def _approval_decide(req: Request, id: str, operation: str):
    p = _approval_boundary(req, decision=True)
    try:
        if req.query_params: raise ValueError("unexpected query")
        uid = _approval_path_id(id)
        body = _approval_body(await req.json(), reason=operation != "approve")
        # The approver has no SELECT. Check task kind/existence via service pool to
        # keep ordinary/missing IDs indistinguishable (404) before decision SQL.
        _approval.ApprovalCommands(_approval_service_connect).get(p, uid)
        repo = _approval.ApprovalCommands(_approval_approver_connect)
        if operation == "approve": return repo.approve(p, uid, body["expected_version"], body["expected_hash"])
        if operation == "revoke": return repo.revoke(p, uid, body["expected_version"], body["expected_hash"], body["reason"])
        return repo.request_changes(p, uid, body["expected_version"], body["expected_hash"], body["reason"])
    except (json.JSONDecodeError, UnicodeError, TypeError):
        raise HTTPException(422, "approval_invalid_input")
    except Exception as exc: _approval_bff_failure(exc)

@app.post("/api/approval/requests/{id}/approve")
async def api_approval_approve(req: Request, id: str):
    return await _approval_decide(req, id, "approve")

@app.post("/api/approval/requests/{id}/revoke")
async def api_approval_revoke(req: Request, id: str):
    return await _approval_decide(req, id, "revoke")

@app.post("/api/approval/requests/{id}/request-changes")
async def api_approval_request_changes(req: Request, id: str):
    return await _approval_decide(req, id, "request_changes")

@app.get("/api/tasks")
def api_tasks(authorization: str = Header(None)):
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT id::text AS id,title,description,project,organization,status,priority,assignee,subagent,"
                    "claimed_by,claimed_at::text AS claimed_at,blocked_reason,"
                    "parent_id::text AS parent_id,due_date::text AS due_date,tags,"
                    "updated_at::text AS updated_at,created_at::text AS created_at FROM tasks "
                    "WHERE request_kind IS DISTINCT FROM 'approval_request' "
                    "ORDER BY organization,project,status")
        return {"tasks": cur.fetchall()}


# ==== Biblioteca de skills (F3) ====
# El registro compartido de skills vive en `chunks_meta` bajo el prefijo 'skills/'.
# Los references hijos ('skills/<name>/references/%') NO son skills: se excluyen.
# Espejo EXACTO de gateway/db.py::list_skills (misma semantica de vigencia y dedup),
# adaptado a la conexion local de Sinapsis (_pg). SOLO SELECT.
_SKILL_PREFIX_LIKE = "skills/%"
_SKILL_REFS_LIKE = "skills/%/references/%"
# M1 (auditoria ): allowlist HARDCODEADA de fragmentos de WHERE. El SQL se
# arma solo con estos literales del FUENTE; los VALORES van siempre parametrizados (%s).
_SKILL_WHERE_BASE = ("source_path LIKE %s", "source_path NOT LIKE %s",
                     "deleted_at IS NULL", "(valid_until IS NULL OR valid_until > now())")
_SKILL_WHERE_Q = ("(source_path ILIKE %s OR coalesce(title,'') ILIKE %s "
                  "OR coalesce(text_full, text_preview, '') ILIKE %s)")


def _skill_description_from_text(text, fallback: str = "") -> str:
    """Extrae la DESCRIPCION del cuerpo de un skill publicado por skill_add.
    Formato que escribe skill_add (mcp_server.skill_add):
        SKILL: <name>
        DESCRIPCION: <description>
        [CUANDO USAR: <when_to_use>]
        <blank>
        <content>
    Si no hay linea 'DESCRIPCION:', cae al primer parrafo no-encabezado (truncado),
    y si tampoco hay nada usa `fallback` (normalmente el title). Nunca devuelve None."""
    t = text or ""
    for line in t.splitlines():
        s = line.strip()
        for tag in ("DESCRIPCION:", "DESCRIPCI\u00d3N:", "DESCRIPTION:"):
            if s.upper().startswith(tag.upper()):
                val = s[len(tag):].strip()
                if val:
                    return val
    # sin encabezado: primer parrafo util, sin las lineas de metadata conocidas
    for line in t.splitlines():
        s = line.strip()
        if not s:
            continue
        up = s.upper()
        if up.startswith(("SKILL:", "CUANDO USAR:", "WHEN TO USE:", "#")):
            continue
        return s[:300]
    return fallback


@app.get("/api/skills")
def api_skills(q: str = "", tag: str = "", limit: int = 50, offset: int = 0,
               authorization: str = Header(None)):
    """Biblioteca: skills VIGENTES del registro compartido, deduplicados por skill.

    Filtros opcionales (AND): `q` substring case-insensitive contra nombre/title/texto,
    `tag` tag exacto en chunks_meta.tags. Paginacion limit/offset sobre orden estable
    por source_path; `total` es el conteo COMPLETO (sin paginar) para saber si hay mas.

    Vigencia: deleted_at IS NULL AND (valid_until IS NULL OR valid_until > now()).
    Dedup: row_number() OVER (PARTITION BY source_path ORDER BY chunk_index) = 1.
    Los references hijos ('skills/<name>/references/%') quedan EXCLUIDOS.

    Auth: sesion (mismo patron que el resto de /api/*). No aplica scope de vault:
    'skills/' no es una ruta del vault (es el registro compartido de la flota) y no
    puede intersectar '95 Secrets'. Es SOLO SELECT."""
    _check(authorization)
    lim = max(1, min(int(limit or 50), 500))
    off = max(0, int(offset or 0))
    # WHERE armado SOLO con literales de la allowlist del fuente (M1). Los valores
    # que vienen del request (q, tag) van 100% parametrizados en `params`.
    where = list(_SKILL_WHERE_BASE)
    params: list = [_SKILL_PREFIX_LIKE, _SKILL_REFS_LIKE]
    qs = (q or "").strip()
    if qs:
        where.append(_SKILL_WHERE_Q)
        like = f"%{qs}%"
        params += [like, like, like]
    tg = (tag or "").strip()
    if tg:
        where.append("%s = ANY(tags)")
        params.append(tg)
    clause = " AND ".join(where)

    base = ("SELECT source_path, chunk_index, title, tags, agent_owner, sensitivity, "
            "category, created_at, updated_at, text_preview, text_full, "
            "count(*) OVER (PARTITION BY source_path) AS chunk_count, "
            "row_number() OVER (PARTITION BY source_path ORDER BY chunk_index ASC) AS rn "
            f"FROM chunks_meta WHERE {clause}")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT count(DISTINCT source_path) AS n FROM chunks_meta WHERE {clause}",
            params)
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"SELECT * FROM ({base}) s WHERE rn = 1 ORDER BY source_path ASC "
            "LIMIT %s OFFSET %s",
            params + [lim, off])
        rows = [dict(r) for r in cur.fetchall()]

    out = []
    for r in rows:
        sp = r["source_path"]
        name = sp[len("skills/"):] if sp.startswith("skills/") else sp
        body = r.get("text_full") or r.get("text_preview") or ""
        title = r.get("title") or name
        out.append({
            "name": name,
            "source_path": sp,
            "title": title,
            "description": _skill_description_from_text(body, fallback=title),
            "tags": list(r.get("tags") or []),
            "agent_owner": r.get("agent_owner"),
            "category": r.get("category"),
            "chunk_count": int(r.get("chunk_count") or 1),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
        })
    return {"skills": out, "count": len(out), "total": total,
            "limit": lim, "offset": off, "q": qs, "tag": tg,
            "has_more": (off + len(out)) < total}


@app.get("/api/skill")
def api_skill(name: str = "", authorization: str = Header(None)):
    """Contenido COMPLETO de UNA skill vigente del registro compartido.

    Concatena TODOS los chunks activos de 'skills/<name>' en orden de chunk_index
    (una skill puede estar partida en varios chunks) separados por doble salto de
    linea. Por chunk usa text_full y cae a text_preview si viene NULL/vacio.

    Vigencia: identica a /api/skills -> deleted_at IS NULL AND
    (valid_until IS NULL OR valid_until > now()).
    Los references hijos ('skills/<name>/references/%') NO son skills: un `name`
    que contenga '/references/' se rechaza (404).

    Auth: sesion via _check (mismo patron que /api/skills). No aplica scope de
    vault: 'skills/' no es una ruta del vault y no puede intersectar '95 Secrets'.

    SQL 100% PARAMETRIZADO (M1): el `name` viaja SIEMPRE como %s, jamas por
    f-string. Es SOLO SELECT."""
    _check(authorization)
    nm = (name or "").strip().strip("/")
    if not nm:
        raise HTTPException(400, "falta name")
    if "/references/" in nm:
        raise HTTPException(404, "no existe")
    source_path = "skills/" + nm
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_index, text_full, text_preview FROM chunks_meta "
            "WHERE source_path = %s AND deleted_at IS NULL "
            "AND (valid_until IS NULL OR valid_until > now()) "
            "ORDER BY chunk_index ASC",
            (source_path,))
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        raise HTTPException(404, "no existe")
    parts = []
    for r in rows:
        body = r.get("text_full")
        if not body:
            body = r.get("text_preview") or ""
        if body:
            parts.append(body.rstrip())
    return {"name": nm, "content": "\n\n".join(parts),
            "chunk_count": len(rows)}


@app.get("/api/meta")
def api_meta(authorization: str = Header(None)):
    """Metadata dinamica para la UI: roster de agentes/lideres/subagentes por equipo
    (tabla agents) y proyectos distintos existentes (tabla tasks). NADA hardcodeado."""
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, team, is_lead, kind FROM agents WHERE active=true ORDER BY name")
        rows = cur.fetchall()
        cur.execute("SELECT DISTINCT project FROM tasks "
                    "WHERE project IS NOT NULL AND project<>'' "
                    "AND request_kind IS DISTINCT FROM 'approval_request' ORDER BY project")
        projects = [r["project"] for r in cur.fetchall()]
        # --- Jerarquia Organizacion -> Proyecto (pedido de admin , cambio de implementación) ---
        # `projects` (string[]) se CONSERVA tal cual: el frontend lo consume como
        # string[] en 6 lugares (api.ts:370, TaskDialog, Board, HomeView,
        # TaskFilterBar, CommandPalette). Cambiarle la forma rompe la UI, asi que
        # la informacion nueva va en claves NUEVAS y aditivas.
        cur.execute("SELECT id, name, color, sort_order FROM organizations "
                    "WHERE active=true ORDER BY sort_order, name")
        organizations = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT p.id, p.name, p.org_id, p.status, p.sort_order "
                    "FROM projects p WHERE p.status<>'archived' "
                    "ORDER BY p.sort_order, p.name")
        projects_v2 = [dict(r) for r in cur.fetchall()]
    agents = [r["name"] for r in rows]
    leads = [r["name"] for r in rows if r["is_lead"]]
    leader_teams = {r["name"]: r["team"] for r in rows
                    if r["is_lead"] and r["kind"] == "agent" and r["team"]}
    subagents_by_team = {}
    for r in rows:
        if not r["is_lead"] and r["team"]:
            subagents_by_team.setdefault(r["team"], []).append(r["name"])
    return {"agents": agents, "leads": leads, "leader_teams": leader_teams,
            "subagents_by_team": subagents_by_team, "projects": projects,
            "organizations": organizations, "projects_v2": projects_v2}


# --- M1 (auditoria ): allowlists HARDCODEADAS de columnas de `tasks` ---
# Unico origen de los nombres de columna interpolados por f-string en el SQL de
# task_update / task_create. Definidas en el FUENTE, jamas derivadas del request:
# el cliente solo puede elegir CUALES de estas usar, nunca introducir un nombre.
# Los VALORES van siempre parametrizados (%s). Si agregas una columna, agregala aca.
_TASK_UPDATE_COLS = ("due_date", "assignee", "subagent", "status", "priority", "title",
                     "project", "parent_id", "claimed_by", "blocked_reason",
                     "organization")
_TASK_CREATE_COLS = ("description", "project", "priority", "assignee", "subagent", "due_date",
                    "organization")

# The BFF historically ignores unrelated create extras (including status/creator).
# Only approval protocol keys must fail rather than silently become no-ops.
_APPROVAL_RESERVED_TASK_COLUMNS = frozenset({
    "request_kind", "version", "creator_kind", "action_type", "resource_kind",
    "resource_id", "scope_version", "scope_canonical", "scope_hash",
    "approved_by", "approved_at", "revoked_at", "finished_by",
    "finished_at", "replaces_request_id",
})
_APPROVAL_CREATE_KEYS = _APPROVAL_RESERVED_TASK_COLUMNS | frozenset({
    "request_id", "new_request_id", "scope", "evidence", "approval_kind",
    "approval_type", "expected_hash", "expected_version", "effects", "actor",
    "channel", "decision_reason",
})


def _approval_create_offenders(body):
    return sorted(k for k in body if isinstance(k, str) and
                  (k in _APPROVAL_CREATE_KEYS or k.startswith("approval")))


def _task_immutable_check(cur, *ids):
    """Lock existing endpoints in deterministic order until transaction ends."""
    cur.execute("SELECT id, request_kind FROM tasks WHERE id::text = ANY(%s::text[]) "
                "ORDER BY id FOR SHARE", ([str(i) for i in ids],))
    if any(row["request_kind"] == "approval_request" for row in cur.fetchall()):
        raise HTTPException(409, "approval_request_immutable")


def _task_visible_read(cur, tid):
    cur.execute("SELECT 1 FROM tasks WHERE id::text = %s "
                "AND request_kind IS DISTINCT FROM 'approval_request'", (str(tid),))
    if not cur.fetchone():
        raise HTTPException(404, "no encontrada")


@app.exception_handler(psycopg.Error)
async def _task_sql_error(_req: Request, exc: psycopg.Error):
    if exc.sqlstate == "PZ101":
        return JSONResponse(status_code=409, content={"detail": "approval_request_immutable"})
    raise exc


@app.post("/api/task_update")
async def api_task_update(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    # Legacy BFF boundary only: historical Sinapsis sends an explicitly-present
    # empty string to detach. Translate it before the shared contract; omitted
    # and null remain no-change, while public REST/MCP continue to require '-'.
    parent_present = "parent_id" in b
    parent_value = "-" if parent_present and b.get("parent_id") == "" else b.get("parent_id")
    parent_requested = parent_present and parent_value is not None
    parent_clear = parent_value == "-"
    with _pg() as conn, conn.cursor() as cur:
        # One connection/transaction covers lock, child resolution, parent/cycle
        # resolution, UPDATE and commit. Never inspect hierarchy before the lock.
        if parent_requested:
            tasks_rbac.acquire_parent_write_guard(cur)
            canonical_tid, status, detail = tasks_rbac.resolve_id_core(cur, tid)
            if detail:
                raise HTTPException(status, detail)
        else:
            canonical_tid = tid
        resolved = None
        if parent_requested and not parent_clear:
            resolved, status, detail = tasks_rbac.resolve_parent_id_core(
                cur, parent_value, child_id=canonical_tid, guard_held=True)
            if detail:
                raise HTTPException(status, detail)
            _task_immutable_check(cur, resolved)
        fields, params = [], []
        previous_status = None
        if b.get("status"):
            cur.execute("SELECT status FROM tasks WHERE id::text = %s", (canonical_tid,))
            previous = cur.fetchone()
            previous_status = previous["status"] if previous else None
        for col in _TASK_UPDATE_COLS:
            if col == "parent_id":
                continue
            if col in b:
                assert col in _TASK_UPDATE_COLS
                v = b[col]
                fields.append(f"{col} = %s"); params.append(v if v != "" else None)
        if parent_requested:
            fields.append("parent_id = %s"); params.append(resolved)
        if not fields:
            raise HTTPException(400, "nada para actualizar")
        fields.append("updated_at = now()")
        params.append(canonical_tid)
        cur.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id::text = %s "
                    "RETURNING id::text AS id,title,status,priority,assignee,subagent,"
                    "claimed_by,claimed_at::text AS claimed_at,blocked_reason,"
                    "parent_id::text AS parent_id,due_date::text AS due_date", params)
        row = cur.fetchone()
        if row and b.get("status") and previous_status != b["status"]:
            cur.execute(
                "INSERT INTO task_activity (task_id, agent, kind, from_status, to_status) "
                "VALUES (%s,%s,'status_change',%s,%s)",
                (row["id"], principal["name"], previous_status, b["status"]))
        unblocked = []
        if row and b.get("status") == "done":
            cur.execute(
                "SELECT DISTINCT t.id::text AS id, t.title, t.status, t.project "
                "FROM tasks t JOIN task_deps d ON d.task_id = t.id "
                "WHERE d.depends_on_id = %s AND t.status = 'todo' "
                "AND t.request_kind IS DISTINCT FROM 'approval_request' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_deps d2 JOIN tasks dt ON dt.id = d2.depends_on_id "
                "  WHERE d2.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' "
                "  AND dt.status <> 'done')",
                (tid,))
            unblocked = cur.fetchall()
        conn.commit()
    if not row:
        raise HTTPException(404, "no encontrada")
    return {"ok": True, "task": row, "unblocked": unblocked}


@app.post("/api/task_create")
async def api_task_create(req: Request, authorization: str = Header(None)):
    _check(authorization)
    b = await req.json()
    if _approval_create_offenders(b):
        raise HTTPException(422, "approval_create_fields_forbidden")
    title = (b.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title requerido")
    cols = {"title": title}
    for col in _TASK_CREATE_COLS:
        if b.get(col) not in (None, ""):
            cols[col] = b[col]
    tags = b.get("tags")
    if isinstance(tags, list) and tags:
        cols["tags"] = tags
    with _pg() as conn, conn.cursor() as cur:
        if b.get("parent_id") not in (None, ""):
            resolved, status, detail = tasks_rbac.resolve_parent_id_core(cur, b["parent_id"])
            if detail:
                raise HTTPException(status, detail)
            _task_immutable_check(cur, resolved)
            cols["parent_id"] = resolved
        names = list(cols.keys())
        placeholders = ", ".join(["%s"] * len(names))
        params = [cols[n] for n in names]
        cur.execute(
            f"INSERT INTO tasks ({', '.join(names)}) VALUES ({placeholders}) "
            "RETURNING id::text AS id,title,description,project,status,priority,assignee,subagent,"
            "parent_id::text AS parent_id,due_date::text AS due_date",
            params,
        )
        row = cur.fetchone(); conn.commit()
    return {"ok": True, "task": row}


@app.post("/api/task_cancel")
async def api_task_cancel(req: Request, authorization: str = Header(None)):
    """Cancelación SOFT: status='cancelled'. NUNCA DELETE (Regla de Oro)."""
    _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET status='cancelled', updated_at=now() WHERE id::text = %s "
                    "RETURNING id::text AS id,status", (tid,))
        row = cur.fetchone(); conn.commit()
    if not row:
        raise HTTPException(404, "no encontrada")
    return {"ok": True, "task": row}


@app.get("/api/subtasks")
def api_subtasks(id: str, authorization: str = Header(None)):
    """Subtareas directas de una tarea (por parent_id)."""
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        _task_visible_read(cur, id)
        cur.execute("SELECT id::text AS id,title,status,priority,assignee,project "
                    "FROM tasks WHERE parent_id::text = %s AND status <> 'cancelled' "
                    "AND request_kind IS DISTINCT FROM 'approval_request' "
                    "AND EXISTS (SELECT 1 FROM tasks p WHERE p.id::text = %s "
                    "AND p.request_kind IS DISTINCT FROM 'approval_request') "
                    "ORDER BY status,title", (id, id))
        return {"subtasks": cur.fetchall()}


@app.post("/api/task_link")
async def api_task_link(req: Request, authorization: str = Header(None)):
    """Crea dependencia: task_id queda BLOQUEADA POR depends_on_id.
    Rechaza auto-dependencia y ciclo directo (si ya existe la inversa)."""
    _check(authorization)
    b = await req.json()
    tid = b.get("task_id"); did = b.get("depends_on_id")
    if not tid or not did:
        raise HTTPException(400, "task_id y depends_on_id requeridos")
    tid, did = str(tid), str(did)
    if tid == did:
        raise HTTPException(400, "auto-dependencia no permitida")
    with _pg() as conn, conn.cursor() as cur:
        _task_immutable_check(cur, tid, did)
        cur.execute("SELECT id::text AS id FROM tasks WHERE id::text = %s", (tid,))
        if not cur.fetchone():
            raise HTTPException(404, "task_id no existe")
        cur.execute("SELECT id::text AS id FROM tasks WHERE id::text = %s", (did,))
        if not cur.fetchone():
            raise HTTPException(404, "depends_on_id no existe")
        # ciclo directo: ya existe la relacion inversa (did depende de tid)
        cur.execute("SELECT 1 FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s",
                    (did, tid))
        if cur.fetchone():
            raise HTTPException(409, "ciclo directo: la dependencia inversa ya existe")
        cur.execute("INSERT INTO task_deps (task_id, depends_on_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", (tid, did))
        conn.commit()
    return {"ok": True, "task_id": tid, "depends_on_id": did}


@app.post("/api/task_unlink")
async def api_task_unlink(req: Request, authorization: str = Header(None)):
    """Quita una dependencia (relacion, NO borra tareas)."""
    _check(authorization)
    b = await req.json()
    tid = b.get("task_id"); did = b.get("depends_on_id")
    if not tid or not did:
        raise HTTPException(400, "task_id y depends_on_id requeridos")
    with _pg() as conn, conn.cursor() as cur:
        _task_immutable_check(cur, tid, did)
        cur.execute("DELETE FROM task_deps WHERE task_id::text = %s AND depends_on_id::text = %s "
                    "RETURNING task_id::text AS task_id", (str(tid), str(did)))
        row = cur.fetchone(); conn.commit()
    if not row:
        raise HTTPException(404, "relacion no encontrada")
    return {"ok": True, "unlinked": {"task_id": str(tid), "depends_on_id": str(did)}}


@app.get("/api/task_deps")
def api_task_deps(id: str, authorization: str = Header(None)):
    """blocked_by: tareas de las que depende esta. blocks: tareas que dependen de esta."""
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        _task_visible_read(cur, id)
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
                    "WHERE d.task_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' "
                    "AND EXISTS (SELECT 1 FROM tasks p WHERE p.id::text=%s "
                    "AND p.request_kind IS DISTINCT FROM 'approval_request') "
                    "ORDER BY dt.status,dt.title", (id, id))
        blocked_by = cur.fetchall()
        cur.execute("SELECT dt.id::text AS id, dt.title, dt.status "
                    "FROM task_deps d JOIN tasks dt ON dt.id = d.task_id "
                    "WHERE d.depends_on_id::text = %s AND dt.request_kind IS DISTINCT FROM 'approval_request' "
                    "AND EXISTS (SELECT 1 FROM tasks p WHERE p.id::text=%s "
                    "AND p.request_kind IS DISTINCT FROM 'approval_request') "
                    "ORDER BY dt.status,dt.title", (id, id))
        blocks = cur.fetchall()
    return {"id": id, "blocked_by": blocked_by, "blocks": blocks}


@app.get("/api/tasks_ready")
def api_tasks_ready(project: str = "", authorization: str = Header(None)):
    """Tareas en 'todo' listas para empezar: sin dependencias, o con TODAS sus deps 'done'."""
    _check(authorization)
    proj = project or None
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.status, t.priority, t.assignee "
            "FROM tasks t WHERE t.status = 'todo' AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND (%s::text IS NULL OR t.project = %s) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' "
            "AND dt.status <> 'done') "
            "ORDER BY t.project,t.priority,t.title",
            (proj, proj))
        return {"tasks_ready": cur.fetchall()}


# ==== F1 ISSUES (additive) ====
# Maquina de estados (SDD secc.4) — espejo de las tools MCP.
# 'submitted' (entregada, esperando gate) FALTABA en las dos estructuras: sin el,
# _can_transition('in_progress','submitted') era False y no habia forma de entregar
# desde el panel. Paridad LITERAL con la fuente canonica del Cerebro
# (gateway/tasks_core.py::VALID_STATUSES / TRANSITIONS) — si cambia alla, cambia aca.
_VALID_STATUSES = {"todo", "in_progress", "blocked", "in_review", "submitted",
                   "done", "deferred", "cancelled"}
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
    return True if frm == to else (to in _TRANSITIONS.get(frm, set()))


def _task_title(tid):
    """Titulo de una tarea, para el cuerpo del aviso del bus. Best-effort: si falla,
    el aviso sale sin titulo antes que no salir."""
    try:
        with _pg() as c, c.cursor() as cur:
            cur.execute("SELECT title FROM tasks WHERE id::text = %s "
                        "AND request_kind IS DISTINCT FROM 'approval_request'", (str(tid),))
            r = cur.fetchone()
            return (r["title"] if r else "") or ""
    except Exception:
        return ""


def _bff_actor(principal):
    """Actor SIEMPRE de la sesion autenticada, NUNCA del body (fix H3).

    Antes se aceptaba 'agent' del body, lo que permitia a cualquier sesion valida
    atribuirse eventos de task_activity como otro agente (spoofing de atribucion).
    El body se ignora por completo para la atribucion."""
    return principal.get("name") or ADMIN_USER


def _task_status(cur, tid):
    """Estado minimo de una tarea para decidir transiciones.

    FIX : se agrego `submitted_by`. Sin esa columna, el gate de auto-firma
    de api_task_review leia None SIEMPRE y por lo tanto NUNCA bloqueaba: existia en el
    codigo, se leia como un control, y no podia fallar. Es el patron del control
    DECORATIVO (mismo genero que un `isError` que pi ignora, o un assert que compara
    un valor contra si mismo). Un gate que no puede disparar es PEOR que no tenerlo,
    porque el proximo que lea el codigo va a creer que el control existe."""
    cur.execute("SELECT status, claimed_by, submitted_by FROM tasks WHERE id::text = %s",
                (tid,))
    return cur.fetchone()


@app.post("/api/task_claim")
async def api_task_claim(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    who = _bff_actor(principal)
    with _pg() as conn, conn.cursor() as cur:
        t = _task_status(cur, str(tid))
        if not t:
            raise HTTPException(404, "no encontrada")
        st = t["status"]
        cb = t["claimed_by"] or ""
        if st not in ("todo", "in_review", "blocked"):
            raise HTTPException(409, f"no se puede tomar: status={st}")
        if cb and cb != who:
            raise HTTPException(409, f"ya claimeada por '{cb}'")
        cur.execute("UPDATE tasks SET claimed_by=%s, claimed_at=now(), status='in_progress', "
                    "updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(claimed_by, '')=%s "
                    "RETURNING id::text AS id,status,claimed_by", (who, str(tid), st, cb))
        row = cur.fetchone()
        if not row:
            raise HTTPException(409, "conflicto concurrente: la tarea cambio despues de leerla")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'claim',%s,'in_progress',%s)", (str(tid), who, st, f"claimed by {who}"))
        conn.commit()
    return {"ok": True, "task": row}


@app.post("/api/task_comment")
async def api_task_comment(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    body = (b.get("body") or "").strip()
    if not tid or not body:
        raise HTTPException(400, "id y body requeridos")
    who = _bff_actor(principal)
    with _pg() as conn, conn.cursor() as cur:
        _task_immutable_check(cur, tid)
        cur.execute("SELECT 1 FROM tasks WHERE id::text = %s", (str(tid),))
        if not cur.fetchone():
            raise HTTPException(404, "no encontrada")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'comment',%s)", (str(tid), who, body))
        conn.commit()
    return {"ok": True}


@app.post("/api/task_handoff")
async def api_task_handoff(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    to_agent = (b.get("to_agent") or "").strip()
    if not tid or not to_agent:
        raise HTTPException(400, "id y to_agent requeridos")
    who = _bff_actor(principal)
    note = b.get("note") or f"handoff -> {to_agent}"
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("UPDATE tasks SET assignee=%s, claimed_by='', updated_at=now() "
                    "WHERE id::text = %s RETURNING id::text AS id,assignee,claimed_by", (to_agent, str(tid)))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "no encontrada")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, body) "
                    "VALUES (%s,%s,'handoff',%s)", (str(tid), who, note))
        conn.commit()
    # Hook bus (cambio de implementación, ). ESTA PUERTA NO NOTIFICABA NADA: el BFF
    # implementa el ciclo completo del tablero y no tenia una sola referencia a
    # bus_messages, asi que toda derivacion hecha desde la UI era invisible para el
    # destinatario, incluso si era lider. `who` es el actor de la SESION (humano),
    # no una key ajena: la atribucion queda correcta (mismo criterio que _bff_actor).
    notified = bus_notify.notify_assignment(
        _pg, who, to_agent,
        f"Tarea recibida por handoff: {_task_title(str(tid))} (de {who})", str(tid))
    return {"ok": True, "task": row, "bus_notified": bool(notified)}


@app.post("/api/task_submit")
async def api_task_submit(req: Request, authorization: str = Header(None)):
    """in_progress -> submitted: el executor ENTREGA para auditoria del gate.

    Faltaba: el panel podia crear/tomar/comentar/derivar/aprobar pero no ENTREGAR,
    que es el paso que alimenta al gate. Sin esto el ciclo no se cerraba desde la UI.

    Escribe `submitted_by` = actor de la SESION (nunca del body, igual que el resto
    via _bff_actor): es el dato con el que `review` evalua el invariante relacional
    "quien entrego no firma". Sin el, ese control no tiene con que operar.
    Espejo de gateway/tasks_core.py::submit (misma transicion, misma columna, mismo
    evento 'status_change' en task_activity)."""
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    who = _bff_actor(principal)
    note = b.get("note") or ""
    with _pg() as conn, conn.cursor() as cur:
        t = _task_status(cur, str(tid))
        if not t:
            raise HTTPException(404, "no encontrada")
        st = t["status"]
        if not _can_transition(st, "submitted"):
            raise HTTPException(409, f"no se puede entregar desde status={st}")
        cur.execute("UPDATE tasks SET status='submitted', submitted_by=%s, updated_at=now() "
                    "WHERE id::text = %s "
                    "RETURNING id::text AS id,status,submitted_by", (who, str(tid)))
        row = cur.fetchone()
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'status_change',%s,'submitted',%s)", (str(tid), who, st, note))
        conn.commit()
    return {"ok": True, "task": row}


@app.post("/api/task_review")
async def api_task_review(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    v = (b.get("verdict") or "").strip().lower()
    if not tid or v not in ("pass", "needs_changes"):
        raise HTTPException(400, "id y verdict (pass|needs_changes) requeridos")
    who = _bff_actor(principal)
    note = b.get("note") or ""
    with _pg() as conn, conn.cursor() as cur:
        t = _task_status(cur, str(tid))
        if not t:
            raise HTTPException(404, "no encontrada")
        # FIX : aceptar TAMBIEN 'submitted'. Antes exigia 'in_review' estricto
        # mientras la UI YA ofrecia el boton Aprobar para tareas 'submitted' y el gateway
        # acepta ("in_review","submitted") => aprobar desde el panel daba 409 sobre las 8
        # tareas que hoy esperan firma. La UI prometia una capacidad que el backend negaba.
        if t["status"] not in ("in_review", "submitted"):
            raise HTTPException(
                409, f"solo se revisa en in_review/submitted (status={t['status']})")
        # FIX : INVARIANTE RELACIONAL "quien entrega no firma". El gateway lo
        # aplica (tasks_core.review -> tasks_rbac.self_review_denied) y este BFF NO, asi
        # que el panel era una PUERTA SIN EL CONTROL: la misma identidad podia entregar y
        # firmar. Es la leccion de las N puertas -- un control aplicado en un solo
        # transporte no esta aplicado. admin queda EXENTO, igual que en el gateway
        # (SELF_REVIEW_EXEMPT), porque es la autoridad de cierre.
        _submitter = (t.get("submitted_by") or "").strip()
        if _submitter and _submitter == who and who != ADMIN_USER:
            raise HTTPException(
                403, f"no podes auditar tu propia entrega: la tarea la entregaste vos "
                     f"('{who}'). Que la firme otro lider o un auditor.")
        _from = t["status"]
        to = "done" if v == "pass" else "in_progress"
        _submitted_by = t.get("submitted_by") or ""
        _claimed_by = t.get("claimed_by") or ""
        cur.execute("UPDATE tasks SET status=%s, updated_at=now() WHERE id::text = %s AND status=%s "
                    "AND COALESCE(submitted_by, '')=%s AND COALESCE(claimed_by, '')=%s "
                    "RETURNING id::text AS id,status", (to, str(tid), _from, _submitted_by, _claimed_by))
        row = cur.fetchone()
        if not row:
            raise HTTPException(409, "conflicto concurrente: la tarea cambio despues de leerla")
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'review',%s,%s,%s)", (str(tid), who, _from, to, f"{v}: {note}"))
        conn.commit()
    return {"ok": True, "task": row}


@app.post("/api/task_block")
async def api_task_block(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    who = _bff_actor(principal)
    reason = b.get("reason") or ""
    with _pg() as conn, conn.cursor() as cur:
        t = _task_status(cur, str(tid))
        if not t:
            raise HTTPException(404, "no encontrada")
        if not _can_transition(t["status"], "blocked"):
            raise HTTPException(409, f"no se puede bloquear desde status={t['status']}")
        cur.execute("UPDATE tasks SET status='blocked', blocked_reason=%s, updated_at=now() "
                    "WHERE id::text = %s RETURNING id::text AS id,status,blocked_reason", (reason, str(tid)))
        row = cur.fetchone()
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'block',%s,'blocked',%s)", (str(tid), who, t["status"], reason))
        conn.commit()
    return {"ok": True, "task": row}


@app.post("/api/task_unblock")
async def api_task_unblock(req: Request, authorization: str = Header(None)):
    principal = _check(authorization)
    b = await req.json()
    tid = b.get("id")
    if not tid:
        raise HTTPException(400, "id requerido")
    who = _bff_actor(principal)
    note = b.get("note") or ""
    with _pg() as conn, conn.cursor() as cur:
        t = _task_status(cur, str(tid))
        if not t:
            raise HTTPException(404, "no encontrada")
        if t["status"] != "blocked":
            raise HTTPException(409, f"no esta blocked (status={t['status']})")
        cur.execute("UPDATE tasks SET status='in_progress', blocked_reason='', updated_at=now() "
                    "WHERE id::text = %s RETURNING id::text AS id,status", (str(tid),))
        row = cur.fetchone()
        cur.execute("INSERT INTO task_activity (task_id, agent, kind, from_status, to_status, body) "
                    "VALUES (%s,%s,'unblock','blocked','in_progress',%s)", (str(tid), who, note))
        conn.commit()
    return {"ok": True, "task": row}


@app.get("/api/tasks_inbox")
def api_tasks_inbox(agent: str = "", authorization: str = Header(None)):
    """Bandeja de un agente (assignee). status en
    {submitted,todo,in_progress,in_review,blocked}, con flag ready (deps cumplidas).

    'submitted' (entregada, espera firma del gate) FALTABA en el filtro: habia 8
    tareas de admin esperando su firma y NINGUNA aparecia en su bandeja — el unico
    estado que existe para PEDIR una firma era el unico que la bandeja no listaba.
    Van PRIMERO en el orden: es trabajo terminado que bloquea el cierre de algo ya
    hecho, no trabajo pendiente. Prioridad y fecha quedan como criterios secundarios."""
    _check(authorization)
    who = (agent or "").strip()
    if not who:
        raise HTTPException(400, "agent requerido")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.id::text AS id, t.title, t.project, t.status, t.priority, t.assignee, "
            "t.claimed_by, t.blocked_reason, "
            "NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks dt ON dt.id = d.depends_on_id "
            "  WHERE d.task_id = t.id AND dt.request_kind IS DISTINCT FROM 'approval_request' "
            "AND dt.status <> 'done') AS ready "
            "FROM tasks t WHERE t.assignee = %s "
            "AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "AND t.status IN ('todo','in_progress','in_review','blocked','submitted') "
            "ORDER BY CASE WHEN t.status = 'submitted' THEN 0 ELSE 1 END, "
            "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.created_at",
            (who,))
        return {"agent": who, "inbox": cur.fetchall()}


@app.get("/api/task_activity")
def api_task_activity(id: str, authorization: str = Header(None)):
    """Hilo cronologico de eventos de una tarea."""
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        _task_visible_read(cur, id)
        cur.execute("SELECT id::text AS id, agent, kind, from_status, to_status, body, "
                    "created_at::text AS created_at FROM task_activity "
                    "WHERE task_id::text = %s AND EXISTS ("
                    "SELECT 1 FROM tasks t WHERE t.id::text = %s "
                    "AND t.request_kind IS DISTINCT FROM 'approval_request') "
                    "ORDER BY created_at", (id, id))
        return {"id": id, "activity": cur.fetchall()}


# Sólo imágenes raster. SVG es texto activo (scripts/foreignObject), nunca embebido.
EXPORT_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                      ".jpeg": "image/jpeg", ".gif": "image/gif",
                      ".webp": "image/webp"}


@app.get("/api/image_bytes")
def api_image_bytes(path: str, authorization: str = Header(None)):
    """Bytes de imagen para export HTML; autenticación y scope sobre ruta RESUELTA.
    Nunca devuelve SVG, secretos, directorios excluidos ni rutas fuera del vault."""
    principal = _check(authorization)
    if not path:
        raise HTTPException(400, "path requerido")
    _, fp = _safe_path(path, must_be_md=False)
    resolved_rel = str(fp.relative_to(VAULT.resolve()))
    # Do not expose the vault root (or let '.' inherit a root grant).
    if not resolved_rel or resolved_rel == '.':
        raise HTTPException(400, "imagen requerida")
    if set(Path(resolved_rel).parts) & SKIP_DIRS:
        raise HTTPException(403, "ruta no accesible")
    if not path_allowed(principal, resolved_rel):
        raise HTTPException(403, "fuera de tu scope")
    mime = EXPORT_IMAGE_TYPES.get(fp.suffix.lower())
    if not mime:
        raise HTTPException(400, "solo imagenes raster permitidas")
    if not fp.is_file():
        raise HTTPException(404, "imagen no encontrada")
    raw = fp.read_bytes()
    # La extensión no prueba el tipo: no servir SVG/HTML renombrado como .png.
    signatures = {".png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
                  ".jpg": raw.startswith(b"\xff\xd8\xff"),
                  ".jpeg": raw.startswith(b"\xff\xd8\xff"),
                  ".gif": raw.startswith((b"GIF87a", b"GIF89a")),
                  ".webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"}
    if not signatures[fp.suffix.lower()]:
        raise HTTPException(415, "contenido no es una imagen permitida")
    return Response(raw, media_type=mime,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "sinapsis"}


# ==== Jerarquia Organizacion -> Proyecto (cambio de implementación, pedido de admin ) ====
# PROBLEMA QUE RESUELVE: hasta hoy "proyecto" NO era una entidad: se derivaba de
# `SELECT DISTINCT project FROM tasks`, o sea existia solo como efecto secundario de
# haber escrito una tarea. Por eso no se podia crear un proyecto desde la UI ni
# declarar que example-suite pertenece a acme. Ahora hay tablas `organizations` y `projects`
# reales, y `tasks.organization`.
# NADA de DELETE: archivar es status='archived' (Regla de Oro #1).
# Gobierno del tablero => _require_admin, mismo criterio que /admin/*.
_ORG_COLS = ("name", "color", "sort_order", "active")
_PROJ_COLS = ("org_id", "name", "status", "sort_order")


@app.get("/api/organizations")
def api_organizations(authorization: str = Header(None)):
    """Organizaciones con conteo de proyectos y de tareas abiertas."""
    _check(authorization)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT o.id, o.name, o.color, o.sort_order, o.active, "
            "  (SELECT count(*) FROM projects p "
            "    WHERE p.org_id=o.id AND p.status<>'archived') AS projects, "
            "  (SELECT count(*) FROM tasks t WHERE t.organization=o.id "
            "    AND t.request_kind IS DISTINCT FROM 'approval_request') AS tasks_total, "
            "  (SELECT count(*) FROM tasks t WHERE t.organization=o.id "
            "    AND t.request_kind IS DISTINCT FROM 'approval_request' "
            "    AND t.status NOT IN ('done','cancelled')) AS tasks_open "
            "FROM organizations o ORDER BY o.sort_order, o.name")
        return {"organizations": cur.fetchall()}


@app.get("/api/projects")
def api_projects(org: str = "", include_archived: bool = False,
                 authorization: str = Header(None)):
    """Proyectos con su organizacion y conteos. Filtro opcional por org."""
    _check(authorization)
    where, params = [], []
    if org:
        where.append("p.org_id=%s")
        params.append(org)
    if not include_archived:
        where.append("p.status<>'archived'")
    sql = ("SELECT p.id, p.name, p.org_id, o.name AS org_name, p.status, p.sort_order, "
           "  (SELECT count(*) FROM tasks t WHERE t.project=p.id "
           "    AND t.request_kind IS DISTINCT FROM 'approval_request') AS tasks_total, "
           "  (SELECT count(*) FROM tasks t WHERE t.project=p.id "
           "    AND t.request_kind IS DISTINCT FROM 'approval_request' "
           "    AND t.status NOT IN ('done','cancelled')) AS tasks_open "
           "FROM projects p JOIN organizations o ON o.id=p.org_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.sort_order, p.sort_order, p.name"
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return {"projects": cur.fetchall()}


@app.post("/api/project_create")
async def api_project_create(req: Request, authorization: str = Header(None)):
    """Crea un proyecto DENTRO de una organizacion. Idempotente por id."""
    _require_admin(authorization)
    body = await req.json()
    pid = (body.get("id") or body.get("name") or "").strip()
    org_id = (body.get("org_id") or "").strip()
    if not pid or not org_id:
        raise HTTPException(400, "id y org_id son obligatorios")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM organizations WHERE id=%s", (org_id,))
        if not cur.fetchone():
            raise HTTPException(400, f"organizacion '{org_id}' no existe")
        cur.execute(
            "INSERT INTO projects (id, org_id, name, status, sort_order) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING "
            "RETURNING id, org_id, name, status",
            (pid, org_id, (body.get("name") or pid).strip(),
             (body.get("status") or "active").strip(),
             int(body.get("sort_order") or 100)))
        row = cur.fetchone()
        conn.commit()
    return {"project": row, "created": bool(row)}


@app.post("/api/project_update")
async def api_project_update(req: Request, authorization: str = Header(None)):
    """Edita un proyecto. Permite REASIGNARLO a otra organizacion (org_id).
    Al reasignar, arrastra `tasks.organization` de sus tareas para que el dato no
    quede mintiendo respecto de projects.org_id. Es reclasificacion, no borrado."""
    _require_admin(authorization)
    body = await req.json()
    pid = (body.get("id") or "").strip()
    if not pid:
        raise HTTPException(400, "id es obligatorio")
    sets, params = [], []
    for col in _PROJ_COLS:
        if col in body:
            sets.append(f"{col}=%s")
            params.append(body[col])
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    with _pg() as conn, conn.cursor() as cur:
        if "org_id" in body:
            cur.execute("SELECT 1 FROM organizations WHERE id=%s", (body["org_id"],))
            if not cur.fetchone():
                raise HTTPException(400, f"organizacion '{body['org_id']}' no existe")
        params.append(pid)
        cur.execute(f"UPDATE projects SET {', '.join(sets)}, updated_at=now() "
                    f"WHERE id=%s RETURNING id, org_id, name, status, sort_order", params)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"proyecto '{pid}' no existe")
        moved = 0
        if "org_id" in body and body.get("reassign_tasks", True):
            cur.execute("UPDATE tasks SET organization=%s "
                        "WHERE project=%s AND organization IS DISTINCT FROM %s",
                        (body["org_id"], pid, body["org_id"]))
            moved = cur.rowcount
        conn.commit()
    return {"project": row, "tasks_reassigned": moved}


@app.post("/api/organization_create")
async def api_organization_create(req: Request, authorization: str = Header(None)):
    """Crea una organizacion. Idempotente por id."""
    _require_admin(authorization)
    body = await req.json()
    oid = (body.get("id") or "").strip().lower()
    if not oid:
        raise HTTPException(400, "id es obligatorio")
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, color, sort_order) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING "
            "RETURNING id, name, color, sort_order, active",
            (oid, (body.get("name") or oid).strip(),
             (body.get("color") or "").strip(),
             int(body.get("sort_order") or 100)))
        row = cur.fetchone()
        conn.commit()
    return {"organization": row, "created": bool(row)}


@app.post("/api/organization_update")
async def api_organization_update(req: Request, authorization: str = Header(None)):
    """Edita una organizacion. Desactivar es active=false, NUNCA DELETE."""
    _require_admin(authorization)
    body = await req.json()
    oid = (body.get("id") or "").strip()
    if not oid:
        raise HTTPException(400, "id es obligatorio")
    sets, params = [], []
    for col in _ORG_COLS:
        if col in body:
            sets.append(f"{col}=%s")
            params.append(body[col])
    if not sets:
        raise HTTPException(400, "nada que actualizar")
    params.append(oid)
    with _pg() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE organizations SET {', '.join(sets)}, updated_at=now() "
                    f"WHERE id=%s RETURNING id, name, color, sort_order, active", params)
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"organizacion '{oid}' no existe")
        conn.commit()
    return {"organization": row}


# --- Static (SPA React) --- se registra AL FINAL, despues de /login y /api/* ---
# Sirve el build de Vite: /assets/* estatico + fallback a index.html para las
# rutas del client-side router (react-router). Reemplaza el index.html vestigial.
if os.path.isdir(DIST):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(DIST, "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> Response:
        candidate = os.path.join(DIST, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(DIST, "index.html"))
else:
    @app.get("/", response_class=HTMLResponse)
    def _no_dist():
        return "<h1>SINAPSIS</h1><p>Build del frontend no encontrado (DIST_DIR).</p>"
