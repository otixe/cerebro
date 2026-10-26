"""Indexer worker.

 1. Al arrancar: scan completo del vault (solo archivos no indexados o con hash distinto)
 2. Luego: watchdog continuo sobre cambios

Idempotencia:
 - chunk_id determinístico (UUID v5 por (source_path, chunk_index)) — ver db.py
 - Si el hash del archivo no cambió respecto al registro en chunks_meta → skipped (no re-upsert).
 - Si cambió: upsert chunks nuevos y soft-delete de los sobrantes (cuando el archivo encoge).

Política de secrets (D10 opción B):
 - Rutas bajo SECRETS_METADATA_ONLY → sensitivity='secret', text vacío.
   Se indexa SOLO el título/path para poder detectar existencia sin exponer contenido.

Política de secrets POR CONTENIDO (2026-08-06):
 - Además de la política por RUTA (arriba), todo archivo normal pasa por
   `sanitize.sanitize()` ANTES de chunkear/embeddear/persistir, con la MISMA
   semántica que ya usa app.py en el write-path de la API:
     · el texto que se chunkea/embeddea/persiste es el REDACTADO (`clean`),
     · si `report["secrets"]` no está vacío → sensitivity='secret'
       (db.upsert_chunk ya evita persistir text_full en PG y el payload en Qdrant).
   Antes de esto la única política era por ruta: un archivo con credenciales en
   claro fuera de "95 Secrets" quedaba sensitivity='normal' y recuperable por
   cualquier agente con key válida vía /memory/search.
   El hash de idempotencia sigue siendo el del ARCHIVO en disco (no del texto
   saneado), así que re-indexar un archivo ya saneado sigue dando skipped.

Politica de credenciales en SKILLS (decision de Otixe, 2026-08-10):
 - Una skill es un PROCEDIMIENTO EJECUTABLE: si el manual de "como conectarse a
   X" no trae la credencial, el agente que lo carga no puede completar la tarea.
   El vault es infra propia y privada (acceso solo con key de la flota, sin
   superficie a terceros): la flota ES el destinatario legitimo de esas
   credenciales.
 - Por eso los archivos bajo `skills/` NO pasan por el saneamiento por
   CONTENIDO: se chunkea/embeddea/persiste el texto ORIGINAL y se deja
   sensitivity='normal'. Sin esta excepcion, `db.upsert_chunk` deja
   text_full=NULL en PG y el payload `text` vacio en Qdrant, y como
   `sensitivity` es por ARCHIVO, degrada la skill ENTERA.
 - El aviso se CONSERVA (log [SKILL-SECRET-KEPT] con familias + cantidad, nunca
   el valor): se quita la degradacion, no la observabilidad.
 - Alcance estricto: NO afecta el camino por RUTA de SECRETS_METADATA_ONLY
   ("95 Secrets", que sigue indexando solo el titulo con sensitivity='secret'),
   ni el saneamiento del resto del vault (notas, informes, dumps), donde una
   credencial aparece por DESCUIDO y el gate es correcto.
 - La deteccion es por RUTA, igual que la rama de SECRETS_METADATA_ONLY, y se
   evalua ANTES de llamar a sanitize.

Modo FORCE-REINDEX (2026-08-10):
 - `index_file(path, force=True)` saltea SOLO la comparación de hash
   (`existing_hash == file_hash`), nada más. Es el drenaje del estanque: un
   archivo YA indexado como 'normal' con una credencial adentro jamás se
   re-evaluaba, porque el archivo en disco no cambió y el skip-por-hash cortaba
   ANTES del saneamiento por contenido. El fix de sanitize cierra el caudal
   futuro (archivos nuevos/modificados); force drena lo ya indexado.
 - Es EXPLÍCITO y opt-in por llamada: el default (`force=False`) deja el
   comportamiento normal intacto. El skip-por-hash es una optimización correcta
   y NO se toca para el flujo normal (watchdog, full_scan, reconciliación).
   Deliberadamente NO se lee ninguna variable de entorno para activarlo: una env
   var puede quedar prendida por accidente en el contenedor y convertir cada
   reconciliación (cada 6h) en un re-embedding completo del vault.
 - Idempotente: el chunk_id es UUID v5 determinístico por (source_path,
   chunk_index) y db.upsert_chunk hace ON CONFLICT (source_path, chunk_index)
   DO UPDATE, así que re-correrlo PISA los mismos chunks y no inserta nuevos.
 - Uso: `python indexer.py --force-reindex <rel_path> [<rel_path> ...]`
   (rutas relativas a VAULT_ROOT). No arranca el watchdog ni hace full_scan.
"""
import os
import time
import hashlib
import signal
from pathlib import Path
from typing import Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import db
import embedder
import chunker
import sanitize

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault"))
EXCLUDE = [p.strip() for p in os.environ.get("INDEX_EXCLUDE_PATHS", "").split(",") if p.strip()]
SECRETS_META = [p.strip() for p in os.environ.get("SECRETS_METADATA_ONLY", "").split(",") if p.strip()]

