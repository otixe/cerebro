"""FastAPI Memory Gateway - endpoints para todos los agentes."""
import logging
import os
import time
import json
import hashlib
import secrets
import uuid
import posixpath  # normalizacion de source_path del caller (fix traversal 2026-08-10)
import re as _re   # denylist de escritura cross-team (politica Otixe 2026-08-05)
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request, BackgroundTasks, APIRouter
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from contextlib import asynccontextmanager

# slowapi - rate-limit por agente
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import psycopg
import db
from policy_roots import ROOT_CACHE
from office_privacy import office_denied, deny_prefixes, filter_hits, filter_office_output
import embedder
import chunker
import sanitize  # write-path saneamiento: private-blocks + secret-redaction
import notegraph  # grafo determinista de notas del vault (backlinks/outlinks) - additive
import semanticgraph  # grafo semantico automatico (Gap 1 Cerebro v2): k-NN Qdrant, additive
import agentregistry  # registro declarativo de la flota (frontmatter de los .md) - additive, read-only
import retrievalmetrics  # metricas de calidad de retrieval (Gap A Ola1 Cerebro v2), additive
import consolidation  # consolidacion/olvido 'sueno' (Gap B Ola1): dedup k-NN + decay, additive
import conversations  # capa replayable sobre el bus (Gap C Ola1): thread/list_threads, read-only additive
import skillmining  # skills vivas (Gap ec2269ab Ola2): destila harvests -> propuestas de skill (k>=2), additive
import correspondencia  # bus v2 paralelo; sólo tablas/rutas correspondencia_*
import audit_feed  # feed self-scoped de audit_log; instalación tras verify_agent
import bus_legacy  # FUENTE ÚNICA del cierre de escritura del bus viejo (common/)
import vault_backups
import approval_commands as approval
import agent_config

# --------------------------------------------------------------------------
# API key loading + authentication
# --------------------------------------------------------------------------
def _load_api_keys() -> dict[str, str]:
    """Devuelve dict name -> key (plaintext). Compara con constant_time."""
    keys = {}
    # FASE 4 (2026-08-03, tarea 77212280): retirados PAPERCLIP y GEMINI (nunca tuvieron
    # fila en `agents`, verificado que ya no autentican: HTTP 401) y LURTZ/GRIMA/PALANTIR/
    # CREBAIN (revocados el 2026-07-22). Mientras estuvieran en esta lista, REPONER la
    # variable en el .env los revivia: la lista hardcodeada era la puerta trasera.
    # Los que quedan tienen fila en `agents`, asi que tras el FIX-1 la DB decide la auth
    # y su entrada del .env solo sirve de bootstrap si algun dia pierden la fila.
    # Cerebro publico: la lista de agentes con bootstrap por .env es CONFIGURABLE
    # (BOOTSTRAP_AGENTS, coma-separada). Cada nombre lee <NOMBRE>_API_KEY. Default
    # 'admin' => una instalacion nueva arranca con ADMIN_API_KEY. La DB (`agents`)
    # sigue decidiendo la auth; esto solo sirve de bootstrap.
    names = [a.strip().upper() for a in os.environ.get("BOOTSTRAP_AGENTS", "admin").split(",") if a.strip()]
    for agent in names:
        v = os.environ.get(f"{agent}_API_KEY", "").strip()
        if v and v != "__CAMBIAR__":
            keys[agent.lower()] = v
    return keys

API_KEYS = _load_api_keys()

# Agentes autorizados a ver 'secret' (D10 opción B)
SECRETS_ALLOWED_AGENTS = {
    a.strip().lower() for a in os.environ.get("SECRETS_ALLOWED_AGENTS", "admin").split(",")
    if a.strip()
}

# --------------------------------------------------------------------------
# Politica de credenciales en SKILLS (decision de Otixe, 2026-08-10)
# --------------------------------------------------------------------------
# Una skill es un PROCEDIMIENTO EJECUTABLE: el manual de "como conectarse a X"
# sin la credencial no se puede ejecutar, y el agente que lo carga no puede
# completar la tarea. El vault es infra propia y privada (acceso solo con key de
# la flota, sin superficie a terceros): la flota ES el destinatario legitimo de
# esas credenciales.
#
# Por eso el saneamiento por CONTENIDO (`sanitize.sanitize`) NO se aplica cuando
# el destino es una skill: se persiste/embeddea el texto ORIGINAL y NO se fuerza
# sensitivity='secret'. Sin esto, `db.upsert_chunk` deja text_full=NULL en PG y
# el payload `text` vacio en Qdrant, y como `sensitivity` es por ARCHIVO degrada
# la skill ENTERA (medido: skills/vpn-s2s-fortinet-formularios 569 chunks y
# skills/memory-gateway-rias 228 chunks, todos con text_full=NULL).
#
# ALCANCE ESTRICTO — esta excepcion NO afecta:
#  · el camino por RUTA de SECRETS_METADATA_ONLY ("95 Secrets"): intacto, esas
#    notas se siguen indexando solo con titulo y sensitivity='secret';
#  · el saneamiento de TODO LO DEMAS (notas, informes, dumps, memorias): sigue
#    activo, porque ahi una credencial aparece por DESCUIDO y el gate es correcto.
# El WARNING se CONSERVA en ambos casos: informar que se detecto una credencial
# (familias + cantidad) no cuesta nada y es observabilidad util; lo que se quita
# es la DEGRADACION, no el aviso.
#
# LIMITE DE LA EXCEPCION (C4, 2026-08-10): cubre CREDENCIALES, no bloques
# <private>. Un <private>...</private> es una decision EXPLICITA del autor de
# ocultar algo; una credencial detectada por patron es un hallazgo AUTOMATICO.
# En skills se conserva la credencial pero se sigue aplicando
# `sanitize.redact_private_blocks` -- si no, el primer autor que use <private> en
# una skill queda silenciosamente traicionado.
#
# ALCANCE POR PATH (fix traversal 2026-08-10, hallazgo de Grima): la excepcion se
# decide con `_is_skill_path`, que ahora NORMALIZA el path (posixpath.normpath)
# antes de comparar el prefijo. Antes decidia por prefijo TEXTUAL sin colapsar
# '..', asi que `source_path='skills/../cualquier-cosa'` se llevaba la excepcion
# para un destino arbitrario: el CALLER elegia si el gate de secretos se le
# aplicaba. Ver `_normalize_source_path` / `_source_path_is_safe`.
SKILLS_PREFIX = "skills/"


def _normalize_source_path(source_path: str) -> str:
    """Normaliza un `source_path` que viene del CALLER a su forma canonica POSIX.

    FIX traversal (2026-08-10, hallazgo de Grima): la version anterior decidia por
    PREFIJO TEXTUAL y nunca colapsaba `..`, asi que el caller elegia si el gate de
    secretos se le aplicaba (`skills/../lo-que-sea` obtenia la excepcion de skills).

    Orden DELIBERADO (cada paso arregla un bypass medido):
      1. strip + barras `\\` -> `/`      (' skills\\x' no esquiva el gate)
      2. quitar prefijos `./` con `removeprefix` en LOOP, NUNCA `lstrip("./")`:
         `lstrip` trata "./" como SET DE CARACTERES y se come los tres puntos de
         `...skills/x`, que asi pasaba como skill.
      3. colapsar `//`, `.` y `..` con `posixpath.normpath` -> `skills/../x` == `x`.

    `normpath` sola NO alcanza y no se usa como unica defensa: `normpath('skills/../../etc')`
    devuelve `'../etc'` y `normpath('/skills/x')` devuelve `'/skills/x'`; esos dos casos
    los descarta explicitamente `_is_skill_path` (ver alli).
    Devuelve "" para entrada vacia.
    """
    if not source_path:
        return ""
    p = str(source_path).strip().replace("\\", "/")
    if not p:
        return ""
    # C2: strip de PREFIJO real, no de set de caracteres.
    while p.startswith("./"):
        p = p.removeprefix("./")
    if not p:
        return ""
    # normpath colapsa '.', '..' y '//'. Preserva un '/' inicial y un '..' que
    # escapa hacia afuera: ambos se rechazan aguas arriba, no se "arreglan" aca.
    p = posixpath.normpath(p)
    if p == ".":
        return ""
    return p


def _source_path_is_safe(source_path: str) -> bool:
    """True si `source_path` normaliza DENTRO del vault (path relativo, sin escapes).

    Rechaza: vacio, absoluto ('/x'), y todo lo que quede colgado de '..' despues de
    normalizar (escape del vault). Es el predicado de la guarda HTTP 400 (C3).
    Medido antes de aplicar: 0 source_path VIVOS en chunks_meta contienen '..'
    (solo los 2 canarios soft-deleted de Grima), asi que la guarda no rompe
    ningun flujo vivo.
    """
    raw = str(source_path or "")
    if not raw.strip():
        return False
    # Rechazo TEXTUAL de '..' ademas del estructural: `a/../a` normaliza a `a`
    # (inocuo) pero seguir aceptandolo mantiene vivas DOS nociones de path
    # (textual y resuelta), que es exactamente la clase de bug que se cierra aca.
    if ".." in raw.replace("\\", "/").split("/"):
        return False
    norm = _normalize_source_path(raw)
    if not norm:
        return False
    if norm.startswith("/") or norm.startswith("../") or norm == "..":
        return False
    return True


def _require_safe_source_path(source_path: str) -> None:
    """Guarda C3 para endpoints de ESCRITURA que reciben `source_path` del caller.
    HTTP 400 si contiene '..' o no normaliza dentro del vault."""
    if not _source_path_is_safe(source_path):
        raise HTTPException(
            400,
            "source_path invalido: no puede contener '..' ni ser absoluto; "
            "debe ser una ruta relativa que normalice dentro del vault",
        )


def _is_skill_path(source_path: str) -> bool:
    """True si `source_path` apunta a una skill del registro compartido.

    Los chunks de skill viven bajo 'skills/<nombre>' (y sus references bajo
    'skills/<nombre>/references/<file>'), asi que basta el prefijo -- pero sobre el
    path NORMALIZADO, nunca sobre el texto crudo del caller.

    Decisiones declaradas (fail-closed: ante la duda NO es skill, y entonces SI se
    sanea; equivocarse hacia este lado nunca abre un bypass):
      · '/skills/x'  -> False. Un source_path absoluto no es una ruta del vault;
        db.upsert_chunk guarda paths relativos ('skills/x'), asi que '/skills/x' es
        un destino DISTINTO y no debe heredar la excepcion.
      · 'Skills/x', 'SKILLS/x' -> False. Comparacion CASE-SENSITIVE: los paths de PG
        son case-sensitive y db.normalize_skill_name emite minusculas, asi que una
        skill real nunca llega con mayusculas.
      · 'skills/../x' y cualquier cosa que escape -> False (el bypass que se cierra).
    """
    p = _normalize_source_path(source_path)
    if not p:
        return False
    # C1: un resultado absoluto o que escapa hacia afuera NO puede ser skill.
    if p.startswith("/") or p == ".." or p.startswith("../"):
        return False
    return p == SKILLS_PREFIX.rstrip("/") or p.startswith(SKILLS_PREFIX)


# Rate-limits configurables
RATE_LIMIT_SEARCH = os.environ.get("RATE_LIMIT_SEARCH", "60/minute")
# GET /vault/read only: proposed per-agent 120/minute ceiling, not measured safe gateway throughput.
RATE_LIMIT_VAULT_READ = os.environ.get("RATE_LIMIT_VAULT_READ", "120/minute")
RATE_LIMIT_ADD    = os.environ.get("RATE_LIMIT_ADD", "120/minute")
RATE_LIMIT_FORGET = os.environ.get("RATE_LIMIT_FORGET", "10/minute")
# H4 (auditoria 2026-07-28): provisioning/revoke de agentes. Estricto: una key de
# lider filtrada no debe poder generar subagentes en masa.
RATE_LIMIT_PROVISION = os.environ.get("RATE_LIMIT_PROVISION", "5/minute")


_auth_log = logging.getLogger("cerebro.auth")


def _ctime_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a.encode(), b.encode()):
        r |= x ^ y
    return r == 0


def _agent_from_key(raw_key: Optional[str]) -> Optional[str]:
    """Resuelve la identidad a partir de la API key.

    FIX AGUJERO DE REVOCACION (2026-08-03, tarea 7b829dfa). Antes, la via `.env`
    se probaba PRIMERO y hacia `return` ahi mismo, sin consultar nunca la DB.
    Como el `.env` no mira `active`, un agente REVOCADO cuyo token seguia en el
    archivo continuaba autenticando indefinidamente: lurtz/grima/palantir/crebain
    estuvieron 12 dias leyendo el Cerebro despues de revocados (medido).

    Orden nuevo: la tabla `agents` es la FUENTE DE VERDAD. El `.env` queda como
    bootstrap acotado a agentes que NO tienen fila todavia (primer arranque).
    """
    if not raw_key:
        return None

    # 1) FUENTE DE VERDAD: fila ACTIVE de `agents` con sha256(raw_key) == api_key_hash.
    #    Respeta `active`, asi que la revocacion es efectiva y en caliente.
    try:
        name = db.agent_by_key_hash(db.key_sha256(raw_key))
    except Exception:
        name = None
    if name:
        return name

    # 2) BOOTSTRAP: keys del `.env` (constant-time). Solo valen si el agente NO
    #    tiene fila en `agents`. Si tiene fila, la DB ya decidio en el paso 1:
    #    que este en el `.env` no puede resucitar una key revocada.
    for env_name, key in API_KEYS.items():
        if _ctime_eq(raw_key, key):
            if db.agent_exists(env_name):
                _auth_log.warning(
                    "AUTH deny (.env): agent=%r tiene fila en `agents` y su key NO "
                    "coincide con api_key_hash activo -> revocada o rotada. "
                    "Purgar la entrada del .env (FASE 4).", env_name)
                return None
            _auth_log.warning(
                "AUTH bootstrap (.env): agent=%r autentico por la via .env porque NO "
                "tiene fila en `agents`. Deberia provisionarse en la tabla.", env_name)
            return env_name
    return None


# --------------------------------------------------------------------------
# AUTHZ T1 (2026-08-27): DELEGACION DE IDENTIDAD HUMANA
# --------------------------------------------------------------------------
# EL DEFECTO QUE CIERRA: el BFF (Sinapsis) autentica al humano por sesion y despues
# habla con el gateway con la credencial de `saruman` => la identidad del humano NO
# llegaba, y sus lecturas quedaban atribuidas a saruman en audit_log.
#
# EL MECANISMO: Sinapsis emite un header con el `name` del principal HUMANO que ya
# resolvio de la sesion (nunca del body, nunca de query param -- mismo precedente que
# _bff_actor / fix H3). El gateway lo acepta SOLO si el request viene autenticado con
# la credencial del EMISOR CONFIABLE.
#
# 🔴 POR QUE LA CONDICION DEL EMISOR ES LA PARTE CRITICA: un header de identidad que el
# gateway cree sin verificar de quien viene NO es una mejora de atribucion, es una
# SUPLANTACION ABIERTA (cualquier subagente con credencial propia podria leer como
# `otixe`). Por eso: credencial != emisor confiable => el header se IGNORA.
#
# SE IGNORA, NO SE RECHAZA: un header invalido no rompe el request; degrada al owner de
# la credencial (fail-closed en identidad, fail-open en disponibilidad). Un rechazo
# convertiria un error de emision en una caida del panel.
DELEGATION_HEADER = "X-Cerebro-On-Behalf-Of"

# Emisor confiable: el UNICO principal cuya credencial habilita la delegacion.
# `sinapsis` = id 78, kind='agent', role='member', team='saruman' (medido en Postgres).
DELEGATION_TRUSTED_EMITTER = os.environ.get(
    "DELEGATION_TRUSTED_EMITTER", "sinapsis").strip().lower()

# Cota de longitud del valor: `agents.name` es text sin limite, pero el valor viaja a
# audit_log.agent y a la key del rate-limiter. 64 es holgado (el nombre mas largo del
# padron es de 12 chars) y acota el blast radius de un header hostil.
DELEGATION_MAX_LEN = 64

_deleg_log = logging.getLogger("cerebro.delegation")


def _resolve_delegation(request: Request, owner: str):
    """Resuelve el principal EFECTIVO de un request ya autenticado.

    Devuelve `(agent_efectivo, rastro | None)`.
      - `agent_efectivo`: el delegado si la delegacion es valida; si no, `owner`.
      - `rastro`: 'on_behalf_of=<delegado> via=<emisor>' para dejar la delegacion
        registrada en audit_log.query. ⚠️ `audit_log` NO tiene columna de via
        (medido: id, ts, agent, action, chunk_id, query, result_count, duration_ms,
        success, error) => el rastro va en `query`. CERO DDL.

    Las CINCO condiciones de aceptacion (todas AND, todas fail-closed al `owner`):
      1. el header viene y no esta vacio/whitespace;
      2. el owner de la credencial ES el emisor confiable;
      3. el valor es sano (longitud acotada, sin CR/LF -- anti header-injection en logs);
      4. el delegado EXISTE y esta ACTIVO en `agents` (db.agent_row filtra active=true);
      5. el delegado es kind='human' (un agente NO se delega: para eso tiene credencial).
    """
    raw = request.headers.get(DELEGATION_HEADER)
    if not raw or not raw.strip():
        return owner, None                      # (1) sin header => comportamiento historico

    delegate = raw.strip()
    owner_l = (owner or "").strip().lower()

    # (2) 🔴 EL CONTROL ANTI-SUPLANTACION. Va PRIMERO despues de la presencia del header:
    # si la credencial no es la del emisor confiable, no se consulta nada mas (ni la DB)
    # y el header se descarta. Se loguea porque un header de delegacion llegando con otra
    # credencial es, o un bug de un cliente, o un intento de suplantacion.
    if owner_l != DELEGATION_TRUSTED_EMITTER:
        _deleg_log.warning(
            "DELEGACION IGNORADA: header presente pero la credencial es de %r, no del "
            "emisor confiable %r. El principal efectivo queda %r.",
            owner, DELEGATION_TRUSTED_EMITTER, owner)
        return owner, None

    # (3) sanidad del valor
    if len(delegate) > DELEGATION_MAX_LEN or "\r" in delegate or "\n" in delegate:
        _deleg_log.warning(
            "DELEGACION IGNORADA: valor no sano (len=%d). Efectivo=%r.",
            len(delegate), owner)
        return owner, None

    # (4)+(5) el delegado tiene que existir, estar activo y ser humano.
    try:
        row = db.agent_row(delegate)
    except Exception:
        # No se pudo resolver => fail-closed al owner. Degradacion RUIDOSA (queda el log
        # y la atribucion al owner), nunca aceptar un nombre sin verificar.
        _deleg_log.error(
            "DELEGACION IGNORADA: no se pudo resolver la fila de %r. Efectivo=%r.",
            delegate, owner)
        return owner, None
    if not row:
        _deleg_log.warning(
            "DELEGACION IGNORADA: %r no existe o esta inactivo en `agents`. Efectivo=%r.",
            delegate, owner)
        return owner, None
    if (row.get("kind") or "").strip().lower() != "human":
        _deleg_log.warning(
            "DELEGACION IGNORADA: %r tiene kind=%r (se exige 'human'). Efectivo=%r.",
            delegate, row.get("kind"), owner)
        return owner, None

    # Se devuelve el nombre CANONICO de la DB cuando esta disponible: `agent_row` matchea
    # case-insensitive, y sin esto 'OTIXE' y 'otixe' serian dos agentes distintos en
    # audit_log y dos buckets distintos en el rate-limiter.
    canonical = (row.get("name") or delegate) if isinstance(row, dict) else delegate
    return canonical, f"on_behalf_of={canonical} via={owner}"


async def verify_agent(
    request: Request,
    x_merlin_key: Optional[str] = Header(None, alias="X-Merlin-Key"),
) -> str:
    if not x_merlin_key:
        raise HTTPException(401, "missing X-Merlin-Key header")
    name = _agent_from_key(x_merlin_key)
    if not name:
        raise HTTPException(401, "invalid api key")
    # AUTHZ T1: delegacion de identidad humana. Se resuelve ACA porque este es el
    # UNICO punto por el que pasa toda la auth del gateway: el middleware de auditoria
    # y el rate-limiter leen `request.state.agent`, asi que la atribucion correcta se
    # propaga sola a audit_log y al limiter SIN tocarlos.
    effective, deleg = _resolve_delegation(request, name)
    request.state.delegation = deleg          # None si no hubo delegacion valida
    request.state.credential_owner = name     # el owner REAL de la credencial

    # Expone el agente al middleware de audit / rate-limit
    request.state.agent = effective
    return effective



