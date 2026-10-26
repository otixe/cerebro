"""DB helpers - Postgres + Qdrant."""
import os
import uuid
import hashlib
import logging
import agent_config
from contextlib import contextmanager
from typing import Any, Optional
import psycopg
from psycopg.rows import dict_row
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct, Filter, FieldCondition,
    MatchValue, MatchAny, MatchExcept, PayloadSchemaType,
)
from archivepolicy import ARCHIVED_PATH_PATTERNS, is_archived_path
from candidatepolicy import CandidatePolicy, build_candidate_policy

# Namespace estable para chunk_ids determinísticos (UUID v5).
# Cualquier cambio a este valor invalida todos los IDs existentes: NO TOCAR.
CHUNK_ID_NAMESPACE = uuid.UUID("6d7e8c2a-4b1f-5e99-9b3a-9d0c1f2e3a4b")

_log = logging.getLogger("cerebro.db")


def deterministic_chunk_id(source_path: str, chunk_index: int) -> str:
    """UUID v5 determinístico para (source_path, chunk_index).
    Garantiza idempotencia en re-indexados: mismo par → mismo id.
    """
    return str(uuid.uuid5(CHUNK_ID_NAMESPACE, f"{source_path}:{chunk_index}"))

PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = int(os.environ.get("POSTGRES_PORT_INTERNAL", "5432"))
PG_USER = os.environ["POSTGRES_USER"]
PG_PASS = os.environ["POSTGRES_PASSWORD"]
PG_DB   = os.environ["POSTGRES_DB"]

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = os.environ.get("QDRANT_COLLECTION", "merlin_memory")
DIMS = int(os.environ.get("EMBEDDING_DIMS", "768"))

# FIX 1 (2026-08-04): el timeout del cliente Qdrant estaba hardcodeado en 30s.
# Un search/scroll sobre una fuente de cientos de chunks (o un set_payload masivo)
# puede excederlo y morir del lado del gateway, donde el timeout del cliente MCP
# no lo cubre. Configurable por entorno, default 120s (alineado con
# CEREBRO_HTTP_TIMEOUT del cliente MCP: el cliente nunca debe cortar antes).
QDRANT_TIMEOUT = float(os.environ.get("CEREBRO_QDRANT_TIMEOUT", "120"))

# Sprint 2 (retrieval hibrido): fusion vector (Qdrant) + BM25 full-text (Postgres) via RRF.
# HYBRID_SEARCH default on; RRF_K=60 (constante estandar de Reciprocal Rank Fusion).
HYBRID_SEARCH_DEFAULT = os.environ.get("HYBRID_SEARCH", "true").strip().lower() in ("1", "true", "yes", "on")
RRF_K = int(os.environ.get("RRF_K", "60"))

# Sprint 10: umbral de confianza, piso de chunk, y busqueda por estado (archivado).
# MIN_SCORE_DEFAULT: umbral sobre el score COSINE (no RRF). None/vacio = desactivado.
_min_score_env = os.environ.get("MIN_SCORE_DEFAULT", "").strip()
MIN_SCORE_DEFAULT = float(_min_score_env) if _min_score_env else None
MIN_CHUNK_LEN_DEFAULT = int(os.environ.get("MIN_CHUNK_LEN", "50"))
# Patrones de source_path considerados "archivados" (ademas de superseded bi-temporal).
#
# FIX E1 (2026-08-10): la lista original se escribio a mano y nunca se valido
# contra las carpetas que EXISTEN en el vault. Faltaban 4 variantes reales que
# dejaban 1651 chunks VIGENTES compitiendo en el retrieval por defecto contra
# documentacion viva (medido en Postgres: deleted_at IS NULL AND valid_until IS NULL).
# Conteos al aplicar el fix:
#     /_Archivo/     <- ExampleProject/Service/_Archivo
#     /Archivo/      <- Session Memory/Archivo (rotated journals)
#     /_archive/     <- 50 Agents/Demo-agent/Infrastructure/_archive
#     /_deprecated/  <- ExampleProject/Diagrams/_deprecated
#
# (a) POR QUE CASE-INSENSITIVE: el vault ya tiene 'Archive' y '_archive',
#     'Archivo' y '_Archivo' conviviendo, asi que la lista no puede depender de
#     como alguien tipeo una carpeta. Normalizamos a lower() UNA vez y los
#     patrones se declaran en minusculas. Ademas cubre 10 chunks de
#     'saas-core/docs/archive/backend-artifacts_20260409/...' (archivado de repo).
#     Verificado: los 5 patrones historicos siguen matcheando en minusculas
#     (ninguno dependia de una mayuscula para funcionar).
#
# (b) POR QUE **NO** ESTA '_Archivado' (no lo agregues "por completitud"):
#     una carpeta 'ExampleProject/Service/_Archivado-*' puede estar cubierta
#     por SOFT-DELETE, que ya excluye deleted=true. No derivar patrones por conteos.
#     Y ojo con la medicion que parece contradecir esto: un
#     `ILIKE '%_Archivado%'` en Postgres devuelve 67 "vigentes", pero es un
#     ARTEFACTO -- en LIKE/ILIKE el '_' es COMODIN de un caracter, asi que
#     matchea '-archivado', 'wikilinks-archivado', 'legacy-o-archivado'... o sea
#     notas VIGENTES que solo hablan de archivar. Con position() literal da 0.
#     Agregar este patron no excluiria nada real y el riesgo es la confusion.
#
# (c) POR QUE SE CONSERVA '/99 archive/' AUNQUE MATCHEE 0 (no lo borres "por
#     limpieza"): la carpeta '99 Archive' EXISTE en el vault, pero esta en
#     .env INDEX_EXCLUDE_PATHS => el indexer nunca la lee, por eso 0 chunks en
#     cualquier estado. Es inalcanzable, no redundante: si algun dia se saca de
#     INDEX_EXCLUDE_PATHS, este patron es su UNICA cobertura, porque
#     '99 Archive/' contiene ' archive/' CON ESPACIO y NO '/archive/'. Borrar un
#     patron con 0 matches reabre un agujero silencioso.
#     Idem '/historical/' (0 filas en cualquier estado): inocuo, se conserva.
#
# ⚠️ NO agregar patrones sin barras (ej. 'deprecated' o 'archivo' a secas):
#    matchearian cualquier nota VIVA que hable de deprecacion o de archivar.
#    Un patron debe identificar una CARPETA, no un tema.
# Policy implementation moved to archivepolicy.py so the declarative registry can
# reuse it without diverging from retrieval behavior.

# Fase 3b.1-A (authz v2 scope): prefijo de indice del vault. Convencion ESPEJO
# de SINAPSIS (_norm_scope_path): los scope_grants guardan prefijos SIN este
# prefijo (ej 'Personal/Example/'), asi que normalizamos quitandolo para que
# path_ancestors matchee 1:1 contra los path_prefix de scope_grants.
INDEX_PREFIX = "obsidian-vault/"

def path_ancestors(source_path: str) -> list[str]:
    """Fase 3b.1-A: lista de prefijos ancestros (cada nivel de carpeta con trailing
    slash) de un source_path, NORMALIZADOS igual que SINAPSIS._norm_scope_path
    (se quita 'obsidian-vault/' y los '/' iniciales). Materializado como payload
    KEYWORD en Qdrant para poder filtrar scope por carpeta con MatchAny (KEYWORD
    solo soporta match exacto: MatchText sobre prefijo hace timeout).

    Ej: 'obsidian-vault/Personal/Example/nota.md' -> ['Personal/', 'Personal/Example/'].
    Estos elementos matchean 1:1 los path_prefix de scope_grants ('Personal/Example/').
    Un archivo en la raiz del vault (sin carpeta) devuelve [] (solo lo cubre un
    grant raiz '', que es admin y no filtra)."""
    sp = source_path or ""
    if sp.startswith(INDEX_PREFIX):
        sp = sp[len(INDEX_PREFIX):]
    sp = sp.lstrip("/")
    parts = [p for p in sp.split("/") if p]
    # Descartamos el ultimo segmento (nombre de archivo); solo carpetas ancestro.
    folders = parts[:-1]
    ancestors = []
    acc = ""
    for f in folders:
        acc += f + "/"
        ancestors.append(acc)
    return ancestors

