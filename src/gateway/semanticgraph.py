"""semanticgraph.py - Grafo SEMANTICO automatico (Cerebro v2, Gap 1, FASE 1 determinista).

Hermano de notegraph.py, pero la señal NO son wikilinks: son las relaciones inferidas
por SIMILITUD VECTORIAL (coseno) entre los embeddings que YA existen en Qdrant
(Bedrock Titan v2, 1024d). SIN LLM, sin re-embeddear: solo k-NN determinista y auditable.

Arquitectura:
  - rebuild(): job BATCH (caro, solo-lider) que para cada chunk activo hace k-NN en Qdrant,
    filtra por umbral y persiste aristas (src,dst,score) en la tabla semantic_edges.
  - query(): LEE semantic_edges (barato, sin computo pesado). Es lo que consume el agente.
  - stats(): totales de la tabla.

Reusa el cliente Qdrant y el pool Postgres existentes (db.qdrant(), db.pg(), db.COLLECTION).
Additive: tabla propia semantic_edges; NO toca notegraph ni chunks_meta ni /graph/query.
"""
import os
import time

import db
from qdrant_client.models import Filter, FieldCondition, MatchValue

# Defaults tuneables (punto de partida; ajustables por parametro del rebuild/endpoint).
DEFAULT_THRESHOLD = float(os.environ.get("SEMGRAPH_THRESHOLD", "0.75"))
DEFAULT_TOP_N = int(os.environ.get("SEMGRAPH_TOP_N", "8"))
DEFAULT_MIN_SCORE = DEFAULT_THRESHOLD
DEFAULT_QUERY_LIMIT = 20


def _neighbor_filter():
    """Excluye del pool de vecinos los chunks borrados/superseded/archivados,
    misma higiene que db.search (no queremos aristas hacia basura)."""
    return Filter(must_not=[
        FieldCondition(key="deleted", match=MatchValue(value=True)),
        FieldCondition(key="superseded", match=MatchValue(value=True)),
        FieldCondition(key="archived", match=MatchValue(value=True)),
    ])


