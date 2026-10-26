"""consolidation.py - Consolidacion/olvido 'sueno' (Cerebro v2, Gap B, FASE 1).

El "sueno" del Cerebro: un job que REVISA la memoria y PROPONE consolidaciones, sin
tocar nada. Tres senales, todas deterministas y auditables:

  - DEDUP:  chunks activos casi-identicos (coseno >= umbral alto, def 0.93) detectados
            por k-NN en Qdrant. Reusa EXACTAMENTE el patron de semanticgraph.py
            (query-by-point-id contra la misma coleccion, mismo filtro de higiene).
            Propone pares near-dup para merge/supersede.
  - DECAY:  chunks candidatos a archivar: (a) superseded (valid_until IS NOT NULL o
            superseded_by IS NOT NULL) que siguen FUERA de un path archivado -> deberian
            moverse a Historico; (b) chunks viejos (created_at > N meses) aun activos.
            NOTA: no existe log de accesos en el schema, asi que "nunca recuperado" NO se
            puede medir con precision hoy; se aproxima por EDAD + estado bi-temporal.
  - SIGNAL DENSITY: por fuente, ratio de senal (activos vs soft-deleted, near-dups).

analyze() -> {near_dup_pairs[], decay_candidates[], signal_density}. SOLO PROPONE:
persiste los candidatos en consolidation_candidates con status 'proposed'. NUNCA borra
chunks ni memoria (Regla de Oro #1). La accion destructiva (supersede/archivar) la ejecuta
Monica/Otixe a partir de estos candidatos, NO este modulo.

Reusa db.qdrant(), db.pg(), db.COLLECTION, db.is_archived_path (higiene existente).
Additive: tabla propia consolidation_candidates; NO toca chunks_meta ni endpoints previos.
"""
import copy
import json
import os
import time
from datetime import datetime, timezone

import db
from qdrant_client.models import Filter, FieldCondition, MatchValue

# Defaults tuneables (punto de partida; ajustables por parametro del analyze/endpoint).
DEFAULT_DUP_THRESHOLD = float(os.environ.get("CONSOLIDATION_DUP_THRESHOLD", "0.93"))
DEFAULT_DECAY_MONTHS = int(os.environ.get("CONSOLIDATION_DECAY_MONTHS", "6"))
DEFAULT_TOP_N = int(os.environ.get("CONSOLIDATION_TOP_N", "5"))
DEFAULT_LIST_LIMIT = 100


def _neighbor_filter():
    """Mismo filtro de higiene que semanticgraph/db.search: excluye del pool de vecinos
    los chunks borrados/superseded/archivados. Para dedup solo comparamos ACTIVOS."""
    return Filter(must_not=[
        FieldCondition(key="deleted", match=MatchValue(value=True)),
        FieldCondition(key="superseded", match=MatchValue(value=True)),
        FieldCondition(key="archived", match=MatchValue(value=True)),
    ])