# --------------------------------------------------------------------------
# Rate limiter (slowapi, in-memory; key = nombre del agente)
# --------------------------------------------------------------------------
def _agent_limit_key(request: Request) -> str:
    # Preferir state.agent (setteado por verify_agent); fallback al header;
    # último recurso la IP (no debería ocurrir si auth pasó).
    ag = getattr(request.state, "agent", None)
    if ag:
        return ag
    raw = request.headers.get("X-Merlin-Key")
    name = _agent_from_key(raw)
    if name:
        return name
    return get_remote_address(request)


def resolve_agent_scope(name: Optional[str]):
    """Authz Fase 3b: resuelve el scope de acceso de un agente.
    Devuelve:
      - None  => acceso total (role=admin o agente de sistema sin fila). Sin filtro.
      - {'read': [prefijos], 'write': [prefijos]}  => member: acotado a esos prefijos.

    SELF-SCOPE (FIX 4, 2026-08-04): un member ve SIEMPRE sus namespaces propios:
      · '50 Agents/<name>/'          (documentos del agente)
      · 'memoria/<name>/'            (cosecha, formato viejo)
      · 'memoria/<team>-<name>/'     (cosecha, convencion VIGENTE del peerage)
    El tercero es imprescindible: la convencion de harvest-before-death que piden
    los SOUL escribe en 'memoria/<team>-<name>/...', que NO cae bajo
    '50 Agents/<name>/'. Antes de este fix, acotar el grant de un subagente le
    MATABA la cosecha (medido empiricamente por ophis en la tarea 350e2a6f:
    'memoria/rias-asia/proc-x' pasaba de write=OK a write=DENY). Por eso este fix
    es PREREQUISITO TECNICO de cualquier acotamiento de los 39 scope_grants.

    Fail-closed: member sin grants queda con solo su self-scope. El scope NUNCA
    excede al del owner (salvo el self-scope, que es inalienable)."""
    if not name:
        return None
    try:
        row = db.agent_row(name)  # {role, owner_principal_id, id, active, team} o None
    except Exception:
        return None
    if row is None:
        # FIX 5 (2026-08-04): distinguir DOS casos que antes colapsaban en fail-open.
        # `agent_row` devuelve None tanto para 'no existe la fila' como para
        # 'la fila existe pero active=false' (su WHERE filtra active=true), asi que
        # un agente REVOCADO obtenia scope TOTAL del vault. Ahora se consulta si la
        # fila existe inactiva y, en ese caso, se falla CERRADO.
        try:
            if db.agent_row_any(name) is not None:
                # Fila presente pero inactiva => key revocada. Scope VACIO: no puede
                # leer ni escribir nada. Fail-CLOSED, no fail-open.
                logging.getLogger("cerebro.scope").warning(
                    "scope: agente '%s' existe pero esta INACTIVO -> scope vacio (fail-closed)",
                    name)
                return {"read": [], "write": []}
        except Exception:
            # Si no se puede determinar, se mantiene el comportamiento historico
            # (fail-open) en vez de dejar sin acceso a un agente legitimo: la via
            # de AUTH ya rechaza la key revocada con 401 antes de llegar aca.
            pass
        return None  # agente de .env sin fila en agents = sistema, acceso total
    if (row.get("role") or "") == "admin":
        return None  # admin = sin filtro
    # member: self-scope + sus grants, acotado por el owner si tiene
    grants = db.scope_grants_of(row["id"])  # [{path_prefix, can_write}]
    read = {g["path_prefix"] for g in grants}
    write = {g["path_prefix"] for g in grants if g.get("can_write")}
    # Namespaces propios INALIENABLES (no los poda el acotamiento del owner).
    self_scopes = {f"50 Agents/{name}/", f"memoria/{name}/"}
    _team = (row.get("team") or "").strip()
    if _team and not name.startswith(f"{_team}-"):
        # Subagente cuyo nombre NO lleva el prefijo del equipo (ej. team='rias',
        # name='asia' => 'memoria/rias-asia/'). Si el nombre YA lo lleva (ej.
        # 'saruman-lurtz'), 'memoria/<name>/' de arriba ya lo cubre.
        self_scopes.add(f"memoria/{_team}-{name}/")
    read |= self_scopes
    write |= self_scopes
    owner_id = row.get("owner_principal_id")
    if owner_id:
        try:
            og = db.scope_grants_of(owner_id)
            owner_prefixes = {g["path_prefix"] for g in og}
            if owner_prefixes:
                # acotar: un prefijo del member es valido si cae bajo alguno del owner
                def _covered(p):
                    return any(p == op or p.startswith(op) for op in owner_prefixes)
                read = {p for p in read if p in self_scopes or _covered(p)}
                write = {p for p in write if p in self_scopes or _covered(p)}
        except Exception:
            pass
    return {"read": sorted(read), "write": sorted(write)}


def _strip_redundant_vault_prefix(rel: str) -> tuple[str, bool]:
    """Quita, de forma ITERATIVA, el prefijo redundante 'obsidian-vault/' (y los
    './' y '/' iniciales) de un path RELATIVO al vault. Es la UNICA verdad de
    normalizacion de paths del vault (la usan _norm_vault_path para el scope y
    _resolve_within_vault para la resolucion en FS).
    Devuelve (rel_normalizado, hubo_cambio)."""
    original = rel
    rel = (rel or "").replace("\\", "/")
    while True:
        p = rel.lstrip("/")
        if p.startswith("./"):
            rel = p[2:]
            continue
        if p == "obsidian-vault" or p.startswith("obsidian-vault/"):
            rel = p[len("obsidian-vault"):].lstrip("/")
            continue
        rel = p
        break
    return rel, (rel != original)


def _norm_vault_path(path: str) -> str:
    """Normaliza un path/source_path a relativo al vault (sin 'obsidian-vault/' ni '/' inicial),
    para comparar 1:1 contra los path_prefix de scope_grants.
    Delega en _strip_redundant_vault_prefix (una sola verdad de normalizacion)."""
    norm, _changed = _strip_redundant_vault_prefix(path or "")
    return norm


# Distingue una carpeta compartida sana (None) de un fallo de infraestructura.
# No usar None para ambos: hacerlo convertia una excepcion de DB en permiso de
# escritura cross-team.
_TEAM_LOOKUP_FAILED = object()


def _team_of_agent_folder(folder: str):
    """Equipo dueno de una carpeta '50 Agents/<Folder>/'.

    Las carpetas de `50 Agents/` son por AGENTE (Akeno, Ophis, Forge, Lurtz...), no
    por equipo, asi que el equipo NO se puede derivar del nombre: se resuelve
    consultando la tabla `agents` (match case-insensitive, tolerando espacios y
    guiones: la carpeta 'Le Fay' es el agente 'lefay'). Devuelve ``None`` solo si
    la carpeta no corresponde a un agente conocido (carpeta compartida); devuelve
    el sentinel privado si la consulta no se pudo completar.
    """
    key = _re.sub(r'[^a-z0-9]', '', (folder or '').lower())
    if not key:
        return None
    try:
        return db.team_by_agent_key(key)
    except Exception:
        return _TEAM_LOOKUP_FAILED


def _cross_team_write_denied(agent: str, norm_path: str) -> bool:
    """POLITICA DE OTIXE (2026-08-05) - unica restriccion de ESCRITURA del vault:

      "solo los lideres de equipo puedan modificar los vault de otros equipos:
       solo tu podrias modificar algo del equipo de merlin y viceversa, pero no
       que un agente de los tuyos pueda modificar a rias. Y los demas no bloquear
       permisos para el vault en general, ya que muchas veces necesitan agregar,
       editar o mover contenido."

    Es una DENYLIST, no una allowlist: todo el vault queda escribible salvo la
    carpeta personal de un agente de OTRO equipo, que solo un LIDER puede tocar.
    Un subagente conserva su propia carpeta (mismo equipo) sin restriccion.

    Devuelve True si hay que DENEGAR."""
    m = _re.match(r'^50 Agents/([^/]+)/', norm_path)
    if not m:
        return False          # no es carpeta de agente => sin restriccion
    owner_team = _team_of_agent_folder(m.group(1))
    if owner_team is _TEAM_LOOKUP_FAILED:
        # No se puede distinguir una carpeta personal de una compartida. Para no
        # abrir una escritura cross-team durante una caida de DB, se degrada de
        # forma controlada antes de backup/index/upsert, sin revelar el path.
        raise HTTPException(503, "vault authorization temporarily unavailable")
    if not owner_team:
        return False          # carpeta compartida (Harness/, Templates/...) => libre
    try:
        row = db.agent_row(agent)
    except Exception:
        # Ya sabemos que el destino es personal: no permitir que un error al
        # resolver al llamador convierta esa denylist en permiso.
        raise HTTPException(503, "vault authorization temporarily unavailable")
    if not row:
        return False          # agente de sistema/.env => sin restriccion
    if (row.get("role") or "") == "admin":
        return False          # conserva la excepcion historica de admin sin scope
    if (row.get("team") or "") == owner_team:
        return False          # su propio equipo => libre
    # Carpeta de OTRO equipo: solo los lideres pueden.
    try:
        if db.is_lead(agent):
            return False
    except Exception:
        return True           # si no se puede confirmar que es lider => denegar
    return True


def _path_in_scope(agent: str, path: str, write: bool = False) -> bool:
    """Authz Fase 3b: True si el agente puede acceder al path.

    LECTURA: abierta a todo el vault (politica de Otixe 2026-08-05: "primero que
    todos los agentes puedan leer todo"). El Cerebro sirve porque es compartido.

    ESCRITURA: abierta al vault general, con UNA sola restriccion (denylist): la
    carpeta '50 Agents/<agente>/' de un agente de OTRO equipo solo la puede
    modificar un LIDER. Ver `_cross_team_write_denied`.

    Se conserva el chequeo de escape del vault con realpath: sin el,
    '<prefijo>/../../Otro/x.md' pasaba el startswith textual mientras el FS
    escribia fuera de la base."""
    base = _os.path.realpath(VAULT_WRITE_BASE)
    stripped, _changed = _strip_redundant_vault_prefix(path)
    real = _os.path.realpath(_os.path.join(base, stripped))
    if real != base and not real.startswith(base + _os.sep):
        return False          # escapa del vault => denegar SIEMPRE
    norm = real[len(base):].lstrip(_os.sep)
    if office_denied(agent, norm):
        return False

    # Evaluar primero la denylist de carpeta personal. ``resolve_agent_scope``
    # conserva ``None`` para agentes de sistema y para su compatibilidad histórica
    # ante error; no debe eclipsar el fallo tipado de esta barrera de escritura.
    if write and _cross_team_write_denied(agent, norm):
        return False

    scope = resolve_agent_scope(agent)
    if scope is None:
        return True           # admin/sistema, sin restriccion

    # Agente REVOCADO (FIX 5): scope vacio explicito => no lee ni escribe nada.
    if not scope["read"] and not scope["write"]:
        return False

    if not write:
        return True           # LECTURA ABIERTA a todo el vault

    return True


limiter = Limiter(key_func=_agent_limit_key, headers_enabled=False)


async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    # Respuesta 429 con Retry-After (slowapi lo agrega automáticamente vía headers_enabled)
    resp = JSONResponse(
        status_code=429,
        content={"error": "rate_limit_exceeded", "detail": str(exc.detail)},
    )
    # Asegura Retry-After si slowapi no lo infla
    if "Retry-After" not in resp.headers:
        resp.headers["Retry-After"] = "60"
    return resp


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.ensure_collection()
    ROOT_CACHE.start() # readonly metadata refresh, immediate + periodic <=5min
    try:
        yield
    finally:
        ROOT_CACHE.stop()


app = FastAPI(title="Merlin Memory Gateway", version="0.2.0", lifespan=lifespan)


_TASK_UPDATE_EXTRA_ERROR = "Parametros no permitidos para task_update."