_qdrant = None
def qdrant() -> QdrantClient:
    global _qdrant
    if _qdrant is None:
        _qdrant = QdrantClient(url=QDRANT_URL, timeout=QDRANT_TIMEOUT)
    return _qdrant

def ensure_collection():
    q = qdrant()
    cols = [c.name for c in q.get_collections().collections]
    if COLLECTION not in cols:
        q.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=DIMS, distance=Distance.COSINE),
        )
    # Índices de payload para filtros rápidos. Se (re)crean SIEMPRE al arrancar
    # (no solo al crear la coleccion): create_payload_index es idempotente y el
    # try/except absorbe "ya existe", asi indices nuevos (ej path_ancestors,
    # Fase 3b.1-A) se materializan sobre la coleccion viva sin recrearla.
    for field, ftype in [
        ("source_path", PayloadSchemaType.KEYWORD),
        ("tags", PayloadSchemaType.KEYWORD),
        ("agent_owner", PayloadSchemaType.KEYWORD),
        ("sensitivity", PayloadSchemaType.KEYWORD),
        ("category", PayloadSchemaType.KEYWORD),
        ("path_ancestors", PayloadSchemaType.KEYWORD),
        ("deleted", PayloadSchemaType.BOOL),
        ("superseded", PayloadSchemaType.BOOL),
        ("archived", PayloadSchemaType.BOOL),
    ]:
        try:
            q.create_payload_index(collection_name=COLLECTION, field_name=field, field_schema=ftype)
        except Exception:
            pass

@contextmanager
def pg():
    with psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASS, dbname=PG_DB,
        row_factory=dict_row, autocommit=False,
    ) as conn:
        yield conn

def upsert_chunk(
    *, chunk_id: Optional[str], source_path: str, source_hash: str, chunk_index: int,
    title: str, breadcrumb: list[str], text: str, vector: Optional[list[float]],
    tags: list[str], agent_owner: str, sensitivity: str = "normal",
    category: str = "general",
) -> str:
    """Insert o reemplaza chunk en Postgres + Qdrant. Devuelve chunk_id.

    chunk_id por defecto = UUID v5 determinístico (source_path, chunk_index)
    para garantizar idempotencia en re-indexados.
    """
    cid = chunk_id or deterministic_chunk_id(source_path, chunk_index)
    if vector is None:
        tags = list(dict.fromkeys([*tags, "system:embedding-pending"]))
    preview = (text[:500] + "…") if len(text) > 500 else text
    # Sprint 2 (hybrid search): text_full alimenta el tsvector BM25 en Postgres.
    # Para secret/metadata_only queda NULL (no exponer secreto en Postgres), igual que el payload Qdrant.
    full_text = text if sensitivity not in ("metadata_only", "secret") else None
    with pg() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO chunks_meta (id, source_path, source_hash, chunk_index, title, tags,
                                     agent_owner, sensitivity, category, text_preview, text_full, deleted_at,
                                     valid_from, valid_until, superseded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, now(), NULL, NULL)
            ON CONFLICT (source_path, chunk_index) DO UPDATE SET
              source_hash = EXCLUDED.source_hash,
              title = EXCLUDED.title,
              tags = EXCLUDED.tags,
              agent_owner = EXCLUDED.agent_owner,
              sensitivity = EXCLUDED.sensitivity,
              category = EXCLUDED.category,
              text_preview = EXCLUDED.text_preview,
              text_full = EXCLUDED.text_full,
              deleted_at = NULL,
              valid_from = now(),
              valid_until = NULL,
              superseded_by = NULL,
              updated_at = now()
            RETURNING id
        """, (cid, source_path, source_hash, chunk_index, title, tags, agent_owner,
              sensitivity, category, preview, full_text))
        cid = str(cur.fetchone()["id"])
        conn.commit()
    if vector is None:
        return cid
    # Para sensitivity=metadata_only o 'secret', payload lleva solo título + path (sin text)
    payload_text = text if sensitivity not in ("metadata_only", "secret") else ""
    qdrant().upsert(
        collection_name=COLLECTION,
        points=[PointStruct(
            id=cid, vector=vector,
            payload={
                "chunk_id": cid,
                "source_path": source_path,
                "title": title,
                "breadcrumb": breadcrumb,
                "text": payload_text,
                "tags": tags,
                "agent_owner": agent_owner,
                "sensitivity": sensitivity,
                "category": category,
                "deleted": False,
                "superseded": False,
                "archived": is_archived_path(source_path),
                "path_ancestors": path_ancestors(source_path),
                "valid_until": None,
            },
        )],
    )
    return cid

def soft_delete_chunks(source_path: str = None, chunk_id: str = None) -> int:
    if not source_path and not chunk_id:
        return 0
    with pg() as conn, conn.cursor() as cur:
        if chunk_id:
            cur.execute("UPDATE chunks_meta SET deleted_at=now() WHERE id=%s RETURNING id", (chunk_id,))
        else:
            cur.execute("UPDATE chunks_meta SET deleted_at=now() WHERE source_path=%s AND deleted_at IS NULL RETURNING id", (source_path,))
        ids = [str(r["id"]) for r in cur.fetchall()]
        conn.commit()
    if ids:
        # Mark payload deleted=true en Qdrant (no removemos vector; hard-delete hace eso)
        qdrant().set_payload(
            collection_name=COLLECTION,
            payload={"deleted": True},
            points=ids,
        )
    return len(ids)

def prune_leftover_chunks(source_path: str, new_count: int) -> int:
    """FIX 2 (2026-08-04): SOFT-delete de los chunks HUERFANOS de una fuente.

    Cuando una fuente se reescribe con menos chunks que antes (el doc encogio, o
    el chunker cambio de politica), los chunks con `chunk_index >= new_count`
    quedaban VIVOS y searchables para siempre: texto viejo mezclado con el nuevo.
    El indexer del vault ya hacia esto; los endpoints de escritura por API
    (/memory/add-markdown, /skills/add-reference) NO, y ahi es donde se acumulo
    la basura.

    Esta es la UNICA implementacion (single source of truth): `indexer.py`
    delega aca. No agregar una tercera copia.

    Contrato:
      - Marca `deleted_at=now()` (NUNCA DELETE: Regla de Oro #1, el dato queda).
      - Espeja el flag en Qdrant con `set_payload({"deleted": True})` sobre los
        mismos ids, para que el filtro de search los excluya del lado vectorial.
      - Es idempotente: `deleted_at IS NULL` en el WHERE, correr dos veces no
        cambia nada y devuelve 0 la segunda vez.
      - `new_count <= 0` es un no-op defensivo: NO borra la fuente entera. Para
        eso existe `soft_delete_chunks(source_path=...)`, explicito.
      - Un fallo de Qdrant NO rompe la escritura: Postgres ya quedo consistente
        (es la fuente de verdad del flag) y el reindexado vuelve a espejar.

    DEUDA RESIDUAL CONOCIDA (documentada, no tapada):
      Un chunk INTERIOR (chunk_index < new_count) que alguien marco a mano como
      superseded/deleted lo RESUCITA el `ON CONFLICT ... DO UPDATE` de
      upsert_chunk (que reescribe la fila con deleted_at=NULL), y este pruning
      no lo alcanza porque solo mira los indices >= new_count. Es decir: el
      pruning arregla la COLA huerfana, no revierte un superseded manual del
      medio. Arreglarlo de verdad requiere que upsert_chunk decida si respeta un
      deleted_at/superseded_by preexistente, que es un cambio de semantica del
      write-path y va aparte.
    """
    if not source_path or new_count is None or new_count <= 0:
        return 0
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE chunks_meta SET deleted_at=now()
               WHERE source_path=%s AND chunk_index >= %s AND deleted_at IS NULL
               RETURNING id""",
            (source_path, new_count))
        ids = [str(r["id"]) for r in cur.fetchall()]
        conn.commit()
    if ids:
        try:
            qdrant().set_payload(
                collection_name=COLLECTION,
                payload={"deleted": True},
                points=ids,
            )
        except Exception as e:
            # Postgres ya es consistente; el espejo en Qdrant se recupera en el
            # proximo reindexado. Se loguea para que la deriva sea visible.
            _log.warning("prune_leftover_chunks: set_payload fallo para %s (%d ids): %s",
                         source_path, len(ids), e)
    return len(ids)