# ----------------------------------------------------------------------------
# 1) DEDUP: pares near-dup por k-NN (patron identico a semanticgraph.rebuild).
# ----------------------------------------------------------------------------
def find_near_dups(threshold: float = None, top_n: int = None,
                   max_chunks: int = None) -> list:
    """k-NN por chunk activo en Qdrant; retiene vecinos con coseno >= threshold (alto).
    Devuelve pares near-dup DEDUPLICADOS (cada par (a,b) una sola vez, con a<b por id).
    NO escribe nada; solo detecta. Reusa query-by-point-id (usa el vector ya almacenado).

    max_chunks acota solo la cantidad de chunks ORIGEN a procesar (para analyze acotado);
    los vecinos siempre se buscan contra la coleccion activa completa."""
    threshold = DEFAULT_DUP_THRESHOLD if threshold is None else float(threshold)
    top_n = DEFAULT_TOP_N if top_n is None else int(top_n)

    q = db.qdrant()
    nfilter = _neighbor_filter()

    with db.pg() as conn, conn.cursor() as cur:
        sql = ("SELECT id, source_path FROM chunks_meta "
               "WHERE deleted_at IS NULL AND valid_until IS NULL ORDER BY id")
        if max_chunks:
            sql += " LIMIT %s"
            cur.execute(sql, (int(max_chunks),))
        else:
            cur.execute(sql)
        sources = [(str(r["id"]), r["source_path"]) for r in cur.fetchall()]

    seen = set()            # frozenset({a,b}) para dedup simetrico
    pairs = []              # {src_chunk, dst_chunk, src_source, dst_source, score}
    for src_id, src_source in sources:
        try:
            resp = q.query_points(
                collection_name=db.COLLECTION,
                query=src_id,                 # query-by-point-id: usa el vector ya guardado
                limit=top_n + 1,
                query_filter=nfilter,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        for pt in resp.points:
            dst_id = str(pt.id)
            if dst_id == src_id:
                continue
            score = float(pt.score)
            if score < threshold:
                continue
            key = frozenset((src_id, dst_id))
            if key in seen:
                continue
            seen.add(key)
            dst_source = (pt.payload or {}).get("source_path", "")
            # Orden estable a<b por id, para persistencia idempotente.
            a, a_src, b, b_src = ((src_id, src_source, dst_id, dst_source)
                                  if src_id < dst_id
                                  else (dst_id, dst_source, src_id, src_source))
            pairs.append({
                "src_chunk": a, "dst_chunk": b,
                "src_source": a_src, "dst_source": b_src,
                "score": round(score, 6),
            })
    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs


# ----------------------------------------------------------------------------
# 2) DECAY: candidatos a archivar (bi-temporal + edad). Puramente SQL.
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# GUARDRAIL POR CONTENIDO (tarea 60fe2bd9, pedido de @rias). NO excluye nada: MARCA.
# Una memoria de causa raiz o un RESUELTO es justo la que NO conviene decaer -- es el
# conocimiento que evita repetir el incidente. Pero la decision es de Otixe/Monica, no
# del proponedor: excluirlas en silencio seria que el instrumento decida por el humano.
# Se ADVIERTE en el candidato para que la revision vea el riesgo sin tener que abrir
# cada chunk.
_PROTECTED_MARKERS = ("causa-raiz", "causa_raiz", "CAUSA-RAIZ", "RESUELTO", "resuelto",
                      "postmortem", "post-mortem", "incidente", "regla", "reglas-de-oro")


def _protected_hint(source_path: str) -> str:
    """Devuelve una etiqueta de advertencia si el path sugiere conocimiento que NO
    conviene decaer. Heuristica declarada: mira el PATH, no el contenido."""
    sp = (source_path or "")
    low = sp.lower()
    if any(m.lower() in low for m in _PROTECTED_MARKERS):
        return "revisar_con_cuidado: el path sugiere causa-raiz/resuelto/regla"
    if low.startswith("skills/"):
        return "revisar_con_cuidado: es una skill (procedimiento activo de la flota)"
    return ""

def find_decay_candidates(decay_months: int = None, max_chunks: int = None) -> list:
    """Candidatos a consolidar/archivar, dos sub-tipos (ambos SOLO propuesta):
      - 'superseded_unarchived': valid_until IS NOT NULL o superseded_by IS NOT NULL,
        pero el source_path NO esta en un path archivado (deberia moverse a Historico).
      - 'stale_old': chunk activo (deleted_at IS NULL, valid_until IS NULL) con
        created_at anterior a now()-decay_months, fuera de path archivado.
    NOTA honesta: no hay log de accesos en el schema -> "nunca recuperado" se aproxima
    por EDAD. Cuando exista retrieval-metrics (Gap A), esto se podra refinar.
    Devuelve lista de {chunk, source_path, created_at, reason}. NO escribe nada."""
    decay_months = DEFAULT_DECAY_MONTHS if decay_months is None else int(decay_months)
    interval = "%d months" % decay_months

    out = []
    with db.pg() as conn, conn.cursor() as cur:
        # (a) superseded/invalidados que siguen fuera de path archivado.
        cur.execute(
            "SELECT id, source_path, created_at, valid_until, superseded_by "
            "FROM chunks_meta "
            "WHERE deleted_at IS NULL "
            "  AND (valid_until IS NOT NULL OR superseded_by IS NOT NULL) "
            "ORDER BY valid_until NULLS LAST, created_at")
        for r in cur.fetchall():
            if db.is_archived_path(r["source_path"]):
                continue
            out.append({
                "chunk": str(r["id"]),
                "source_path": r["source_path"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "reason": "superseded_unarchived",
                # OJO: esta regla NO usa decay_months. Un candidato de hace 10 dias NO
                # es un bug del filtro temporal: es una memoria REEMPLAZADA que quedo
                # fuera de un path archivado. Sin el reason persistido, esa distincion
                # era invisible y parecia que el filtro de edad estaba roto.
                "valid_until": r["valid_until"].isoformat() if r.get("valid_until") else None,
                "superseded_by": str(r["superseded_by"]) if r.get("superseded_by") else None,
                "protected_hint": _protected_hint(r["source_path"]),
            })
        # (b) viejos aun activos y vigentes, fuera de path archivado.
        cur.execute(
            "SELECT id, source_path, created_at FROM chunks_meta "
            "WHERE deleted_at IS NULL AND valid_until IS NULL "
            "  AND created_at < now() - %s::interval "
            "ORDER BY created_at",
            (interval,))
        rows = cur.fetchall()
    for r in rows:
        if db.is_archived_path(r["source_path"]):
            continue
        _age = None
        if r["created_at"]:
            _age = int((datetime.now(timezone.utc) - r["created_at"]).days)
        out.append({
            "chunk": str(r["id"]),
            "source_path": r["source_path"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "reason": "stale_old",
            "age_days": _age,
            "decay_months": decay_months,
            "protected_hint": _protected_hint(r["source_path"]),
        })
    if max_chunks:
        out = out[:int(max_chunks)]
    return out


# ----------------------------------------------------------------------------
# 3) SIGNAL DENSITY: metrica por fuente (activos vs soft-deleted; carga de near-dups).
# ----------------------------------------------------------------------------
def signal_density(near_dup_pairs: list = None, top: int = 20) -> dict:
    """Densidad de senal por fuente. Global: activos/soft-deleted/superseded. Por fuente:
    top fuentes con mas ruido (mas soft-deleted o mas near-dups). near_dup_pairs opcional:
    si se pasa, agrega el conteo de near-dups por source_path para rankear redundancia."""
    dup_by_source = {}
    if near_dup_pairs:
        for p in near_dup_pairs:
            for s in (p.get("src_source"), p.get("dst_source")):
                if s:
                    dup_by_source[s] = dup_by_source.get(s, 0) + 1

    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE deleted_at IS NULL AND valid_until IS NULL) AS active, "
            "       count(*) FILTER (WHERE deleted_at IS NOT NULL) AS soft_deleted, "
            "       count(*) FILTER (WHERE deleted_at IS NULL AND valid_until IS NOT NULL) AS superseded, "
            "       count(DISTINCT source_path) FILTER (WHERE deleted_at IS NULL) AS active_sources "
            "FROM chunks_meta")
        g = cur.fetchone()
        cur.execute(
            "SELECT source_path, "
            "       count(*) FILTER (WHERE deleted_at IS NULL) AS active, "
            "       count(*) FILTER (WHERE deleted_at IS NOT NULL) AS soft_deleted "
            "FROM chunks_meta GROUP BY source_path")
        per_source = cur.fetchall()

    active = int(g["active"] or 0)
    soft = int(g["soft_deleted"] or 0)
    superseded = int(g["superseded"] or 0)
    total = active + soft + superseded

    noisy = []
    for r in per_source:
        sp = r["source_path"]
        a = int(r["active"] or 0)
        d = int(r["soft_deleted"] or 0)
        dups = dup_by_source.get(sp, 0)
        noise = d + dups
        if noise > 0:
            noisy.append({
                "source_path": sp, "active": a, "soft_deleted": d,
                "near_dups": dups,
                "dup_ratio": round(dups / a, 4) if a else None,
                "noise": noise,
            })
    noisy.sort(key=lambda x: x["noise"], reverse=True)

    return {
        "global": {
            "active": active,
            "soft_deleted": soft,
            "superseded_active": superseded,
            "total": total,
            "active_sources": int(g["active_sources"] or 0),
            "signal_ratio": round(active / total, 4) if total else None,
        },
        "noisiest_sources": noisy[:top],
    }


# ----------------------------------------------------------------------------
# Persistencia: SOLO PROPONE. Escribe candidatos con status 'proposed'.
# ----------------------------------------------------------------------------
_DETECTOR_NS = "_consolidation_detector"
# A malformed legacy namespace is data, not an invitation to overwrite it.  In
# that case observations are written to this explicitly versioned sibling.
_DETECTOR_NS_V2 = "_consolidation_detector_v2"
_DETECTOR_VERSION = 1
_DECAY_RULES = ("superseded_unarchived", "stale_old")


def _canonical_dedup(src, dst):
    """Canonicalize at the persistence boundary, never trusting a proposer."""
    return tuple(sorted((str(src), str(dst))))


def _detector_slot(detail):
    """Return a safe machine namespace without silently replacing legacy JSON."""
    value = detail.get(_DETECTOR_NS) if isinstance(detail, dict) else None
    if value is not None and (not isinstance(value, dict) or
                              ("reasons" in value and not isinstance(value["reasons"], dict))):
        return _DETECTOR_NS_V2
    # A v2 sibling already means the legacy shape was incompatible or that a prior
    # migration explicitly selected v2; keep validating and extending that slot.
    if isinstance(detail, dict) and _DETECTOR_NS_V2 in detail:
        return _DETECTOR_NS_V2
    return _DETECTOR_NS


def _merge_machine_detail(detail, observations, active_reasons):
    """Deep-merge detector-owned known fields while preserving every unknown JSON key.

    Only ``version``, ``active_reasons`` and observed metadata fields are machine
    owned.  An invalid legacy namespace is retained verbatim and gets a v2 sibling.
    """
    result = copy.deepcopy(detail) if isinstance(detail, dict) else {}
    slot = _detector_slot(result)
    prior = result.get(slot, {})
    prior = copy.deepcopy(prior) if isinstance(prior, dict) else {}
    prior_reasons = prior.get("reasons", {})
    prior_reasons = copy.deepcopy(prior_reasons) if isinstance(prior_reasons, dict) else {}
    # Type conflicts are retained exactly in their old slot.  A new versioned
    # sibling carries observations; never coerce a legacy JSON shape.
    if any(rule in prior_reasons and not isinstance(prior_reasons[rule], dict)
           for rule in observations):
        if slot == _DETECTOR_NS_V2:
            raise ValueError("invalid detector v2 reason type; refusing silent data loss")
        slot, prior, prior_reasons = _DETECTOR_NS_V2, {}, {}
    for rule, fields in observations.items():
        merged = copy.deepcopy(prior_reasons.get(rule, {}))
        # A warning is review metadata, not a replaceable measurement.  Preserve an
        # older namespaced hint and retain a changed observation explicitly so the
        # read projection can show both without losing unknown legacy data.
        previous_hint = merged.get("protected_hint")
        incoming_hint = fields.get("protected_hint")
        merged.update(fields)
        if isinstance(previous_hint, str) and isinstance(incoming_hint, str) and previous_hint != incoming_hint:
            merged["protected_hint"] = previous_hint
            merged["protected_hints"] = sorted(set(merged.get("protected_hints", [])) | {previous_hint, incoming_hint})
        prior_reasons[rule] = merged
    prior["version"] = _DETECTOR_VERSION
    prior["reasons"] = prior_reasons
    prior["active_reasons"] = sorted(active_reasons)
    result[slot] = prior
    return result


def _detector(detail):
    """Read current then legacy detector data, accepting historical top-level detail."""
    if not isinstance(detail, dict):
        return {}
    for slot in (_DETECTOR_NS_V2, _DETECTOR_NS):
        value = detail.get(slot)
        if isinstance(value, dict):
            return value
    return {}


def _protected_warnings(detail):
    """Stable, metadata-only projection of old and namespaced protected hints."""
    if not isinstance(detail, dict):
        return []
    hints = {detail.get("protected_hint")} if isinstance(detail.get("protected_hint"), str) else set()
    for slot in (_DETECTOR_NS, _DETECTOR_NS_V2):
        detector = detail.get(slot)
        if not isinstance(detector, dict) or not isinstance(detector.get("reasons"), dict):
            continue
        for evidence in detector["reasons"].values():
            if not isinstance(evidence, dict):
                continue
            if isinstance(evidence.get("protected_hint"), str):
                hints.add(evidence["protected_hint"])
            if isinstance(evidence.get("protected_hints"), list):
                hints.update(hint for hint in evidence["protected_hints"] if isinstance(hint, str))
    return sorted(hint for hint in hints if hint)


def _persist(near_dup_pairs: list, decay_candidates: list, *, decay_coverage=None) -> int:
    """Atomically merge proposals without deleting history or human review metadata.

    ``decay_coverage`` is a per-rule proof that the scan exhausted that rule's input.
    Missing/false proof is *unknown validity*, never a reason to mark an absence stale.
    The partial current-identity index is the non-cooperative backstop; a per-logical-
    identity advisory transaction lock serializes cooperative analyzers. Historical
    applied/dismissed rows are deliberately not candidates for refresh or respawn.
    """
    decay_coverage = decay_coverage or {}
    # Group detector observations before SQL: two decay rules are one review identity.
    grouped = {}
    for p in near_dup_pairs:
        src, dst = _canonical_dedup(p["src_chunk"], p["dst_chunk"])
        key = ("dedup", src, dst)
        grouped[key] = {"score": p["score"], "source": p.get("src_source"),
                        "reasons": {"near_duplicate": {"cosine": round(float(p["score"]), 4),
                        "threshold": p.get("threshold"), "dst_source": p.get("dst_source")}}}
    for d in decay_candidates:
        key = ("decay", str(d["chunk"]), None)
        item = grouped.setdefault(key, {"score": None, "source": d["source_path"], "reasons": {}})
        item["reasons"][d["reason"]] = {k: v for k, v in d.items() if k in (
            "created_at", "valid_until", "superseded_by", "age_days", "decay_months", "protected_hint")}

    n = 0
    seen_by_rule = {rule: set() for rule in _DECAY_RULES}
    for d in decay_candidates:
        if d.get("reason") in seen_by_rule:
            seen_by_rule[d["reason"]].add(str(d["chunk"]))
    with db.pg() as conn, conn.cursor() as cur:
        for (kind, src, dst), item in grouped.items():
            # This key locks one canonical logical identity, rather than serializing
            # unrelated analyzer work.  It is held through lookup/refresh/insert and
            # the partial unique index remains the backstop for non-cooperative SQL.
            identity = f"{kind}:{src}:{dst if dst is not None else '<NULL>'}"
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (identity,))
            cur.execute(
                "SELECT id, detail FROM consolidation_candidates WHERE kind=%s AND "
                "src_chunk=%s AND dst_chunk IS NOT DISTINCT FROM %s AND "
                "status IN ('proposed','stale') FOR UPDATE", (kind, src, dst))
            existing = cur.fetchone()
            if not existing:
                # A human decision is history, not a current row.  Absence of a
                # current projection beside applied/dismissed does NOT authorize an
                # analyzer to re-open it; a distinct-evidence policy needs review.
                cur.execute(
                    "SELECT 1 FROM consolidation_candidates WHERE kind=%s AND "
                    "src_chunk=%s AND dst_chunk IS NOT DISTINCT FROM %s AND "
                    "status IN ('applied','dismissed') LIMIT 1", (kind, src, dst))
                if cur.fetchone():
                    n += 1
                    continue
            reasons = item["reasons"]
            detail = (existing or {}).get("detail") or {}
            prior = _detector(detail)
            old_active = prior.get("active_reasons", []) if isinstance(prior.get("active_reasons", []), list) else []
            active = set(old_active) | set(reasons)
            reason_summary = ",".join(sorted(active))
            machine = json.dumps(_merge_machine_detail(detail, reasons, active))
            if existing:
                # jsonb || only replaces our namespaced object; top-level human fields survive.
                cur.execute(
                    "UPDATE consolidation_candidates SET score=%s, source_path=%s, reason=%s, "
                    "detail=COALESCE(detail, '{}'::jsonb) || %s::jsonb, proposed_at=now(), "
                    "status='proposed' WHERE id=%s AND status IN ('proposed','stale')",
                    (item["score"], item["source"], reason_summary, machine, existing["id"]))
                if cur.rowcount != 1:
                    # Meaningful CAS: never turn a reviewer transition into a new row.
                    # The FOR UPDATE lock normally makes this unreachable, but keeping
                    # it fail-closed protects future query/transaction refactors.
                    raise RuntimeError("current consolidation candidate changed during refresh")
            else:
                cur.execute(
                    "INSERT INTO consolidation_candidates "
                    "(kind,src_chunk,dst_chunk,score,source_path,status,reason,detail) "
                    "VALUES (%s,%s,%s,%s,%s,'proposed',%s,%s::jsonb)",
                    (kind, src, dst, item["score"], item["source"], reason_summary, machine))
            n += 1

        # A decay proposal becomes stale only if EVERY still-applicable historical rule
        # was exhaustively scanned and was absent. Partial/failed scans preserve it.
        cur.execute("SELECT id, src_chunk, detail FROM consolidation_candidates "
                    "WHERE kind='decay' AND status='proposed' FOR UPDATE")
        for row in cur.fetchall():
            detector = _detector(row.get("detail") or {})
            active = set(detector.get("active_reasons", [])) if isinstance(detector.get("active_reasons", []), list) else set()
            if not active or not all(decay_coverage.get(rule) is True for rule in active):
                continue
            if any(str(row["src_chunk"]) in seen_by_rule.get(rule, set()) for rule in active):
                continue
            cur.execute("UPDATE consolidation_candidates SET status='stale' WHERE id=%s "
                        "AND status='proposed'", (row["id"],))
        conn.commit()
    return n