# Prefijo de las skills del registro compartido. Es el MISMO valor que
# `SKILLS_PREFIX` en app.py; esta duplicado a proposito y no importado, porque
# app.py es la app FastAPI (importarla desde el worker del indexer arrastraria
# todo el stack HTTP). Si cambia alla, cambia aca.
SKILLS_PREFIX = "skills/"

# Extensiones a indexar
EXTS = {".md", ".markdown", ".txt", ".rst"}

# Archivos específicos a ignorar
IGNORE_NAMES = {"heartbeat-state.json", ".env", ".env.example"}

# Contadores globales (reseteados por full_scan / reportados al final)
_STATS = {"new": 0, "modified": 0, "skipped": 0, "skipped_empty": 0,
          "tiny_document_fallback": 0, "errors": 0, "deleted_leftover": 0,
          "secrets": 0, "secrets_redacted": 0, "forced": 0, "skill_secrets_kept": 0}


def _should_skip(path: Path) -> Optional[str]:
    try:
        rel = path.relative_to(VAULT_ROOT)
    except ValueError:
        return "outside-vault"
    s = str(rel)
    if path.name.startswith("."):
        return "hidden"
    if path.name in IGNORE_NAMES:
        return "ignored-name"
    if path.suffix.lower() not in EXTS:
        return "ext"
    for ex in EXCLUDE:
        if s.startswith(ex) or f"/{ex}/" in "/" + s + "/":
            return f"excluded:{ex}"
    return None


def _path_matches_secrets(path: Path) -> bool:
    try:
        rel = str(path.relative_to(VAULT_ROOT))
    except ValueError:
        return False
    for ex in SECRETS_META:
        if rel.startswith(ex) or f"/{ex}/" in "/" + rel + "/":
            return True
    return False


def _path_is_skill(path: Path) -> bool:
    """True si el archivo cae bajo `skills/` dentro del vault.

    Deteccion por RUTA (misma mecanica que `_path_matches_secrets`), evaluada
    ANTES del saneamiento por contenido. Ver "Politica de credenciales en
    SKILLS" en el docstring del modulo.
    """
    try:
        rel = str(path.relative_to(VAULT_ROOT))
    except ValueError:
        return False
    rel = rel.replace("\\", "/")
    return rel == SKILLS_PREFIX.rstrip("/") or rel.startswith(SKILLS_PREFIX)


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_text(p: Path) -> str:
    """Lee el archivo y SANEA los bytes que Postgres rechaza.

    FIX E3 (2026-08-03, tarea b37fdc0b): sin el saneo, un archivo con NUL (0x00)
    reventaba en el INSERT con "PostgreSQL text fields cannot contain NUL (0x00)
    bytes", el hash nunca se guardaba, y por lo tanto el archivo se reintentaba
    en CADA pasada del indexer, para siempre: medido 15 reintentos del mismo
    archivo, con su costo de embedding cada vez. Era un bucle de reintento
    infinito disfrazado de error transitorio.
    """
    try:
        txt = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = p.read_text(encoding="latin-1", errors="ignore")
    # Postgres no acepta NUL en campos text; se descarta el byte, no el archivo.
    if "\x00" in txt:
        txt = txt.replace("\x00", "")
    return txt