def hard_delete_chunks(source_path: str = None, chunk_ids: list[str] = None, tags: list[str] = None,
                       deny_prefixes: list[str] = None) -> int:
    # M1 (auditoria 2026-07-28): el WHERE se arma por f-string con fragmentos
    # LITERALES hardcodeados aca abajo ('source_path=%s', 'id = ANY(%s)',
    # 'tags && %s'); los VALORES van SIEMPRE parametrizados en `params`. Ningun
    # nombre de columna ni operador proviene del input. No interpolar `params` aqui.
    with pg() as conn, conn.cursor() as cur:
        conds = []
        params: list[Any] = []
        if source_path:
            conds.append("source_path=%s"); params.append(source_path)
        if chunk_ids:
            conds.append("id = ANY(%s)"); params.append(chunk_ids)
        if tags:
            conds.append("tags && %s"); params.append(tags)
        if deny_prefixes:
            deny = [p + "%" for p in deny_prefixes]
            cur.execute("SELECT count(*) AS n FROM chunks_meta WHERE tags && %s "
                        "AND regexp_replace(source_path, '^obsidian-vault/', '') LIKE ANY(%s)", (tags, deny))
            _log.warning("office privacy excluded %d forget-by-tags chunks", cur.fetchone()["n"])
            conds.append("NOT (regexp_replace(source_path, '^obsidian-vault/', '') LIKE ANY(%s))")
            params.append(deny)
        if not conds:
            return 0
        where = " AND ".join(conds)
        cur.execute(f"DELETE FROM chunks_meta WHERE {where} RETURNING id", params)
        ids = [str(r["id"]) for r in cur.fetchall()]
        conn.commit()
    if ids:
        qdrant().delete(collection_name=COLLECTION, points_selector=ids)
    return len(ids)