def analyze(dup_threshold: float = None, decay_months: int = None, top_n: int = None,
            max_chunks: int = None, persist: bool = True) -> dict:
    """Job 'sueno': corre las 3 senales y (por defecto) PERSISTE los candidatos como
    'proposed'. SOLO PROPONE, jamas borra chunks/memoria (Regla de Oro #1).

    max_chunks: acota el origen del dedup y el tope de decay (para corridas de validacion).
    persist=False: dry-run (calcula y devuelve, sin escribir en consolidation_candidates).
    Devuelve {near_dup_pairs, decay_candidates, signal_density, counts, params, duration}."""
    t0 = time.time()
    dup_threshold = DEFAULT_DUP_THRESHOLD if dup_threshold is None else float(dup_threshold)
    decay_months = DEFAULT_DECAY_MONTHS if decay_months is None else int(decay_months)

    near = find_near_dups(threshold=dup_threshold, top_n=top_n, max_chunks=max_chunks)
    decay = find_decay_candidates(decay_months=decay_months, max_chunks=max_chunks)
    density = signal_density(near_dup_pairs=near)

    # ``max_chunks is None`` means only "no caller cap", not proof of exhaustive
    # coverage. The current detector has no scan receipt/predicate coverage proof,
    # so it deliberately reports unknown validity and never stales on absence.
    decay_coverage = {rule: False for rule in _DECAY_RULES}
    persisted = _persist(near, decay, decay_coverage=decay_coverage) if persist else 0

    return {
        "near_dup_pairs": near,
        "decay_candidates": decay,
        "signal_density": density,
        "counts": {
            "near_dup_pairs": len(near),
            "decay_candidates": len(decay),
            "persisted": persisted,
        },
        "params": {
            "dup_threshold": dup_threshold,
            "decay_months": decay_months,
            "top_n": DEFAULT_TOP_N if top_n is None else int(top_n),
            "max_chunks": int(max_chunks) if max_chunks else None,
            "persist": persist,
            "decay_coverage": decay_coverage,
            "coverage_note": "unknown: detector has no per-rule exhaustive scan receipt; absence never stales",
        },
        "duration": round(time.time() - t0, 3),
    }