async def _task_update_validation_error(request: Request, exc: RequestValidationError):
    """No reflejar claves ni valores extras de la frontera task_update."""
    if request.url.path == "/task/create":
        # Pydantic's before-validator retains the input dict in the error; only
        # return offending KEY NAMES, never input values or ValueError.context.
        for error in exc.errors():
            if error.get("type") == "value_error" and "approval_fields_not_allowed" in str(error.get("ctx", {}).get("error", "")):
                offenders = _approval_create_offenders(error.get("input"))
                return JSONResponse(status_code=422, content={
                    "detail": {"code": "approval_fields_not_allowed", "keys": offenders}})
        return await request_validation_exception_handler(request, exc)
    if request.url.path != "/task/update":
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    if any(error.get("type") == "extra_forbidden" for error in exc.errors()):
        return JSONResponse(status_code=422, content={"detail": _TASK_UPDATE_EXTRA_ERROR})
    # Los errores de campos conocidos conservan su localizacion/tipo, pero nunca
    # devuelven el valor que Pydantic recibio.
    errors = [{key: value for key, value in error.items() if key != "input"}
              for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


app.add_exception_handler(RequestValidationError, _task_update_validation_error)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Ruta aditiva instalada tras construir `app` y definir la dependencia real,
# que resuelve autenticación y delegación humana al principal efectivo.
audit_feed.install_routes(app, verify_agent)


# --------------------------------------------------------------------------
# Audit middleware universal
# --------------------------------------------------------------------------
# Deriva "action" del path si el handler no lo setteó
def _route_action(request, path: str) -> str:
    """FIX E5 D1 (2026-08-03): normaliza la `action` del audit_log.
    El fallback anterior era `path.strip("/")`, que metia paths CRUDOS con UUID
    adentro ('tasks/f41734b3', 'task/859fda68/review', 'memory/chunk/d6fece81-...').
    Resultado: 260 valores distintos de `action`, imposible agrupar por endpoint.
    Se usa el TEMPLATE de la ruta de FastAPI ('/task/{task_id}'), que es estable.
    NO se reescriben las filas historicas (Regla de Oro 1): solo las nuevas.
    """
    try:
        r = request.scope.get("route")
        tpl = getattr(r, "path", None)
        if tpl:
            return tpl.strip("/").replace("/", "_") or "root"
    except Exception:
        pass
    return path.strip("/")


_PATH_ACTION = {
    "/memory/search": "search",
    "/memory/add": "add",
    "/memory/add-markdown": "add-markdown",
    "/memory/forget": "forget",
    "/memory/supersede": "supersede",
    "/memory/by-source": "by-source",
    "/vault/write": "vault_write",
    "/stats": "stats",
    "/agents/provision": "agent_provision",
    "/agents/provision-leader": "agent_provision_leader",
    "/agents/revoke": "agent_revoke",
    "/agents/list": "agent_list",
    "/checkpoint/agent-save": "agent_checkpoint_save",
    "/checkpoint/agent-latest": "agent_checkpoint_latest",
    "/checkpoint/agent-liveness": "agent_liveness_query",
    "/skills/list": "skill_list",
    # RBAC de tareas FASE A (2026-07-28): cada handler /task* ya setea
    # request.state.audit["action"], esto es el fallback si revienta antes.
    "/task/create": "task_create",
    "/task/list": "task_list",
    "/task/get": "task_get",
    "/task/update": "task_update",
    "/task/claim": "task_claim",
    "/task/comment": "task_comment",
    "/task/submit": "task_submit",
    "/task/review": "task_review",
    "/task/handoff": "task_handoff",
    "/task/block": "task_block",
    "/task/unblock": "task_unblock",
    "/task/cancel": "task_cancel",
    "/task/link": "task_link",
    "/task/unlink": "task_unlink",
    "/task/deps": "task_deps",
    "/task/activity": "task_activity",
    "/tasks/ready": "tasks_ready",
    "/tasks/inbox": "tasks_inbox",
    "/checkpoint/save": "checkpoint_save",
    "/checkpoint/list": "checkpoint_list",
    "/checkpoint/latest": "checkpoint_latest",
    "/project/summary": "project_summary",
}


def _audit_query_with_delegation(request: Request, query):
    """AUTHZ T1: concatena el rastro de delegacion al `query` que se persiste.

    ⚠️ `audit_log` NO tiene columna de via (medido). El rastro va en `query` y CERO DDL.
    Permite distinguir 'lo hizo otixe por el panel' de 'lo hizo otixe con credencial
    propia', que con solo `agent='otixe'` seria indistinguible.
    Si no hubo delegacion, devuelve `query` TAL CUAL (cero cambio de forma)."""
    deleg = getattr(request.state, "delegation", None)
    if not deleg:
        return query
    if query is None or query == "":
        return f"[{deleg}]"
    return f"{query} [{deleg}]"


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    t0 = time.time()
    path = request.url.path
    # No auditamos /health (ruido) ni /docs / /openapi / /favicon etc.
    skip_audit = path in ("/health",) or path.startswith("/docs") \
        or path.startswith("/openapi") or path.startswith("/redoc") \
        or path == "/favicon.ico"

    try:
        response = await call_next(request)
    except Exception as e:
        # Logueamos el fallo antes de reventar
        if not skip_audit:
            agent = getattr(request.state, "agent", None) \
                or _agent_from_key(request.headers.get("X-Merlin-Key")) \
                or "anonymous"
            ctx = getattr(request.state, "audit", {}) or {}
            db.log_audit(
                agent, ctx.get("action") or _PATH_ACTION.get(path, path.strip("/")),
                chunk_id=ctx.get("chunk_id"),
                query=_audit_query_with_delegation(request, ctx.get("query")),
                result_count=ctx.get("result_count"),
                duration_ms=int((time.time() - t0) * 1000),
                success=False,
                error=str(e)[:500],
            )
        raise

    if skip_audit:
        return response

    duration_ms = int((time.time() - t0) * 1000)
    # ctx lo setea cada endpoint (query, result_count, chunk_id, action override)
    ctx = getattr(request.state, "audit", {}) or {}
    agent = getattr(request.state, "agent", None) \
        or _agent_from_key(request.headers.get("X-Merlin-Key")) \
        or "anonymous"
    action = ctx.get("action") or _PATH_ACTION.get(path) or _route_action(request, path)
    success = 200 <= response.status_code < 400
    # FIX E5 D3 (2026-08-03): registrar SIEMPRE la causa del fallo. Antes solo se
    # persistia ctx["error"], que un HTTPException lanzado por el handler NUNCA setea:
    # 22 de 32 busquedas rotas quedaron con error NULL, o sea ciegos justo en el caso
    # que importa. Ahora, si no hay causa declarada, se anota el status HTTP.
    err = ctx.get("error") if not success else None
    if not success and not err:
        err = f"HTTP {response.status_code}"

    # Escritura en audit_log — protegida contra fallos
    try:
        db.log_audit(
            agent, action,
            chunk_id=ctx.get("chunk_id"),
            query=_audit_query_with_delegation(request, ctx.get("query")),
            result_count=ctx.get("result_count"),
            duration_ms=duration_ms,
            success=success,
            error=err,
        )
    except Exception:
        pass

    return response


# Approval auth boundary: never use the effective (possibly delegated) actor.
async def _approval_principal(request: Request):
    if not approval.approval_enabled():
        raise HTTPException(503, 'approval_disabled')
    if DELEGATION_HEADER.lower() in request.headers:
        # Authenticate even rejected delegated attempts; no approval/task SQL.
        await verify_agent(request, request.headers.get('X-' + 'Merlin-Key'))
        raise HTTPException(403, 'delegation_not_accepted')
    cached = getattr(request.state, '_approval_verified', None)
    if cached is not None:
        return cached
    await verify_agent(request, request.headers.get('X-' + 'Merlin-Key'))
    owner = getattr(request.state, 'credential_owner', None)
    if getattr(request.state, 'delegation', None) is not None or owner == DELEGATION_TRUSTED_EMITTER:
        raise HTTPException(403, 'delegation_not_accepted')
    if not owner:
        raise HTTPException(401, 'invalid api key')
    request.state._approval_verified = approval.trusted_principal(owner, 'agent', 'rest')
    return request.state._approval_verified


class _ApprovalRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def guarded(request: Request):
            if not approval.approval_enabled():
                return JSONResponse(status_code=503, content={'detail': 'approval_disabled'})
            # Authentication/authority precedes FastAPI body and query parsing.
            await _approval_principal(request)
            return await handler(request)
        return guarded

_approval_router = APIRouter(prefix='/approval', route_class=_ApprovalRoute)


# ---------- Models ----------
# Categorías válidas para el campo 'category' de las memorias (fase 1 failure-tagging).
# default = 'general' preserva el comportamiento previo a este campo.
VALID_CATEGORIES = {
    "failure", "correction", "insight", "preference",
    "convention", "tool-quirk", "general",
}

class AddRequest(BaseModel):
    text: str
    source_path: str = Field(..., min_length=1)
    title: Optional[str] = None
    tags: list[str] = []
    sensitivity: str = "normal"
    category: str = "general"
    agent_owner: Optional[str] = None  # override (default: caller agent)
    chunk_index: int = 0

class AddResponse(BaseModel):
    chunk_id: str
    dims: int
    warnings: list[str] = []

class AddMarkdownRequest(BaseModel):
    markdown: str
    source_path: str
    title: Optional[str] = None
    tags: list[str] = []
    sensitivity: str = "normal"
    agent_owner: Optional[str] = None

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    tags: Optional[list[str]] = None
    agent_owner: Optional[str] = None
    category: Optional[str] = None
    sensitivity: Optional[list[str]] = None  # ej: ["normal", "pii"]
    source_prefix: Optional[str] = None
    include_secrets: bool = False  # D10 opción B: default excluye secrets
    include_superseded: bool = False  # Sprint 1: default solo memorias vigentes
    hybrid: Optional[bool] = None  # Sprint 2: None=default env; True=vector+BM25 RRF; False=solo vector
    min_score: Optional[float] = None  # Sprint 10 (O1): umbral sobre score COSINE; None=env MIN_SCORE_DEFAULT
    include_archived: bool = False  # Sprint 10: True incluye notas archivadas (/Historico//Archive/...) desde el inicio
    include_sessions: bool = False  # False excluye Sesiones; True selecciona SOLO Sesiones dentro del scope
    include_logs: bool = False  # True agrega logs narrativos aprobados, sin relajar scope/ACL ni selección Sesiones
    min_chunk_len: Optional[int] = None  # Sprint 10 (O6): piso de longitud text; None=env MIN_CHUNK_LEN

class ForgetRequest(BaseModel):
    mode: str = "soft"  # soft | hard
    chunk_id: Optional[str] = None
    source_path: Optional[str] = None
    tags: Optional[list[str]] = None

class SupersedeRequest(BaseModel):
    # Sprint 1: marca la memoria vieja como reemplazada por la nueva (sin borrar).
    # Cada lado acepta un chunk_id (uuid) o un source_path.
    old: str = Field(..., min_length=1)
    new: str = Field(..., min_length=1)


# Gap 2a Cerebro v2 (2026-07-20): harvest libre. 'agent' NUNCA viene del body
# (anti-spoof); se deriva de la key en verify_agent.
AGENT_CKPT_KINDS = {"progress", "harvest", "pitfall", "landmark"}
AGENT_CKPT_HARVEST_KINDS = {"harvest", "pitfall", "landmark"}

class AgentCheckpointRequest(BaseModel):
    session_ref: str = ""
    step_index: int = 0
    label: str = ""
    kind: str = "progress"
    state: dict = Field(default_factory=dict)


# ---------- Routes ----------
@app.get("/health")
def health():
    inventory = ROOT_CACHE.detail()
    detail = {"collection": db.COLLECTION, "dims": db.DIMS,
              "candidate_policy_inventory": inventory}
    try:
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            if cur.fetchone()["?column?"] != 1:
                raise RuntimeError("database health probe failed")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", **detail})
    if inventory['status'] != 'ok':
        return JSONResponse(status_code=503, content={"status": "degraded", **detail})
    return {"status": "ok", **detail}


@app.get("/health/reachability")
def health_reachability(request: Request, window_mins: int = 30):
    """D3 de la tarea 6ee3d3ea: distinguir "EL SERVICIO ESTA VIVO" de "MIS CLIENTES ME
    ESTAN ALCANZANDO". Son preguntas distintas y /health solo responde la primera.

    POR QUE EXISTE: /health hace un db.stats() y devuelve 200 si Postgres contesta. Eso
    es un FALSO VERDE en la capa de observabilidad -- @rudeus y @merlin midieron dos casos
    el 2026-08-03 donde /health daba 200 en 0,18s mientras un cliente estaba MUDO por MCP.
    Cualquier monitoreo basado en /health reportaria todo sano con la flota desconectada.

    Lee `audit_log` (que ya registra CADA request autenticado, por agente) y responde:
    quien me alcanzo en la ventana, quien callo, y hace cuanto. No instrumenta nada nuevo:
    el dato ya estaba, faltaba exponerlo.

    PUBLICO A PROPOSITO (sin auth, igual que /health): un agente que perdio el transporte
    necesita poder consultar su propia alcanzabilidad, y si exigiera credencial no podria
    usarlo justo en el escenario para el que existe. NO expone contenido: solo nombres de
    agente, conteos y timestamps -- la misma clase de metadata que /health.
    """
    try:
        w = int(window_mins)
    except (TypeError, ValueError):
        w = 30
    w = max(1, min(w, 1440))
    request.state.audit = {"action": "health_reachability", "query": f"window={w}m"}
    out = {"window_mins": w, "reachable": [], "silent": [], "total_requests": 0}
    try:
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT agent, count(*) AS reqs, max(ts) AS last_seen, "
                "       round(extract(epoch from (now() - max(ts))))::int AS secs_ago "
                "  FROM audit_log WHERE ts > now() - (%s || ' minutes')::interval "
                " GROUP BY agent ORDER BY max(ts) DESC", (w,))
            for r in cur.fetchall():
                out["reachable"].append({
                    "agent": r["agent"], "requests": int(r["reqs"] or 0),
                    "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                    "secs_ago": int(r["secs_ago"] or 0)})
                out["total_requests"] += int(r["reqs"] or 0)
            # Los que EXISTEN y estan activos pero NO aparecieron en la ventana. Es la
            # mitad que importa: un agente mudo no genera filas, asi que contar solo lo
            # presente no puede detectar una ausencia (ausencia de dato != dato).
            seen = tuple(a["agent"] for a in out["reachable"]) or ("",)
            cur.execute(
                "SELECT name FROM agents WHERE active = true AND name <> ALL(%s) "
                "ORDER BY name", (list(seen),))
            out["silent"] = [r["name"] for r in cur.fetchall()]
    except Exception as e:
        out["error"] = repr(e)[:300]
        out["status"] = "degraded"
        return out
    out["status"] = "ok"
    out["reachable_count"] = len(out["reachable"])
    out["silent_count"] = len(out["silent"])
    return out


@app.get("/stats")
def stats(request: Request, agent: str = Depends(verify_agent)):
    request.state.audit = {"action": "stats"}
    return db.stats()


# --------------------------------------------------------------------------
# Grafo de NOTAS del vault (determinista: backlinks/outlinks) - additive
# --------------------------------------------------------------------------
@app.get("/graph/query")
def graph_query(request: Request, node: str = Query(..., min_length=1),
                direction: str = Query("both"), agent: str = Depends(verify_agent)):
    """Consulta estructural del grafo de notas del vault: outlinks/backlinks de una nota.
    node: stem o path parcial (case-insensitive). direction: out|in|both (default both)."""
    request.state.audit = {"action": "graph_query", "query": (node + "|" + (direction or "both"))[:500]}
    direction = (direction or "both").lower()
    if direction not in ("both", "out", "in"):
        raise HTTPException(400, "invalid direction")
    out = notegraph.query(node, direction, allowed=lambda p: not office_denied(agent, p),
                          cache_key=tuple(deny_prefixes(agent)))
    if out.get("error"):
        raise HTTPException(404, "node not found")
    # Resolved node must remain inaccessible even if query resolution returned a private stem.
    if out.get("resolved") and office_denied(agent, notegraph._graph()["paths"].get(out["resolved"], "")):
        raise HTTPException(404, "node not found")
    if out.get("path") and office_denied(agent, out["path"]):
        raise HTTPException(404, "node not found")
    # Stems are not paths: resolve links from the same graph's path map.
    paths = notegraph._graph()["paths"]
    for key in ("out", "in"):
        out[key] = [stem for stem in out.get(key, []) if not office_denied(agent, paths.get(stem, ""))]
        out[key + "_count"] = len(out[key])
    return out


@app.get("/graph/stats")
def graph_stats(request: Request, agent: str = Depends(verify_agent)):
    """Totales del grafo de notas: nodes/links."""
    request.state.audit = {"action": "graph_stats"}
    return notegraph.stats()


# --------------------------------------------------------------------------
# Gap 1 Cerebro v2 (2026-07-20): Grafo SEMANTICO automatico (FASE 1 determinista).
# Aristas inferidas por similitud vectorial (coseno) sobre embeddings YA en Qdrant.
# SEPARADO del grafo de wikilinks (notegraph). query/stats leen la tabla; rebuild es
# job batch caro -> SOLO lider. Additive. - additive
# --------------------------------------------------------------------------
class SemanticRebuildRequest(BaseModel):
    threshold: Optional[float] = None
    top_n: Optional[int] = None
    max_chunks: Optional[int] = None


class RetrievalRunRequest(BaseModel):
    k: Optional[int] = 5
    # Calibracion del umbral de los CONTROLES NEGATIVOS (tarea 0434db94). Sin
    # exponerlo, la specificity no se puede barrer y la metrica queda inutil para
    # fijar MIN_SCORE_DEFAULT con fundamento.
    neg_threshold: Optional[float] = None
    # dry-run: permite barrer umbrales sin ensuciar el historico de runs.
    persist: Optional[bool] = True


class EvalsetAddRequest(BaseModel):
    query: str
    expected_sources: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class ConsolidationAnalyzeRequest(BaseModel):
    dup_threshold: Optional[float] = None
    decay_months: Optional[int] = None
    top_n: Optional[int] = None
    max_chunks: Optional[int] = None
    persist: bool = True


class SkillMineRequest(BaseModel):
    window_days: Optional[int] = None
    min_corroboration: Optional[int] = None
    sim_threshold: Optional[float] = None
    max_items: Optional[int] = None
    persist: bool = True


@app.get("/graph/semantic-query")
def graph_semantic_query(request: Request, source: str = Query(..., min_length=1),
                         direction: str = Query("out"), min_score: Optional[float] = Query(None),
                         limit: int = Query(20, ge=1, le=200),
                         agent: str = Depends(verify_agent)):
    """Consulta el grafo SEMANTICO: notas relacionadas por similitud vectorial (no wikilinks).
    source: source_path (exacto o substring). direction: out|in|both. min_score sobre coseno."""
    request.state.audit = {"action": "graph_semantic_query",
                           "query": (source + "|" + (direction or "out"))[:500]}
    direction = (direction or "out").lower()
    if direction not in ("both", "out", "in"):
        raise HTTPException(400, "invalid direction")
    out = semanticgraph.query(source, direction, min_score, limit, deny_prefixes(agent))
    if out.get("error"):
        raise HTTPException(404, "source not found")
    if out.get("resolved") and office_denied(agent, out["resolved"]):
        raise HTTPException(404, "source not found")
    for key in ("out", "in"):
        out[key] = filter_hits(agent, out.get(key, []), "source")
        out[key + "_count"] = len(out[key])
    return out


@app.get("/graph/semantic-stats")
def graph_semantic_stats(request: Request, agent: str = Depends(verify_agent)):
    """Totales del grafo semantico: total_edges/distinct_sources/avg_score/last_rebuild."""
    request.state.audit = {"action": "graph_semantic_stats"}
    return semanticgraph.stats()


@app.post("/graph/semantic-rebuild")
def graph_semantic_rebuild(request: Request, req: SemanticRebuildRequest,
                           background_tasks: BackgroundTasks,
                           sync: bool = Query(False),
                           agent: str = Depends(verify_agent)):
    """Dispara el rebuild BATCH del grafo semantico (k-NN Qdrant -> semantic_edges).
    Operacion CARA: SOLO lider (verify is_lead). Body: {threshold?, top_n?, max_chunks?}.

    Por defecto es ASYNC: crea un job, lo corre en background (threadpool, no bloquea
    el event loop) y devuelve inmediato {job_id, status:'started'}. El progreso/estado
    se consulta en GET /graph/semantic-rebuild-status. El full (~50k chunks, ~27min)
    excede cualquier timeout HTTP, por eso NO corre sincrono.

    ?sync=true fuerza el rebuild SINCRONO (bloqueante) y devuelve el stats final -
    solo para tests/rebuilds acotados (usar con max_chunks bajo).
    """
    request.state.audit = {"action": "graph_semantic_rebuild",
                           "query": f"sync={sync} threshold={req.threshold} top_n={req.top_n} max_chunks={req.max_chunks}"}
    _require_leader(agent)

    if sync:
        # Camino sincrono acotado (tests). Mantiene la firma/salida original de rebuild().
        stats = semanticgraph.rebuild(threshold=req.threshold, top_n=req.top_n,
                                      max_chunks=req.max_chunks)
        request.state.audit = {"action": "graph_semantic_rebuild",
                               "query": f"SYNC edges={stats['edges_written']} chunks={stats['chunks_processed']}/{stats['chunks_total']} dur={stats['duration']}s"}
        stats["mode"] = "sync"
        return stats

    # Camino ASYNC (default): job persistido + rebuild en background (threadpool).
    job_id = semanticgraph.create_job(triggered_by=agent, threshold=req.threshold,
                                      top_n=req.top_n, max_chunks=req.max_chunks)
    background_tasks.add_task(semanticgraph.run_rebuild_job, job_id,
                             req.threshold, req.top_n, req.max_chunks)
    request.state.audit = {"action": "graph_semantic_rebuild",
                           "query": f"ASYNC started job={job_id} max_chunks={req.max_chunks}"}
    return {"job_id": job_id, "status": "started", "mode": "async"}


@app.get("/graph/semantic-rebuild-status")
def graph_semantic_rebuild_status(request: Request, job_id: Optional[str] = Query(None),
                                  agent: str = Depends(verify_agent)):
    """Estado de un job de rebuild async. job_id opcional: sin arg devuelve el ULTIMO job.
    -> {job_id, status[running|done|failed|none], progress, edges_written, started_at, finished_at}."""
    request.state.audit = {"action": "graph_semantic_rebuild_status",
                           "query": f"job_id={job_id}"}
    return semanticgraph.job_status(job_id)


# --------------------------------------------------------------------------
# Ola 1 Cerebro v2 (2026-07-21): Metricas retrieval (A) + Consolidacion (B) +
# Conversacion replayable (C). Todo additive. Los jobs caros (eval, analyze)
# son solo-lider; las lecturas (stats, candidates, thread, threads) baratas.
# --------------------------------------------------------------------------
@app.post("/eval/retrieval-run")
def eval_retrieval_run(request: Request, req: RetrievalRunRequest,
                       agent: str = Depends(verify_agent)):
    """Corre la evaluacion de retrieval sobre el golden set y guarda el run.
    CARA (N embeddings + N busquedas): SOLO lider. Body: {k?} (default 5)."""
    request.state.audit = {"action": "eval_retrieval_run", "query": f"k={req.k}"}
    _require_leader(agent)
    result = retrievalmetrics.evaluate(
        k=req.k or 5,
        persist=(True if req.persist is None else bool(req.persist)),
        triggered_by=agent,
        neg_threshold=req.neg_threshold)
    request.state.audit = {"action": "eval_retrieval_run",
                           "query": f"k={result['k']} n={result['n_queries']} "
                                    f"p={result['precision_at_k']} r={result['recall_at_k']} "
                                    f"mrr={result['mrr']} run={result.get('run_id')}"}
    return filter_office_output(agent, result, "retrieval")


@app.get("/eval/retrieval-stats")
def eval_retrieval_stats(request: Request, agent: str = Depends(verify_agent)):
    """Ultimo run de eval + agregados historicos + tamano del golden set. Barato."""
    request.state.audit = {"action": "eval_retrieval_stats"}
    return retrievalmetrics.stats()


@app.post("/eval/evalset-add")
def eval_evalset_add(request: Request, req: EvalsetAddRequest,
                     agent: str = Depends(verify_agent)):
    """Agrega una query al golden set. expected_sources: fragmentos de source_path."""
    request.state.audit = {"action": "eval_evalset_add", "query": (req.query or "")[:200]}
    res = retrievalmetrics.add_evalset_query(req.query, req.expected_sources, req.note)
    if res.get("error"):
        raise HTTPException(400, res["error"])
    return res


@app.post("/consolidation/analyze")
def consolidation_analyze_ep(request: Request, req: ConsolidationAnalyzeRequest,
                             agent: str = Depends(verify_agent)):
    """'Sueno': detecta near-dups, decay y densidad. Persiste propuestas sin borrar
    candidatos ni memoria. Una corrida limitada no invalida propuestas ausentes; una
    ausencia en scan completo de decay puede exponerse como ``stale`` (no dismissal)."""
    request.state.audit = {"action": "consolidation_analyze",
                           "query": f"dup={req.dup_threshold} decay={req.decay_months} max={req.max_chunks} persist={req.persist}"}
    _require_leader(agent)
    result = consolidation.analyze(dup_threshold=req.dup_threshold,
                                   decay_months=req.decay_months, top_n=req.top_n,
                                   max_chunks=req.max_chunks, persist=req.persist)
    request.state.audit = {"action": "consolidation_analyze",
                           "query": f"near_dups={result['counts']['near_dup_pairs']} decay={result['counts']['decay_candidates']} persisted={result['counts']['persisted']} dur={result['duration']}s"}
    return filter_office_output(agent, result, "consolidation")


@app.get("/consolidation/candidates")
def consolidation_candidates_ep(request: Request, kind: Optional[str] = Query(None),
                                status: str = Query("proposed"),
                                limit: int = Query(100, ge=1, le=1000),
                                agent: str = Depends(verify_agent)):
    """Lee candidatos de consolidacion. ``status=stale`` es opt-in y significa que
    un scan completo de decay ya no detecto la regla; no es decision humana ni borra
    historia. El default ``proposed`` conserva compatibilidad de la bandeja."""
    request.state.audit = {"action": "consolidation_candidates",
                           "query": f"kind={kind} status={status} limit={limit}"}
    return filter_office_output(agent, consolidation.candidates(kind=kind, status=status, limit=limit), "consolidation")


@app.post("/skills/mine")
def skills_mine_ep(request: Request, req: SkillMineRequest,
                   agent: str = Depends(verify_agent)):
    """Destila los harvests recientes (memorias failure|insight|correction|tool-quirk +
    checkpoints pitfall|landmark) en PROPUESTAS de skill con corroboracion k>=2 de agentes
    DISTINTOS. CARO -> SOLO lider. SOLO PROPONE: jamas crea/edita skills (Regla Oro)."""
    request.state.audit = {"action": "skills_mine",
                           "query": f"window={req.window_days} mincorr={req.min_corroboration} sim={req.sim_threshold} max={req.max_items} persist={req.persist}"}
    _require_leader(agent)
    result = skillmining.mine(window_days=req.window_days,
                              min_corroboration=req.min_corroboration,
                              sim_threshold=req.sim_threshold,
                              max_items=req.max_items, persist=req.persist)
    request.state.audit = {"action": "skills_mine",
                           "query": f"signals={result['counts']['signals']} clusters={result['clusters_found']} proposals={result['counts']['proposals']} persisted={result['counts']['persisted']} dur={result['duration']}s"}
    return filter_office_output(agent, result, "proposals")


@app.get("/skills/proposals")
def skills_proposals_ep(request: Request, status: str = Query("proposed"),
                        kind: Optional[str] = Query(None),
                        limit: int = Query(100, ge=1, le=1000),
                        agent: str = Depends(verify_agent)):
    """Lee propuestas de skill (barato). Filtros status (proposed|accepted|dismissed) y
    kind (new|update). Lo consume Monica/Otixe."""
    request.state.audit = {"action": "skills_proposals",
                           "query": f"status={status} kind={kind} limit={limit}"}
    return filter_office_output(agent, skillmining.proposals(status=status, kind=kind, limit=limit), "proposals")


class BusPublishRequest(BaseModel):
    """Publicación REST idempotente. `sender` no existe en el modelo: siempre sale
    de verify_agent, incluida la identidad humana delegada por T1."""
    model_config = {"extra": "forbid"}
    recipients: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    performative: Literal["inform", "request"] = "inform"
    refs_task: str = ""
    conversation_id: str = ""
    idempotency_key: uuid.UUID


def _resolve_bus_refs_task(cur, ref: str):
    """Resuelve UUID completo o prefijo único de tarea; vacío significa NULL."""
    value = (ref or "").strip()
    if not value:
        return None
    if len(value) < 8 or not _re.fullmatch(r"[0-9a-fA-F-]+", value):
        raise HTTPException(422, "refs_task debe ser UUID o prefijo hexadecimal de al menos 8 caracteres")
    cur.execute("SELECT id FROM tasks WHERE id::text = %s OR id::text LIKE %s LIMIT 2",
                (value, value + "%"))
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(422, "refs_task no existe")
    if len(rows) > 1:
        raise HTTPException(422, "refs_task ambiguo")
    return rows[0]["id"]


@app.post("/bus/publish")
def bus_publish_ep(request: Request, req: BusPublishRequest,
                   agent: str = Depends(verify_agent)):
    """Publica en el bus con identidad autenticada e idempotencia sin DDL.

    La UUID del intento ES bus_messages.id. Un conflicto sólo es éxito si el payload
    persistido coincide exactamente; reutilizar la clave con otro significado da 409.
    Mensaje y deliveries se crean dentro de la misma transacción.

    🔴 RETIRADO (orden de Otixe, 2026-09-15): el bus viejo quedó en SÓLO LECTURA. La
    ruta NO se borra —eso rompería a todo cliente con un 404 indistinguible de un
    typo— y responde 410 Gone con el mismo texto que el tool MCP. Reversible por
    `BUS_LEGACY_PUBLISH` (default: cerrado). Lecturas: BUS_LEGACY_READ.
    """
    if bus_legacy.publish_closed():
        # Se audita el intento ANTES de rechazarlo: sin esto, "nadie intentó publicar"
        # y "alguien intentó y no quedó rastro" serían indistinguibles, y justo ahora
        # es cuando hace falta medir quién sigue usando el bus viejo.
        request.state.audit = {
            "action": "bus_publish",
            "query": f"RECHAZADO {bus_legacy.STATUS}: bus viejo en sólo-lectura",
            "result_count": 0,
        }
        raise HTTPException(bus_legacy.STATUS, bus_legacy.MESSAGE)
    recs = sorted({
        part.strip().lower() for part in (req.recipients or "").split(",")
        if part.strip()
    })
    if not recs:
        raise HTTPException(422, "recipients debe contener al menos un agente")
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(422, "content no puede estar vacío")
    conversation_id = (req.conversation_id or "").strip()
    message_id = req.idempotency_key

    with db.pg() as conn, conn.cursor() as cur:
        refs_task = _resolve_bus_refs_task(cur, req.refs_task)
        cur.execute(
            "INSERT INTO bus_messages "
            "(id, sender, recipients, performative, content, conversation_id, refs_task) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            (message_id, agent, recs, req.performative, content,
             conversation_id, refs_task))
        created = cur.fetchone() is not None
        if created:
            for recipient in recs:
                cur.execute(
                    "INSERT INTO bus_deliveries (message_id, recipient, status) "
                    "VALUES (%s,%s,'pending')",
                    (message_id, recipient))
        else:
            cur.execute(
                "SELECT sender, recipients, performative, content, conversation_id, refs_task "
                "FROM bus_messages WHERE id=%s",
                (message_id,))
            stored = cur.fetchone()
            same_payload = bool(stored) and (
                stored["sender"] == agent
                and list(stored["recipients"] or []) == recs
                and stored["performative"] == req.performative
                and stored["content"] == content
                and (stored["conversation_id"] or "") == conversation_id
                and stored["refs_task"] == refs_task
            )
            if not same_payload:
                raise HTTPException(
                    409, "idempotency_key ya fue usada con un payload distinto")
        conn.commit()

    request.state.audit = {
        "action": "bus_publish",
        "query": f"msg:{message_id} -> {','.join(recs)} created={created}"[:500],
        "result_count": len(recs),
    }
    return {
        "id": str(message_id),
        "sender": agent,
        "recipients": recs,
        "performative": req.performative,
        "created": created,
        "deduplicated": not created,
    }


@app.get("/bus/thread")
def bus_thread_ep(request: Request, conversation_id: str = Query(..., min_length=1),
                  include_content: bool = Query(False),
                  agent: str = Depends(verify_agent)):
    """Replay de un hilo del bus: mensajes de conversation_id ordenados cronologicamente
    con sender/recipients/performative/refs_task/timestamp + entregas. Read-only.

    EL CUERPO ES EXPLICITO (2026-08-18, tarea 1cd25772). Por defecto NO se devuelve
    `content`: viajan `content_len` y `content_preview` (120 chars), que alcanzan para
    triar. `include_content=true` devuelve los cuerpos completos y queda registrado en
    audit_log como LECTURA DELIBERADA: el objetivo es que una lectura por comodidad deje
    de ser indistinguible de una necesaria."""
    if bus_legacy.read_closed(agent):
        raise HTTPException(bus_legacy.STATUS, bus_legacy.READ_MESSAGE)
    request.state.audit = {"action": "bus_thread",
                           "query": (conversation_id[:480]
                                     + " include_content=%s" % bool(include_content))}
    return conversations.thread(conversation_id, include_content=include_content)


# Patron de fecha 'YYYY-MM-DD' para los filtros since/until del bus: un formato invalido
# se rechaza con 422 en el borde, sin llegar al SQL.
_BUS_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


@app.get("/bus/threads")
def bus_threads_ep(request: Request, agent_filter: Optional[str] = Query(None, alias="agent"),
                   limit: int = Query(50, ge=1, le=500),
                   include_unthreaded: bool = Query(False),
                   project: Optional[str] = Query(None),
                   since: Optional[str] = Query(None, pattern=_BUS_DATE_RE),
                   until: Optional[str] = Query(None, pattern=_BUS_DATE_RE),
                   q: Optional[str] = Query(None),
                   agent: str = Depends(verify_agent)):
    """Lista hilos (conversation_id) en los que participo un agente (o TODOS). Cada hilo:
    count, participantes, ultimo mensaje.

    include_unthreaded=true suma las conversaciones IMPLICITAS: los mensajes publicados SIN
    conversation_id (la gran mayoria del bus), agrupados por conjunto canonico de
    participantes (ej. 'merlin|rias'). En ese modo cada item trae `kind`: 'thread' (id en
    `conversation_id`) o 'implicit' (id en `participants_key`); el kind lo decide el BACKEND.
    Por defecto es false => respuesta IDENTICA a la historica (cambio aditivo).

    Filtros opcionales, combinables con AND: project (via refs_task -> tasks.project; los
    mensajes SIN refs_task no tienen proyecto y quedan FUERA), since/until ('YYYY-MM-DD'
    sobre ts; `until` incluye el dia completo) y q (coincidencia de TEXTO en
    content/conversation_id/participantes, NO busqueda semantica: el bus no esta en Qdrant).
    Read-only."""
    if bus_legacy.read_closed(agent):
        raise HTTPException(bus_legacy.STATUS, bus_legacy.READ_MESSAGE)
    request.state.audit = {"action": "bus_threads",
                          "query": ("agent=%s unthreaded=%s project=%s since=%s until=%s q=%s"
                                    % ((agent_filter or "*"), include_unthreaded,
                                       (project or ""), (since or ""), (until or ""),
                                       (q or "")))[:500]}
    return conversations.list_threads(agent_filter, limit,
                                      include_unthreaded=include_unthreaded,
                                      project=project, since=since, until=until, q=q)


@app.get("/bus/group")
def bus_group_ep(request: Request, participants: str = Query(..., min_length=1),
                 limit: int = Query(200, ge=1, le=500),
                 project: Optional[str] = Query(None),
                 since: Optional[str] = Query(None, pattern=_BUS_DATE_RE),
                 until: Optional[str] = Query(None, pattern=_BUS_DATE_RE),
                 q: Optional[str] = Query(None),
                 include_content: bool = Query(False),
                 agent: str = Depends(verify_agent)):
    """Replay de una conversacion IMPLICITA: los mensajes SIN conversation_id cuyo conjunto
    canonico de participantes es `participants` (ej. 'merlin|rias'), en orden cronologico
    ascendente y con los mismos campos que /bus/thread. Filtra SOLO mensajes sin hilo, asi
    un mensaje que ya pertenece a un hilo declarado no se lee dos veces. `participants` vacio
    se rechaza con 422 (misma simetria que conversation_id en /bus/thread); una clave
    inexistente devuelve el campo `error` poblado, igual que un hilo inexistente. Acepta los
    mismos filtros opcionales que /bus/threads. Read-only.

    EL CUERPO ES EXPLICITO (2026-08-18, tarea 1cd25772): mismo contrato que /bus/thread
    (default metadata + preview; `include_content=true` audita la lectura deliberada).
    ESTE es el endpoint de la fuga que origino el cambio: se llamo para verificar un
    `count == 12` y devolvio los 12 mensajes completos de una conversacion privada."""
    if bus_legacy.read_closed(agent):
        raise HTTPException(bus_legacy.STATUS, bus_legacy.READ_MESSAGE)
    request.state.audit = {"action": "bus_group",
                           "query": (participants[:480]
                                     + " include_content=%s" % bool(include_content))}
    return conversations.group(participants, limit, project=project, since=since,
                              until=until, q=q, include_content=include_content)


# --------------------------------------------------------------------------
# Gap 2a Cerebro v2 (2026-07-20): Harvest LIBRE + telemetria de vida - additive
# Checkpoint atado a (agent, session_ref) sin task_id. agent SIEMPRE del token
# (anti-spoof). Solo INSERT/SELECT. Reusa db.pg() (contextmanager) para el acceso.
# --------------------------------------------------------------------------
def _now_utc():
    return datetime.now(timezone.utc)


@app.post("/checkpoint/agent-save")
def checkpoint_agent_save(request: Request, req: AgentCheckpointRequest,
                          agent: str = Depends(verify_agent)):
    """Harvest/checkpoint LIBRE: guarda un snapshot de progreso o cosecha atado a
    (agent, session_ref) en vez de a una tarea. 'agent' se deriva de la key
    (anti-spoof); cualquier agent en el body se ignora. kind in progress|harvest|
    pitfall|landmark. Solo INSERT."""
    kind = (req.kind or "progress").strip().lower()
    if kind not in AGENT_CKPT_KINDS:
        request.state.audit = {"action": "agent_checkpoint_save", "error": "bad_kind"}
        raise HTTPException(400, f"invalid kind '{req.kind}'; valid: {sorted(AGENT_CKPT_KINDS)}")
    ck_state, _surr_n = sanitize.repair_surrogates_deep(req.state or {})
    ck_session_ref, _session_surr_n = sanitize.repair_surrogates(req.session_ref or "")
    ck_label, _label_surr_n = sanitize.repair_surrogates(req.label or "")
    _surr_n += _session_surr_n + _label_surr_n
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_checkpoints (agent, session_ref, step_index, label, kind, state) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
            (agent, ck_session_ref, req.step_index, ck_label,
             kind, json.dumps(ck_state)))
        row = cur.fetchone()
        conn.commit()
    request.state.audit = {"action": "agent_checkpoint_save",
                           "query": f"{agent} kind={kind} session={ck_session_ref[:60]}"}
    return {"id": str(row["id"]), "agent": agent, "kind": kind,
            "created_at": row["created_at"].isoformat()}