def supersede_chunk(old_id: str, new_id: str) -> dict:
    """Sprint 1: marca la memoria old_id como reemplazada por new_id SIN borrarla.
    UPDATE chunks_meta SET valid_until=now(), superseded_by=new_id WHERE id=old_id.
    Actualiza el payload Qdrant del old: superseded=true, valid_until=<now iso>.
    Devuelve {'old_id','new_id','valid_until'} o {} si old_id no existe."""
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE chunks_meta SET valid_until=now(), superseded_by=%s
               WHERE id=%s RETURNING id, valid_until""",
            (new_id, old_id))
        row = cur.fetchone()
        conn.commit()
    if not row:
        return {}
    valid_until_iso = row["valid_until"].isoformat() if row.get("valid_until") else None
    # Marca el vector viejo como superseded para que search lo filtre por default.
    qdrant().set_payload(
        collection_name=COLLECTION,
        payload={"superseded": True, "valid_until": valid_until_iso},
        points=[old_id],
    )
    return {"old_id": old_id, "new_id": new_id, "valid_until": valid_until_iso}

def _payload_to_result(p: dict, score: float) -> dict:
    """FIX E1 (2026-08-03, tarea 1dd87ff9): la respuesta declara la ESCALA de cada
    numero. Antes solo salia el campo score, que en modo hibrido es RRF con techo matematico
    2/(RRF_K+1)=0.0328 — leido contra una tabla escrita para COSENO hacia que TODO
    acierto se interpretara como "probablemente irrelevante".
    score_cosine es None (JAMAS 0.0) cuando el hit vino SOLO por BM25: None significa
    "no hubo match vectorial", 0.0 significaria "el match fue pesimo". No es lo mismo.
    """
    return {
        "chunk_id": p.get("chunk_id"),
        "score": score,
        "score_cosine": score,
        "score_rrf": None,
        "rank": None,
        "retrieval": "vector",
        "source_path": p.get("source_path"),
        "title": p.get("title"),
        "breadcrumb": p.get("breadcrumb", []),
        "text": p.get("text", ""),
        "tags": p.get("tags", []),
        "sensitivity": p.get("sensitivity", "normal"),
        "category": p.get("category", "general"),
        "agent_owner": p.get("agent_owner"),
    }


def _bm25_search(query_text: str, *, top_k: int, candidate_policy: CandidatePolicy,
                 include_archived: bool, tags: list[str]=None, agent_owner: str=None,
                 category: str=None, sensitivity: list[str]=None, exclude_secret: bool=True,
                 include_superseded: bool=False, source_prefix: str=None,
                 scope_prefixes: list[str]=None, deny_prefixes: list[str]=None, strict: bool=False) -> list[str]:
    """Sprint 2 (rama BM25 de la busqueda hibrida): full-text de Postgres via tsvector.
    Aplica los MISMOS filtros que la rama vectorial (owner/tags/category/sensitivity/
    superseded/deleted/source_prefix). Devuelve chunk_ids ordenados por ts_rank desc.
    Chunks con text_full NULL (preexistentes o secret/metadata_only) no matchean por
    cuerpo; el vector search los sigue encontrando (additive, sin regresion)."""
    conds = ["deleted_at IS NULL", "tsv @@ plainto_tsquery('spanish', %(q)s)"]
    params: dict[str, Any] = {"q": query_text, "lim": top_k * 2}
    if not include_superseded:
        # Equivalente Postgres del filtro superseded del vector: vigente = valid_until NULL.
        conds.append("valid_until IS NULL")
    if tags:
        conds.append("tags && %(tags)s"); params["tags"] = tags
    if agent_owner:
        conds.append("agent_owner = %(owner)s"); params["owner"] = agent_owner
    if category:
        conds.append("category = %(cat)s"); params["cat"] = category
    if sensitivity:
        conds.append("sensitivity = ANY(%(sens)s)"); params["sens"] = sensitivity
    if exclude_secret and not (sensitivity and "secret" in sensitivity):
        conds.append("sensitivity <> 'secret'")
    if source_prefix:
        conds.append("source_path LIKE %(pfx)s"); params["pfx"] = source_prefix + "%"
    if scope_prefixes is not None:
        # Authz Fase 3b: scope por carpeta (member). Espeja el MatchAny(path_ancestors)
        # del vector: la nota cae bajo alguno de los prefijos permitidos. source_path en
        # Postgres lleva el prefijo 'obsidian-vault/'; los scope prefixes NO -> comparar
        # contra el source_path normalizado (sin 'obsidian-vault/').
        if not scope_prefixes:
            # member sin grants => sin acceso (fail-closed): condicion imposible.
            conds.append("false")
        else:
            ors = []
            for i, pref in enumerate(scope_prefixes):
                k = f"scope{i}"
                ors.append(f"regexp_replace(source_path, '^obsidian-vault/', '') LIKE %({k})s")
                params[k] = pref + "%"
            conds.append("(" + " OR ".join(ors) + ")")
    if deny_prefixes:
        conds.append("NOT (regexp_replace(source_path, '^obsidian-vault/', '') LIKE ANY(%(deny)s))")
        params["deny"] = [p + "%" for p in deny_prefixes]
    policy_conds, policy_params = candidate_policy.pg_conditions(pass_include_archived=include_archived)
    conds.extend(policy_conds)
    params.update(policy_params)
    where = " AND ".join(conds)
    sql = (f"SELECT id, ts_rank(tsv, plainto_tsquery('spanish', %(q)s)) AS rank "
           f"FROM chunks_meta WHERE {where} ORDER BY rank DESC LIMIT %(lim)s")
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [str(r["id"]) for r in cur.fetchall()]
    except Exception:
        if strict:
            raise
        # BM25 nunca debe romper la busqueda: ante error caemos a solo-vector.
        return []


def search_lexical(query_vec=None, *, query_text, top_k=5, include_archived=False,
                   include_sessions=False, include_logs=False,
                   min_chunk_len=None, min_score=None, hybrid=None, **filters):
    """Explicit degraded search: SQL authorization AND request policy PRE-LIMIT.

    One immutable policy across archive fallback, just like semantic search.
    No embeddings/Qdrant/cosine claim; existing deferred writes are unaffected.
    """
    candidate_policy = build_candidate_policy(include_sessions=include_sessions,
                                              include_logs=include_logs,
                                              include_archived=include_archived)
    def run_pass(pass_include_archived):
        ids = _bm25_search(query_text, top_k=max(top_k * 10, 50), strict=True,
                           candidate_policy=candidate_policy,
                           include_archived=pass_include_archived, **filters)
        if not ids:
            return []
        with pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT id,source_path,title,text_full,text_preview,tags,sensitivity,category,agent_owner "
                        "FROM chunks_meta WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL", (ids,))
            rows = {str(r["id"]): r for r in cur.fetchall()}
        out = []
        for cid in ids:
            r = rows.get(cid)
            if not r or (not pass_include_archived and is_archived_path(r["source_path"])):
                continue
            text = r["text_full"] or r["text_preview"] or ""
            if len(text) < (MIN_CHUNK_LEN_DEFAULT if min_chunk_len is None else min_chunk_len):
                continue
            out.append({"chunk_id": cid, "source_path": r["source_path"], "title": r["title"],
                        "text": text, "tags": r["tags"], "sensitivity": r["sensitivity"],
                        "category": r["category"], "agent_owner": r["agent_owner"],
                        "breadcrumb": [], "score": 1.0 / (60 + len(out) + 1),
                        "score_cosine": None, "retrieval": "lexical_degraded", "rank": len(out)+1})
            if len(out) >= top_k:
                break
        return out
    out = run_pass(include_archived)
    from_archive = False
    if not out and not include_archived:
        out = run_pass(True)
        from_archive = bool(out)
        for r in out:
            r["from_archive"] = True
    return {"results": out, "low_confidence": False, "from_archive": from_archive}


def _better_result(a: dict, b: dict) -> bool:
    """O3 dedup: True si `a` es preferible a `b` como representante canonico.
    Prioridad: (1) no-archivado sobre archivado; (2) mayor score."""
    a_arch = is_archived_path(a.get("source_path") or "")
    b_arch = is_archived_path(b.get("source_path") or "")
    if a_arch != b_arch:
        return not a_arch  # preferir el NO archivado
    return (a.get("score") or 0.0) > (b.get("score") or 0.0)


def _dedup_results(results: list[dict]) -> list[dict]:
    """O3: colapsa hits duplicados (mismo title exacto O text casi-identico:
    primeros 120 chars normalizados). Se queda con el representante mas canonico
    (no-archivado / mejor score) via _better_result. Preserva el orden de entrada."""
    kept: list[dict] = []
    title_idx: dict[str, int] = {}
    text_idx: dict[str, int] = {}
    for r in results:
        title = (r.get("title") or "").strip().lower()
        tprefix = (r.get("text") or "").strip().lower()[:120]
        hit = None
        if title and title in title_idx:
            hit = title_idx[title]
        elif tprefix and tprefix in text_idx:
            hit = text_idx[tprefix]
        if hit is not None:
            if _better_result(r, kept[hit]):
                kept[hit] = r
                if title:
                    title_idx[title] = hit
                if tprefix:
                    text_idx[tprefix] = hit
            continue
        idx = len(kept)
        kept.append(r)
        if title:
            title_idx[title] = idx
        if tprefix:
            text_idx[tprefix] = idx
    return kept


def _run_pass(query_vec: list[float], *, query_text: str, top_k: int, tags, agent_owner,
              sensitivity, source_prefix, exclude_secret: bool, category,
              include_superseded: bool, do_hybrid: bool, include_archived: bool,
              over_fetch: int, candidate_policy: CandidatePolicy,
              scope_prefixes: list[str]=None, deny_prefixes: list[str]=None) -> tuple[list[dict], dict[str, float]]:
    """Una pasada de busqueda (Sprint 10). Devuelve (resultados_ordenados, cosine_by_id).
    cosine_by_id mapea chunk_id -> score COSINE del vector (para el umbral O1; los hits
    que vienen SOLO por BM25 no aparecen ahi). Si include_archived es False excluye
    archivados: por payload archived=true (chunks nuevos/backfilled) en Qdrant, y por
    is_archived_path(source_path) post-fetch (fallback para chunks viejos sin el payload)."""
    must = [FieldCondition(key="deleted", match=MatchValue(value=False))]
    must_not = []
    if not include_superseded:
        # Chunks viejos sin el campo 'superseded' se tratan como NO superseded
        # (additive-safe): solo se excluyen los explicitamente superseded=true.
        must_not.append(FieldCondition(key="superseded", match=MatchValue(value=True)))
    if not include_archived:
        # Excluye los que tienen el payload archived=true (chunks nuevos/backfilled).
        # Los viejos sin el payload se filtran post-fetch via is_archived_path.
        must_not.append(FieldCondition(key="archived", match=MatchValue(value=True)))
    if tags:
        must.append(FieldCondition(key="tags", match=MatchAny(any=tags)))
    if agent_owner:
        must.append(FieldCondition(key="agent_owner", match=MatchValue(value=agent_owner)))
    if category:
        must.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if sensitivity:
        must.append(FieldCondition(key="sensitivity", match=MatchAny(any=sensitivity)))
    if exclude_secret and not (sensitivity and "secret" in sensitivity):
        # D10 opción B: excluir 'secret' salvo whitelist
        must_not.append(FieldCondition(key="sensitivity", match=MatchValue(value="secret")))
    if scope_prefixes is not None:
        # Authz Fase 3b: filtro de scope por carpeta para agentes member.
        # scope_prefixes=None => admin/sin restriccion (no se agrega la condicion).
        # scope_prefixes=[] => sin acceso a nada (member sin grants) => fail-closed.
        # MatchAny sobre path_ancestors (KEYWORD, backfilled) = 'la nota cuelga de alguno
        # de estos prefijos'. Nativo en Qdrant, no post-hoc.
        must.append(FieldCondition(key="path_ancestors", match=MatchAny(any=scope_prefixes)))
    if deny_prefixes:
        must_not.append(FieldCondition(key="path_ancestors", match=MatchAny(any=deny_prefixes)))
    policy_must, policy_must_not = candidate_policy.qdrant_conditions(pass_include_archived=include_archived)
    must.extend(policy_must)
    must_not.extend(policy_must_not)
    # Pedimos de mas para sobrevivir dedup/piso/umbral y el corte final a top_k.
    fetch_k = over_fetch * 2 if do_hybrid else over_fetch
    hits = qdrant().search(
        collection_name=COLLECTION, query_vector=query_vec, limit=fetch_k,
        query_filter=Filter(must=must, must_not=must_not or None),
        with_payload=True,
    )
    result_by_id: dict[str, dict] = {}
    cosine_by_id: dict[str, float] = {}
    vec_ranking: list[str] = []
    for h in hits:
        p = h.payload or {}
        sp = p.get("source_path", "")
        if source_prefix and not sp.startswith(source_prefix):
            continue
        if not include_archived and is_archived_path(sp):
            continue
        cid = p.get("chunk_id")
        if cid is None:
            continue
        result_by_id[cid] = _payload_to_result(p, float(h.score))
        cosine_by_id[cid] = float(h.score)
        vec_ranking.append(cid)

    if not do_hybrid:
        # Comportamiento historico: solo vector, orden por score.
        return [result_by_id[c] for c in vec_ranking[:over_fetch]], cosine_by_id

    bm25_ranking = _bm25_search(
        query_text, top_k=over_fetch, tags=tags, agent_owner=agent_owner, category=category,
        sensitivity=sensitivity, exclude_secret=exclude_secret,
        include_superseded=include_superseded, source_prefix=source_prefix,
        scope_prefixes=scope_prefixes, deny_prefixes=deny_prefixes,
        candidate_policy=candidate_policy, include_archived=include_archived)

    # Reciprocal Rank Fusion: score(doc) = Σ 1/(RRF_K + rank_i), rank 0-based por lista.
    rrf: dict[str, float] = {}
    for rank, cid in enumerate(vec_ranking):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, cid in enumerate(bm25_ranking):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

    ordered = sorted(rrf, key=lambda c: rrf[c], reverse=True)[:over_fetch]

    # Payloads de ids que vinieron SOLO por BM25 (no estaban en el pool vectorial).
    missing = [c for c in ordered if c not in result_by_id]
    if missing:
        try:
            for pt in qdrant().retrieve(collection_name=COLLECTION, ids=missing, with_payload=True):
                p = pt.payload or {}
                r_bm = _payload_to_result(p, 0.0)
                r_bm["score_cosine"] = None      # sin match vectorial: None != 0.0
                r_bm["retrieval"] = "bm25_only"
                result_by_id[str(pt.id)] = r_bm
        except Exception:
            pass

    out = []
    for cid in ordered:
        r = result_by_id.get(cid)
        if r is None:
            continue
        # Post-filtro de archivado tambien para hits que entraron por BM25.
        if not include_archived and is_archived_path(r.get("source_path", "")):
            continue
        r = dict(r)
        # FIX E1: los tres numeros por separado, cada uno con su escala declarada.
        r["score_rrf"] = round(rrf[cid], 6)
        r["score"] = r["score_rrf"]          # compat: score NO cambia de significado
        if cid in cosine_by_id:
            r["score_cosine"] = round(cosine_by_id[cid], 4)
            r["retrieval"] = "hybrid"
        else:
            r["score_cosine"] = None          # solo-BM25: None, nunca 0.0
            r["retrieval"] = "bm25_only"
        r["rank"] = len(out) + 1              # 1-based, orden final tras la fusion
        out.append(r)
    return out, cosine_by_id


def search(query_vec: list[float], *, query_text: str = None, top_k=5, tags: list[str]=None,
           agent_owner: str=None, sensitivity: list[str]=None, since=None, source_prefix: str=None,
           exclude_secret: bool = True, category: str = None,
           include_superseded: bool = False, hybrid: bool = None,
           min_score: float = None, include_archived: bool = False,
           min_chunk_len: int = None, scope_prefixes: list[str]=None,
           deny_prefixes: list[str]=None, include_sessions: bool=False,
           include_logs: bool=False) -> dict:
    """Busqueda semantica (Qdrant) + BM25 (Postgres) via RRF cuando hybrid on.
    Sprint 10 devuelve un DICT: {'results': [...], 'low_confidence': bool, 'from_archive': bool}.

    - Busqueda por estado (2 pasadas): por default excluye archivados
      (source_path bajo /Historico//Archive/... o superseded). Si la pasada 1 da 0
      resultados y include_archived es False, corre una 2a pasada incluyendo
      archivados y los marca con from_archive=true. include_archived=True incluye
      archivados desde el inicio.
    - O1 umbral (min_score): evaluado sobre el score COSINE (no el RRF). None =
      env MIN_SCORE_DEFAULT. Si el mejor cosine < min_score -> results=[] y
      low_confidence=true.
    - O3 dedup: colapsa hits con mismo title / text casi-identico.
    - O6 piso (min_chunk_len): descarta chunks con text mas corto que el piso."""
    if hybrid is None:
        hybrid = HYBRID_SEARCH_DEFAULT
    do_hybrid = bool(hybrid and query_text)
    if min_score is None:
        min_score = MIN_SCORE_DEFAULT
    if min_chunk_len is None:
        min_chunk_len = MIN_CHUNK_LEN_DEFAULT
    # Sobre-pedimos para que dedup/piso/umbral no dejen la lista por debajo de top_k.
    over_fetch = max(top_k * 4, 20)

    candidate_policy = build_candidate_policy(include_sessions=include_sessions,
                                              include_logs=include_logs,
                                              include_archived=include_archived)
    pass_kwargs = dict(
        query_text=query_text, top_k=top_k, tags=tags, agent_owner=agent_owner,
        sensitivity=sensitivity, source_prefix=source_prefix, exclude_secret=exclude_secret,
        category=category, include_superseded=include_superseded, do_hybrid=do_hybrid,
        over_fetch=over_fetch, scope_prefixes=scope_prefixes, deny_prefixes=deny_prefixes,
        candidate_policy=candidate_policy)

    # PASADA 1: excluir archivados (salvo que el caller pida include_archived).
    results, cosine_by_id = _run_pass(query_vec, include_archived=include_archived, **pass_kwargs)
    from_archive = False
    if not results and not include_archived:
        # PASADA 2 automatica: incluir archivados y marcarlos.
        results, cosine_by_id = _run_pass(query_vec, include_archived=True, **pass_kwargs)
        if results:
            from_archive = True
            for r in results:
                r["from_archive"] = True

    # O6 piso: descartar chunks demasiado cortos (ruido: UUIDs, lineas sueltas).
    if min_chunk_len and min_chunk_len > 0:
        results = [r for r in results if len((r.get("text") or "")) >= min_chunk_len]

    # O3 dedup y corte final a top_k por score.
    results = _dedup_results(results)
    results.sort(key=lambda r: r.get("score") or 0.0, reverse=True)
    results = results[:top_k]

    # O1 umbral: sobre el mejor COSINE de los resultados finales (los hits solo-BM25
    # no tienen cosine y no vetan). Si por debajo del umbral -> sin resultados confiables.
    low_confidence = False
    if min_score is not None:
        cosines = [cosine_by_id[r["chunk_id"]] for r in results
                   if r.get("chunk_id") in cosine_by_id]
        if cosines and max(cosines) < min_score:
            low_confidence = True
            results = []

    return {"results": results, "low_confidence": low_confidence, "from_archive": from_archive}

def stats() -> dict:
    with pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM v_stats")
        row = cur.fetchone()
    return dict(row) if row else {}

def log_audit(agent, action, *, chunk_id=None, query=None, result_count=None, duration_ms=None, success=True, error=None):
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO audit_log (agent, action, chunk_id, query, result_count,
                          duration_ms, success, error)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (agent, action, chunk_id, (query or "")[:500], result_count,
                         duration_ms, success, (error or "")[:500] if error else None))
            conn.commit()
    except Exception:
        pass  # audit nunca debe romper ops

def log_index(source_path, action, *, chunks_count=None, duration_ms=None, error=None):
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO index_log (source_path, action, chunks_count, duration_ms, error)
                          VALUES (%s,%s,%s,%s,%s)""",
                        (source_path, action, chunks_count, duration_ms, (error or "")[:500] if error else None))
            conn.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Gestion self-service de agentes / keys (tabla `agents`)
