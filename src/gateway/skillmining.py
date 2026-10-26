"""skillmining.py - Skills procedimentales VIVAS (Cerebro v2, Gap ec2269ab, Ola 2, FASE 1).

Cierra el closed-learning-loop de la flota. Los harvests individuales (memorias
category IN failure|insight|correction|tool-quirk + agent_checkpoints kind IN
pitfall|landmark) YA existen. Este modulo los DESTILA en PROPUESTAS de skill:

  harvest (individual) -> patron repetido (colectivo) -> PROPUESTA -> revision humana.

Corroboracion k>=2 (F3 5.1, anti prior-poisoning): un patron NO se propone hasta que
>= min_corroboration agentes DISTINTOS lo reportaron. Un solo agente repitiendo la
misma senal NO corrobora (evita que una alucinacion contamine la biblioteca de skills).

PRINCIPIO INVIOLABLE: este modulo SOLO PROPONE. Jamas crea ni edita skills
(nunca invoca skill_add / skill_remove; los chunks source_path 'skills/%' no se tocan).
La creacion/edicion la decide un humano/Monica a partir de las propuestas. FASE 1 es
DETERMINISTA: agrupa senales reales por similitud y las referencia como EVIDENCIA; NO
usa LLM para inventar el texto de la skill (eso lo redacta quien la aprueba, fase 2).

Arquitectura (reusa EXACTO el patron de semanticgraph.py / consolidation.py):
  - Senales-memoria: sus embeddings YA viven en Qdrant (Titan v2, 1024d). Se agrupan por
    k-NN query-by-point-id contra la coleccion, restringido al conjunto de senales
    (mismo patron que semanticgraph.rebuild / consolidation.find_near_dups). SIN re-embed.
  - Senales-checkpoint (pitfall/landmark): NO estan en Qdrant. Se embeben on-the-fly
    (embedder.embed) y se enlazan por k-NN query-by-vector + coseno checkpoint-checkpoint.
  - Clusters = componentes conexas del grafo de similitud (union-find, single-linkage).
  - Skill existente que cubra el cluster: db.search(source_prefix='skills/') sobre el
    texto del medoide -> kind='update' (related_skill) si hay match, si no kind='new'.
  - Persiste en skill_proposals (status 'proposed'). Lo consume Monica/Otixe.

Reusa db.qdrant(), db.pg(), db.COLLECTION, db.search, embedder.embed. Additive: tabla
propia skill_proposals; NO toca chunks_meta, agent_checkpoints, ni endpoints previos.
"""
import math
import os
import time

import db
import embedder
from qdrant_client.models import Filter, FieldCondition, MatchValue

# Defaults tuneables (punto de partida; ajustables por parametro de mine()/endpoint).
DEFAULT_WINDOW_DAYS = int(os.environ.get("SKILLMINING_WINDOW_DAYS", "30"))
DEFAULT_MIN_CORROBORATION = int(os.environ.get("SKILLMINING_MIN_CORROBORATION", "2"))
DEFAULT_SIM_THRESHOLD = float(os.environ.get("SKILLMINING_SIM_THRESHOLD", "0.82"))
# Umbral para decidir si YA existe una skill que cubre el cluster (kind update vs new).
DEFAULT_SKILL_MATCH = float(os.environ.get("SKILLMINING_SKILL_MATCH", "0.60"))
DEFAULT_TOP_N = int(os.environ.get("SKILLMINING_TOP_N", "12"))
DEFAULT_LIST_LIMIT = 100

MEMORY_CATEGORIES = ("failure", "insight", "correction", "tool-quirk")
CHECKPOINT_KINDS = ("pitfall", "landmark")


def _neighbor_filter():
    """Mismo filtro de higiene que semanticgraph/consolidation/db.search: excluye del
    pool de vecinos los chunks borrados/superseded/archivados."""
    return Filter(must_not=[
        FieldCondition(key="deleted", match=MatchValue(value=True)),
        FieldCondition(key="superseded", match=MatchValue(value=True)),
        FieldCondition(key="archived", match=MatchValue(value=True)),
    ])