@app.get("/checkpoint/agent-latest")
def checkpoint_agent_latest(request: Request, agent: str = Query(..., min_length=1),
                            session_ref: str = Query(""),
                            caller: str = Depends(verify_agent)):
    """Ultimo checkpoint de un agente (por 'agent', el sujeto consultado). Si session_ref
    dado, el ultimo de esa sesion; si no, el ultimo del agente por created_at. Devuelve
    metadata siempre; el 'state' completo solo si el caller es el propio agente o un lider
    (telemetria de vida: la metadata no es sensible, el contenido si)."""
    request.state.audit = {"action": "agent_checkpoint_latest", "query": f"{agent} session={(session_ref or '')[:60]}"}
    with db.pg() as conn, conn.cursor() as cur:
        if session_ref:
            cur.execute(
                "SELECT id, agent, session_ref, step_index, label, kind, state, created_at "
                "FROM agent_checkpoints WHERE agent=%s AND session_ref=%s "
                "ORDER BY step_index DESC, created_at DESC LIMIT 1", (agent, session_ref))
        else:
            cur.execute(
                "SELECT id, agent, session_ref, step_index, label, kind, state, created_at "
                "FROM agent_checkpoints WHERE agent=%s "
                "ORDER BY created_at DESC, step_index DESC LIMIT 1", (agent,))
        r = cur.fetchone()
    if not r:
        return {"agent": agent, "found": False}
    out = {
        "found": True,
        "id": str(r["id"]),
        "agent": r["agent"],
        "session_ref": r["session_ref"],
        "step_index": r["step_index"],
        "label": r["label"],
        "kind": r["kind"],
        "created_at": r["created_at"].isoformat(),
    }
    # 'state' solo si el caller es el propio agente o un lider (anti-fuga de contenido).
    if caller == r["agent"] or db.is_lead(caller):
        out["state"] = r["state"]
    else:
        out["state_redacted"] = True
    return out


@app.get("/checkpoint/agent-liveness")
def checkpoint_agent_liveness(request: Request, agent: str = Query(..., min_length=1),
                              window_secs: int = Query(600, ge=1),
                              caller: str = Depends(verify_agent)):
    """Telemetria de vida de un agente: cuando fue su ultimo checkpoint y su ultima
    cosecha (harvest/pitfall/landmark en agent_checkpoints O cualquier memory_add en
    chunks_meta), y si cosecho dentro de la ventana. Metadata no sensible: cualquier key
    valida puede consultarla (la usa el harness externo de Gap 2b para decidir si forzar
    la cosecha antes de matar a un subagente). Solo SELECT."""
    request.state.audit = {"action": "agent_liveness_query", "query": f"{agent} window={window_secs}"}
    with db.pg() as conn, conn.cursor() as cur:
        # Ultimo checkpoint (cualquier kind) y ultima cosecha en agent_checkpoints.
        cur.execute(
            "SELECT max(created_at) AS last_ckpt, "
            "       max(created_at) FILTER (WHERE kind = ANY(%s)) AS last_harvest_ckpt, "
            "       count(*) FILTER (WHERE created_at > now() - make_interval(secs => %s)) AS ckpts_in_window "
            "FROM agent_checkpoints WHERE agent=%s",
            (list(AGENT_CKPT_HARVEST_KINDS), window_secs, agent))
        ck = cur.fetchone()
        # Ultimo memory_add del agente (chunks_meta vigentes, no borradas).
        cur.execute(
            "SELECT max(created_at) AS last_mem "
            "FROM chunks_meta WHERE agent_owner=%s AND deleted_at IS NULL", (agent,))
        mm = cur.fetchone()
        # Existe cosecha (checkpoint harvest-kind O memory_add) dentro de la ventana?
        cur.execute(
            "SELECT "
            "  EXISTS(SELECT 1 FROM agent_checkpoints "
            "         WHERE agent=%s AND kind = ANY(%s) "
            "           AND created_at > now() - make_interval(secs => %s)) "
            "  OR EXISTS(SELECT 1 FROM chunks_meta "
            "         WHERE agent_owner=%s AND deleted_at IS NULL "
            "           AND created_at > now() - make_interval(secs => %s)) AS harvested",
            (agent, list(AGENT_CKPT_HARVEST_KINDS), window_secs, agent, window_secs))
        hv = cur.fetchone()

    def _iso(v):
        return v.isoformat() if v else None

    def _secs_since(v):
        if not v:
            return None
        return int((_now_utc() - v).total_seconds())

    last_ckpt = ck["last_ckpt"]
    # ultima cosecha = max(ultimo checkpoint harvest-kind, ultimo memory_add)
    harvest_candidates = [x for x in (ck["last_harvest_ckpt"], mm["last_mem"]) if x]
    last_harvest = max(harvest_candidates) if harvest_candidates else None
    return {
        "agent": agent,
        "window_secs": window_secs,
        "last_checkpoint_at": _iso(last_ckpt),
        "last_harvest_at": _iso(last_harvest),
        "seconds_since_checkpoint": _secs_since(last_ckpt),
        "seconds_since_harvest": _secs_since(last_harvest),
        "harvested_in_window": bool(hv["harvested"]),
        "checkpoints_in_window": int(ck["ckpts_in_window"] or 0),
    }


# --------------------------------------------------------------------------
# Registro DECLARATIVO de la flota (frontmatter de los .md) - additive, read-only
# --------------------------------------------------------------------------
@app.get("/agents/registry")
def agents_registry(request: Request, name: str = Query(""),
                    agent: str = Depends(verify_agent)):
    """Registro declarativo de la flota: modelo/tools/thinking/runtime por agente.
    Sin name -> lista todos. Con name -> devuelve ese agente (case-insensitive)."""
    if name and name.strip():
        request.state.audit = {"action": "agents_registry_get", "query": name.strip()[:500]}
        return agentregistry.get_agent(name)
    request.state.audit = {"action": "agents_registry_list"}
    return agentregistry.list_agents()


@app.get("/agents/registry/{name}")
def agents_registry_by_name(request: Request, name: str, agent: str = Depends(verify_agent)):
    """Registro declarativo de un agente por nombre (path param, case-insensitive)."""
    request.state.audit = {"action": "agents_registry_get", "query": (name or "")[:500]}
    return agentregistry.get_agent(name)


@app.post("/memory/add", response_model=AddResponse)
@limiter.limit(lambda: RATE_LIMIT_ADD)
def add(request: Request, req: AddRequest, agent: str = Depends(verify_agent)):
    # C3 (fix traversal 2026-08-10): el source_path del caller es arbitrario; se
    # rechaza '..'/absoluto ANTES de cualquier decision de scope o saneamiento.
    _require_safe_source_path(req.source_path)
    if not _path_in_scope(agent, req.source_path, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"invalid category '{req.category}'; valid: {sorted(VALID_CATEGORIES)}")
    # --- Saneamiento write-path (ADDITIVE): private-blocks + secret-redaction ---
    clean, report = sanitize.sanitize(req.text)
    warnings: list[str] = []
    sensitivity = req.sensitivity
    if report["secrets"]:
        # Secretos de alta confianza detectados: NO se persiste el valor en claro
        # (ya redactado en `clean`), se fuerza sensitivity='secret' y se avisa.
        sensitivity = "secret"
        kinds = ", ".join(sorted({s["kind"] for s in report["secrets"]}))
        warnings.append(f"secret(s) redacted before persist: {kinds} (count={len(report['secrets'])})")
    if report["private_redacted"]:
        warnings.append(f"private block(s) redacted: {report['private_redacted']}")

    text_for_embed = (req.title + "\n\n" + clean) if req.title else clean
    gemma = embedder.profile_active()
    # Explicit emergency mode: PostgreSQL remains authoritative while the
    # semantic index is unavailable. ACK means durable, NOT semantically indexed.
    deferred_embedding = os.environ.get("EMBEDDING_WRITE_DEFERRED", "0") == "1"
    vec = None if deferred_embedding else (embedder.embed(clean, title=req.title) if gemma else embedder.embed(text_for_embed))
    if deferred_embedding:
        warnings.append("durably_saved; semantic_index_pending")
    owner = agent  # anti-spoofing: owner SIEMPRE derivado de la key; se ignora req.agent_owner
    src_hash = hashlib.sha256(clean.encode()).hexdigest()
    cid = db.upsert_chunk(
        chunk_id=None, source_path=req.source_path, source_hash=src_hash,
        chunk_index=req.chunk_index, title=req.title or "",
        breadcrumb=[req.title] if req.title else [],
        text=clean, vector=vec,
        tags=req.tags, agent_owner=owner, sensitivity=sensitivity,
        category=req.category,
    )
    request.state.audit = {"action": "add", "chunk_id": cid, "secrets_redacted": len(report["secrets"]), "private_redacted": report["private_redacted"]}
    return AddResponse(chunk_id=cid, dims=db.DIMS, warnings=warnings)