# --------------------------------------------------------------------------
# La columna `api_key_hash` almacena sha256(token) en hex. NUNCA se guarda el
# token en claro. Los 4 lideres (merlin/rias/saruman/radagast) tienen
# is_lead=true, team=<su nombre>. Un lider provisiona/revoca subagentes de SU team.
LEADERS = agent_config.leaders()


def key_sha256(raw_key: str) -> str:
    """sha256 hex del token (mismo formato que api_key_hash)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def agent_by_key_hash(key_hash: str) -> Optional[str]:
    """Devuelve el `name` de un agente ACTIVE cuya api_key_hash coincide, o None.
    Usado como fallback de auth para keys provisionadas por self-service."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM agents WHERE api_key_hash=%s AND active=true LIMIT 1",
                (key_hash,))
            row = cur.fetchone()
        return row["name"] if row else None
    except Exception:
        return None


def agent_exists(name: str) -> bool:
    """FIX agujero de revocacion (2026-08-03): True si EXISTE una fila para `name`,
    con independencia de `active`.

    Se usa para decidir si la via `.env` sigue siendo legitima para un agente:
    solo lo es cuando el agente NO tiene fila todavia (bootstrap de primer arranque).
    NO sirve `agent_row()` para esto porque filtra `active=true` y por lo tanto no
    distingue "sin fila" de "fila revocada" — y esa distincion ES el fix.

    DELIBERADO: ante error de DB devuelve True (=> la via .env queda CERRADA, fail-closed).
    Un `return False` aqui reintroduciria el agujero cada vez que Postgres tosa.
    """
    if not name:
        return True
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agents WHERE lower(name)=lower(%s) LIMIT 1", (name,))
            return cur.fetchone() is not None
    except Exception:
        return True


