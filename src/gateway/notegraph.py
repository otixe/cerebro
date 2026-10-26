"""notegraph.py - Grafo DETERMINISTA de notas del vault (backlinks/outlinks).

Escanea los .md del vault, parsea [[wikilinks]] reusando la misma convencion que
visor.build_graph (nodo = stem del archivo; target = ultimo segmento del wikilink,
sin alias |alias ni #heading), y arma adyacencia out/in. Cache en memoria con TTL
para no re-escanear en cada query. Additive: NO toca Postgres ni schema.

API:
  query(node, direction=both) -> {node, resolved, path, direction, out, in, out_count, in_count} | {..., error}
  stats() -> {nodes, links, notes_root, ttl}
"""
import os
import re
import time
from pathlib import Path

# El gateway monta el vault en /vault; las notas viven bajo <root>/obsidian-vault
# (misma convencion que VAULT_WRITE_BASE en app.py y VAULT_ROOT del visor).
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault"))
NOTES_ROOT = VAULT_ROOT / "obsidian-vault"
if not NOTES_ROOT.is_dir() and VAULT_ROOT.is_dir():
    NOTES_ROOT = VAULT_ROOT

# Mismos dirs excluidos que visor.build_graph (+ .trash/.smart-env del listado del visor).
EXCLUDE = ("node_modules", ".venv", "venv", ".git", "site-packages", "__pycache__",
           ".trash", ".smart-env", ".cache", ".deno")

# Convencion de parseo identica a build_graph: captura hasta ] | o #.
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")

CACHE_TTL = int(os.environ.get("NOTEGRAPH_TTL", "60"))
_cache = {"ts": 0.0, "graph": None}


def _scan(allowed=None):
    """Escanea el vault y arma el grafo. Devuelve {paths, out, in}.

    paths: stem -> path relativo (el primer archivo con ese stem gana, igual que build_graph).
    out:   stem -> set(targets)   (a quien apunta)
    in:    stem -> set(sources)   (quien la apunta = backlinks)
    """
    root = NOTES_ROOT
    files = [p for p in root.rglob("*.md") if not any(x in str(p) for x in EXCLUDE)
             and (allowed is None or allowed(str(p.relative_to(root))))]
    paths = {}
    for p in files:
        stem = p.stem
        if stem not in paths:
            paths[stem] = str(p.relative_to(root))
    out, inl = {}, {}
    for p in files:
        stem = p.stem
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in WIKILINK_RE.findall(txt):
            tgt = m.strip().split("/")[-1].strip()
            if tgt in paths and tgt != stem:
                out.setdefault(stem, set()).add(tgt)
                inl.setdefault(tgt, set()).add(stem)
    return {"paths": paths, "out": out, "in": inl}


def _graph(force=False):
    now = time.time()
    if force or _cache["graph"] is None or (now - _cache["ts"]) > CACHE_TTL:
        _cache["graph"] = _scan()
        _cache["ts"] = now
    return _cache["graph"]


def _resolve(g, node):
    """Resuelve node (stem o path parcial, case-insensitive) a un stem real.

    Devuelve (stem, None) si matchea unico; (None, error) si no matchea o es ambiguo.
    """
    if not node or not node.strip():
        return None, "node vacio"
    paths = g["paths"]
    q = node.strip()
    q = q[:-3] if q.lower().endswith(".md") else q
    # 0) match exacto CASE-SENSITIVE por stem (gana sobre colisiones de mayusculas)
    if q in paths:
        return q, None
    last_cs = q.split("/")[-1]
    if last_cs in paths:
        return last_cs, None

    ql = q.lower()
    last = ql.split("/")[-1]

    # 1) match exacto por stem (case-insensitive)
    exact = [s for s in paths if s.lower() == ql or s.lower() == last]
    exact = sorted(set(exact))
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "ambiguo: %r matchea varios stems: %s" % (node, exact[:10])

    # 2) match por path parcial (substring case-insensitive sobre el path relativo)
    pmatch = sorted(s for s, rel in paths.items() if ql in rel.lower())
    if len(pmatch) == 1:
        return pmatch[0], None
    if len(pmatch) > 1:
        narrow = [s for s in pmatch if s.lower() == last]
        if len(narrow) == 1:
            return narrow[0], None
        return None, "ambiguo: %r matchea %d notas: %s" % (node, len(pmatch), pmatch[:10])

    # 3) substring sobre el stem
    smatch = sorted(s for s in paths if last in s.lower())
    if len(smatch) == 1:
        return smatch[0], None
    if len(smatch) > 1:
        return None, "ambiguo: %r matchea %d notas: %s" % (node, len(smatch), smatch[:10])

    return None, "no encontrada: %r no matchea ninguna nota del vault" % node