def _derive_tags(path: Path) -> list[str]:
    try:
        rel = path.relative_to(VAULT_ROOT)
    except ValueError:
        return []
    parts = list(rel.parts[:-1])
    tags = []
    for part in parts:
        # Limpiar "50 Agents" → "agents"
        t = part.lower().lstrip("0123456789 -_").strip()
        if t:
            tags.append(t.replace(" ", "-"))
    return tags[:8]


def _get_indexed_state(path_str: str):
    """Devuelve (hash_actual, max_chunk_idx) para chunks activos del source_path, o (None, None)."""
    with db.pg() as conn, conn.cursor() as cur:
        cur.execute("""SELECT source_hash, MAX(chunk_index) AS max_idx, COUNT(*) AS n
                       FROM chunks_meta
                       WHERE source_path=%s AND deleted_at IS NULL
                       GROUP BY source_hash
                       ORDER BY n DESC LIMIT 1""", (path_str,))
        row = cur.fetchone()
        if not row:
            return None, None, 0
        return row["source_hash"], row["max_idx"], row["n"]


def _soft_delete_leftover(path_str: str, new_count: int) -> int:
    """Soft-delete chunks con chunk_index >= new_count (archivo encogio).

    FIX 2 (2026-08-04): la implementacion se movio a `db.prune_leftover_chunks`
    para que el indexer Y los endpoints de escritura por API
    (/memory/add-markdown, /skills/add-reference) usen EXACTAMENTE la misma
    logica. Antes vivia aca sola y los endpoints no prunaban nada, dejando
    chunks huerfanos vivos. Esto queda como alias fino por compatibilidad de
    llamadas internas; el comportamiento (deleted_at=now() + espejo
    set_payload deleted=True en Qdrant) es identico.
    """
    return db.prune_leftover_chunks(path_str, new_count)


