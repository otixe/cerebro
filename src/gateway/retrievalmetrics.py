"""retrievalmetrics.py - Metricas de calidad de RETRIEVAL (Cerebro v2, Gap A, 4514dca5).

Instrumento fundacional: mide si las busquedas del Cerebro (memory_search) son buenas.
Sin esto no sabemos si el grafo semantico, la consolidacion o cualquier cambio MEJORAN
o DEGRADAN el retrieval.

Arquitectura (hermano de semanticgraph.py / notegraph.py):
  - Golden set: tabla retrieval_evalset(query, expected_sources jsonb) con queries curadas
    y las fuentes (source_path, exacto o substring) que DEBERIAN salir como relevantes.
  - evaluate(k): para cada query del golden set corre el MISMO pipeline de busqueda que el
    endpoint /memory/search (embedder.embed + db.search), compara el top-k contra los
    expected_sources y calcula precision@k, recall@k, MRR. Agrega promedio global y persiste
    el run en retrieval_eval_runs (historial).
  - stats(): ultimo run + agregados historicos + tamano del evalset.

Reusa los clientes existentes: embedder.embed() (query embedding), db.search() (pipeline
real de retrieval), db.pg() (pool Postgres, contextmanager). Additive: tablas propias
retrieval_evalset / retrieval_eval_runs; NO toca nada existente.

MATCHING de relevancia (determinista): un resultado del top-k cuenta como relevante si su
source_path CONTIENE (case-insensitive) alguno de los expected_sources de esa query. Los
expected_sources se guardan como fragmentos de path (ej '50 Agents/Saruman/Vigente/SOUL.md'
o 'memoria/saruman/harness-peerage'), de modo que matchean sin depender del prefijo exacto
(obsidian-vault/..., etc).
"""
import json
import os
import time

import db
import embedder

DEFAULT_K = 5


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _matches(source_path: str, expected: list) -> bool:
    """True si source_path contiene (case-insensitive) alguno de los fragmentos esperados."""
    sp = _norm(source_path)
    if not sp:
        return False
    for exp in expected:
        e = _norm(exp)
        if e and e in sp:
            return True
    return False


def _matched_expected(results_sources: list, expected: list) -> set:
    """Conjunto de expected_sources (indices) cubiertos por al menos un resultado del top-k.
    Sirve para recall: cuantas de las fuentes relevantes esperadas aparecieron."""
    covered = set()
    for i, exp in enumerate(expected):
        e = _norm(exp)
        if not e:
            continue
        for sp in results_sources:
            if e in _norm(sp):
                covered.add(i)
                break
    return covered