_office_cache = {}
OFFICE_CACHE_MAX = int(os.environ.get("NOTEGRAPH_OFFICE_CACHE_MAX", "2"))


def _office_view(allowed):
    # Filtering the FILES is enough: _scan only records link targets that exist in
    # `paths`, and `paths` here holds only notes the caller may read. So a private note
    # can't appear as a node, a link target, a backlink, or merged under a homonym.
    return _scan(allowed)


def query(node, direction="both", allowed=None, cache_key=None):
    if allowed is None:
        g = _graph()
    else:
        # One cached office VIEW per deny signature (same TTL as the global graph).
        hit = _office_cache.get(cache_key) if cache_key is not None else None
        if hit and (time.time() - hit[0]) < CACHE_TTL:
            g = hit[1]
        else:
            g = _office_view(allowed)
            if cache_key is not None:
                now = time.time()
                # Bounded: drop expired views, keep at most OFFICE_CACHE_MAX (each view of
                # the whole vault weighs ~25 MB and the LXC shares 2 GiB with PG/Qdrant).
                for k in [k for k, (ts, _) in _office_cache.items() if now - ts >= CACHE_TTL]:
                    _office_cache.pop(k, None)
                while len(_office_cache) >= OFFICE_CACHE_MAX:
                    _office_cache.pop(min(_office_cache, key=lambda k: _office_cache[k][0]))
                _office_cache[cache_key] = (now, g)
    direction = (direction or "both").lower()
    if direction not in ("out", "in", "both"):
        return {"node": node, "resolved": None,
                "error": "direction invalida: %r (use out|in|both)" % direction}
    stem, err = _resolve(g, node)
    if err:
        return {"node": node, "resolved": None, "error": err}
    out_list = sorted(g["out"].get(stem, set())) if direction in ("out", "both") else []
    in_list = sorted(g["in"].get(stem, set())) if direction in ("in", "both") else []
    return {
        "node": node,
        "resolved": stem,
        "path": g["paths"].get(stem),
        "direction": direction,
        "out": out_list,
        "in": in_list,
        "out_count": len(out_list),
        "in_count": len(in_list),
    }


def stats():
    g = _graph()
    return {
        "nodes": len(g["paths"]),
        "links": sum(len(v) for v in g["out"].values()),
        "notes_root": str(NOTES_ROOT),
        "ttl": CACHE_TTL,
    }


if __name__ == "__main__":
    import json
    g = _graph(force=True)
    st = stats()
    print("[notegraph self-test] stats:", json.dumps(st))
    assert st["nodes"] > 0, "FAIL: no se encontraron notas en %s" % NOTES_ROOT
    # hub = nodo con mas conexiones (out+in)
    scored = sorted(g["paths"].keys(),
                    key=lambda s: len(g["out"].get(s, set())) + len(g["in"].get(s, set())),
                    reverse=True)
    hub = scored[0]
    print("[notegraph self-test] hub:", hub)
    r = query(hub, "both")
    print("[notegraph self-test] query(hub,both):", json.dumps(r)[:800])
    assert r.get("resolved") == hub, "FAIL: resolucion del hub"
    # case-insensitive
    r2 = query(hub.lower(), "out")
    assert r2.get("resolved") is not None, "FAIL: resolucion case-insensitive"
    # error claro para inexistente
    r3 = query("__no_existe_zzz_qqq__", "both")
    assert r3.get("error"), "FAIL: deberia dar error para nota inexistente"
    # direction invalida
    r4 = query(hub, "sideways")
    assert r4.get("error"), "FAIL: direction invalida deberia dar error"
    print("[notegraph self-test] PASS")