@app.post("/memory/add-markdown")
@limiter.limit(lambda: RATE_LIMIT_ADD)
def add_markdown(request: Request, req: AddMarkdownRequest, agent: str = Depends(verify_agent)):
    """Chunkea markdown + indexa todos los chunks de un golpe."""
    # C3 (fix traversal 2026-08-10): guarda ANTES del scope y del gate de skills.
    # Este era el endpoint explotado: source_path='skills/../x' se llevaba la
    # excepcion de skills para un destino arbitrario.
    _require_safe_source_path(req.source_path)
    if not _path_in_scope(agent, req.source_path, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    # --- Saneamiento write-path (ADDITIVE) ANTES de chunkear ---
    # SKILLS: se escanea igual (para poder AVISAR) y NO se degrada por CREDENCIAL,
    # pero los bloques <private> SI se redactan -- ver C4 abajo.
    is_skill = _is_skill_path(req.source_path)
    clean_md, report = sanitize.sanitize(req.markdown)
    warnings: list[str] = []
    sensitivity = req.sensitivity
    # El texto que se PERSISTE y el que se EMBEDDEA tienen que ser el MISMO: si
    # se embeddeara el redactado y se persistiera el original, el vector no
    # corresponderia al texto y la busqueda se degradaria de otra forma.
    # C4 (2026-08-10): para skills se parte del ORIGINAL (conserva la credencial)
    # pero se le aplica SOLO redact_private_blocks. Un <private> es una decision
    # EXPLICITA del autor de ocultar algo; una credencial detectada por patron es
    # un hallazgo automatico. La excepcion de Otixe cubre credenciales, no <private>.
    if is_skill:
        md_to_persist, _priv_n = sanitize.redact_private_blocks(req.markdown)
    else:
        md_to_persist = clean_md
    if report["secrets"]:
        kinds = ", ".join(sorted({s["kind"] for s in report["secrets"]}))
        if is_skill:
            # Se informa, no se degrada: la skill necesita la credencial para ser
            # ejecutable y el vault es privado.
            warnings.append(
                f"secret(s) detected but KEPT (skill path): {kinds} "
                f"(count={len(report['secrets'])})"
            )
        else:
            sensitivity = "secret"
            warnings.append(f"secret(s) redacted before persist: {kinds} (count={len(report['secrets'])})")
    if report["private_redacted"]:
        # C4: identico en skill y no-skill; el bloque <private> se redacta SIEMPRE.
        warnings.append(f"private block(s) redacted: {report['private_redacted']}")

    chunks = chunker.chunk_markdown(md_to_persist, doc_title=req.title or "")
    owner = agent  # anti-spoofing: owner SIEMPRE derivado de la key; se ignora req.agent_owner
    src_hash = hashlib.sha256(md_to_persist.encode()).hexdigest()
    ids = []
    for c in chunks:
        text_emb = chunker.enrich_text(c)
        vec = (embedder.embed(c.text, title=c.title) if embedder.profile_active()
               else embedder.embed(text_emb))
        cid = db.upsert_chunk(
            chunk_id=None, source_path=req.source_path, source_hash=src_hash,
            chunk_index=c.index, title=c.title, breadcrumb=c.breadcrumb,
            text=c.text, vector=vec,
            tags=req.tags, agent_owner=owner, sensitivity=sensitivity,
        )
        ids.append(cid)
    # FIX 2 (2026-08-04): pruning de huerfanos. Si esta fuente tenia MAS chunks
    # antes (doc encogio, o el chunker cambio de politica), los chunk_index >=
    # len(ids) quedaban VIVOS y searchables: texto viejo mezclado con el nuevo.
    # El indexer del vault ya prunaba; este endpoint no, y aca se acumulo la
    # basura. Soft-delete (deleted_at=now()) + espejo en Qdrant; NUNCA DELETE.
    pruned = db.prune_leftover_chunks(req.source_path, len(ids))
    if pruned:
        warnings.append(f"pruned {pruned} orphan chunk(s) with chunk_index >= {len(ids)}")
    request.state.audit = {"action": "add-markdown", "result_count": len(ids), "secrets_redacted": 0 if is_skill else len(report["secrets"]), "secrets_kept_skill": len(report["secrets"]) if is_skill else 0, "private_redacted": report["private_redacted"], "pruned": pruned}
    return {"chunk_ids": ids, "count": len(ids), "pruned": pruned, "warnings": warnings}


@app.post("/memory/search")
@limiter.limit(lambda: RATE_LIMIT_SEARCH)
def search(request: Request, req: SearchRequest, agent: str = Depends(verify_agent)):
    if req.category is not None and req.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"invalid category '{req.category}'; valid: {sorted(VALID_CATEGORIES)}")
    # FIX E5 (2026-08-03, tarea 4f110f83): una query vacia NO se busca.
    # Antes embedder.embed("") devolvia un vector CERO y la busqueda "funcionaba":
    # HTTP 200 con resultados de score 0.0000 y low_confidence=false, o sea datos
    # irrelevantes con apariencia de acierto. Ahora es un error explicito.
    if not req.query or not req.query.strip():
        request.state.audit = {"action": "search", "error": "query vacia"}
        raise HTTPException(400, "query vacia: no se puede buscar sin terminos")
    lexical_degraded = os.environ.get("SEARCH_LEXICAL_ONLY", "0") == "1"
    vec = None if lexical_degraded else embedder.embed(req.query, input_type="search_query")

    # D10 opción B: por defecto excluir sensitivity='secret'.
    # Sólo agentes en SECRETS_ALLOWED_AGENTS pueden solicitar include_secrets=true.
    sensitivity = req.sensitivity
    exclude_secret = True
    if req.include_secrets:
        if agent not in SECRETS_ALLOWED_AGENTS:
            raise HTTPException(403, "agent not allowed to include secrets")
        exclude_secret = False
        # Si el caller no pidió filtro de sensitivity explícito, devolvemos todo
        # (incluye secret/metadata_only/pii/normal).
    # Si el caller pidió explicitamente sensitivity list, respetamos pero filtramos 'secret'
    # cuando no tenga permisos.
    if sensitivity is not None and exclude_secret:
        sensitivity = [s for s in sensitivity if s != "secret"]
        if not sensitivity:
            sensitivity = None  # evitar filtro vacío que no matchea nada

    # Authz Fase 3b: scope por carpeta. admin/sistema => None (sin filtro).
    # member => solo sus prefijos de lectura.
    _scope = resolve_agent_scope(agent)
    _scope_read = None if _scope is None else _scope["read"]

    search_result = (db.search_lexical if lexical_degraded else db.search)(
        vec, query_text=req.query, top_k=req.top_k, tags=req.tags, agent_owner=req.agent_owner,
        sensitivity=sensitivity, source_prefix=req.source_prefix,
        exclude_secret=exclude_secret, category=req.category,
        include_superseded=req.include_superseded, hybrid=req.hybrid,
        min_score=req.min_score, include_archived=req.include_archived,
        include_sessions=req.include_sessions, include_logs=req.include_logs,
        min_chunk_len=req.min_chunk_len, scope_prefixes=_scope_read,
        deny_prefixes=deny_prefixes(agent),
    )
    results = filter_hits(agent, search_result["results"])
    request.state.audit = {"action": "search", "query": req.query, "result_count": len(results)}
    return {
        "results": results, "count": len(results), "query": req.query,
        "low_confidence": search_result["low_confidence"],
        "from_archive": search_result["from_archive"],
        # FIX E5: senal EXPLICITA de que la busqueda se ejecuto. Permite al cliente
        # distinguir "no encontre nada" (search_ok=true, count=0) de "no pude buscar"
        # (error HTTP). Si el embed o el motor fallan, no se llega hasta aca.
        "search_ok": True,
        "retrieval_mode": "lexical_degraded" if lexical_degraded else "semantic",
        "warnings": ["semantic_search_unavailable; lexical_only; cosine_threshold_not_applied"] if lexical_degraded else [],
    }


def _forget_enforce_scope(agent: str, req: "ForgetRequest") -> None:
    """Authz Bloque B: enforcement de propiedad/scope en /memory/forget.
    admin/sistema (resolve_agent_scope -> None) => sin restriccion. member => solo puede
    borrar chunks que le pertenecen (chunks_meta.agent_owner == agent) o cuyo source_path
    cae en su scope de ESCRITURA. Borrado masivo por tags: solo admin. Viola => HTTP 403."""
    if req.source_path and office_denied(agent, req.source_path):
        raise HTTPException(403, "fuera de tu scope")
    scope = resolve_agent_scope(agent)
    if not req.chunk_id and not req.source_path:
        if scope is None:
            return  # tags-only: hard-delete filters denied offices in SQL
        raise HTTPException(403, "no podes borrar memoria por tags fuera de admin")
    # resolver los chunks afectados (por chunk_id o source_path) y su owner+path
    with db.pg() as conn, conn.cursor() as cur:
        rows = []
        if req.chunk_id:
            try:
                uuid.UUID(str(req.chunk_id))
            except (ValueError, AttributeError, TypeError):
                raise HTTPException(400, "chunk_id invalido")
            cur.execute("SELECT agent_owner, source_path FROM chunks_meta WHERE id=%s",
                        (req.chunk_id,))
            rows = [dict(r) for r in cur.fetchall()]
        elif req.source_path:
            cur.execute("SELECT agent_owner, source_path FROM chunks_meta WHERE source_path=%s",
                        (req.source_path,))
            rows = [dict(r) for r in cur.fetchall()]
        else:
            raise HTTPException(403, "no podes borrar memoria fuera de tu scope/propiedad")
    # chunk inexistente => rows vacio: no es error de scope, dejar que el borrado devuelva 0
    for r in rows:
        if office_denied(agent, r.get("source_path") or ""):
            if req.chunk_id:
                return False  # missing and forbidden UUID both return deleted=0
            raise HTTPException(403, "no podes borrar memoria fuera de tu scope/propiedad")
        owner_ok = (r.get("agent_owner") or "") == agent
        path_ok = _path_in_scope(agent, r.get("source_path") or "", write=True)
        if scope is None or owner_ok or path_ok:
            continue
        raise HTTPException(403, "no podes borrar memoria fuera de tu scope/propiedad")
    return True


@app.post("/memory/forget")
@limiter.limit(lambda: RATE_LIMIT_FORGET)
def forget(request: Request, req: ForgetRequest, agent: str = Depends(verify_agent)):
    if req.mode not in ("soft", "hard"):
        raise HTTPException(400, "mode must be 'soft' or 'hard'")
    if _forget_enforce_scope(agent, req) is False:
        # Same audit row and body as a missing chunk: denial must not be an oracle.
        request.state.audit = {"action": f"forget-{req.mode}", "chunk_id": req.chunk_id, "result_count": 0}
        return {"deleted": 0, "mode": req.mode}
    if req.mode == "soft":
        n = db.soft_delete_chunks(source_path=req.source_path, chunk_id=req.chunk_id)
    else:
        ids = [req.chunk_id] if req.chunk_id else None
        n = db.hard_delete_chunks(source_path=req.source_path, chunk_ids=ids, tags=req.tags,
                                  deny_prefixes=deny_prefixes(agent) if req.tags and not ids and not req.source_path else None)
    request.state.audit = {
        "action": f"forget-{req.mode}", "chunk_id": req.chunk_id, "result_count": n,
    }
    return {"deleted": n, "mode": req.mode}


def _resolve_chunk_id(ref: str) -> Optional[str]:
    """Sprint 1: resuelve una referencia a chunk_id. Si `ref` ya es un uuid valido
    de un chunk existente, lo devuelve; si no, lo trata como source_path y devuelve
    el id del chunk vigente (deleted_at IS NULL) de menor chunk_index."""
    with db.pg() as conn, conn.cursor() as cur:
        # 1) match directo por id -- SOLO si `ref` es un uuid valido.
        # Pre-validar evita disparar el cast invalido de Postgres, que aborta
        # la transaccion y hace fallar la 2a query (por source_path) con
        # InFailedSqlTransaction. (fix bug supersede por source_path -> HTTP 500)
        try:
            uuid.UUID(str(ref))
            is_uuid = True
        except (ValueError, AttributeError, TypeError):
            is_uuid = False
        if is_uuid:
            cur.execute("SELECT id FROM chunks_meta WHERE id=%s", (ref,))
            row = cur.fetchone()
            if row:
                return str(row["id"])
        # 2) match por source_path (chunk vigente de menor indice)
        cur.execute(
            """SELECT id FROM chunks_meta WHERE source_path=%s AND deleted_at IS NULL
               ORDER BY chunk_index LIMIT 1""", (ref,))
        row = cur.fetchone()
        if row:
            return str(row["id"])
        # 3) PREFIJO de chunk_id (tarea 89fea402, 2026-08-05). Cierra la ultima de las
        #    TRES conductas distintas que tenian los endpoints frente a un prefijo:
        #    `parent_id` ya resolvia, `refs_task` daba el error crudo de uuid de
        #    Postgres, y este caia al branch de source_path y devolvia 404 — el modo
        #    SILENCIOSO, que es el peor: parece "no existe" cuando en realidad el
        #    formato del id no era el esperado.
        #    Se exige >=8 chars para no matchear medio corpus con 2 letras.
        pref = str(ref or "").strip()
        if len(pref) >= 8 and _re.fullmatch(r'[0-9a-fA-F-]+', pref):
            cur.execute("SELECT id FROM chunks_meta WHERE id::text LIKE %s LIMIT 2",
                        (pref + "%",))
            hits = cur.fetchall()
            if len(hits) == 1:
                return str(hits[0]["id"])
            if len(hits) > 1:
                # Ambiguo: error EXPLICITO en vez de elegir uno al azar o devolver
                # 404. Devolver 404 aca haria creer que no existe cuando existen VARIOS.
                raise HTTPException(
                    400,
                    f"prefijo de chunk ambiguo: {pref!r} matchea mas de un chunk. "
                    f"Usa el uuid completo o el source_path.")
        return None


@app.post("/memory/supersede")
@limiter.limit(lambda: RATE_LIMIT_ADD)
def supersede(request: Request, req: SupersedeRequest, agent: str = Depends(verify_agent)):
    """Sprint 1: marca la memoria `old` como reemplazada por `new` (NO borra).
    old/new aceptan chunk_id o source_path. La vieja queda con valid_until + superseded_by
    y deja de aparecer en search por default (include_superseded=true la recupera).

    AUTORIZACION POR PROPIEDAD (2026-08-05, tarea 84f79c6f paso 1):
    Antes este endpoint NO tenia NINGUN control: cualquier key valida podia jubilar
    la memoria de CUALQUIER agente. Es la operacion mas sensible despues de un
    borrado, porque decide que DEJA DE VER la flota (no borra, pero el efecto sobre
    el lector es equivalente a ocultar).
    Regla, coherente con la politica de vault de Otixe (2026-08-05):
      · lo PROPIO (agent_owner == llamador) => permitido siempre.
      · lo AJENO => solo LIDERES (los mismos que pueden escribir el vault de otro
        equipo). Un subagente no jubila memoria de otro agente.
    Hallazgo original: Sentinel (auditora de Merlin), ampliado por saruman."""
    old_id = _resolve_chunk_id(req.old)
    if not old_id:
        raise HTTPException(404, f"old memory not found: {req.old!r}")
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_path FROM chunks_meta WHERE id=%s", (old_id,))
        old_row = cur.fetchone()
    if not old_row or office_denied(agent, old_row["source_path"]):
        raise HTTPException(404, f"old memory not found: {req.old!r}")
    new_id = _resolve_chunk_id(req.new)
    if not new_id:
        raise HTTPException(404, f"new memory not found: {req.new!r}")
    if old_id == new_id:
        raise HTTPException(400, "old and new resolve to the same chunk")
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_path FROM chunks_meta WHERE id=%s", (new_id,))
        new_row = cur.fetchone()
    if not new_row or office_denied(agent, new_row["source_path"]):
        raise HTTPException(404, f"new memory not found: {req.new!r}")
    # --- Gate de propiedad: va DESPUES de resolver los ids (necesita el owner de la
    #     fila) y ANTES de mutar. Fail-OPEN si no se puede determinar el owner: no
    #     se bloquea una operacion legitima por una consulta que fallo.
    try:
        _owner = db.chunk_owner(old_id)
    except Exception:
        _owner = None
    if _owner and _owner != agent:
        try:
            _es_lider = db.is_lead(agent)
        except Exception:
            _es_lider = False
        if not _es_lider:
            raise HTTPException(
                403,
                f"memoria de otro agente ('{_owner}'): supersede de memoria ajena "
                f"esta reservado a lideres. Podes supersedear la tuya.")
    result = db.supersede_chunk(old_id, new_id)
    if not result:
        raise HTTPException(404, f"old memory not found: {req.old!r}")
    request.state.audit = {"action": "supersede", "chunk_id": old_id, "query": f"{old_id}->{new_id}"}
    return {"superseded": old_id, "superseded_by": new_id, "valid_until": result.get("valid_until")}


@app.get("/memory/by-source")
def by_source(request: Request, path: str = Query(...), agent: str = Depends(verify_agent)):
    if not _path_in_scope(agent, path):
        raise HTTPException(403, "fuera de tu scope")
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("""SELECT id, source_path, chunk_index, title, tags, sensitivity, agent_owner,
                       created_at, updated_at, deleted_at, text_preview
                       FROM chunks_meta WHERE source_path=%s ORDER BY chunk_index""",
                    (path,))
        rows = [dict(r) for r in cur.fetchall()]
    rows = filter_hits(agent, rows)
    request.state.audit = {"action": "by-source", "query": path, "result_count": len(rows)}
    return {"chunks": rows, "count": len(rows)}


# --------------------------------------------------------------------------
# Vault write - escritura estandar de archivos .md al vault (atribuida + auditada)
# --------------------------------------------------------------------------
import os as _os
import logging

VAULT_ROOT = _os.environ.get("VAULT_ROOT", "/vault")
VAULT_WRITE_BASE = _os.path.join(VAULT_ROOT, "obsidian-vault")
# Must be a dedicated persistent bind mount outside /vault.  No tmp/overlay fallback.
VAULT_BACKUP_ROOT = _os.environ.get("VAULT_BACKUP_ROOT", "")

# Logger de telemetria del vault: WARN cuando se normaliza un path del cliente
# (mismo criterio que el WARN de secretos: no rompe, deja rastro del culpable).
_vault_log = logging.getLogger("cerebro.vault")


class VaultWriteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str
    overwrite: bool = True
    # Strong SHA-256 entity tag returned by GET /vault/read. Optional preserves
    # legacy blind overwrite callers; wildcard and weak validators have no CAS
    # semantics here and are deliberately rejected when supplied.
    if_match: Optional[str] = None


_ETAG_RE = _re.compile(r'^"[0-9a-f]{64}"$')


def _etag_for_bytes(data: bytes) -> str:
    """Return the strong, quoted SHA-256 validator for exact stored bytes."""
    return '"' + hashlib.sha256(data).hexdigest() + '"'


def _require_valid_if_match(if_match: Optional[str]) -> None:
    if if_match is not None and not _ETAG_RE.fullmatch(if_match):
        raise HTTPException(422, "if_match must be a quoted lowercase SHA-256 ETag")


class VaultRestoreRequest(BaseModel):
    path: str = Field(..., min_length=1)
    version: str = Field(..., min_length=48, max_length=48)


def _resolve_within_vault(path: str, *, require_md: bool) -> str:
    """Resolve one vault request to its canonical filesystem target.

    Relative paths accept the historical ``obsidian-vault/`` compatibility
    prefix.  An absolute path is accepted only when its *resolved* target is
    inside ``VAULT_WRITE_BASE``.  The returned realpath is consequently the
    single value suitable for both filesystem access and authorization.
    """
    base = _os.path.realpath(VAULT_WRITE_BASE)
    p = (path or "").replace("\\", "/")
    if _os.path.isabs(p):
        candidate, changed = p, False
    else:
        rel, changed = _strip_redundant_vault_prefix(p)
        candidate = _os.path.join(base, rel)
    real = _os.path.realpath(candidate)
    try:
        inside = _os.path.commonpath((base, real)) == base
    except ValueError:  # Different drives/platform-invalid mix: never authorize.
        inside = False
    if not inside:
        # Deliberately constant: do not reflect request paths or filesystem state.
        raise HTTPException(403, "path outside vault")
    if require_md and not real.lower().endswith(".md"):
        raise HTTPException(400, "solo se permiten archivos .md")
    if changed:
        _vault_log.warning(
            "vault path normalizado (prefijo redundante removido): entrada=%r -> %r",
            path, real,
        )
    return real


def _vault_path_in_scope(agent: str, real: str, write: bool = False) -> bool:
    """Authorize a canonical vault target without reinterpreting raw input.

    ``_path_in_scope`` remains the compatibility API for memory/source paths.
    Vault endpoints must use this helper after ``_resolve_within_vault`` so an
    accepted absolute target cannot become ``base/base/...`` during authz.
    """
    base = _os.path.realpath(VAULT_WRITE_BASE)
    real = _os.path.realpath(real)
    try:
        if _os.path.commonpath((base, real)) != base:
            return False
    except ValueError:
        return False
    return _path_in_scope(agent, _os.path.relpath(real, base), write=write)


def _resolve_vault_path(path: str) -> str:
    """Resuelve path (relativo a obsidian-vault o absoluto) y valida que quede
    DENTRO de /vault/obsidian-vault y que sea .md. Bloquea traversal (..)."""
    return _resolve_within_vault(path, require_md=True)