def index_file(path: Path, force: bool = False) -> None:
    """Indexa un archivo del vault.

    force=True saltea ÚNICAMENTE el skip por `source_hash` (ver docstring del
    módulo). No cambia el chunkeo, ni el camino por RUTA, ni la política de
    saneamiento: solo obliga a que el archivo vuelva a pasar por el pipeline
    completo (leer -> sanitize -> chunkear -> embeddear -> upsert) aunque su
    hash en disco no haya cambiado.
    """
    skip = _should_skip(path)
    if skip:
        return
    t0 = time.time()
    try:
        path_str = str(path.relative_to(VAULT_ROOT))
    except ValueError:
        return

    try:
        # Secret policy B: indexamos SOLO título con sensitivity='secret', text vacío
        if _path_matches_secrets(path):
            doc_title = path.stem
            tags = _derive_tags(path) + ["secrets-metadata"]
            # Hash sobre nombre + mtime para detectar renames/updates sin abrir contenido sensible.
            st = path.stat()
            meta_src = f"{path_str}|{st.st_mtime_ns}|{st.st_size}"
            file_hash = hashlib.sha256(meta_src.encode()).hexdigest()

            existing_hash, _existing_max, _existing_n = _get_indexed_state(path_str)
            if existing_hash == file_hash:
                if not force:
                    _STATS["skipped"] += 1
                    return
                _STATS["forced"] += 1

            placeholder = f"Archivo sensible (solo metadata): {path_str}\nTítulo: {doc_title}"
            vec = (embedder.embed("", title=doc_title) if embedder.profile_active()
                   else embedder.embed(placeholder))
            db.upsert_chunk(
                chunk_id=None, source_path=path_str, source_hash=file_hash, chunk_index=0,
                title=doc_title, breadcrumb=[doc_title],
                text="", vector=vec,
                tags=tags, agent_owner="indexer", sensitivity="secret",
            )
            _soft_delete_leftover(path_str, new_count=1)
            if existing_hash is None:
                _STATS["new"] += 1
            else:
                _STATS["modified"] += 1
            _STATS["secrets"] += 1
            db.log_index(path_str, "indexed-secret-metadata", chunks_count=1,
                         duration_ms=int((time.time()-t0)*1000))
            return

        # Archivo normal
        text = _read_text(path)
        if not text.strip():
            _STATS["skipped_empty"] += 1
            db.log_index(path_str, "skipped-empty",
                         duration_ms=int((time.time()-t0)*1000))
            print(f"[indexer] ⏭ {path_str} (empty/whitespace)", flush=True)
            return
        file_hash = _hash_file(path)

        existing_hash, _existing_max, _existing_n = _get_indexed_state(path_str)
        if existing_hash == file_hash:
            if not force:
                _STATS["skipped"] += 1
                return
            # Mismo hash pero re-indexado a proposito: el saneamiento por
            # CONTENIDO de mas abajo es justamente lo que nunca corrio.
            _STATS["forced"] += 1

        doc_title = path.stem
        tags = _derive_tags(path)

        # --- Saneamiento por CONTENIDO (misma semantica que app.py /memory/add) ---
        # Se aplica ANTES de chunkear/embeddear/persistir: el texto que viaja al
        # chunker, al embedder y a Postgres/Qdrant es SIEMPRE el redactado (`clean`),
        # nunca el original. Si hay secretos de alta confianza -> sensitivity='secret'
        # (db.upsert_chunk deja text_full=NULL y payload vacio).
        #
        # EXCEPCION SKILLS (Otixe, 2026-08-10): si el archivo cae bajo `skills/`,
        # se escanea igual para poder AVISAR, pero se descarta el efecto: se
        # persiste/embeddea el ORIGINAL y queda sensitivity='normal'. Una skill sin
        # su credencial no es ejecutable, y el vault es privado.
        is_skill = _path_is_skill(path)
        clean, report = sanitize.sanitize(text)
        # El texto que se EMBEDDEA es el mismo que se PERSISTE (chunker.enrich_text
        # se calcula sobre estos mismos chunks): si se separaran, el vector no
        # corresponderia al texto guardado.
        text_to_index = text if is_skill else clean
        sensitivity = "normal"
        if report["secrets"]:
            kinds = ", ".join(sorted({s["kind"] for s in report["secrets"]}))
            if is_skill:
                _STATS["skill_secrets_kept"] += 1
                # NUNCA se loguea el valor del secreto: solo path + familias + cantidad.
                print(f"[indexer] [SKILL-SECRET-KEPT] {path_str} "
                      f"kinds={kinds} count={len(report['secrets'])} "
                      f"sensitivity=normal (skill path: credencial preservada a proposito)",
                      flush=True)
            else:
                sensitivity = "secret"
                _STATS["secrets_redacted"] += 1
                print(f"[indexer] [SECRET] redacted before persist: {path_str} "
                      f"kinds={kinds} count={len(report['secrets'])} sensitivity=secret",
                      flush=True)
        if report["private_redacted"]:
            print(f"[indexer] [PRIVATE] block(s) "
                  f"{'detected but KEPT (skill path)' if is_skill else 'redacted'}: {path_str} "
                  f"count={report['private_redacted']}", flush=True)

        chunks = chunker.chunk_markdown(text_to_index, doc_title=doc_title)
        # FLOOR_CHARS es una politica para FRAGMENTOS, no para una nota completa.
        # El chunker puede devolver [] para un documento entero de menos de 25
        # caracteres; preservar exactamente esa nota evita que una referencia breve
        # desaparezca del indice. No se aplica a documentos largos ni reconstituye
        # fragmentos: conserva las protecciones de ruta/sanitize de arriba intactas.
        tiny_document_limit = chunker.FLOOR_CHARS // 2
        if (not chunks and text_to_index.strip()
                and len(text_to_index.strip()) < tiny_document_limit):
            chunks = [chunker.Chunk(
                index=0, title=doc_title, breadcrumb=[doc_title], text=text_to_index,
                char_start=0, char_end=len(text_to_index),
            )]
            _STATS["tiny_document_fallback"] += 1
            print(f"[indexer] ↪ {path_str} (preserved complete tiny document; "
                  f"chars={len(text_to_index.strip())} < {tiny_document_limit})", flush=True)
        if not chunks:
            # Do not prune with new_count=0: an anomalous chunker result must not
            # soft-delete an earlier active representation. It is observable, unlike
            # the old successful-looking `indexed (0 chunks)` outcome.
            _STATS["skipped"] += 1
            db.log_index(path_str, "skipped-no-chunks",
                         duration_ms=int((time.time()-t0)*1000))
            print(f"[indexer] ⏭ {path_str} (no chunks; active representation retained)", flush=True)
            return

        emb_texts = [chunker.enrich_text(c) for c in chunks]
        if embedder.profile_active():
            vecs = embedder.embed_batch([c.text for c in chunks], titles=[c.title for c in chunks])
        else:
            vecs = embedder.embed_batch(emb_texts)
        for c, vec in zip(chunks, vecs):
            db.upsert_chunk(
                chunk_id=None, source_path=path_str, source_hash=file_hash,
                chunk_index=c.index, title=c.title, breadcrumb=c.breadcrumb,
                text=c.text, vector=vec,
                tags=tags, agent_owner="indexer", sensitivity=sensitivity,
            )

        removed = _soft_delete_leftover(path_str, new_count=len(chunks))
        if existing_hash is None:
            _STATS["new"] += 1
        else:
            _STATS["modified"] += 1
        if removed:
            _STATS["deleted_leftover"] += removed

        db.log_index(path_str,
                     "indexed-secret-redacted" if sensitivity == "secret"
                     else ("indexed-skill-secret-kept" if (is_skill and report["secrets"])
                           else "indexed"),
                     chunks_count=len(chunks),
                     duration_ms=int((time.time()-t0)*1000))
        print(f"[indexer] ✓ {path_str} ({len(chunks)} chunks, sensitivity={sensitivity})", flush=True)
    except Exception as e:
        _STATS["errors"] += 1
        db.log_index(path_str, "error", duration_ms=int((time.time()-t0)*1000), error=str(e))
        print(f"[indexer] ✗ {path_str}: {e}", flush=True)