def agent_row(name: str) -> Optional[dict]:
    """Authz Fase 3b: fila de un agente ACTIVO por name. None si no existe O si
    esta inactivo (el WHERE filtra active=true).
    ⚠️ Esa ambigüedad era un agujero: quien la consuma NO puede distinguir
    'no existe' de 'revocado', y `resolve_agent_scope` colapsaba ambos en
    fail-open (scope total). Para desambiguar existe `agent_row_any` (FIX 5).
    Incluye `team` (FIX 4): lo necesita el self-scope de cosecha
    'memoria/<team>-<name>/'."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                # AUTHZ T1 (delegacion): se agregan `kind` y `name` al SELECT.
                # `kind`: _resolve_delegation exige kind='human' en el delegado.
                # `name`: el match es case-insensitive (lower(name)=lower(%s)), asi
                #   que sin el nombre CANONICO de la DB, 'OTIXE' y 'otixe' serian
                #   dos agentes distintos en audit_log y dos buckets del rate-limiter.
                # Aditivo: los consumidores existentes leen con .get().
                "SELECT id, name, role, owner_principal_id, team, active, kind FROM agents "
                "WHERE lower(name)=lower(%s) AND active=true LIMIT 1", (name,))
            return cur.fetchone()
    except Exception:
        return None


def agent_row_any(name: str) -> Optional[dict]:
    """FIX 5 (2026-08-04): fila de un agente SIN filtrar por `active`.
    Sirve para distinguir 'la fila no existe' (agente de sistema/.env => fail-open
    historico) de 'la fila existe pero active=false' (key REVOCADA => debe ser
    fail-CLOSED). No se usa para autorizar: solo para desambiguar el borde."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, role, owner_principal_id, team, active FROM agents "
                "WHERE lower(name)=lower(%s) LIMIT 1", (name,))
            return cur.fetchone()
    except Exception:
        return None