@app.post("/vault/write")
@limiter.limit(lambda: RATE_LIMIT_ADD)
def vault_write(request: Request, req: VaultWriteRequest, agent: str = Depends(verify_agent)):
    """Crea o sobreescribe un archivo .md en el vault. owner = agente de la key
    (anti-spoofing). El indexer (watchdog) lo indexa solo."""
    real = _resolve_vault_path(req.path)
    if not _vault_path_in_scope(agent, real, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    rel = _os.path.relpath(real, _os.path.realpath(VAULT_ROOT))
    # Reject an unsafe configured root before mkdir/access/lock changes anything.
    try:
        store = (vault_backups.VaultBackupStore(
            VAULT_BACKUP_ROOT, vault_root=VAULT_ROOT, vault_write_base=VAULT_WRITE_BASE
        ) if VAULT_BACKUP_ROOT else None)
    except vault_backups.BackupStoreError as exc:
        request.state.audit = {"action": "vault_write", "query": rel, "error": "unsafe_backup_root"}
        raise HTTPException(500, "vault backup store is unsafe or unavailable") from exc
    # Validate only after authentication + scope checks so denied callers cannot
    # use a token probe to distinguish paths or validators.
    _require_valid_if_match(req.if_match)
    # --- Private-blocks: NO deben llegar a disco/indice ---
    content, n_priv = sanitize.redact_private_blocks(req.content)
    # --- Secretos: por ahora solo WARN (no se modifica el archivo mas alla de private-blocks) ---
    warnings: list[str] = []
    sec_findings = sanitize.scan_secrets(content)
    if n_priv:
        warnings.append(f"private block(s) redacted: {n_priv}")
    if sec_findings:
        kinds = ", ".join(sorted({s["kind"] for s in sec_findings}))
        msg = f"WARN vault_write: potential secret(s) in {rel}: {kinds} (count={len(sec_findings)}) [NOT modified]"
        warnings.append(msg)
        print(msg, flush=True)
    data = content.encode("utf-8")
    existed = _os.path.isfile(real)
    try:
        if store:
            # Lock before the existence/read decision: all gateway writers share one
            # cooperative critical section for this normalized real path.
            with store.lock(real):
                existed = _os.path.isfile(real)
                # Read and compare under the same cooperative flock as backup and
                # replacement: no check-then-write window between ETag validation
                # and the durable overwrite transaction.
                previous = None
                if existed:
                    with open(real, "rb") as previous_file:
                        previous = previous_file.read()
                current_etag = _etag_for_bytes(previous) if previous is not None else None
                if req.if_match is not None and req.if_match != current_etag:
                    request.state.audit = {"action": "vault_write", "query": rel, "error": "etag_conflict"}
                    return JSONResponse(status_code=409, content={
                        "ok": False, "path": rel, "etag": current_etag,
                        "detail": "vault version conflict; re-read before writing",
                    })
                if existed and not req.overwrite:
                    request.state.audit = {"action": "vault_write", "query": rel, "error": "exists"}
                    raise HTTPException(409, "el archivo ya existe y overwrite=false")
                # `data` is the sanitized UTF-8 representation that will be
                # handed to replace_bytes.  Derive the success validator here,
                # inside the same lock/transaction, rather than by rereading a
                # file that another cooperative writer could already change.
                stored_etag = _etag_for_bytes(data)
                _os.makedirs(_os.path.dirname(real), exist_ok=True)
                if existed:
                    mode = _os.stat(real).st_mode
                    store.backup_bytes(real, previous)
                    vault_backups.replace_bytes(real, data, mode)
                else:
                    vault_backups.replace_bytes(real, data, None)
        else:
            # Keep legacy creation behavior without a store, but an ETag can only
            # match an existing representation; a missing target is a conflict.
            existed = _os.path.isfile(real)
            if req.if_match is not None:
                request.state.audit = {"action": "vault_write", "query": rel, "error": "etag_conflict"}
                return JSONResponse(status_code=409, content={
                    "ok": False, "path": rel, "etag": None,
                    "detail": "vault version conflict; re-read before writing",
                })
            if existed and not req.overwrite:
                request.state.audit = {"action": "vault_write", "query": rel, "error": "exists"}
                raise HTTPException(409, "el archivo ya existe y overwrite=false")
            if existed:
                raise vault_backups.BackupStoreError("VAULT_BACKUP_ROOT is required for overwrites")
            # There is no existing representation in this legacy create-only
            # path.  Hash the exact sanitized bytes passed to replace_bytes.
            stored_etag = _etag_for_bytes(data)
            _os.makedirs(_os.path.dirname(real), exist_ok=True)
            vault_backups.replace_bytes(real, data, None)
    except vault_backups.DurabilityUncertainError as exc:
        request.state.audit = {"action": "vault_write", "query": rel, "error": "durability_uncertain"}
        raise HTTPException(500, "write completed but final durability confirmation failed") from exc
    except vault_backups.BackupStoreError as exc:
        request.state.audit = {"action": "vault_write", "query": rel, "error": "backup_or_write_failed"}
        raise HTTPException(500, "vault write failed before replacing the original") from exc
    request.state.audit = {"action": "vault_overwrite" if existed else "vault_create", "query": rel, "private_redacted": n_priv, "secrets_detected": len(sec_findings)}
    return JSONResponse(
        content={"ok": True, "path": rel, "owner": agent, "bytes": len(data),
                 "warnings": warnings, "etag": stored_etag},
        headers={"ETag": stored_etag},
    )


def _resolve_vault_read_path(path: str) -> str:
    """Resuelve path para LECTURA: dentro de /vault/obsidian-vault, bloquea traversal.
    No exige .md (permite leer cualquier doc del vault)."""
    return _resolve_within_vault(path, require_md=False)


@app.get("/vault/read")
@limiter.limit(lambda: RATE_LIMIT_VAULT_READ)
def vault_read(request: Request, path: str = Query(..., min_length=1), agent: str = Depends(verify_agent)):
    """Lee un archivo del vault (bajo obsidian-vault) y devuelve su contenido crudo.
    owner = agente de la key (auditado). Bloquea traversal; cap 2 MB."""
    real = _resolve_vault_read_path(path)
    if not _vault_path_in_scope(agent, real):
        raise HTTPException(403, "fuera de tu scope")
    rel = _os.path.relpath(real, _os.path.realpath(VAULT_ROOT))
    if not _os.path.isfile(real):
        request.state.audit = {"action": "vault_read", "query": rel, "error": "notfound"}
        raise HTTPException(404, "archivo no encontrado en el vault")
    size = _os.path.getsize(real)
    if size > 2 * 1024 * 1024:
        raise HTTPException(413, f"archivo demasiado grande ({size} bytes, max 2MB)")
    with open(real, "rb") as f:
        raw = f.read()
    # Hash exact on-disk bytes, not decoded/re-encoded text. Replacement decoding
    # is presentation-only and must never make a conditional writer's validator.
    etag = _etag_for_bytes(raw)
    content = raw.decode("utf-8", errors="replace")
    request.state.audit = {"action": "vault_read", "query": rel}
    return JSONResponse(content={"ok": True, "path": rel, "bytes": len(raw), "content": content, "etag": etag},
                        headers={"ETag": etag})


@app.get("/vault/backups")
@limiter.limit(lambda: RATE_LIMIT_SEARCH)
def vault_backups_list(request: Request, path: str = Query(..., min_length=1), agent: str = Depends(verify_agent)):
    """List metadata only for one authorized vault path; never expose backup bytes."""
    real = _resolve_vault_path(path)
    # Both normal authorization checks happen before touching the backup store,
    # preventing denied callers from learning whether a path has history.
    if not _vault_path_in_scope(agent, real) or not _vault_path_in_scope(agent, real, write=True):
        raise HTTPException(403, "fuera de tu scope")
    rel = _os.path.relpath(real, _os.path.realpath(VAULT_ROOT))
    try:
        versions = vault_backups.VaultBackupStore(
            VAULT_BACKUP_ROOT, vault_root=VAULT_ROOT, vault_write_base=VAULT_WRITE_BASE
        ).list_versions(real)
    except vault_backups.BackupStoreError as exc:
        request.state.audit = {"action": "vault_backups_list", "query": rel, "error": "store_failed"}
        raise HTTPException(500, "backup store unavailable") from exc
    request.state.audit = {"action": "vault_backups_list", "query": rel, "count": len(versions)}
    return {"ok": True, "path": rel, "versions": versions}


@app.post("/vault/restore")
@limiter.limit(lambda: RATE_LIMIT_ADD)
def vault_restore(request: Request, req: VaultRestoreRequest, agent: str = Depends(verify_agent)):
    """Restore one verified version, backing up the current bytes first."""
    real = _resolve_vault_path(req.path)
    if not _vault_path_in_scope(agent, real) or not _vault_path_in_scope(agent, real, write=True):
        raise HTTPException(403, "fuera de tu scope")
    rel = _os.path.relpath(real, _os.path.realpath(VAULT_ROOT))
    try:
        store = vault_backups.VaultBackupStore(
            VAULT_BACKUP_ROOT, vault_root=VAULT_ROOT, vault_write_base=VAULT_WRITE_BASE
        )
        with store.lock(real):
            # Validate historical bytes before touching the current target.
            historical = store.read_version(real, req.version)
            if not _os.path.isfile(real):
                raise vault_backups.BackupCorruptError("restore target is absent")
            with open(real, "rb") as previous_file:
                previous = previous_file.read()
            mode = _os.stat(real).st_mode
            store.backup_bytes(real, previous)
            vault_backups.replace_bytes(real, historical, mode)
    except vault_backups.DurabilityUncertainError as exc:
        request.state.audit = {"action": "vault_restore", "query": rel, "error": "durability_uncertain"}
        raise HTTPException(500, "restore replaced the file but final durability confirmation failed") from exc
    except vault_backups.BackupCorruptError as exc:
        request.state.audit = {"action": "vault_restore", "query": rel, "error": "invalid_version"}
        raise HTTPException(404, "backup version is unavailable or failed integrity validation") from exc
    except vault_backups.BackupStoreError as exc:
        request.state.audit = {"action": "vault_restore", "query": rel, "error": "backup_or_restore_failed"}
        raise HTTPException(500, "restore failed before replacing the original") from exc
    request.state.audit = {"action": "vault_restore", "query": rel, "version": req.version}
    return {"ok": True, "path": rel, "version": req.version, "bytes": len(historical)}


# --------------------------------------------------------------------------
# SKILLS: lectura integra (skill_get) + references. ADITIVO PURO (sin DDL).
# skill_get espeja GET /vault/read: valida scope, resuelve, devuelve content COMPLETO.
# El truncado historico de skills era el slice [:800] de presentacion del wrapper MCP
# skill_search; ese slice NO se toca. Esto es un camino de lectura alternativo.
# --------------------------------------------------------------------------
class AddSkillReferenceRequest(BaseModel):
    skill: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    content: str
    tags: list[str] = []


@app.get("/skills/list")
@limiter.limit(lambda: RATE_LIMIT_SEARCH)
def skills_list(request: Request,
                q: str = Query("", description="substring (nombre/title/texto), case-insensitive"),
                tag: str = Query("", description="tag exacto de chunks_meta.tags"),
                limit: int = Query(50, ge=1, le=500),
                offset: int = Query(0, ge=0),
                include_references_count: bool = Query(False),
                agent: str = Depends(verify_agent)):
    """ENUMERA los skills vigentes del registro compartido (descubrimiento por catalogo,
    complemento de skill_search que es por relevancia semantica).

    Un skill es la fila raiz `source_path='skills/<name>'`. Los references hijos
    (`skills/<name>/references/<archivo>`) NO son skills y quedan EXCLUIDOS.
    DEDUP: una entrada por skill aunque este chunkeado en varias filas (se reporta
    chunk_count). Devuelve name/title/description/tags + paginacion (limit/offset,
    total, has_more) y filtros opcionales q (substring) / tag.

    Auth: MISMO patron de lectura que /skills/get (verify_agent + scope del namespace
    'skills/'); con RBAC de vault, un agente read-only con scope de lectura la puede usar.
    Solo SELECT: no escribe nada."""
    if not _path_in_scope(agent, "skills/"):
        raise HTTPException(403, "fuera de tu scope")
    out = db.list_skills(q=q, tag=tag, limit=limit, offset=offset,
                         include_references_count=include_references_count)
    request.state.audit = {"action": "skill_list",
                           "query": f"q={q or '-'} tag={tag or '-'} limit={limit} offset={offset}"[:500],
                           "result_count": out["count"]}
    return {"ok": True, **out}


@app.get("/skills/get")
@limiter.limit(lambda: RATE_LIMIT_SEARCH)
def skills_get(request: Request, name: str = Query(..., min_length=1),
               include_references: bool = Query(True),
               agent: str = Depends(verify_agent)):
    """Devuelve el contenido INTEGRO (sin truncar) de una skill o de uno de sus references.
    `name` acepta 3 formas (ronda 2 FIX-4, contrato oficial):
      - '<skill>'                        -> la skill raiz
      - 'skills/<skill>'                 -> idem (prefijo tolerado)
      - '<skill>/references/<archivo>'   -> el contenido de ESE reference
    Se normaliza antes de construir el source_path (unquote + rechazo de control chars).
    La respuesta declara 'kind' ('skill'|'reference') y 'parent_skill' cuando es reference.
    include_references (default true) ENUMERA los references hijos (paths + tamanos),
    NO inlinea su contenido; para una skill sin references la lista es vacia, y para un
    reference no aplica (no tiene hijos)."""
    try:
        norm = db.normalize_skill_name(name)
    except ValueError as e:
        request.state.audit = {"action": "skill_get", "query": (name or "")[:500], "error": "invalid"}
        raise HTTPException(400, f"nombre de skill invalido: {e}")
    source_path = f"skills/{norm}"
    if not _path_in_scope(agent, source_path):
        raise HTTPException(403, "fuera de tu scope")
    data = db.get_skill_full(norm, include_references=include_references)
    _what = "reference" if "/references/" in norm else "skill"
    if data is None:
        request.state.audit = {"action": "skill_get", "query": source_path, "error": "notfound"}
        raise HTTPException(404, f"{_what} '{norm}' no existe en el registro")
    if data.get("soft_deleted"):
        request.state.audit = {"action": "skill_get", "query": source_path, "error": "soft_deleted"}
        raise HTTPException(404, f"{_what} '{norm}' existe pero esta retirado (soft-deleted / no vigente)")
    request.state.audit = {"action": "skill_get", "query": source_path,
                           "result_count": data["chunk_count"],
                           "kind": data.get("kind")}
    return {
        "ok": True,
        "name": norm,
        "source_path": source_path,
        "kind": data.get("kind", "skill"),
        "parent_skill": data.get("parent_skill"),
        "title": data["title"],
        "content": data["content"],
        "chars": data["chars"],
        "chunk_count": data["chunk_count"],
        "metadata": data["metadata"],
        "references": data["references"],
        "references_count": data["references_count"],
    }


@app.post("/skills/add-reference")
@limiter.limit(lambda: RATE_LIMIT_ADD)
def skills_add_reference(request: Request, req: AddSkillReferenceRequest,
                         agent: str = Depends(verify_agent)):
    """Agrega un reference a una skill existente como chunk(s) hijo namespaceado:
    source_path = skills/<skill>/references/<filename>. Valida que la skill raiz exista
    (404 si no, para no dejar references huerfanos). Chunkea si supera chunker.MAX_CHARS.
    Idempotente por (source_path, chunk_index).
    Tags asignados (ronda 2 FIX-3): ['skill-reference', 'skill:<skill>'] + los del caller.
    SIN el tag 'skill': skill_search filtra por ese tag y debe listar solo skills raiz."""
    try:
        skill = db.normalize_skill_name(req.skill)
    except ValueError as e:
        raise HTTPException(400, f"nombre de skill invalido: {e}")
    try:
        fname = db.normalize_reference_filename(req.filename)
    except ValueError as e:
        request.state.audit = {"action": "skill_add_reference",
                               "query": (req.filename or "")[:500], "error": "invalid_filename"}
        raise HTTPException(400, f"filename invalido: {e}")
    source_path = f"skills/{skill}/references/{fname}"
    # defensa en profundidad: el path construido DEBE seguir dentro del namespace
    if not source_path.startswith(f"skills/{skill}/references/") or ".." in source_path:
        raise HTTPException(400, "path de reference fuera del namespace de la skill")
    # C3 (fix traversal 2026-08-10): misma guarda que las otras dos superficies de
    # escritura. Aca el path es CONSTRUIDO, asi que es redundante por diseno -- se
    # aplica igual para que la invariante sea una sola y no dependa de que
    # normalize_skill_name/normalize_reference_filename no cambien de contrato.
    _require_safe_source_path(source_path)
    if not _path_in_scope(agent, source_path, write=True):
        raise HTTPException(403, "fuera de tu scope de escritura")
    if not db.skill_exists(skill):
        request.state.audit = {"action": "skill_add_reference", "query": source_path,
                               "error": "skill_notfound"}
        raise HTTPException(404, f"la skill '{skill}' no existe: no se crean references huerfanos")

    # SUPERFICIE C (Otixe, 2026-08-10): un reference es contenido operativo de la
    # skill (de hecho la via recomendada para skills grandes es partirlas en
    # references), asi que le aplica la MISMA politica que a la skill raiz: si el
    # destino cae bajo `skills/`, se escanea para AVISAR pero NO se degrada.
    # `source_path` aca siempre arranca con 'skills/', pero se consulta el helper
    # igual (una sola fuente de verdad, y defensa si el prefijo cambia).
    is_skill = _is_skill_path(source_path)
    clean, report = sanitize.sanitize(req.content)
    warnings: list[str] = []
    sensitivity = "normal"
    # Mismo invariante que en /memory/add-markdown: lo que se PERSISTE es lo que
    # se EMBEDDEA. Para una skill se parte del ORIGINAL (conserva la credencial)
    # pero C4 (2026-08-10) le aplica SOLO la redaccion de bloques <private>.
    if is_skill:
        content_to_persist, _priv_n = sanitize.redact_private_blocks(req.content)
    else:
        content_to_persist = clean
    if report["secrets"]:
        kinds = ", ".join(sorted({s["kind"] for s in report["secrets"]}))
        if is_skill:
            warnings.append(
                f"secret(s) detected but KEPT (skill path): {kinds} "
                f"(count={len(report['secrets'])})"
            )
        else:
            sensitivity = "secret"
            warnings.append(f"secret(s) redacted before persist: {kinds} (count={len(report['secrets'])})")
    if report["private_redacted"]:
        # C4: identico en skill y no-skill; el bloque <private> se redacta SIEMPRE.
        warnings.append(f"private block(s) redacted: {report['private_redacted']}")

    # Ronda 2 FIX-3: SIN el tag 'skill'. skill_search filtra tags=['skill'] y debe
    # devolver SOLO skills raiz; con el tag, cada reference invadia el descubrimiento
    # de skills (con 32 references de una skill el top-k se llenaba de fragmentos).
    # Los references siguen siendo recuperables por memory_search y por skill_get(...).references.
    base_tags = ["skill-reference", f"skill:{skill}"]
    tags = base_tags + [t for t in (req.tags or []) if t and t not in base_tags]
    src_hash = hashlib.sha256(content_to_persist.encode()).hexdigest()
    title = fname
    ids: list[str] = []
    if len(content_to_persist) > chunker.MAX_CHARS:
        chunks = chunker.chunk_markdown(content_to_persist, doc_title=title)
        for c in chunks:
            vec = embedder.embed(chunker.enrich_text(c))
            ids.append(db.upsert_chunk(
                chunk_id=None, source_path=source_path, source_hash=src_hash,
                chunk_index=c.index, title=c.title or title, breadcrumb=c.breadcrumb,
                text=c.text, vector=vec, tags=tags, agent_owner=agent,
                sensitivity=sensitivity, category="general",
            ))
    else:
        vec = embedder.embed(title + "\n\n" + content_to_persist)
        ids.append(db.upsert_chunk(
            chunk_id=None, source_path=source_path, source_hash=src_hash,
            chunk_index=0, title=title, breadcrumb=[title],
            text=content_to_persist, vector=vec, tags=tags, agent_owner=agent,
            sensitivity=sensitivity, category="general",
        ))
    # FIX 2 (2026-08-04): mismo pruning que en /memory/add-markdown. Critico aca
    # porque un reference que pasa de >MAX_CHARS (N chunks) a corto (1 chunk)
    # dejaba N-1 fragmentos viejos vivos bajo el mismo source_path.
    pruned = db.prune_leftover_chunks(source_path, len(ids))
    if pruned:
        warnings.append(f"pruned {pruned} orphan chunk(s) with chunk_index >= {len(ids)}")
    request.state.audit = {"action": "skill_add_reference", "query": source_path,
                           "result_count": len(ids),
                           "pruned": pruned,
                           "secrets_redacted": 0 if is_skill else len(report["secrets"]),
                           "secrets_kept_skill": len(report["secrets"]) if is_skill else 0,
                           "private_redacted": report["private_redacted"]}
    return {"ok": True, "skill": skill, "filename": fname, "source_path": source_path,
            "chunk_ids": ids, "chunk_count": len(ids), "pruned": pruned,
            "chars": len(content_to_persist),
            "owner": agent, "tags": tags, "warnings": warnings}


# --------------------------------------------------------------------------
# Self-service de gestion de agentes / keys (solo LIDERES)
# --------------------------------------------------------------------------
class ProvisionRequest(BaseModel):
    name: str = Field(..., min_length=1)
    team: Optional[str] = None  # ignorado por seguridad: team = caller (lider)
    # Solo lo consume /agents/provision-leader: crear un LIDER exige autorizacion
    # explicita de Otixe (politica 2026-08-05). Para /agents/provision (subagente)
    # se ignora: un lider provisiona los suyos sin pedir permiso.
    authorized_by: Optional[str] = None


class RevokeRequest(BaseModel):
    name: str = Field(..., min_length=1)


def _require_leader(agent: str):
    """403 si el caller no es lider. (verify_agent ya garantizo 401 si no hay key)."""
    if not db.is_lead(agent):
        raise HTTPException(403, "solo los lideres pueden gestionar agentes/keys")


@app.post("/agents/provision")
@limiter.limit(lambda: RATE_LIMIT_PROVISION)
def agents_provision(request: Request, req: ProvisionRequest, agent: str = Depends(verify_agent)):
    """Un LIDER provisiona una key para un subagente de SU team. Genera un token
    fuerte (secrets.token_urlsafe(32)), guarda sha256 en `agents` y lo devuelve UNA
    sola vez. El token NUNCA se loguea. name activo -> 409; nombre lider -> 403."""
    request.state.audit = {"action": "agent_provision", "query": f"provision {req.name}"}
    _require_leader(agent)
    token = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    status = db.provision_agent(req.name, key_hash, team=agent, created_by=agent)
    if status == "is_leader":
        raise HTTPException(403, "no se puede provisionar un nombre que es lider")
    if status == "exists":
        raise HTTPException(409, "ya existe un agente activo con ese nombre")
    request.state.audit = {"action": "agent_provision", "query": f"provisioned {req.name} team={agent}"}
    # 'key' se entrega UNA sola vez; jamas se persiste ni se loguea en claro.
    return {"ok": True, "name": req.name, "team": agent, "key": token}


# ---------------------------------------------------------------------------
# ADMINISTRACION DE execution_class POR API (tarea 6a611e35, pedido de rudeus).
# Antes NO se podia ni LEER ni SETEAR la clase que gobierna el RBAC: habia que
# entrar por SQL directo al Postgres del LXC 101, que es la DB COMPARTIDA de los
# 5 equipos. Eso es propenso a error, no queda auditado, y creaba un cuello: cada
# vez que un lider provisionaba un subagente, otro tenia que correr el UPDATE.
# Caso testigo (2026-08-05): NERO nacio con clase NULL = fail-closed = read-only,
# o sea SIN poder hacer task_review, que es su unica funcion.
#
# POLITICA (Otixe 2026-08-05, textual): "si la idea es que un lider pueda cambiar
# la clase de sus agentes, pero no puede cambiar la clase de agentes que no son de
# su grupo".
#   · LECTURA: cualquier lider ve la clase de cualquier agente (coherente con la
#     politica de vault: lectura abierta, escritura acotada).
#   · ESCRITURA: un lider solo cambia la clase de agentes de SU team. Otro team -> 403.
#   · A un LIDER no se le cambia la clase por API: es orchestrator por `is_lead`.
# ---------------------------------------------------------------------------
class ExecClassRequest(BaseModel):
    name: str = Field(..., min_length=1)
    execution_class: str = Field(..., min_length=1)


@app.get("/agents/execution-class")
def agents_exec_class_get(request: Request, name: str = Query(""),
                          agent: str = Depends(verify_agent)):
    """Lee la execution_class. Sin `name` devuelve TODA la flota (lectura abierta a
    lideres); con `name`, solo ese agente. Incluye la clase EFECTIVA (resuelta con
    el mismo fail-closed que usa el RBAC), no solo la columna cruda: si son
    distintas, el agente esta sin backfill."""
    request.state.audit = {"action": "agent_exec_class_get", "query": name or "(todos)"}
    _require_leader(agent)
    rows = db.list_agents_exec_class(name or None)
    for r in rows:
        r["effective"] = resolve_execution_class(r["name"])
        r["needs_backfill"] = (not r.get("execution_class")) and not r.get("is_lead")
    return {"agents": rows, "count": len(rows)}


@app.post("/agents/execution-class")
@limiter.limit(lambda: RATE_LIMIT_PROVISION)
def agents_exec_class_set(request: Request, req: ExecClassRequest,
                          agent: str = Depends(verify_agent)):
    """Un LIDER setea la execution_class de un agente de SU team."""
    request.state.audit = {"action": "agent_exec_class_set",
                           "query": f"{req.name} -> {req.execution_class}"}
    _require_leader(agent)
    cls = (req.execution_class or "").strip().lower()
    if cls not in tasks_rbac.EXECUTION_CLASSES:
        raise HTTPException(
            400, f"execution_class invalida '{cls}'; validas: "
                 f"{sorted(tasks_rbac.EXECUTION_CLASSES)}")
    status, before = db.set_agent_exec_class(req.name, cls, caller=agent)
    if status == "not_found":
        raise HTTPException(404, f"agente '{req.name}' no encontrado")
    if status == "is_leader":
        raise HTTPException(
            403, "a un LIDER no se le cambia la clase por API: es 'orchestrator' "
                 "por su flag is_lead")
    if status == "other_team":
        raise HTTPException(
            403, f"'{req.name}' no es de tu equipo: un lider solo cambia la clase de "
                 f"SUS agentes. Pedile al lider de ese equipo que lo haga.")
    return {"ok": True, "name": req.name, "execution_class": cls,
            "before": before, "changed_by": agent}


@app.post("/agents/provision-leader")
@limiter.limit(lambda: RATE_LIMIT_PROVISION)
def agents_provision_leader(request: Request, req: ProvisionRequest, agent: str = Depends(verify_agent)):
    """Un LIDER provisiona una key para un NUEVO LIDER con equipo propio.

    ⚠️ REQUIERE AUTORIZACION EXPLICITA DE OTIXE (politica 2026-08-05, textual: "un
    lider puede crear otros lideres pero solo con autorizacion explicita mia").
    ANTES cualquier lider podia crear otros lideres libremente — la propia docstring
    lo declaraba como capacidad normal. Un lider nuevo nace `orchestrator` (todas las
    operaciones del tablero) y con capacidad de crear MAS lideres, o sea era una
    escalada de privilegios en cadena sin control humano.
    AHORA hay que pasar `authorized_by='otixe'` en el body. Es una salvaguarda
    DELIBERADA, no criptografica: quien tiene la key de un lider podria mandar ese
    campo igual. Lo que evita es el caso real — que un agente cree un lider por su
    cuenta "porque le pareció util" — y deja la autorizacion ASENTADA en el audit_log.
    Genera token fuerte, guarda sha256 y lo devuelve UNA sola vez."""
    request.state.audit = {"action": "agent_provision_leader", "query": f"provision-leader {req.name}"}
    _require_leader(agent)
    _authz = (getattr(req, "authorized_by", "") or "").strip().lower()
    if _authz != "otixe":
        raise HTTPException(
            403,
            "crear un LIDER requiere autorizacion explicita de Otixe: pasa "
            "authorized_by='otixe' en el body, y solo si te lo autorizo de verdad. "
            "Un lider nuevo nace orchestrator y puede crear mas lideres.")
    token = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    status = db.provision_leader(req.name, key_hash, created_by=agent)
    if status == "is_leader":
        raise HTTPException(403, "ese nombre ya es un lider canonico")
    if status == "exists":
        raise HTTPException(409, "ya existe un agente activo con ese nombre")
    request.state.audit = {"action": "agent_provision_leader",
                           "query": f"provisioned-leader {req.name} by={agent} authorized_by=otixe"}
    return {"ok": True, "name": req.name, "team": req.name, "is_lead": True, "key": token}


@app.post("/agents/revoke")
@limiter.limit(lambda: RATE_LIMIT_PROVISION)
def agents_revoke(request: Request, req: RevokeRequest, agent: str = Depends(verify_agent)):
    """Un LIDER revoca (soft) un subagente de SU team. active=false + revoked_at=now().
    NO borra datos historicos. Lider -> 403; agente de otro team -> 403; inexistente -> 404."""
    request.state.audit = {"action": "agent_revoke", "query": f"revoke {req.name}"}
    _require_leader(agent)
    status = db.revoke_agent(req.name, caller=agent)
    if status == "not_found":
        raise HTTPException(404, "agente no encontrado")
    if status == "forbidden":
        raise HTTPException(403, "no podes revocar ese agente (lider o de otro equipo)")
    return {"ok": True, "name": req.name, "active": False}


@app.get("/agents/list")
def agents_list(request: Request, agent: str = Depends(verify_agent)):
    """Un LIDER lista los agentes de SU team (name, active, created_at)."""
    request.state.audit = {"action": "agent_list"}
    _require_leader(agent)
    rows = db.list_team_agents(agent)
    for r in rows:
        ca = r.get("created_at")
        r["created_at"] = ca.isoformat() if ca else None
    return {"agents": rows, "count": len(rows)}


# --------------------------------------------------------------------------
# RBAC de tareas por execution_class (FASE A - additive, warn-only por default)
# Spec: 50 Agents/Saruman/Specs/spec-rbac-task-rest-h3-2026-07-28.md (secc.1-3)
#
# `execution_class` = poder de EJECUCION (que operaciones de tarea puede hacer).
# Es ORTOGONAL a `agents.role` (admin|member|guest), que es scope de VAULT.
#
# Anti-spoofing: la clase se deriva SIEMPRE de la key autenticada (verify_agent),
# JAMAS del body. Fail-closed: sin clase declarada => 'read-only'.
#
# Modo: env RBAC_ENFORCE (default 'false' = warn-only). En warn-only una operacion
# no permitida se LOGUEA (agente+op+clase) y SE DEJA PASAR -> el deploy es additive
# y no rompe a ningun agente antes del backfill coordinado (DECISION B de la spec).
# Con RBAC_ENFORCE=true la misma condicion devuelve 403.
# --------------------------------------------------------------------------
import tasks_core
import tasks_rbac

_rbac_log = logging.getLogger("cerebro.rbac")

# FUENTE UNICA (tarea c6b61bd1, 2026-08-05): la matriz y los conjuntos vivian aca
# COMO LITERAL y estaban COPIADOS en mcp/mcp_server.py, con un comentario admitiendo
# que "esta copia DEBE mantenerse en sincronia". Ahora los dos transportes importan
# `common/tasks_rbac.py` (traido a cada imagen con `additional_contexts` de Compose,
# porque gateway/ y mcp/ son build contexts distintos y un COPY ../common es imposible).
# Estos son ALIAS de reexport, NO definiciones: no toques la politica aca, editala en
# common/tasks_rbac.py o los dos transportes vuelven a divergir.
EXECUTION_CLASSES = tasks_rbac.EXECUTION_CLASSES

_CLS_ALL = tasks_rbac.CLS_ALL                    # lectura/comment/checkpoint
_CLS_WRITER_PLUS = tasks_rbac.CLS_WRITER_PLUS     # claim/submit/block/...
_CLS_REVIEW = tasks_rbac.CLS_REVIEW               # INVARIANTE: gate de QA
_CLS_ORCH = tasks_rbac.CLS_ORCH                   # create/update/cancel/link

TASK_RBAC = tasks_rbac.TASK_RBAC


def _rbac_enforcing() -> bool:
    """True si el RBAC de tareas BLOQUEA (403). Default false = warn-only.
    Se lee del entorno en cada llamada para poder activarlo sin recompilar la
    matriz (y para que los tests puedan alternar el modo)."""
    return os.environ.get("RBAC_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


def resolve_execution_class(name: Optional[str]) -> str:
    """Clase de ejecucion efectiva de un agente. Derivada de la KEY, nunca del body.

    Orden: lider (db.is_lead) -> 'orchestrator'; si no, agents.execution_class;
    NULL / valor desconocido / agente sin fila -> 'read-only' (FAIL-CLOSED).
    Nunca devuelve None: el caller siempre recibe una clase valida."""
    if not name:
        return "read-only"
    try:
        if db.is_lead(name):
            return "orchestrator"
    except Exception:
        pass
    try:
        cls = db.agent_execution_class(name)
    except Exception:
        cls = None
    cls = (cls or "").strip().lower()
    return tasks_rbac.normalize_execution_class(cls)


def _require_execution_class(agent: str, allowed, op: str = "") -> str:
    """Gate RBAC de una operacion de tareas. Devuelve la clase resuelta del agente.

    - enforce (RBAC_ENFORCE=true): si la clase no esta en `allowed` -> HTTPException 403.
    - warn-only (default): loguea WARNING con agente+op+clase+permitidas y DEJA PASAR.
    Asi la Fase A es additive: nadie pierde acceso antes del backfill coordinado."""
    cls = resolve_execution_class(agent)
    if cls in allowed:
        return cls
    allowed_txt = ",".join(sorted(allowed))
    if _rbac_enforcing():
        _rbac_log.warning(
            "RBAC deny (enforce): agent=%r op=%r class=%r allowed=%s",
            agent, op, cls, allowed_txt)
        raise HTTPException(
            403, f"execution_class '{cls}' no autorizada para '{op}' (permitidas: {allowed_txt})")
    _rbac_log.warning(
        "RBAC warn-only (habria sido 403): agent=%r op=%r class=%r allowed=%s",
        agent, op, cls, allowed_txt)
    return cls


def _rbac(agent: str, op: str) -> str:
    """Azucar: aplica la matriz TASK_RBAC a la operacion `op`."""
    return _require_execution_class(agent, TASK_RBAC[op], op)


def _task_err(e: Exception) -> HTTPException:
    """Translate the DB approval guard without exposing SQL or committing partial writes."""
    if isinstance(e, psycopg.Error) and e.sqlstate == "PZ101":
        return HTTPException(409, "approval_request_immutable")
    if isinstance(e, tasks_core.TaskError):
        return HTTPException(e.status, e.detail)
    raise e


# Literal inventory from approval-release-20260930/build_sql.py:5,33:
# RESERVED_COLUMNS=('request_kind','version',*cols.keys()). Keep all 15 in sync
# with the DB migration; the CT900 test compares this list to migrated columns.
_APPROVAL_RESERVED_TASK_COLUMNS = frozenset({
    "request_kind", "version", "creator_kind", "action_type", "resource_kind",
    "resource_id", "scope_version", "scope_canonical", "scope_hash",
    "approved_by", "approved_at", "revoked_at", "finished_by",
    "finished_at", "replaces_request_id",
})
# Preserve permissive handling of unrelated legacy extras; reject every approval
# column and protocol-specific approval input (plus approval* names).
_APPROVAL_CREATE_KEYS = _APPROVAL_RESERVED_TASK_COLUMNS | frozenset({
    "request_id", "new_request_id", "scope", "evidence",
    "approval_kind", "approval_type", "expected_hash", "expected_version",
    # status/creator were historically ignored extras; auth still derives creator.
    "effects", "actor", "channel", "decision_reason",
})


def _approval_create_offenders(value):
    if not isinstance(value, dict):
        return []
    return sorted(k for k in value if isinstance(k, str) and
                  (k in _APPROVAL_CREATE_KEYS or k.startswith("approval")))

# ---------- Models de tareas ----------
class TaskCreateRequest(BaseModel):
    # Historical unknown extras remain ignored for backward compatibility, but
    # approval-specific keys may never be silently discarded.
    @model_validator(mode="before")
    @classmethod
    def reject_approval_keys(cls, value):
        if _approval_create_offenders(value):
            raise ValueError("approval_fields_not_allowed")
        return value

    title: str = Field(..., min_length=1)
    description: str = ""
    project: str = ""
    organization: str = ""   # opcional; vacio => el trigger la deriva del project
    priority: str = "medium"
    assignee: str = ""
    subagent: str = ""
    tags: list[str] = []
    parent_id: str = ""
    due_date: str = ""


class TaskUpdateRequest(BaseModel):
    # Esta puerta no puede convertir typos en "actualizada": a diferencia de
    # los modelos historicos, rechaza exclusivamente sus propios extras.
    # No amplia la firma (en particular, `tags` sigue sin ser actualizable).
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(..., min_length=1)
    status: str = ""
    assignee: str = ""
    subagent: str = ""
    priority: str = ""
    project: str = ""
    organization: str = ""   # opcional; explicita gana sobre la derivacion
    title: str = ""
    description: str = ""
    # 2026-08-12 (saruman): task_create aceptaba due_date desde el inicio y
    # task_update NO, asi que una tarea con ventana no se podia re-agendar por API.
    # Lo pidio @rias el 2026-08-08 ("lo vengo necesitando para las tareas con
    # ventana"). La columna `due_date date` ya existia en init.sql:125 y ya se
    # devuelve en los SELECT de list: el unico hueco era la escritura.
    # Formato YYYY-MM-DD. Cadena vacia = no tocar (igual que el resto de campos).
    # "-" o "null" = LIMPIAR la fecha (poner NULL): sin un centinela explicito no
    # habria forma de desagendar una tarea, porque "" ya significa "no tocar".
    due_date: str = ""
    # JSON null/omisión = no tocar; '-' = LIMPIAR explícitamente el padre.
    # Otros textos, incluidos none/null/clear, se validan como IDs y fallan.
    parent_id: str | None = None


class TaskIdRequest(BaseModel):
    task_id: str = Field(..., min_length=1)


class TaskCommentRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)


class TaskNoteRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    note: str = ""


class TaskReviewRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    verdict: str = Field(..., min_length=1)  # pass | needs_changes
    note: str = ""


class TaskHandoffRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    to_agent: str = Field(..., min_length=1)
    note: str = ""


class TaskBlockRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    reason: str = ""


class TaskLinkRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    depends_on_id: str = Field(..., min_length=1)


class TaskCheckpointRequest(BaseModel):
    task_id: str = Field(..., min_length=1)
    step_index: int = 0
    label: str = ""
    state: dict = Field(default_factory=dict)


# ---------- Endpoints de tareas (REST) ----------
@app.post("/task/create")
def task_create_ep(request: Request, req: TaskCreateRequest, agent: str = Depends(verify_agent)):
    """Crea una tarea. creator = agente de la key (anti-spoofing)."""
    request.state.audit = {"action": "task_create", "query": (req.title or "")[:200]}
    _rbac(agent, "task_create")
    try:
        out = tasks_core.create(
            agent, req.title, description=req.description, project=req.project,
            priority=req.priority, assignee=req.assignee, subagent=req.subagent,
            tags=req.tags, parent_id=req.parent_id, due_date=req.due_date,
            organization=req.organization)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)
    request.state.audit = {"action": "task_create", "query": f"task:{out['id']} {req.title}"[:200]}
    return out