def rebuild(threshold: float = None, top_n: int = None, max_chunks: int = None,
            progress_cb=None) -> dict:
    """Job BATCH: reconstruye semantic_edges por k-NN en Qdrant.

    Para cada chunk activo (chunks_meta.deleted_at IS NULL), busca top_n+1 vecinos por
    coseno (el +1 porque el propio chunk sale con score~1.0 y se descarta), filtra
    score >= threshold, dst != src y dst_source != src_source (no auto-aristas dentro
    de la misma nota), y UPSERT a semantic_edges.

    FASE 1: TRUNCATE + recompute completo (simple y auditable). max_chunks limita solo
    la cantidad de chunks ORIGEN a procesar (para rebuild acotado de validacion); los
    vecinos siempre se buscan contra la coleccion completa.

    Devuelve {edges_written, chunks_processed, chunks_total, threshold, top_n, duration}.
    """
    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    top_n = DEFAULT_TOP_N if top_n is None else int(top_n)
    t0 = time.time()

    q = db.qdrant()
    nfilter = _neighbor_filter()

    # 1) Chunks origen (activos). max_chunks acota solo el origen.
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM chunks_meta WHERE deleted_at IS NULL")
        chunks_total = int(cur.fetchone()["c"])
        sql = ("SELECT id, source_path FROM chunks_meta "
               "WHERE deleted_at IS NULL ORDER BY id")
        if max_chunks:
            sql += " LIMIT %s"
            cur.execute(sql, (int(max_chunks),))
        else:
            cur.execute(sql)
        sources = [(str(r["id"]), r["source_path"]) for r in cur.fetchall()]

    # 2) k-NN por chunk. Acumula aristas en memoria; flush por lotes a Postgres.
    edges = []          # (src_chunk, dst_chunk, src_source, dst_source, score)
    edges_written = 0
    chunks_processed = 0
    BATCH = 2000

    def _flush(rows):
        if not rows:
            return 0
        with db.pg() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO semantic_edges "
                "(src_chunk, dst_chunk, src_source, dst_source, score) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (src_chunk, dst_chunk) DO UPDATE SET "
                "  score=EXCLUDED.score, src_source=EXCLUDED.src_source, "
                "  dst_source=EXCLUDED.dst_source, computed_at=now()",
                rows)
            conn.commit()
        return len(rows)

    # TRUNCATE: recompute completo, determinista (FASE 1).
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE semantic_edges")
        conn.commit()

    for src_id, src_source in sources:
        try:
            resp = q.query_points(
                collection_name=db.COLLECTION,
                query=src_id,                 # query-by-point-id (usa el vector ya almacenado)
                limit=top_n + 1,
                query_filter=nfilter,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            chunks_processed += 1
            continue
        kept = 0
        for pt in resp.points:
            dst_id = str(pt.id)
            if dst_id == src_id:
                continue
            score = float(pt.score)
            if score < threshold:
                continue
            dst_source = (pt.payload or {}).get("source_path", "")
            if not dst_source or dst_source == src_source:
                continue          # no auto-aristas dentro de la misma nota
            edges.append((src_id, dst_id, src_source, dst_source, round(score, 6)))
            kept += 1
            if kept >= top_n:
                break
        chunks_processed += 1
        if len(edges) >= BATCH:
            edges_written += _flush(edges)
            edges = []
        # Progreso periodico (para jobs async): cada 500 chunks origen.
        if progress_cb is not None and chunks_processed % 500 == 0:
            try:
                progress_cb(chunks_processed, chunks_total, edges_written + len(edges))
            except Exception:
                pass

    edges_written += _flush(edges)

    return {
        "edges_written": edges_written,
        "chunks_processed": chunks_processed,
        "chunks_total": chunks_total,
        "threshold": threshold,
        "top_n": top_n,
        "duration": round(time.time() - t0, 3),
    }


def _resolve_source(cur, source_path: str, denied=None):
    """Resuelve source_path a un valor exacto de la tabla. Exacto gana; si no,
    LIKE %substr% sobre src_source. Devuelve (resolved, error)."""
    sp = (source_path or "").strip()
    if not sp:
        return None, "source vacio"
    deny_sql = " AND NOT (regexp_replace(src_source, '^(obsidian-vault/)+', '') ILIKE ANY(%s))" if denied else ""
    deny_args = (denied,) if denied else ()
    cur.execute("SELECT 1 FROM semantic_edges WHERE src_source=%s" + deny_sql + " LIMIT 1", (sp,) + deny_args)
    if cur.fetchone():
        return sp, None
    cur.execute(
        "SELECT DISTINCT src_source FROM semantic_edges WHERE src_source ILIKE %s" + deny_sql + " LIMIT 11",
        ("%" + sp + "%",) + deny_args)
    cands = [r["src_source"] for r in cur.fetchall()]
    if len(cands) == 1:
        return cands[0], None
    if len(cands) > 1:
        return None, "ambiguo: %r matchea %d fuentes: %s" % (source_path, len(cands), cands[:10])
    return None, "no encontrada: %r no tiene aristas semanticas (rebuild pendiente?)" % source_path


def query(source_path: str, direction: str = "out", min_score: float = None,
          limit: int = DEFAULT_QUERY_LIMIT, deny_prefixes=None) -> dict:
    """Lee semantic_edges: notas relacionadas SEMANTICAMENTE con source_path.
    direction: out (source_path es origen) | in (es destino) | both. Determinista, sin
    computo pesado. Agrega por (nota relacionada) quedandose con el MAX score entre sus
    chunks, y ordena por score decreciente."""
    direction = (direction or "out").lower()
    if direction not in ("out", "in", "both"):
        return {"source": source_path, "resolved": None,
                "error": "direction invalida: %r (use out|in|both)" % direction}
    min_score = DEFAULT_MIN_SCORE if min_score is None else float(min_score)
    limit = int(limit) if limit else DEFAULT_QUERY_LIMIT

    denied = [p + "%" for p in (deny_prefixes or [])]
    with db.pg() as conn, conn.cursor() as cur:
        resolved, err = _resolve_source(cur, source_path, denied)
        if err:
            return {"source": source_path, "resolved": None, "error": err}

        out_rows, in_rows = [], []
        if direction in ("out", "both"):
            cur.execute(
                "SELECT dst_source AS related, max(score) AS score, count(*) AS via_chunks "
                "FROM semantic_edges WHERE src_source=%s AND score>=%s "
                + ("AND NOT (regexp_replace(dst_source, '^(obsidian-vault/)+', '') ILIKE ANY(%s)) " if denied else "") +
                "GROUP BY dst_source ORDER BY score DESC LIMIT %s",
                (resolved, min_score) + ((denied,) if denied else ()) + (limit,))
            out_rows = [{"source": r["related"], "score": round(float(r["score"]), 6),
                         "via_chunks": int(r["via_chunks"])} for r in cur.fetchall()]
        if direction in ("in", "both"):
            cur.execute(
                "SELECT src_source AS related, max(score) AS score, count(*) AS via_chunks "
                "FROM semantic_edges WHERE dst_source=%s AND score>=%s "
                + ("AND NOT (regexp_replace(src_source, '^(obsidian-vault/)+', '') ILIKE ANY(%s)) " if denied else "") +
                "GROUP BY src_source ORDER BY score DESC LIMIT %s",
                (resolved, min_score) + ((denied,) if denied else ()) + (limit,))
            in_rows = [{"source": r["related"], "score": round(float(r["score"]), 6),
                        "via_chunks": int(r["via_chunks"])} for r in cur.fetchall()]

    return {
        "source": source_path,
        "resolved": resolved,
        "direction": direction,
        "min_score": min_score,
        "out": out_rows,
        "in": in_rows,
        "out_count": len(out_rows),
        "in_count": len(in_rows),
    }


def stats() -> dict:
    """Totales de la tabla semantic_edges: aristas, fuentes distintas, score promedio,
    ultimo rebuild (max computed_at)."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total_edges, "
            "       count(DISTINCT src_source) AS distinct_sources, "
            "       avg(score) AS avg_score, "
            "       max(computed_at) AS last_rebuild "
            "FROM semantic_edges")
        r = cur.fetchone()
    return {
        "total_edges": int(r["total_edges"] or 0),
        "distinct_sources": int(r["distinct_sources"] or 0),
        "avg_score": round(float(r["avg_score"]), 6) if r["avg_score"] is not None else None,
        "last_rebuild": r["last_rebuild"].isoformat() if r["last_rebuild"] else None,
        "threshold_default": DEFAULT_THRESHOLD,
        "top_n_default": DEFAULT_TOP_N,
    }


# ----------------------------------------------------------------------------
# Jobs ASYNC de rebuild (Gap 1 Cerebro v2, 2026-07-20).
# El full (~50k chunks, ~27min) no puede correr sincrono en un request HTTP.
# El endpoint POST /graph/semantic-rebuild (async) crea un job, lo corre en el
# threadpool (BackgroundTasks) y persiste su estado en semantic_rebuild_jobs.
# GET /graph/semantic-rebuild-status lee el estado. Determinista y auditable.
# ----------------------------------------------------------------------------
def create_job(triggered_by: str, threshold=None, top_n=None, max_chunks=None) -> str:
    """Inserta un job en estado 'running' y devuelve su id (uuid str)."""
    thr = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    tn = DEFAULT_TOP_N if top_n is None else int(top_n)
    mc = int(max_chunks) if max_chunks else None
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO semantic_rebuild_jobs "
            "(status, triggered_by, threshold, top_n, max_chunks) "
            "VALUES ('running', %s, %s, %s, %s) RETURNING id",
            (triggered_by, thr, tn, mc))
        job_id = str(cur.fetchone()["id"])
        conn.commit()
    return job_id


def _job_progress(job_id: str, chunks_processed: int, chunks_total: int, edges_written: int):
    """Callback de progreso: actualiza contadores del job en curso (best-effort)."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE semantic_rebuild_jobs SET chunks_processed=%s, chunks_total=%s, "
            "edges_written=%s WHERE id=%s",
            (chunks_processed, chunks_total, edges_written, job_id))
        conn.commit()