def scope_grants_of(principal_id: int) -> list[dict]:
    """Authz Fase 3b: scope_grants activos de un principal (path_prefix, can_write)."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT path_prefix, can_write FROM scope_grants "
                "WHERE principal_id=%s AND revoked_at IS NULL", (principal_id,))
            return list(cur.fetchall())
    except Exception:
        return []


def is_lead(name: str) -> bool:
    """True si `name` es lider (por set canonico o flag is_lead activo en DB)."""
    if name and name.lower() in LEADERS:
        return True
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT is_lead FROM agents WHERE lower(name)=lower(%s) AND active=true",
                        (name,))
            row = cur.fetchone()
        return bool(row and row["is_lead"])
    except Exception:
        return False


def chunk_owner(chunk_id: str) -> Optional[str]:
    """`agent_owner` de un chunk. None si no existe o si no se pudo consultar.
    Lo usa el gate de propiedad de /memory/supersede: solo el dueno (o un lider)
    puede jubilar una memoria."""
    if not chunk_id:
        return None
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT agent_owner FROM chunks_meta WHERE id=%s LIMIT 1", (chunk_id,))
            row = cur.fetchone()
        return (row["agent_owner"] if row else None)
    except Exception:
        return None


def team_by_agent_key(key: str) -> Optional[str]:
    """Equipo del agente cuyo nombre NORMALIZADO coincide con `key`.

    `key` es el nombre sin caracteres no alfanumericos y en minusculas, para que la
    carpeta `50 Agents/Le Fay/` resuelva al agente `lefay` y `50 Agents/Saruman/`
    a `saruman`. Lo usa la denylist de escritura cross-team de app.py.

    Devuelve None si ninguna carpeta corresponde a un agente conocido: eso incluye
    las carpetas COMPARTIDAS (Harness/, Convenciones/, Templates/, _Shared-Tools/),
    que a proposito quedan sin dueno y por lo tanto libres para todos."""
    if not key:
        return None
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT team FROM agents "
                " WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') = %s "
                "   AND active=true AND coalesce(team,'') <> '' LIMIT 1", (key,))
            row = cur.fetchone()
        return (row["team"] if row else None)
    except Exception:
        return None


# RBAC de tareas (FASE A, 2026-07-28): clases de EJECUCION validas.
# Ortogonal a `role` (admin|member|guest = scope de vault). Ver
# db/postgres/migracion-rbac-execution-class.sql y app.resolve_execution_class.
EXECUTION_CLASSES = {"read-only", "writer", "executor", "auditor", "orchestrator"}


def agent_execution_class(name: str) -> Optional[str]:
    """RBAC Fase A: `agents.execution_class` de un agente ACTIVE, o None.

    None significa 'sin clase declarada' (columna NULL, agente inexistente/inactivo,
    o error de DB) y el caller DEBE tratarlo fail-closed (=> 'read-only'). Nunca
    devuelve un valor fuera de EXECUTION_CLASSES: un valor desconocido en DB se
    reporta como None para que el caller caiga a read-only.
    Case-insensitive por name, igual que agent_row/is_lead."""
    if not name:
        return None
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT execution_class FROM agents "
                "WHERE lower(name)=lower(%s) AND active=true LIMIT 1", (name,))
            row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    val = (row.get("execution_class") or "").strip().lower()
    return val if val in EXECUTION_CLASSES else None


def provision_agent(name: str, key_hash: str, team: str, created_by: str) -> str:
    """Crea (o re-activa si estaba revocado) un subagente.
    Devuelve: 'ok' | 'exists' (activo, 409) | 'is_leader' (prohibido).
    Comparacion de nombre case-insensitive; nunca provisiona un lider."""
    if name.lower() in LEADERS:
        return "is_leader"
    with pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, is_lead, active FROM agents WHERE lower(name)=lower(%s)", (name,))
        row = cur.fetchone()
        if row:
            if row["is_lead"]:
                return "is_leader"
            if row["active"]:
                return "exists"
            # Estaba revocado: re-provisiona con nueva key
            cur.execute(
                """UPDATE agents SET api_key_hash=%s, team=%s, is_lead=false,
                   active=true, created_by=%s, created_at=now(), revoked_at=NULL
                   WHERE lower(name)=lower(%s)""",
                (key_hash, team, created_by, name))
        else:
            cur.execute(
                """INSERT INTO agents (name, api_key_hash, team, is_lead, active, created_by)
                   VALUES (%s,%s,%s,false,true,%s)""",
                (name, key_hash, team, created_by))
        conn.commit()
    return "ok"


def provision_leader(name: str, key_hash: str, created_by: str) -> str:
    """Crea (o re-activa si estaba revocado) un LIDER nuevo con equipo propio
    (is_lead=true, team=<su propio nombre>). El lider resultante puede a su vez
    provisionar subagentes (provision_agent) y otros lideres (provision_leader).
    Devuelve: 'ok' | 'exists' (activo, 409) | 'is_leader' (nombre lider canonico).
    Restringido a callers lider (enforced en el endpoint). Case-insensitive."""
    if name.lower() in LEADERS:
        return "is_leader"
    with pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, is_lead, active FROM agents WHERE lower(name)=lower(%s)", (name,))
        row = cur.fetchone()
        if row:
            if row["active"]:
                return "exists"
            # Estaba revocado: re-provisiona como LIDER
            cur.execute(
                """UPDATE agents SET api_key_hash=%s, team=%s, is_lead=true,
                   active=true, created_by=%s, created_at=now(), revoked_at=NULL
                   WHERE lower(name)=lower(%s)""",
                (key_hash, name, created_by, name))
        else:
            cur.execute(
                """INSERT INTO agents (name, api_key_hash, team, is_lead, active, created_by)
                   VALUES (%s,%s,%s,true,true,%s)""",
                (name, key_hash, name, created_by))
        conn.commit()
    return "ok"


def revoke_agent(name: str, caller: str) -> str:
    """Revoca (soft) un subagente del team del caller.
    Devuelve: 'ok' | 'not_found' | 'forbidden' (lider o de otro team).
    NO borra datos historicos: solo active=false + revoked_at=now()."""
    with pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, is_lead, team, active FROM agents WHERE lower(name)=lower(%s)",
                    (name,))
        row = cur.fetchone()
        if not row:
            return "not_found"
        if row["is_lead"] or (row["team"] or "").lower() != caller.lower():
            return "forbidden"
        cur.execute(
            "UPDATE agents SET active=false, revoked_at=now() WHERE lower(name)=lower(%s)",
            (name,))
        conn.commit()
    return "ok"


def list_agents_exec_class(name: Optional[str] = None) -> list[dict]:
    """execution_class de la flota (o de un agente si se pasa `name`).
    LECTURA ABIERTA a lideres: ver la clase de otro equipo no da ningun poder sobre
    el, y sirve para diagnosticar un 403 ajeno sin pedirle a nadie que corra SQL.
    Tarea 6a611e35 (pedido de rudeus)."""
    sql = ("SELECT name, team, is_lead, role, execution_class, active "
           "FROM agents")
    params: list = []
    if name:
        sql += " WHERE lower(name)=lower(%s)"
        params.append(name)
    sql += " ORDER BY is_lead DESC, team, name"
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def set_agent_exec_class(name: str, cls: str, *, caller: str) -> tuple[str, Optional[str]]:
    """Setea execution_class de un agente de TU team. Devuelve (status, valor_anterior).

    status: 'ok' | 'not_found' | 'is_leader' | 'other_team'
    POLITICA (Otixe 2026-08-05): un lider cambia la clase de SUS agentes, no la de
    agentes de otro grupo. A un LIDER no se le cambia por API (es orchestrator por
    su flag is_lead, no por la columna).
    El chequeo de equipo va en el SQL, no en la superficie: asi vale para cualquier
    transporte que llame a esta funcion y no depende de que el llamador se acuerde."""
    try:
        with pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, team, is_lead, execution_class FROM agents "
                        " WHERE lower(name)=lower(%s) LIMIT 1", (name,))
            row = cur.fetchone()
            if not row:
                return "not_found", None
            if row["is_lead"]:
                return "is_leader", row["execution_class"]
            if (row["team"] or "").lower() != (caller or "").lower():
                return "other_team", row["execution_class"]
            before = row["execution_class"]
            cur.execute("UPDATE agents SET execution_class=%s WHERE lower(name)=lower(%s)",
                        (cls, name))
            conn.commit()
            return "ok", before
    except Exception:
        return "not_found", None


def list_team_agents(team: str) -> list[dict]:
    """Lista los agentes cuyo team == caller (name, active, created_at)."""
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT name, active, created_at FROM agents
               WHERE lower(team)=lower(%s) ORDER BY created_at""",
            (team,))
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# SKILLS: lectura integra (skill_get) + enumeracion de references. ADITIVO.
# Solo SELECT sobre chunks_meta. No hay DDL: un skill es source_path='skills/<name>'
# y sus references son 'skills/<name>/references/<archivo>' (jerarquia = indice).
# --------------------------------------------------------------------------
def _sanitize_path_input(raw: str, *, label: str) -> str:
    """Pipeline COMPARTIDO de sanitizacion de un input de path logico (skill o filename).
    Unico criterio para ambos normalizadores -> simetria garantizada (ronda 2 FIX-2).

    Orden (importa): 1) strip + no vacio  2) '\\' -> '/'  3) unquote repetido (deshace
    %2f/%2e y doble encoding tipo %252f)  4) RECHAZO de caracteres de control / NUL.

    (4) es FIX-1: un NUL (0x00) que llegaba entero al INSERT reventaba en psycopg
    ('PostgreSQL text fields cannot contain NUL bytes') -> HTTP 500. Se rechaza DESPUES
    del unquote (para atrapar %00) y ANTES de validar segmentos, y el caller lo traduce
    a 400. Mismo patron de NUL que ya nos mordio en el indexer: filtrar en TODO borde
    de escritura. NO valida traversal/prefijos: eso es especifico de cada normalizador."""
    s = (raw or "").strip()
    if not s:
        raise ValueError(f"{label} vacio")
    s = s.replace("\\", "/")
    from urllib.parse import unquote
    prev = None
    for _ in range(3):
        if s == prev:
            break
        prev = s
        s = unquote(s)
    s = s.replace("\\", "/")
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise ValueError(f"{label} contiene caracteres de control")
    return s


def normalize_skill_name(raw: str) -> str:
    """Normaliza el input de nombre de skill ANTES de construir el source_path.
    Acepta '<name>', 'skills/<name>' o '<skill>/references/<archivo>' (y variantes con
    '/' inicial o 'obsidian-vault/').
    Lanza ValueError si hay traversal ('..'), caracteres de control o queda vacio.
    Leccion 2026-07-27 (bug vault_write): validar 'esta dentro del namespace' NO alcanza
    si el input no se normaliza primero.
    Ronda 2 FIX-2: comparte _sanitize_path_input con normalize_reference_filename, asi
    'name' y 'filename' resuelven percent-encoding y control chars con el MISMO criterio."""
    s = _sanitize_path_input(raw, label="nombre de skill")
    if s.startswith("obsidian-vault/"):
        s = s[len("obsidian-vault/"):]
    s = s.lstrip("/")
    if s.startswith("skills/"):
        s = s[len("skills/"):]
    s = s.strip().strip("/")
    if not s:
        raise ValueError("nombre de skill vacio tras normalizar")
    parts = [seg for seg in s.split("/")]
    for seg in parts:
        if seg in ("", ".", ".."):
            raise ValueError("nombre de skill invalido (traversal o segmento vacio)")
    return "/".join(parts)


def normalize_reference_filename(raw: str) -> str:
    """Normaliza/valida el filename de un reference. Rechaza traversal, '/' inicial,
    paths absolutos, caracteres de control/NUL y segmentos vacios.
    Permite subcarpetas simples (a/b.md).
    Ronda 2 FIX-1/FIX-2: el unquote repetido y el rechazo de control chars viven ahora en
    _sanitize_path_input, compartido con normalize_skill_name."""
    s = _sanitize_path_input(raw, label="filename")
    if s.startswith("/"):
        raise ValueError("filename no puede ser absoluto")
    parts = s.split("/")
    for seg in parts:
        if seg in ("", ".", ".."):
            raise ValueError("filename invalido (traversal o segmento vacio)")
    return "/".join(parts)