def delete_file(path: Path) -> None:
    try:
        path_str = str(path.relative_to(VAULT_ROOT))
    except ValueError:
        return
    n = db.soft_delete_chunks(source_path=path_str)
    if n:
        db.log_index(path_str, "deleted", chunks_count=n)
        print(f"[indexer] 🗑 {path_str} ({n} chunks soft-deleted)", flush=True)


def full_scan():
    print(f"[indexer] 🔎 Full scan {VAULT_ROOT}", flush=True)
    for k in _STATS:
        _STATS[k] = 0
    total = 0
    for p in VAULT_ROOT.rglob("*"):
        if p.is_file():
            index_file(p)
            total += 1
    print(
        f"[indexer] ✅ Full scan done "
        f"(files_seen={total} new={_STATS['new']} modified={_STATS['modified']} "
        f"skipped={_STATS['skipped']} skipped_empty={_STATS['skipped_empty']} "
        f"tiny_document_fallback={_STATS['tiny_document_fallback']} errors={_STATS['errors']} "
        f"leftover_deleted={_STATS['deleted_leftover']} secrets={_STATS['secrets']} "
        f"secrets_redacted={_STATS['secrets_redacted']} "
        f"skill_secrets_kept={_STATS['skill_secrets_kept']})",
        flush=True,
    )