@app.get("/task/list")
def task_list_ep(request: Request, status: str = Query(""), project: str = Query(""),
                 assignee: str = Query(""), include_done: bool = Query(False),
                 organization: str = Query(""),
                 limit: int = Query(30, ge=1, le=500), agent: str = Depends(verify_agent)):
    """Lista tareas del tablero (excluye done/cancelled salvo include_done).
    `organization` filtra opcionalmente por organizacion."""
    request.state.audit = {"action": "task_list",
                           "query": f"status={status or '-'} proj={project or '-'} org={organization or '-'}"[:200]}
    _rbac(agent, "task_list")
    out = tasks_core.list_tasks(status=status, project=project, assignee=assignee,
                                include_done=include_done, limit=limit,
                                organization=organization)
    request.state.audit = {"action": "task_list", "result_count": out["count"]}
    return out


@app.get("/task/get")
def task_get_ep(request: Request, task_id: str = Query(..., min_length=1),
                agent: str = Depends(verify_agent)):
    """Detalle de una tarea (id o prefijo) + subtareas."""
    request.state.audit = {"action": "task_get", "query": task_id[:200]}
    _rbac(agent, "task_get")
    try:
        return tasks_core.get(task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/update")
def task_update_ep(request: Request, req: TaskUpdateRequest, agent: str = Depends(verify_agent)):
    """Actualiza campos de una tarea (status/assignee/priority/...)."""
    request.state.audit = {"action": "task_update",
                           "query": f"task:{req.task_id} status={req.status or '-'}"[:200]}
    _rbac(agent, "task_update")
    try:
        return tasks_core.update(
            agent, req.task_id, status=req.status, assignee=req.assignee,
            subagent=req.subagent, priority=req.priority, project=req.project,
            title=req.title, description=req.description,
            organization=req.organization, due_date=req.due_date, parent_id=req.parent_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/claim")
def task_claim_ep(request: Request, req: TaskIdRequest, agent: str = Depends(verify_agent)):
    """Toma una tarea: claimed_by = agente de la key -> in_progress."""
    request.state.audit = {"action": "task_claim", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_claim")
    try:
        return tasks_core.claim(agent, req.task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/comment")
def task_comment_ep(request: Request, req: TaskCommentRequest, agent: str = Depends(verify_agent)):
    """Comentario en el hilo de actividad, atribuido a la key."""
    request.state.audit = {"action": "task_comment", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_comment")
    try:
        return tasks_core.comment(agent, req.task_id, req.body)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/submit")
def task_submit_ep(request: Request, req: TaskNoteRequest, agent: str = Depends(verify_agent)):
    """Entrega la tarea al gate: in_progress -> submitted."""
    request.state.audit = {"action": "task_submit", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_submit")
    try:
        return tasks_core.submit(agent, req.task_id, req.note)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/review")
def task_review_ep(request: Request, req: TaskReviewRequest, agent: str = Depends(verify_agent)):
    """Gate de QA: pass -> done, needs_changes -> in_progress.
    INVARIANTE (spec secc.2/3): SOLO {auditor, orchestrator}. El ejecutor no se
    auto-aprueba. Refuerzo 'claimed_by != caller' queda para la Fase B."""
    request.state.audit = {"action": "task_review",
                           "query": f"task:{req.task_id} {req.verdict}"[:200]}
    _rbac(agent, "task_review")
    try:
        return tasks_core.review(agent, req.task_id, req.verdict, req.note)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/handoff")
def task_handoff_ep(request: Request, req: TaskHandoffRequest, agent: str = Depends(verify_agent)):
    """Deriva la tarea a otro agente/lider y libera el claim."""
    request.state.audit = {"action": "task_handoff",
                           "query": f"task:{req.task_id} -> {req.to_agent}"[:200]}
    _rbac(agent, "task_handoff")
    try:
        return tasks_core.handoff(agent, req.task_id, req.to_agent, req.note)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/block")
def task_block_ep(request: Request, req: TaskBlockRequest, agent: str = Depends(verify_agent)):
    """Marca la tarea blocked con motivo."""
    request.state.audit = {"action": "task_block", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_block")
    try:
        return tasks_core.block(agent, req.task_id, req.reason)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/unblock")
def task_unblock_ep(request: Request, req: TaskNoteRequest, agent: str = Depends(verify_agent)):
    """blocked -> in_progress."""
    request.state.audit = {"action": "task_unblock", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_unblock")
    try:
        return tasks_core.unblock(agent, req.task_id, req.note)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/cancel")
def task_cancel_ep(request: Request, req: TaskIdRequest, agent: str = Depends(verify_agent)):
    """Cancela SOFT (status=cancelled). NUNCA borra (Regla de Oro)."""
    request.state.audit = {"action": "task_cancel", "query": f"task:{req.task_id}"[:200]}
    _rbac(agent, "task_cancel")
    try:
        return tasks_core.cancel(agent, req.task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/link")
def task_link_ep(request: Request, req: TaskLinkRequest, agent: str = Depends(verify_agent)):
    """Encadena tareas: task_id queda bloqueada por depends_on_id."""
    request.state.audit = {"action": "task_link",
                           "query": f"task:{req.task_id} depends_on:{req.depends_on_id}"[:200]}
    _rbac(agent, "task_link")
    try:
        return tasks_core.link(agent, req.task_id, req.depends_on_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.post("/task/unlink")
def task_unlink_ep(request: Request, req: TaskLinkRequest, agent: str = Depends(verify_agent)):
    """Quita SOLO la relacion de dependencia (las tareas quedan intactas).
    Additive respecto de la lista de la spec: existe en MCP y en tasks_core, y sin
    endpoint la entrada 'task_unlink' de TASK_RBAC quedaria muerta."""
    request.state.audit = {"action": "task_unlink",
                           "query": f"task:{req.task_id} depends_on:{req.depends_on_id}"[:200]}
    _rbac(agent, "task_unlink")
    try:
        return tasks_core.unlink(agent, req.task_id, req.depends_on_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.get("/task/deps")
def task_deps_ep(request: Request, task_id: str = Query(..., min_length=1),
                 agent: str = Depends(verify_agent)):
    """blocked_by + blocks de una tarea."""
    request.state.audit = {"action": "task_deps", "query": task_id[:200]}
    _rbac(agent, "task_deps")
    try:
        return tasks_core.deps(task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.get("/tasks/ready")
def tasks_ready_ep(request: Request, project: str = Query(""), assignee: str = Query(""),
                   organization: str = Query(""),
                   agent: str = Depends(verify_agent)):
    """Tareas en 'todo' listas para empezar (sin deps pendientes).
    `organization` filtra opcionalmente por organizacion."""
    request.state.audit = {"action": "tasks_ready",
                           "query": f"proj={project or '-'} org={organization or '-'}"[:200]}
    _rbac(agent, "tasks_ready")
    out = tasks_core.ready(project=project, assignee=assignee, organization=organization)
    request.state.audit = {"action": "tasks_ready", "result_count": out["count"]}
    return out


@app.get("/tasks/inbox")
def tasks_inbox_ep(request: Request, agent_filter: Optional[str] = Query(None, alias="agent"),
                   organization: str = Query(""),
                   caller: str = Depends(verify_agent)):
    """Bandeja de tareas asignadas. Default = owner de la key (param 'agent' consulta a otro).
    `organization` filtra opcionalmente por organizacion."""
    who = (agent_filter or caller)
    request.state.audit = {"action": "tasks_inbox", "query": who[:200]}
    _rbac(caller, "tasks_inbox")
    try:
        out = tasks_core.inbox(who, organization=organization)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)
    request.state.audit = {"action": "tasks_inbox", "query": who[:200], "result_count": out["count"]}
    return out


@app.get("/task/activity")
def task_activity_ep(request: Request, task_id: str = Query(..., min_length=1),
                     agent: str = Depends(verify_agent)):
    """Hilo cronologico de eventos de una tarea."""
    request.state.audit = {"action": "task_activity", "query": task_id[:200]}
    _rbac(agent, "task_activity")
    try:
        out = tasks_core.activity(task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)
    request.state.audit = {"action": "task_activity", "query": task_id[:200],
                           "result_count": out["count"]}
    return out


@app.post("/checkpoint/save")
def task_checkpoint_save_ep(request: Request, req: TaskCheckpointRequest,
                            agent: str = Depends(verify_agent)):
    """Checkpoint de progreso ATADO A UNA TAREA (distinto de /checkpoint/agent-save,
    que es el harvest libre por agente/sesion)."""
    request.state.audit = {"action": "checkpoint_save",
                           "query": f"task:{req.task_id} step={req.step_index}"[:200]}
    _rbac(agent, "checkpoint_save")
    try:
        return tasks_core.checkpoint_save(agent, req.task_id, req.step_index,
                                         req.label, req.state)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.get("/checkpoint/list")
def task_checkpoint_list_ep(request: Request, task_id: str = Query(..., min_length=1),
                            agent: str = Depends(verify_agent)):
    """Checkpoints de una tarea, por step_index."""
    request.state.audit = {"action": "checkpoint_list", "query": task_id[:200]}
    _rbac(agent, "checkpoint_list")
    try:
        out = tasks_core.checkpoint_list(task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)
    request.state.audit = {"action": "checkpoint_list", "query": task_id[:200],
                           "result_count": out["count"]}
    return out


@app.get("/checkpoint/latest")
def task_checkpoint_latest_ep(request: Request, task_id: str = Query(..., min_length=1),
                             agent: str = Depends(verify_agent)):
    """Checkpoint mas avanzado de una tarea (donde retomar)."""
    request.state.audit = {"action": "checkpoint_latest", "query": task_id[:200]}
    _rbac(agent, "checkpoint_latest")
    try:
        return tasks_core.checkpoint_latest(task_id)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


@app.get("/project/summary")
def project_summary_ep(request: Request, project: str = Query(""),
                       organization: str = Query(""),
                       agent: str = Depends(verify_agent)):
    """Conteo de tareas por proyecto y estado + jerarquia org -> proyectos -> conteos."""
    request.state.audit = {"action": "project_summary",
                           "query": f"{project or '*'} org={organization or '*'}"[:200]}
    _rbac(agent, "project_summary")
    out = tasks_core.project_summary(project, organization)
    request.state.audit = {"action": "project_summary", "query": (project or "*")[:200],
                           "result_count": out["count"]}
    return out


# ---------- Organizaciones / Proyectos (jerarquia org -> proyecto -> tarea) ----------
# Agregado 2026-08-04 (tarea 5ae5afd9). El esquema ya estaba migrado en prod; esto
# solo lo EXPONE. Ver tasks_core.organizations_list / projects_list / *_create / *_update.
#
# RBAC: se REUSA `_require_execution_class` (la misma funcion que usa `_rbac`), NO se
# escribe un chequeo nuevo. Crear/editar organizaciones y proyectos es GOBIERNO DEL
# TABLERO, misma clase que task_create => `_CLS_ORCH` (solo orchestrator). Las lecturas
# son `_CLS_ALL` como el resto de los GET de tareas.
#
# Por que un dict aparte y no una entrada en TASK_RBAC: TASK_RBAC es la matriz de la
# spec de RBAC de TAREAS (secc.2) y sus claves estan acopladas a los tests de esa spec.
# Estas operaciones son de otro dominio (catalogo org/proyecto), asi que van en su
# propio mapa, apuntando a los MISMOS conjuntos de clases. Un solo origen de verdad
# para las clases (_CLS_ALL / _CLS_ORCH), dos matrices por dominio.
ORG_RBAC = {
    "organizations_list": _CLS_ALL,
    "projects_list": _CLS_ALL,
    "organization_create": _CLS_ORCH,
    "organization_update": _CLS_ORCH,
    "project_create": _CLS_ORCH,
    "project_update": _CLS_ORCH,
}


def _rbac_org(agent: str, op: str) -> str:
    """Azucar: aplica la matriz ORG_RBAC reusando el gate comun."""
    return _require_execution_class(agent, ORG_RBAC[op], op)


class OrganizationCreateRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    color: str = ""
    sort_order: int = 100


class OrganizationUpdateRequest(BaseModel):
    org_id: str = Field(..., min_length=1)
    name: str = ""
    color: str = ""
    sort_order: Optional[int] = None
    active: Optional[bool] = None


class ProjectCreateRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    org_id: str = Field(..., min_length=1)
    name: str = ""
    status: str = "active"
    sort_order: int = 100


class ProjectUpdateRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    org_id: str = ""          # reasignar el proyecto a otra organizacion
    name: str = ""
    status: str = ""          # active | archived | paused (archivar = NO borrar)
    sort_order: Optional[int] = None
    reassign_tasks: bool = True


@app.get("/organizations")
def organizations_list_ep(request: Request, include_inactive: bool = Query(False),
                          agent: str = Depends(verify_agent)):
    """Organizaciones (id, name, color, sort_order, active) + conteo de proyectos
    y de tareas abiertas. Por defecto solo las activas."""
    request.state.audit = {"action": "organizations_list",
                           "query": f"include_inactive={include_inactive}"}
    _rbac_org(agent, "organizations_list")
    out = tasks_core.organizations_list(include_inactive=include_inactive)
    request.state.audit = {"action": "organizations_list", "result_count": out["count"]}
    return out


@app.get("/projects")
def projects_list_ep(request: Request, org: str = Query(""),
                     include_archived: bool = Query(False),
                     agent: str = Depends(verify_agent)):
    """Proyectos con org_id, name, status y conteos. `org` filtra por organizacion.
    Por defecto EXCLUYE los archivados."""
    request.state.audit = {"action": "projects_list",
                           "query": f"org={org or '*'} archived={include_archived}"[:200]}
    _rbac_org(agent, "projects_list")
    out = tasks_core.projects_list(org=org, include_archived=include_archived)
    request.state.audit = {"action": "projects_list", "result_count": out["count"]}
    return out


@app.post("/organization/create")
def organization_create_ep(request: Request, req: OrganizationCreateRequest,
                           agent: str = Depends(verify_agent)):
    """Crea una organizacion. Idempotente por id (si existe, no la toca)."""
    request.state.audit = {"action": "organization_create", "query": f"org:{req.org_id}"[:200]}
    _rbac_org(agent, "organization_create")
    try:
        return tasks_core.organization_create(agent, req.org_id, req.name,
                                              color=req.color, sort_order=req.sort_order)
    except tasks_core.TaskError as e:
        raise _task_err(e)


@app.post("/organization/update")
def organization_update_ep(request: Request, req: OrganizationUpdateRequest,
                           agent: str = Depends(verify_agent)):
    """Actualiza una organizacion (name/color/sort_order/active). NUNCA borra:
    para retirarla, active=false (Regla de Oro #1)."""
    request.state.audit = {"action": "organization_update", "query": f"org:{req.org_id}"[:200]}
    _rbac_org(agent, "organization_update")
    try:
        return tasks_core.organization_update(agent, req.org_id, name=req.name,
                                              color=req.color, sort_order=req.sort_order,
                                              active=req.active)
    except tasks_core.TaskError as e:
        raise _task_err(e)


@app.post("/project/create")
def project_create_ep(request: Request, req: ProjectCreateRequest,
                      agent: str = Depends(verify_agent)):
    """Crea un proyecto en una organizacion. Valida que la org exista (404 si no).
    Idempotente por id: si el proyecto ya existe, lo devuelve sin modificarlo."""
    request.state.audit = {"action": "project_create",
                           "query": f"proj:{req.project_id} org:{req.org_id}"[:200]}
    _rbac_org(agent, "project_create")
    try:
        return tasks_core.project_create(agent, req.project_id, req.org_id, req.name,
                                         status=req.status, sort_order=req.sort_order)
    except tasks_core.TaskError as e:
        raise _task_err(e)


@app.post("/project/update")
def project_update_ep(request: Request, req: ProjectUpdateRequest,
                      agent: str = Depends(verify_agent)):
    """Actualiza un proyecto. Caso central: REASIGNARLO a otra organizacion (org_id),
    que arrastra por defecto las tareas del proyecto (reassign_tasks=true) para que
    tasks.organization no quede desincronizada de projects.org_id.
    Archivar = status='archived'. NUNCA borra (Regla de Oro #1)."""
    request.state.audit = {"action": "project_update",
                           "query": f"proj:{req.project_id} org:{req.org_id or '-'}"[:200]}
    _rbac_org(agent, "project_update")
    try:
        return tasks_core.project_update(agent, req.project_id, org_id=req.org_id,
                                         name=req.name, status=req.status,
                                         sort_order=req.sort_order,
                                         reassign_tasks=req.reassign_tasks)
    except (tasks_core.TaskError, psycopg.Error) as e:
        raise _task_err(e)


# Rutas REST aditivas; identidad resuelta exclusivamente por verify_agent.
correspondencia.install_routes(app, verify_agent)

# Approval: only the existing service role, never an approver credential.
def _approval_connect():
    return psycopg.connect(host=db.PG_HOST, port=db.PG_PORT, user=db.PG_USER,
                           password=db.PG_PASS, dbname=db.PG_DB, autocommit=False)


_APPROVAL_OUTCOME_UNKNOWN_DETAIL = 'outcome_unknown; resultado incierto; no repetir automáticamente; reconciliar según §5 (UUID propio en request/supersede, tupla de estado en los demás) antes de una nueva decisión'


def _approval_fail(exc):
    if isinstance(exc, approval.ApprovalError):
        if exc.code == 'outcome_unknown':
            raise HTTPException(503, _APPROVAL_OUTCOME_UNKNOWN_DETAIL) from exc
        raise HTTPException(exc.status, exc.code) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(422, 'approval_invalid_input') from exc
    raise HTTPException(500, 'approval_error') from exc


def _approval_scope(data):
    if type(data) is not dict or set(data) != {'action_type','resource_kind','resource_id','constraints','scope_version'}:
        raise ValueError('invalid scope shape')
    constraints = data['constraints']
    if type(constraints) is not dict or any(type(v) is not str for k,v in constraints.items() if k != 'backup_refs'):
        raise ValueError('invalid constraints')
    if 'backup_refs' in constraints and (type(constraints['backup_refs']) is not list or any(type(v) is not str for v in constraints['backup_refs'])):
        raise ValueError('invalid backup_refs')
    return approval.Scope(**data)


class _ApprovalBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class _ApprovalRequestBody(_ApprovalBody):
    request_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    scope: dict

class _ApprovalCAS(_ApprovalBody):
    expected_version: int = Field(ge=0)
    expected_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

class _ApprovalFinish(_ApprovalCAS):
    evidence: str = Field(min_length=1, max_length=10000)

class _ApprovalSupersede(_ApprovalCAS):
    new_request_id: str
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    scope: dict

class _ApprovalEffect(_ApprovalCAS):
    occurred_at: str
    evidence: str = Field(min_length=1, max_length=10000)


def _approval_uuid(value, *, v4=False):
    return str(approval._uuid(value, v4))


def _approval_query(request, *, mine=False):
    allowed = {'limit','cursor','status'} if mine else {'limit','cursor'}
    params = request.query_params
    if any(k not in allowed or len(params.getlist(k)) != 1 for k in params):
        raise ValueError('invalid query parameters')
    raw = params.get('limit', '50')
    if not _re.fullmatch(r'[1-9][0-9]*', raw):
        raise ValueError('invalid limit')
    limit = approval._limit(int(raw))
    status = params.get('status')
    if status is not None and status not in approval._STATUSES:
        raise ValueError('invalid status')
    cursor = params.get('cursor')
    if cursor is not None:
        approval.decode_cursor(cursor)
    return limit, cursor, status


def _approval_run(fn):
    try:
        return fn()
    except Exception as exc:
        _approval_fail(exc)


@_approval_router.post('/request')
async def approval_request_ep(request: Request, body: _ApprovalRequestBody, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).request(
        principal, _approval_uuid(body.request_id, v4=True), body.title, body.description, _approval_scope(body.scope)))


@_approval_router.get('/inbox')
async def approval_inbox_ep(request: Request, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).list_inbox(
        principal, *_approval_query(request)[:2]))


@_approval_router.get('/mine')
async def approval_mine_ep(request: Request, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).list_mine(
        principal, *_approval_query(request, mine=True)))


@_approval_router.get('/requests/{id}')
async def approval_get_ep(request: Request, id: str, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).get(principal, _approval_uuid(id)))


@_approval_router.post('/requests/{id}/finish')
async def approval_finish_ep(request: Request, id: str, body: _ApprovalFinish, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).finish(
        principal, _approval_uuid(id), body.expected_version, body.expected_hash, body.evidence))


@_approval_router.post('/requests/{id}/withdraw')
async def approval_withdraw_ep(request: Request, id: str, body: _ApprovalCAS, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).withdraw(
        principal, _approval_uuid(id), body.expected_version, body.expected_hash))


@_approval_router.post('/requests/{id}/supersede')
async def approval_supersede_ep(request: Request, id: str, body: _ApprovalSupersede, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).supersede(
        principal, _approval_uuid(id), body.expected_version, body.expected_hash,
        _approval_uuid(body.new_request_id, v4=True), body.title, body.description, _approval_scope(body.scope)))


@_approval_router.post('/requests/{id}/record-effect')
async def approval_effect_ep(request: Request, id: str, body: _ApprovalEffect, principal=Depends(_approval_principal)):
    return _approval_run(lambda: approval.ApprovalCommands(_approval_connect).record_effect(
        principal, _approval_uuid(id), body.expected_version, body.expected_hash, approval._time(body.occurred_at), body.evidence))


@_approval_router.post('/requests/{id}/approve')
@_approval_router.post('/requests/{id}/revoke')
@_approval_router.post('/requests/{id}/request-changes')
async def approval_denied_ep(request: Request, id: str, principal=Depends(_approval_principal)):
    raise HTTPException(403, 'approval_forbidden')


app.include_router(_approval_router)