def get_skill_full(name: str, *, include_references: bool = True) -> Optional[dict]:
    """Devuelve el SKILL.md INTEGRO de una skill por nombre (sin truncar).
    `name` ya debe venir normalizado (normalize_skill_name). Solo chunks vigentes:
    deleted_at IS NULL AND (valid_until IS NULL OR valid_until > now()).
    Si hay >1 chunk se concatenan por chunk_index ASC.
    Devuelve None si no existe ninguna fila para ese source_path;
    {'soft_deleted': True} si existe pero esta soft-deleted / no vigente.

    Ronda 2 FIX-4: `name` puede ser una skill raiz ('<skill>') o un reference
    ('<skill>/references/<archivo>'). El dict declara explicitamente 'kind'
    ('skill' | 'reference') y, si es reference, 'parent_skill'. Antes un reference
    devolvia references_count=0 sin forma de distinguirlo de una skill sin references."""
    sp = f"skills/{name}"
    if "/references/" in name:
        kind = "reference"
        parent_skill = name.split("/references/", 1)[0] or None
    else:
        kind = "skill"
        parent_skill = None
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT chunk_index, title, tags, agent_owner, sensitivity, category,
                      created_at, updated_at, text_full, text_preview
                 FROM chunks_meta
                WHERE source_path = %s
                  AND deleted_at IS NULL
                  AND (valid_until IS NULL OR valid_until > now())
                ORDER BY chunk_index ASC""",
            (sp,))
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            cur.execute(
                "SELECT count(*) AS n FROM chunks_meta WHERE source_path = %s", (sp,))
            existed = (cur.fetchone() or {}).get("n", 0)
            if existed:
                return {"soft_deleted": True, "source_path": sp,
                        "kind": kind, "parent_skill": parent_skill}
            return None
        refs = []
        if include_references and kind == "skill":
            cur.execute(
                """SELECT source_path,
                          count(*) AS chunks,
                          coalesce(sum(length(coalesce(text_full, ''))), 0) AS chars
                     FROM chunks_meta
                    WHERE source_path LIKE %s
                      AND deleted_at IS NULL
                      AND (valid_until IS NULL OR valid_until > now())
                    GROUP BY source_path
                    ORDER BY source_path ASC""",
                (sp + "/references/%",))
            refs = [
                {"path": r["source_path"], "chars": int(r["chars"]), "chunks": int(r["chunks"])}
                for r in cur.fetchall()
            ]
    parts = []
    for r in rows:
        t = r.get("text_full")
        if t is None:
            # sensitivity secret/metadata_only no persiste text_full en Postgres
            t = r.get("text_preview") or ""
        parts.append(t)
    content = "\n".join(parts) if len(parts) > 1 else (parts[0] if parts else "")
    first = rows[0]
    return {
        "source_path": sp,
        "kind": kind,
        "parent_skill": parent_skill,
        "content": content,
        "chars": len(content),
        "chunk_count": len(rows),
        "title": first.get("title") or name,
        "metadata": {
            "tags": list(first.get("tags") or []),
            "category": first.get("category"),
            "agent_owner": first.get("agent_owner"),
            "sensitivity": first.get("sensitivity"),
            "created_at": first["created_at"].isoformat() if first.get("created_at") else None,
            "updated_at": first["updated_at"].isoformat() if first.get("updated_at") else None,
        },
        "references": refs,
        "references_count": len(refs),
    }


def skill_exists(name: str) -> bool:
    """True si existe la skill raiz vigente 'skills/<name>' (name ya normalizado)."""
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM chunks_meta
                WHERE source_path = %s AND deleted_at IS NULL
                  AND (valid_until IS NULL OR valid_until > now())
                LIMIT 1""",
            (f"skills/{name}",))
        return cur.fetchone() is not None


# --------------------------------------------------------------------------
# SKILLS: enumeracion del registro (skill_list). ADITIVO PURO (solo SELECT).
# Un skill es la fila raiz source_path='skills/<name>'; sus references viven en
# 'skills/<name>/references/<archivo>' y NO son skills -> se EXCLUYEN.
# DEDUP: un skill puede estar chunkeado (varias filas con el mismo source_path);
# se devuelve UNA entrada por source_path (la del chunk_index mas bajo, que es la
# que trae el encabezado 'SKILL:/DESCRIPCION:' que escribe skill_add) + chunk_count.
# --------------------------------------------------------------------------
_SKILL_PREFIX_LIKE = "skills/%"
_SKILL_REFS_LIKE = "skills/%/references/%"


def _skill_description_from_text(text: Optional[str], *, fallback: str = "") -> str:
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
        for tag in ("DESCRIPCION:", "DESCRIPCIÓN:", "DESCRIPTION:"):
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


def list_skills(*, q: str = "", tag: str = "", limit: int = 50, offset: int = 0,
                include_references_count: bool = False) -> dict:
    """Enumera los skills VIGENTES del registro compartido, deduplicados por skill.

    Filtros (opcionales, combinables con AND):
      q   -> substring case-insensitive contra el nombre, el title y el texto del skill.
      tag -> el skill tiene ese tag exacto en chunks_meta.tags (el tag 'skill' que
             pone skill_add cuenta, asi que tag='skill' devuelve todos).
    Paginacion: limit/offset sobre el orden estable por nombre (source_path ASC).
    `total` es el conteo COMPLETO de skills que matchean (sin paginar), para que el
    caller sepa si hay mas paginas.
    include_references_count=True agrega 'references_count' por skill (1 query extra
    agregada; off por default para no pagar el GROUP BY cuando no se usa).

    Solo filas vigentes: deleted_at IS NULL AND (valid_until IS NULL OR valid_until > now()).
    EXCLUYE los references hijos ('skills/<name>/references/%'): no son skills."""
    lim = max(1, min(int(limit or 50), 500))
    off = max(0, int(offset or 0))
    where = ["source_path LIKE %s", "source_path NOT LIKE %s",
             "deleted_at IS NULL", "(valid_until IS NULL OR valid_until > now())"]
    params: list = [_SKILL_PREFIX_LIKE, _SKILL_REFS_LIKE]
    if q:
        where.append("(source_path ILIKE %s OR coalesce(title,'') ILIKE %s "
                     "OR coalesce(text_full, text_preview, '') ILIKE %s)")
        like = f"%{q}%"
        params += [like, like, like]
    if tag:
        where.append("%s = ANY(tags)")
        params.append(tag)
    clause = " AND ".join(where)

    base = f"""
        SELECT source_path, chunk_index, title, tags, agent_owner, sensitivity,
               category, created_at, updated_at, text_preview, text_full,
               count(*) OVER (PARTITION BY source_path) AS chunk_count,
               row_number() OVER (PARTITION BY source_path ORDER BY chunk_index ASC) AS rn
          FROM chunks_meta
         WHERE {clause}
    """
    with pg() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT count(DISTINCT source_path) AS n FROM chunks_meta WHERE {clause}",
            params)
        total = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            f"SELECT * FROM ({base}) s WHERE rn = 1 ORDER BY source_path ASC "
            "LIMIT %s OFFSET %s",
            params + [lim, off])
        rows = [dict(r) for r in cur.fetchall()]

        refs_by_skill: dict = {}
        if include_references_count and rows:
            paths = [r["source_path"] for r in rows]
            cur.execute(
                """SELECT split_part(source_path, '/references/', 1) AS skill_path,
                          count(DISTINCT source_path) AS n
                     FROM chunks_meta
                    WHERE source_path LIKE %s
                      AND deleted_at IS NULL
                      AND (valid_until IS NULL OR valid_until > now())
                      AND split_part(source_path, '/references/', 1) = ANY(%s)
                    GROUP BY 1""",
                (_SKILL_REFS_LIKE, paths))
            refs_by_skill = {r["skill_path"]: int(r["n"]) for r in cur.fetchall()}

    out = []
    for r in rows:
        sp = r["source_path"]
        name = sp[len("skills/"):] if sp.startswith("skills/") else sp
        body = r.get("text_full") or r.get("text_preview") or ""
        title = r.get("title") or name
        item = {
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
        }
        if include_references_count:
            item["references_count"] = refs_by_skill.get(sp, 0)
        out.append(item)
    return {"skills": out, "count": len(out), "total": total,
            "limit": lim, "offset": off,
            "has_more": (off + len(out)) < total}
