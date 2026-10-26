"""backfill_sanitize.py — drena el estanque: re-indexa con force los archivos ya
indexados cuyo contenido contiene secretos detectables por sanitize.

SEGURIDAD
 - NUNCA imprime el valor de un secreto: solo path + kind + sha256(valor)[:16].
 - CERO delete. Re-indexar es UPSERT sobre el mismo chunk_id determinístico.
 - NO modifica ningún .md: el indexer solo LEE el vault.

GUARDA DE PÉRDIDA DE ACCESO (lo más importante)
 Antes de procesar un archivo verifica que la credencial EXISTE en el .md en
 disco: que el archivo exista, sea legible, y que el/los fingerprint(s) del
 secreto que hoy están en text_full estén TAMBIÉN en el archivo. Si no se
 cumple, el Cerebro podría ser la única copia y marcarlo 'secret' (text_full
 =NULL) sería pérdida de acceso irreversible => NO se procesa y se reporta.

 Solo procesa chunks de agent_owner='indexer'. Los de otros owners (merlin,
 rias, ...) son de otros equipos (Regla de Oro 4): se reportan y se dejan
 intactos.

Uso:
  python backfill_sanitize.py --dry-run
  python backfill_sanitize.py --apply [--only <rel_path>]
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import db
import sanitize
import indexer

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault"))


def fp(val: str) -> str:
    return hashlib.sha256(val.encode("utf-8", "ignore")).hexdigest()[:16]


def secret_fps(text):
    """{(kind, sha256[:16])} de los secretos detectados. Nunca devuelve valores."""
    out = set()
    for f in sanitize.scan_secrets(text or ""):
        s, e = f["span"]
        out.add((f["kind"], fp((text or "")[s:e])))
    return out


def scan_expuestos(include_mislabeled=False):
    """Chunks VIVOS que exponen un secreto detectable por sanitize.

    Caso 1 (el declarado): sensitivity='normal' con text_full poblado.
    Caso 2 (include_mislabeled): sensitivity IN ('secret','metadata_only') pero que
      AUN exponen el valor en text_full y/o text_preview. Es una inconsistencia
      estructural respecto de db.upsert_chunk (que deja text_full=NULL para esas
      sensitivities): son chunks etiquetados por un camino anterior que no limpio
      el texto. Se arreglan con el MISMO mecanismo (force-reindex => upsert).
    """
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, source_path, chunk_index, agent_owner, sensitivity,
                   text_full, text_preview
            FROM chunks_meta
            WHERE deleted_at IS NULL
              AND ( (sensitivity='normal' AND text_full IS NOT NULL)
                    OR (%s AND sensitivity IN ('secret','metadata_only')
                        AND (text_full IS NOT NULL
                             OR (text_preview IS NOT NULL AND text_preview <> ''))) )
            ORDER BY source_path, chunk_index
        """, (include_mislabeled,))
        rows = cur.fetchall()

    indexer_hits, otros_hits = [], []
    for r in rows:
        if r["sensitivity"] == "normal":
            f = secret_fps(r["text_full"])
        else:
            f = secret_fps(r["text_full"]) | secret_fps(r["text_preview"])
        if not f:
            continue
        item = {"chunk_id": str(r["id"]), "source_path": r["source_path"],
                "chunk_index": r["chunk_index"], "owner": r["agent_owner"],
                "sensitivity": r["sensitivity"],
                "kinds": sorted({k for k, _ in f}), "fps": sorted({h for _, h in f})}
        (indexer_hits if r["agent_owner"] == "indexer" else otros_hits).append(item)
    return indexer_hits, otros_hits