def _load_evalset() -> list:
    """Lee el golden set. Devuelve [{id, query, expected_sources[list], note, kind}]."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, query, expected_sources, note, kind FROM retrieval_evalset "
            "ORDER BY created_at, id")
        rows = cur.fetchall()
    out = []
    for r in rows:
        exp = r["expected_sources"]
        if isinstance(exp, str):
            try:
                exp = json.loads(exp)
            except Exception:
                exp = [exp]
        if not isinstance(exp, list):
            exp = []
        # `kind` DEBE propagarse: sin esto evaluate() cae al default "positive" y los
        # controles negativos se cuentan como positivos fallidos (n_negative=0,
        # specificity=null). Bug que cometi al aplicar el fix: agregue kind al SELECT
        # y me olvide del dict de salida -- el SELECT correcto se lee como fix completo.
        out.append({"id": str(r["id"]), "query": r["query"],
                    "expected_sources": exp, "note": r["note"],
                    "kind": (r.get("kind") or "positive")})
    return out


def evaluate(k: int = DEFAULT_K, persist: bool = True, triggered_by: str = None,
             neg_threshold: float = None) -> dict:
    """Corre el golden set completo y calcula metricas de retrieval @k.

    Para cada query:
      - embed (input_type='search_query') + db.search(top_k=k) -> mismo pipeline que /memory/search.
      - relevantes_en_topk = # resultados del top-k que matchean algun expected_source.
      - precision@k = relevantes_en_topk / k (k = tamano del corte, no #resultados).
      - recall@k = # expected_sources cubiertos por el top-k / # expected_sources.
      - reciprocal_rank = 1/rank del PRIMER resultado relevante (0 si ninguno).

    Agrega el promedio macro (media sobre queries) de precision, recall y MRR. Persiste el
    run en retrieval_eval_runs si persist=True.

    Devuelve {k, n_queries, precision_at_k, recall_at_k, mrr, per_query[], duration, run_id?}.
    """
    k = int(k) if k else DEFAULT_K
    if k < 1:
        k = DEFAULT_K
    t0 = time.time()

    evalset = _load_evalset()
    per_query = []
    sum_p = sum_r = sum_rr = 0.0
    # Acumuladores de los CONTROLES NEGATIVOS (tarea 0434db94), separados a proposito:
    # specificity responde otra pregunta que precision/recall y no se promedia con ellas.
    n_neg = 0
    sum_spec = 0.0
    # UMBRAL DE NEGATIVOS: 0.62 es el VALOR OPERATIVO VALIDADO, no un placeholder.
    # (2026-08-24, lurtz, tarea 2e479a7a punto 3.) EVAL_NEG_COSINE_THRESHOLD esta
    # DELIBERADAMENTE NO SETEADO en el contenedor: los 12 runs sanos posteriores al fix de
    # _load_evalset dan specificity=1.0 consistente con este default, o sea el default ya esta
    # validado por medicion. Setearlo explicito agregaria una segunda fuente de verdad que puede
    # divergir del codigo sin que nadie lo note. Sigue siendo parametrizable por env o por el
    # parametro `neg_threshold` del POST /eval/retrieval-run para experimentar sin rebuild.
    if neg_threshold is None:
        _env = (os.environ.get("EVAL_NEG_COSINE_THRESHOLD") or "").strip()
        try:
            neg_threshold = float(_env) if _env else 0.62
        except ValueError:
            neg_threshold = 0.62

    for item in evalset:
        query = item["query"]
        expected = item["expected_sources"]
        try:
            vec = embedder.embed(query, input_type="search_query")
            search_result = db.search(vec, query_text=query, top_k=k)
            results = search_result.get("results", [])
        except Exception as e:
            per_query.append({
                "id": item["id"], "query": query, "expected_count": len(expected),
                "error": repr(e)[:500], "precision_at_k": 0.0, "recall_at_k": 0.0,
                "reciprocal_rank": 0.0, "hits": [],
            })
            continue

        result_sources = [r.get("source_path") or "" for r in results]

        # ---- CONTROL NEGATIVO (tarea 0434db94): metrica PROPIA, no precision/recall ----
        # Un negativo que funciona PERFECTO (el sistema no devuelve nada relevante, que
        # es lo correcto) daria precision 0 / recall 0 / MRR 0 y HUNDIRIA los promedios,
        # ademas de ser indistinguible de una query que fallo de verdad. Por eso se mide
        # con RECHAZO CORRECTO y se EXCLUYE de precision/recall/MRR.
        if (item.get("kind") or "positive") == "negative":
            _best_cos = 0.0
            for r in results:
                _c = r.get("score_cosine")
                if _c is not None:
                    try:
                        _best_cos = max(_best_cos, float(_c))
                    except (TypeError, ValueError):
                        pass
            _low_conf = bool(search_result.get("low_confidence")) \
                if isinstance(search_result, dict) else False
            # Acierto si: no devolvio nada, O se declaro poco confiable, O el mejor
            # coseno queda por debajo del umbral de la corrida.
            _ok = (len(results) == 0) or _low_conf or (
                neg_threshold is not None and _best_cos < neg_threshold)
            n_neg += 1
            sum_spec += 1.0 if _ok else 0.0
            per_query.append({
                "id": item["id"], "query": query, "kind": "negative",
                "expected_count": 0, "retrieved": len(results),
                "best_score_cosine": round(_best_cos, 4),
                "low_confidence": _low_conf,
                "rejected_correctly": _ok,
                "hits": [{"rank": i + 1, "source_path": sp,
                          "score_cosine": round(float(results[i].get("score_cosine") or 0.0), 4)}
                         for i, sp in enumerate(result_sources)],
            })
            continue

        # precision@k: cuantos del top-k son relevantes / k
        rel_flags = [_matches(sp, expected) for sp in result_sources]
        n_rel = sum(1 for f in rel_flags if f)
        precision = n_rel / k

        # recall@k: cuantos expected quedaron cubiertos / total expected
        covered = _matched_expected(result_sources, expected)
        recall = (len(covered) / len(expected)) if expected else 0.0

        # reciprocal rank: 1/rank del primer relevante
        rr = 0.0
        for idx, flag in enumerate(rel_flags):
            if flag:
                rr = 1.0 / (idx + 1)
                break

        sum_p += precision
        sum_r += recall
        sum_rr += rr

        per_query.append({
            "id": item["id"],
            "query": query,
            "expected_count": len(expected),
            "retrieved": len(results),
            "relevant_in_topk": n_rel,
            "expected_covered": len(covered),
            "precision_at_k": round(precision, 6),
            "recall_at_k": round(recall, 6),
            "reciprocal_rank": round(rr, 6),
            "hits": [{"rank": i + 1, "source_path": sp, "relevant": rel_flags[i],
                      "score": round(float(results[i].get("score") or 0.0), 4)}
                     for i, sp in enumerate(result_sources)],
        })

    # n POSITIVOS para los promedios: incluir los negativos aca romperia la
    # comparabilidad con el historico de runs (que no los tenia) y mezclaria dos
    # preguntas distintas -- "encuentra lo que existe?" vs "sabe cuando NO existe?".
    n_pos = len(evalset) - n_neg
    precision_at_k = round(sum_p / n_pos, 6) if n_pos else 0.0
    recall_at_k = round(sum_r / n_pos, 6) if n_pos else 0.0
    mrr = round(sum_rr / n_pos, 6) if n_pos else 0.0
    specificity = round(sum_spec / n_neg, 6) if n_neg else None
    n = n_pos
    duration = round(time.time() - t0, 3)

    run = {
        "k": k,
        "n_queries": n_pos,
        "precision_at_k": precision_at_k,
        "recall_at_k": recall_at_k,
        "mrr": mrr,
        "n_negative": n_neg,
        "specificity": specificity,
        "neg_threshold": neg_threshold,
        "per_query": per_query,
        "duration": duration,
    }

    if persist and n:
        detail = {"per_query": per_query, "triggered_by": triggered_by,
                  "duration": duration}
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO retrieval_eval_runs (k, n_queries, precision, recall, mrr, "
                "triggered_by, detail, specificity, n_negative) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (k, n, precision_at_k, recall_at_k, mrr, triggered_by,
                 json.dumps(detail), specificity, n_neg))
            run["run_id"] = str(cur.fetchone()["id"])
            conn.commit()

    return run


def add_evalset_query(query: str, expected_sources, note: str = None) -> dict:
    """Agrega una query al golden set. expected_sources: lista de fragmentos de source_path
    (o string coma-separado / JSON). Devuelve {id, query, expected_sources}."""
    q = (query or "").strip()
    if not q:
        return {"error": "query vacia"}
    if isinstance(expected_sources, str):
        s = expected_sources.strip()
        try:
            parsed = json.loads(s)
            expected_sources = parsed if isinstance(parsed, list) else [s]
        except Exception:
            expected_sources = [x.strip() for x in s.split(",") if x.strip()]
    if not isinstance(expected_sources, list):
        expected_sources = []
    expected_sources = [str(x).strip() for x in expected_sources if str(x).strip()]
    # CONTROL NEGATIVO (tarea 0434db94, 2026-08-11): expected_sources=[] ya NO es un
    # error, es la forma de declarar "la respuesta correcta es NO ENCONTRAR NADA".
    # Antes esto devolvia {"error": "expected_sources vacio"} y el golden set era
    # ESTRUCTURALMENTE INCAPAZ de medir especificidad: sin casos de esa clase, un
    # barrido de umbrales daba "sin efecto" en los 8 candidatos (0,20 a 0,50) y un
    # lector apurado concluia "cualquier umbral es gratis" -- falso y peligroso.
    kind = "negative" if not expected_sources else "positive"
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO retrieval_evalset (query, expected_sources, note, kind) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (q, json.dumps(expected_sources), note, kind))
        new_id = str(cur.fetchone()["id"])
        conn.commit()
    return {"id": new_id, "query": q, "expected_sources": expected_sources,
            "note": note, "kind": kind}


def stats() -> dict:
    """Ultimo run + agregados historicos + tamano del evalset. Barato (solo lee)."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM retrieval_evalset")
        evalset_size = int(cur.fetchone()["c"])
        # DESGLOSE POR kind (2026-08-17, saruman). Reportado por @monica (tarea f5bc6dc3):
        # leyo "evalset_size: 32" y "n_queries: 24" y concluyo, razonablemente, que 8 queries
        # no entraban al calculo. NO era un bug: los 8 son CONTROLES NEGATIVOS y su metrica es
        # specificity, no precision/recall. El defecto era del REPORTE, que mostraba los dos
        # numeros que no cierran y escondia el que los reconcilia.
        # ⇒ Es la deuda que marco @saruman-grima el 15-ago: "un instrumento cuya metrica nueva
        # no aparece en el reporte que la flota lee es medio instrumento". Su prediccion se
        # cumplio al primer uso por otra persona.
        cur.execute("SELECT kind, count(*) AS c FROM retrieval_evalset GROUP BY kind")
        por_kind = {r["kind"]: int(r["c"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT id, k, n_queries, n_negative, specificity, precision, recall, mrr, "
            "triggered_by, run_at "
            "FROM retrieval_eval_runs ORDER BY run_at DESC LIMIT 1")
        last = cur.fetchone()

        # AGREGADOS HISTORICOS: EXCLUYEN los runs marcados detail->>'invalid' = 'true'
        # (2026-08-24, lurtz, tarea 2e479a7a; deuda que marco @saruman-grima).
        # POR QUE: el run 09e0faca (2026-08-11) se corrio con el bug de _load_evalset activo y
        # conto los 8 controles negativos como positivos fallidos (n_queries=32, n_negative=0,
        # specificity=NULL, p=0.21875). Un promedio que mezcla ese run con los posteriores no es
        # comparable y quien lo lea no tiene forma de saberlo. El run NO SE BORRA (Regla de Oro 1):
        # se marca en su propio `detail` y se excluye del avg().
        # ⚠️ Y LA MITAD QUE NO ES OPCIONAL: si el reporte cambia el numero y no dice por que, se
        # reemplaza un agregado sesgado por otro SIN PROCEDENCIA -- el mismo defecto con el signo
        # invertido. Por eso `total_runs` sigue contando TODOS los runs y se publican
        # `runs_excluded_invalid` + `runs_in_avg`: el run no desaparece del reporte, deja de
        # contaminar el promedio.
        # NULL-safety: detail NULL ⇒ detail->>'invalid' NULL ⇒ IS DISTINCT FROM 'true' ⇒ SI entra
        # al promedio. Un run sin detail es valido, no sospechoso (excluir de mas es peor que el
        # sesgo que se esta corrigiendo).
        cur.execute(
            "SELECT count(*) AS runs_total, "
            "count(*) FILTER (WHERE detail->>'invalid' IS DISTINCT FROM 'true') AS runs_valid, "
            "count(*) FILTER (WHERE detail->>'invalid' = 'true') AS runs_excluded, "
            "avg(precision) FILTER (WHERE detail->>'invalid' IS DISTINCT FROM 'true') AS avg_p, "
            "avg(recall) FILTER (WHERE detail->>'invalid' IS DISTINCT FROM 'true') AS avg_r, "
            "avg(mrr) FILTER (WHERE detail->>'invalid' IS DISTINCT FROM 'true') AS avg_mrr, "
            "avg(specificity) FILTER (WHERE detail->>'invalid' IS DISTINCT FROM 'true') AS avg_spec "
            "FROM retrieval_eval_runs")
        agg = cur.fetchone()

    last_run = None
    if last:
        last_run = {
            "run_id": str(last["id"]),
            "k": last["k"],
            "n_queries": last["n_queries"],
            "precision_at_k": round(float(last["precision"]), 6) if last["precision"] is not None else None,
            "recall_at_k": round(float(last["recall"]), 6) if last["recall"] is not None else None,
            "mrr": round(float(last["mrr"]), 6) if last["mrr"] is not None else None,
            # n_negative + specificity: sin estos dos, evalset_size y n_queries no cierran
            # y el lector concluye que faltan queries. n_queries cuenta SOLO los positivos
            # a proposito (n_pos = len(evalset) - n_neg); los negativos se miden aparte.
            "n_negative": last["n_negative"],
            "specificity": (round(float(last["specificity"]), 6)
                            if last["specificity"] is not None else None),
            "triggered_by": last["triggered_by"],
            "run_at": last["run_at"].isoformat() if last["run_at"] else None,
        }

    return {
        "evalset_size": evalset_size,
        # Desglose que hace AUTOEXPLICATIVA la diferencia entre evalset_size y n_queries:
        # {"positive": 24, "negative": 8} ⇒ 24 = los que entran a precision/recall.
        "evalset_breakdown": por_kind,
        # total_runs sigue siendo TODOS los runs persistidos (el marcado NO desaparece del
        # reporte). runs_in_avg + runs_excluded_invalid dan la PROCEDENCIA del promedio.
        "total_runs": int(agg["runs_total"] or 0),
        "runs_in_avg": int(agg["runs_valid"] or 0),
        "runs_excluded_invalid": int(agg["runs_excluded"] or 0),
        "historical_avg_precision": round(float(agg["avg_p"]), 6) if agg["avg_p"] is not None else None,
        "historical_avg_recall": round(float(agg["avg_r"]), 6) if agg["avg_r"] is not None else None,
        "historical_avg_mrr": round(float(agg["avg_mrr"]), 6) if agg["avg_mrr"] is not None else None,
        "historical_avg_specificity": (round(float(agg["avg_spec"]), 6)
                                       if agg["avg_spec"] is not None else None),
        "last_run": last_run,
    }