def candidates(kind: str = None, status: str = "proposed", limit: int = None) -> dict:
    """Lee consolidation_candidates (barato, sin computo). ``stale`` es un estado
    de validez del detector, no un dismissal humano; se pide explicitamente con
    status='stale'. Defaults conservan la bandeja historica ``proposed``."""
    limit = DEFAULT_LIST_LIMIT if limit is None else int(limit)
    where, args = [], []
    if kind:
        where.append("kind=%s"); args.append(kind)
    if status:
        where.append("status=%s"); args.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, src_chunk, dst_chunk, score, source_path, status, "
            "       proposed_at, reason, detail "
            "FROM consolidation_candidates" + clause +
            " ORDER BY score DESC NULLS LAST, proposed_at DESC LIMIT %s",
            tuple(args) + (limit,))
        rows = cur.fetchall()
        cur.execute(
            "SELECT kind, status, count(*) AS n FROM consolidation_candidates "
            "GROUP BY kind, status")
        agg = cur.fetchall()
    items = [{
        "id": int(r["id"]),
        "kind": r["kind"],
        "src_chunk": str(r["src_chunk"]) if r["src_chunk"] else None,
        "dst_chunk": str(r["dst_chunk"]) if r["dst_chunk"] else None,
        "score": float(r["score"]) if r["score"] is not None else None,
        "source_path": r["source_path"],
        "status": r["status"],
        "proposed_at": r["proposed_at"].isoformat() if r["proposed_at"] else None,
        # reason/detail EN LA LECTURA (tarea 60fe2bd9). Persistirlos no alcanza: si el
        # motivo se guarda y no se devuelve, la revision humana sigue siendo imposible.
        # Es el mismo defecto que cometi hoy en retrievalmetrics: agregar la columna al
        # SELECT y olvidar el mapeo de salida (o al reves) se lee como fix completo.
        "reason": r.get("reason"),
        "detail": r.get("detail"),
        "warning": "\n".join(_protected_warnings(r.get("detail"))) or None,
    } for r in rows]
    summary = {}
    for r in agg:
        summary["%s/%s" % (r["kind"], r["status"])] = int(r["n"])
    return {"count": len(items), "candidates": items, "summary": summary}