def estado_archivo(sp):
    """Evidencia de estado de TODOS los chunks vivos de un source_path (owner=indexer)."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS chunks,
                   COUNT(*) FILTER (WHERE sensitivity='secret') AS n_secret,
                   COUNT(*) FILTER (WHERE sensitivity='normal') AS n_normal,
                   COUNT(*) FILTER (WHERE text_full IS NULL) AS n_tf_null,
                   COUNT(*) FILTER (WHERE text_full IS NOT NULL) AS n_tf_present,
                   MAX(source_hash) AS source_hash
            FROM chunks_meta
            WHERE deleted_at IS NULL AND source_path=%s AND agent_owner='indexer'
        """, (sp,))
        st = dict(cur.fetchone())

        # valor presente? booleano: buscamos el fingerprint en text_full Y text_preview
        cur.execute("""SELECT text_full, text_preview FROM chunks_meta
                       WHERE deleted_at IS NULL AND source_path=%s AND agent_owner='indexer'""", (sp,))
        fps_full, fps_prev = set(), set()
        for r in cur.fetchall():
            fps_full |= {h for _, h in secret_fps(r["text_full"])}
            fps_prev |= {h for _, h in secret_fps(r["text_preview"])}
    st["fps_en_text_full"] = sorted(fps_full)
    st["fps_en_text_preview"] = sorted(fps_prev)
    st["valor_presente_en_text_full"] = bool(fps_full)
    st["valor_presente_en_text_preview"] = bool(fps_prev)
    return st


def guarda_disco(sp, fps_en_db):
    """Verifica que el valor SOBREVIVE en el .md en disco antes de borrarlo de PG."""
    p = VAULT_ROOT / sp
    g = {"path_abs_existe": p.exists(), "es_archivo": p.is_file() if p.exists() else False,
         "legible": False, "fps_en_disco": [], "kinds_en_disco": [],
         "cubre_fps_de_db": False, "ok": False}
    if not g["es_archivo"]:
        g["motivo"] = "sin archivo detras (posible unica copia)"
        return g
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        g["motivo"] = f"ilegible: {type(e).__name__}"
        return g
    g["legible"] = True
    df = secret_fps(txt)
    g["fps_en_disco"] = sorted({h for _, h in df})
    g["kinds_en_disco"] = sorted({k for k, _ in df})
    if not df:
        g["motivo"] = "el .md en disco NO contiene el patron (posible unica copia)"
        return g
    g["cubre_fps_de_db"] = set(fps_en_db).issubset(set(g["fps_en_disco"]))
    if not g["cubre_fps_de_db"]:
        g["motivo"] = "el .md no contiene TODOS los secretos que estan en PG (posible unica copia)"
        return g
    g["ok"] = True
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="ejecuta el force-reindex")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", action="append", default=None, help="limitar a estos rel_path")
    ap.add_argument("--include-mislabeled", action="store_true",
                    help="incluir chunks ya marcados secret/metadata_only que AUN exponen "
                         "el valor en text_full/text_preview (inconsistencia estructural)")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        ap.error("elegí --dry-run o --apply")

    indexer_hits, otros_hits = scan_expuestos(include_mislabeled=args.include_mislabeled)

    rep = {"modo": "apply" if args.apply else "dry-run",
           "chunks_expuestos_owner_indexer": len(indexer_hits),
           "otros_owners_INTACTOS": otros_hits,
           "procesados": {}, "no_procesados_posible_unica_copia": {}}

    # agrupar por archivo: sensitivity es por-ARCHIVO, no por-chunk
    por_archivo = {}
    for h in indexer_hits:
        por_archivo.setdefault(h["source_path"], set()).update(h["fps"])

    objetivo = sorted(por_archivo)
    if args.only:
        objetivo = [p for p in objetivo if p in set(args.only)]

    a_procesar = []
    for sp in objetivo:
        g = guarda_disco(sp, por_archivo[sp])
        if not g["ok"]:
            rep["no_procesados_posible_unica_copia"][sp] = g
            continue
        a_procesar.append((sp, g))

    for sp, g in a_procesar:
        rep["procesados"][sp] = {"guarda_disco": g, "antes": estado_archivo(sp)}

    if args.apply and a_procesar:
        paths = [sp for sp, _ in a_procesar]
        print(f"[backfill] force-reindex de {len(paths)} archivos", flush=True)
        rep["stats_indexer"] = indexer.force_reindex(paths)
        for sp, _ in a_procesar:
            rep["procesados"][sp]["despues"] = estado_archivo(sp)

    print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