def force_reindex(rel_paths: list[str]) -> dict:
    """Re-indexa las rutas dadas (relativas a VAULT_ROOT) ignorando source_hash.

    Devuelve una copia de los contadores. NO arranca watchdog ni full_scan.
    Solo LEE los .md del vault: el archivo en disco nunca se modifica.
    """
    for k in _STATS:
        _STATS[k] = 0
    for rel in rel_paths:
        p = VAULT_ROOT / rel
        if not p.is_file():
            print(f"[indexer] ⚠ force-reindex: no es un archivo: {rel}", flush=True)
            _STATS["errors"] += 1
            continue
        index_file(p, force=True)
    print(
        f"[indexer] ✅ force-reindex done "
        f"(files={len(rel_paths)} forced={_STATS['forced']} new={_STATS['new']} "
        f"modified={_STATS['modified']} skipped={_STATS['skipped']} "
        f"skipped_empty={_STATS['skipped_empty']} "
        f"tiny_document_fallback={_STATS['tiny_document_fallback']} "
        f"errors={_STATS['errors']} leftover_deleted={_STATS['deleted_leftover']} "
        f"secrets={_STATS['secrets']} secrets_redacted={_STATS['secrets_redacted']} "
        f"skill_secrets_kept={_STATS['skill_secrets_kept']})",
        flush=True,
    )
    return dict(_STATS)


class _Handler(FileSystemEventHandler):
    def on_modified(self, ev):
        if not ev.is_directory: index_file(Path(ev.src_path))
    def on_created(self, ev):
        if not ev.is_directory: index_file(Path(ev.src_path))
    def on_deleted(self, ev):
        if not ev.is_directory: delete_file(Path(ev.src_path))
    def on_moved(self, ev):
        if not ev.is_directory:
            delete_file(Path(ev.src_path))
            index_file(Path(ev.dest_path))


# FIX E3 (2026-08-03, tarea b37fdc0b): reconciliacion periodica.
# Antes el full_scan corria SOLO al arrancar el contenedor, asi que un evento inotify
# perdido quedaba desincronizado hasta el proximo reinicio (el indexer llevaba 4 dias
# up = 4 dias sin reconciliar). Ahora se repite cada RECONCILE_EVERY_SECS.
# Es BARATO: index_file() deduplica por hash (_get_indexed_state), asi que una pasada
# sobre un vault sin cambios no re-embeddea nada — solo hace stat + compara hash.
RECONCILE_EVERY_SECS = int(os.environ.get("INDEXER_RECONCILE_SECS", str(6 * 3600)))


def main():
    db.ensure_collection()
    full_scan()

    print(f"[indexer] 👁  Watching {VAULT_ROOT} "
          f"(reconciliacion cada {RECONCILE_EVERY_SECS}s)", flush=True)
    obs = Observer()
    obs.schedule(_Handler(), str(VAULT_ROOT), recursive=True)
    obs.start()

    stop = False
    def _sig(*_):
        nonlocal stop; stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    ultimo_scan = time.time()
    try:
        while not stop:
            time.sleep(1)
            if RECONCILE_EVERY_SECS > 0 and (time.time() - ultimo_scan) >= RECONCILE_EVERY_SECS:
                try:
                    print("[indexer] 🔁 Reconciliacion periodica", flush=True)
                    full_scan()
                except Exception as e:
                    print(f"[indexer] ⚠ reconciliacion fallo: {type(e).__name__}: {e}", flush=True)
                ultimo_scan = time.time()
    finally:
        obs.stop(); obs.join()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--force-reindex":
        # Modo aparte y explícito: no toca el worker normal (sin watchdog,
        # sin full_scan, sin reconciliación periódica).
        targets = sys.argv[2:]
        if not targets:
            print("uso: python indexer.py --force-reindex <rel_path> [...]", flush=True)
            raise SystemExit(2)
        db.ensure_collection()
        force_reindex(targets)
    else:
        main()