def run_rebuild_job(job_id: str, threshold=None, top_n=None, max_chunks=None) -> dict:
    """Ejecuta rebuild() para un job ya creado, actualizando estado/progreso y
    marcando done|failed al terminar. Pensado para correr en un thread (no bloquea
    el event loop). Devuelve el stats final (o el error)."""
    def cb(cp, ct, ew):
        _job_progress(job_id, cp, ct, ew)
    try:
        stats = rebuild(threshold=threshold, top_n=top_n, max_chunks=max_chunks,
                        progress_cb=cb)
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE semantic_rebuild_jobs SET status='done', "
                "chunks_processed=%s, chunks_total=%s, edges_written=%s, "
                "finished_at=now() WHERE id=%s",
                (stats["chunks_processed"], stats["chunks_total"],
                 stats["edges_written"], job_id))
            conn.commit()
        stats["job_id"] = job_id
        stats["status"] = "done"
        return stats
    except Exception as e:
        err = repr(e)[:2000]
        try:
            with db.pg() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE semantic_rebuild_jobs SET status='failed', error=%s, "
                    "finished_at=now() WHERE id=%s", (err, job_id))
                conn.commit()
        except Exception:
            pass
        return {"job_id": job_id, "status": "failed", "error": err}


def job_status(job_id: str = None) -> dict:
    """Estado de un job por id, o el ULTIMO job si job_id es None. Barato (lee tabla)."""
    with db.pg() as conn, conn.cursor() as cur:
        if job_id:
            cur.execute("SELECT * FROM semantic_rebuild_jobs WHERE id=%s", (job_id,))
        else:
            cur.execute(
                "SELECT * FROM semantic_rebuild_jobs ORDER BY started_at DESC LIMIT 1")
        r = cur.fetchone()
    if not r:
        return {"job_id": job_id, "status": "none",
                "error": "no hay jobs de rebuild" + (" con ese id" if job_id else "")}
    ct = r["chunks_total"] or 0
    cp = r["chunks_processed"] or 0
    progress = round(cp / ct, 4) if ct else None
    return {
        "job_id": str(r["id"]),
        "status": r["status"],
        "triggered_by": r["triggered_by"],
        "threshold": float(r["threshold"]) if r["threshold"] is not None else None,
        "top_n": r["top_n"],
        "max_chunks": r["max_chunks"],
        "chunks_total": ct,
        "chunks_processed": cp,
        "progress": progress,
        "edges_written": r["edges_written"] or 0,
        "error": r["error"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
    }