def _cosine(a, b):
    """Coseno entre dos vectores (para aristas checkpoint-checkpoint / checkpoint-memoria
    cuando el vector se embebe on-the-fly). Titan v2 ya normaliza, pero normalizamos por
    robustez/auditabilidad. Devuelve 0.0 si algun vector es nulo."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ----------------------------------------------------------------------------
# 1) Recoleccion de senales de aprendizaje recientes (memorias + checkpoints).
# ----------------------------------------------------------------------------
def _collect_signals(window_days: int, max_items: int = None):
    """Recolecta senales de aprendizaje de la ventana window_days:
      - memorias: chunks_meta activos, category IN MEMORY_CATEGORIES.
      - checkpoints: agent_checkpoints, kind IN CHECKPOINT_KINDS.
    Devuelve lista de dicts:
      {node, kind_group('memory'|'checkpoint'), ref(source_path|ckpt id), agent,
       label(category|kind), text, snippet}
    node = 'mem:<chunk_id>' | 'ckpt:<uuid>'. Ordenado determinista por created_at, id.
    max_items acota el TOTAL de senales (para corridas de validacion)."""
    interval = "%d days" % int(window_days)
    signals = []
    with db.pg() as conn, conn.cursor() as cur:
        # Memorias: senal = aprendizaje explicito de un agente.
        cur.execute(
            "SELECT id, source_path, agent_owner, category, "
            "       COALESCE(text_full, text_preview, '') AS text, created_at "
            "FROM chunks_meta "
            "WHERE deleted_at IS NULL AND valid_until IS NULL "
            "  AND category = ANY(%s) "
            "  AND created_at > now() - %s::interval "
            "ORDER BY created_at, id",
            (list(MEMORY_CATEGORIES), interval))
        for r in cur.fetchall():
            txt = (r["text"] or "").strip()
            if not txt:
                continue
            signals.append({
                "node": "mem:" + str(r["id"]),
                "kind_group": "memory",
                "ref": r["source_path"],
                "chunk_id": str(r["id"]),
                "agent": r["agent_owner"] or "",
                "label": r["category"] or "general",
                "text": txt,
                "snippet": txt[:240],
            })
        # Checkpoints: senal = pitfall/landmark cosechado por un subagente.
        cur.execute(
            "SELECT id, agent, kind, label, state, created_at "
            "FROM agent_checkpoints "
            "WHERE kind = ANY(%s) "
            "  AND created_at > now() - %s::interval "
            "ORDER BY created_at, id",
            (list(CHECKPOINT_KINDS), interval))
        for r in cur.fetchall():
            # Texto = label + valores de state (jsonb). Determinista.
            parts = []
            if r["label"]:
                parts.append(str(r["label"]))
            st = r["state"] or {}
            if isinstance(st, dict):
                for k in ("text", "note", "detail", "summary", "lesson", "body"):
                    v = st.get(k)
                    if v:
                        parts.append(str(v))
                if not parts[1:]:  # sin campos conocidos: serializa el dict
                    parts.append(" ".join("%s=%s" % (k, v) for k, v in st.items()))
            txt = " | ".join(p for p in parts if p).strip()
            if not txt:
                continue
            signals.append({
                "node": "ckpt:" + str(r["id"]),
                "kind_group": "checkpoint",
                "ref": "agent_checkpoint:" + str(r["id"]),
                "chunk_id": None,
                "agent": r["agent"] or "",
                "label": r["kind"],
                "text": txt,
                "snippet": txt[:240],
            })
    if max_items:
        signals = signals[:int(max_items)]
    return signals


# ----------------------------------------------------------------------------
# 2) Grafo de similitud entre senales -> aristas (node_a, node_b, score).
# ----------------------------------------------------------------------------
def _build_edges(signals, sim_threshold: float, top_n: int):
    """Construye aristas de similitud entre senales del conjunto:
      - memoria->*: k-NN query-by-point-id en Qdrant (patron semanticgraph), retiene
        vecinos que TAMBIEN son senales del conjunto y score>=sim_threshold. SIN re-embed.
      - checkpoint->*: se embebe el texto (embedder.embed) y se enlaza por coseno contra
        los vectores del conjunto (checkpoints entre si + checkpoint->memoria via k-NN
        query-by-vector). Los checkpoints hoy suelen ser 0; el camino real es memoria.
    Devuelve (edges, member_meta) donde edges = [(a, b, score)] con a<b lexicografico."""
    q = db.qdrant()
    nfilter = _neighbor_filter()

    mem_by_chunk = {s["chunk_id"]: s["node"] for s in signals if s["kind_group"] == "memory"}
    mem_member_ids = set(mem_by_chunk.keys())
    checkpoints = [s for s in signals if s["kind_group"] == "checkpoint"]

    edges = {}  # frozenset({a,b}) -> score (dedup simetrico, gana el mayor)

    def _add(a, b, score):
        if a == b:
            return
        key = frozenset((a, b))
        if key not in edges or score > edges[key]:
            edges[key] = score

    # (a) Memoria: query-by-point-id (usa el vector ya guardado en Qdrant).
    for s in signals:
        if s["kind_group"] != "memory":
            continue
        try:
            resp = q.query_points(
                collection_name=db.COLLECTION,
                query=s["chunk_id"],           # query-by-point-id
                limit=top_n + 1,
                query_filter=nfilter,
                with_payload=False,
                with_vectors=False,
            )
        except Exception:
            continue
        for pt in resp.points:
            dst_id = str(pt.id)
            if dst_id == s["chunk_id"]:
                continue
            if dst_id not in mem_member_ids:      # solo aristas DENTRO del conjunto
                continue
            score = float(pt.score)
            if score < sim_threshold:
                continue
            _add(s["node"], mem_by_chunk[dst_id], round(score, 6))

    # (b) Checkpoints: embed on-the-fly + coseno. Solo si hay checkpoints.
    if checkpoints:
        cp_vecs = {}
        for s in checkpoints:
            try:
                cp_vecs[s["node"]] = embedder.embed(s["text"], input_type="search_document")
            except Exception:
                cp_vecs[s["node"]] = None
        # checkpoint <-> checkpoint (coseno directo)
        for i in range(len(checkpoints)):
            for j in range(i + 1, len(checkpoints)):
                a, b = checkpoints[i]["node"], checkpoints[j]["node"]
                sc = _cosine(cp_vecs.get(a), cp_vecs.get(b))
                if sc >= sim_threshold:
                    _add(a, b, round(sc, 6))
        # checkpoint -> memoria (k-NN query-by-vector contra la coleccion)
        for s in checkpoints:
            vec = cp_vecs.get(s["node"])
            if not vec:
                continue
            try:
                resp = q.query_points(
                    collection_name=db.COLLECTION,
                    query=vec,
                    limit=top_n + 1,
                    query_filter=nfilter,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception:
                continue
            for pt in resp.points:
                dst_id = str(pt.id)
                if dst_id not in mem_member_ids:
                    continue
                score = float(pt.score)
                if score < sim_threshold:
                    continue
                _add(s["node"], mem_by_chunk[dst_id], round(score, 6))

    out = []
    for key, score in edges.items():
        a, b = sorted(key)
        out.append((a, b, score))
    return out


# ----------------------------------------------------------------------------
# 3) Clustering: componentes conexas (union-find single-linkage) sobre las aristas.
# ----------------------------------------------------------------------------
def _cluster(nodes, edges):
    """Union-find: agrupa nodos conectados por >=1 arista. Devuelve dict
    cluster_root -> {nodes:set, edges:[(a,b,score)]}. Nodos aislados se omiten (un
    patron necesita >=2 senales conectadas)."""
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for a, b, _ in edges:
        if a in parent and b in parent:
            union(a, b)

    clusters = {}
    for a, b, score in edges:
        root = find(a)
        c = clusters.setdefault(root, {"nodes": set(), "edges": []})
        c["nodes"].add(a)
        c["nodes"].add(b)
        c["edges"].append((a, b, score))
    return clusters


def _match_skill(query_text: str):
    """Busca si ya existe una skill que cubra el patron. Embebe el texto del medoide y
    hace db.search restringido a source_prefix='skills/' (hybrid=False -> score=coseno).
    Devuelve (related_skill_source_path, cosine) o (None, best_cosine) si no supera umbral."""
    try:
        qv = embedder.embed(query_text, input_type="search_query")
        res = db.search(qv, top_k=1, source_prefix="skills/", hybrid=False,
                        min_score=0.0, include_superseded=False)
    except Exception:
        return None, None
    results = res.get("results") if isinstance(res, dict) else res
    if not results:
        return None, None
    top = results[0]
    cos = float(top.get("score") or 0.0)
    if cos >= DEFAULT_SKILL_MATCH:
        return top.get("source_path"), round(cos, 6)
    return None, round(cos, 6)


# ----------------------------------------------------------------------------
# Persistencia: SOLO PROPONE. Escribe skill_proposals con status 'proposed'.
# ----------------------------------------------------------------------------
def _persist(proposals):
    """Refresca las propuestas 'proposed' (regenerables) y reinserta. NUNCA toca filas
    'accepted'/'dismissed' (decisiones humanas se preservan). NUNCA toca skills ni
    chunks_meta. Devuelve cuantas propuestas quedaron insertadas."""
    import json
    n = 0
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM skill_proposals WHERE status='proposed'")
        for p in proposals:
            cur.execute(
                "INSERT INTO skill_proposals "
                "(kind, title, rationale, evidence, corroboration, related_skill, "
                " avg_sim, status) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,'proposed')",
                (p["kind"], p["title"], p["rationale"], json.dumps(p["evidence"]),
                 p["corroboration"], p.get("related_skill"), p.get("avg_sim")))
            n += 1
        conn.commit()
    return n


def mine(window_days: int = None, min_corroboration: int = None,
         sim_threshold: float = None, max_items: int = None,
         persist: bool = True) -> dict:
    """Destila los harvests recientes en PROPUESTAS de skill. SOLO PROPONE.

    1. Recolecta senales (memorias failure|insight|correction|tool-quirk + checkpoints
       pitfall|landmark) de la ventana window_days.
    2. Agrupa por similitud (k-NN Qdrant, patron semanticgraph) en clusters.
    3. Para cada cluster con >= min_corroboration agentes DISTINTOS (corroboracion k>=2):
       busca skill existente que lo cubra -> kind='update' (related_skill) o kind='new'.
    4. Persiste (por defecto) las propuestas en skill_proposals status 'proposed'.

    Devuelve {clusters_found, proposals[], counts, params, duration}. JAMAS crea/edita
    skills (no invoca skill_add/skill_remove; 'skills/%' intacto)."""
    t0 = time.time()
    window_days = DEFAULT_WINDOW_DAYS if window_days is None else int(window_days)
    min_corroboration = (DEFAULT_MIN_CORROBORATION if min_corroboration is None
                         else int(min_corroboration))
    sim_threshold = DEFAULT_SIM_THRESHOLD if sim_threshold is None else float(sim_threshold)

    signals = _collect_signals(window_days, max_items=max_items)
    by_node = {s["node"]: s for s in signals}
    edges = _build_edges(signals, sim_threshold=sim_threshold, top_n=DEFAULT_TOP_N)
    clusters = _cluster(list(by_node.keys()), edges)

    proposals = []
    for root, c in clusters.items():
        members = [by_node[n] for n in c["nodes"] if n in by_node]
        if len(members) < 2:
            continue
        agents = sorted({m["agent"] for m in members if m["agent"]})
        corroboration = len(agents)
        # Corroboracion k>=2: >= min_corroboration agentes DISTINTOS. Un solo agente
        # repitiendo (corroboration==1) NO se promueve (anti prior-poisoning).
        if corroboration < min_corroboration:
            continue

        scores = [e[2] for e in c["edges"]]
        avg_sim = round(sum(scores) / len(scores), 6) if scores else None

        # Medoide: nodo con mayor suma de similitud incidente (representante del cluster).
        incident = {}
        for a, b, sc in c["edges"]:
            incident[a] = incident.get(a, 0.0) + sc
            incident[b] = incident.get(b, 0.0) + sc
        medoid_node = max(incident, key=incident.get) if incident else members[0]["node"]
        medoid = by_node.get(medoid_node, members[0])

        related_skill, match_cos = _match_skill(medoid["text"])
        kind = "update" if related_skill else "new"

        # Titulo tentativo: primera linea del medoide (acotada).
        first_line = medoid["text"].splitlines()[0] if medoid["text"] else ""
        title = (first_line[:120].strip() or "patron sin titulo")

        labels = sorted({m["label"] for m in members})
        rationale = (
            "Patron corroborado por %d agentes distintos (%s) en %d senales "
            "[%s]; cohesion media %s. %s"
            % (corroboration, ", ".join(agents), len(members), ", ".join(labels),
               avg_sim,
               ("Refuerza skill existente %s (coseno %s)." % (related_skill, match_cos))
               if related_skill else
               ("Sin skill que lo cubra (mejor coseno skills/ = %s)." % match_cos)))

        evidence = [{
            "ref": m["ref"],
            "agent": m["agent"],
            "signal": m["label"],
            "kind_group": m["kind_group"],
            "snippet": m["snippet"],
        } for m in sorted(members, key=lambda x: (x["agent"], x["ref"]))]

        proposals.append({
            "kind": kind,
            "title": title,
            "rationale": rationale,
            "evidence": evidence,
            "corroboration": corroboration,
            "related_skill": related_skill,
            "avg_sim": avg_sim,
            "cluster_size": len(members),
            "agents": agents,
        })

    proposals.sort(key=lambda p: (p["corroboration"], p.get("avg_sim") or 0.0),
                   reverse=True)
    persisted = _persist(proposals) if persist else 0

    return {
        "clusters_found": len([c for c in clusters.values() if len(c["nodes"]) >= 2]),
        "proposals": proposals,
        "counts": {
            "signals": len(signals),
            "edges": len(edges),
            "clusters": len(clusters),
            "proposals": len(proposals),
            "new": len([p for p in proposals if p["kind"] == "new"]),
            "update": len([p for p in proposals if p["kind"] == "update"]),
            "persisted": persisted,
        },
        "params": {
            "window_days": window_days,
            "min_corroboration": min_corroboration,
            "sim_threshold": sim_threshold,
            "skill_match_threshold": DEFAULT_SKILL_MATCH,
            "top_n": DEFAULT_TOP_N,
            "max_items": int(max_items) if max_items else None,
            "persist": persist,
        },
        "duration": round(time.time() - t0, 3),
    }


def proposals(status: str = "proposed", kind: str = None, limit: int = None) -> dict:
    """Lee skill_proposals (barato, sin computo). Filtros opcionales por status
    (proposed|accepted|dismissed) y kind (new|update). Lo consume Monica/Otixe."""
    limit = DEFAULT_LIST_LIMIT if limit is None else int(limit)
    where, args = [], []
    if status:
        where.append("status=%s"); args.append(status)
    if kind:
        where.append("kind=%s"); args.append(kind)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, rationale, evidence, corroboration, related_skill, "
            "       avg_sim, status, proposed_at "
            "FROM skill_proposals" + clause +
            " ORDER BY corroboration DESC, avg_sim DESC NULLS LAST, proposed_at DESC "
            "LIMIT %s",
            tuple(args) + (limit,))
        rows = cur.fetchall()
        cur.execute(
            "SELECT kind, status, count(*) AS n FROM skill_proposals GROUP BY kind, status")
        agg = cur.fetchall()
    items = [{
        "id": int(r["id"]),
        "kind": r["kind"],
        "title": r["title"],
        "rationale": r["rationale"],
        "evidence": r["evidence"],
        "corroboration": int(r["corroboration"]),
        "related_skill": r["related_skill"],
        "avg_sim": float(r["avg_sim"]) if r["avg_sim"] is not None else None,
        "status": r["status"],
        "proposed_at": r["proposed_at"].isoformat() if r["proposed_at"] else None,
    } for r in rows]
    summary = {}
    for r in agg:
        summary["%s/%s" % (r["kind"], r["status"])] = int(r["n"])
    return {"count": len(items), "proposals": items, "summary": summary}
