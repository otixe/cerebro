"""Markdown chunker - por secciones de encabezado, con overlap.
Preserva breadcrumb de headers (título jerárquico) para mejor contexto."""
import re
from dataclasses import dataclass, field

HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Tamaños por chunk (aprox. por caracteres, ~4 chars = 1 token)
#
# FIX E2 (2026-08-03, tarea 370a7d3a): MAX_CHARS estaba en 3200 mientras el embedder
# trunca a 2048 (cohere.embed-multilingual-v3, embedder.py: t[:2048]). Medido sobre el
# indice real: 3.589 chunks (5,66%) superaban 2048 y 5.259.639 chars (14,49% del texto)
# quedaban INVISIBLES al vector, en silencio.
# EMBED_LIMIT es el limite duro del modelo. MAX_CHARS deja margen para el breadcrumb,
# porque lo que se manda a embeddear es enrich_text() = "[breadcrumb]\n\n" + text
# (overhead medido: 102 chars promedio, 878 el peor caso; 379 chunks se truncaban SOLO
# por el breadcrumb). Si MAX_CHARS fuera 2048 a secas, el corte seguiria mordiendo texto.
EMBED_LIMIT = 2048        # limite duro de cohere embed v3 (ver embedder.py)
BREADCRUMB_BUDGET = 200   # reserva para "[breadcrumb]\n\n" (p95 del overhead medido)
MAX_CHARS = EMBED_LIMIT - BREADCRUMB_BUDGET   # 1848: todo el chunk entra al vector
MIN_CHARS = 200    # evita chunks minúsculos
OVERLAP_CHARS = 400  # ~100 tokens de solapamiento

@dataclass
class Chunk:
    index: int
    title: str
    breadcrumb: list[str]
    text: str
    char_start: int
    char_end: int

def _split_by_headers(md: str) -> list[tuple[int, int, str]]:
    """Devuelve lista (level, start, title)."""
    headers = [(len(m.group(1)), m.start(), m.group(2).strip()) for m in HEADER_RE.finditer(md)]
    return headers

def chunk_markdown(md: str, doc_title: str = "") -> list[Chunk]:
    md = md.rstrip() + "\n"
    headers = _split_by_headers(md)
    if not headers:
        return _chunk_plain(md, doc_title)

    chunks: list[Chunk] = []
    breadcrumb: list[tuple[int, str]] = []  # (level, title)
    # Segmentar por header: cada sección desde header N hasta siguiente header de nivel <= N
    for i, (lvl, start, title) in enumerate(headers):
        end = headers[i + 1][1] if i + 1 < len(headers) else len(md)
        # mantener breadcrumb jerárquico
        breadcrumb = [(l, t) for l, t in breadcrumb if l < lvl]
        breadcrumb.append((lvl, title))
        body = md[start:end].strip()
        if len(body) < MIN_CHARS and chunks:
            # pegar al anterior si es muy chico.
            # FIX E2: el merge NO puede desbordar MAX_CHARS. Antes sumaba sin
            # re-verificar el techo, y por eso habia 378 chunks activos POR ENCIMA
            # del propio MAX_CHARS del chunker (medido) — el limite se violaba a si
            # mismo. Si no cabe, el fragmento va como chunk propio.
            if len(chunks[-1].text) + len(body) + 2 <= MAX_CHARS:
                chunks[-1].text += "\n\n" + body
                chunks[-1].char_end = end
                continue
        crumb = [t for _, t in breadcrumb]
        # si sección es enorme, subdividir
        for sub in _split_large(body, MAX_CHARS, OVERLAP_CHARS):
            chunks.append(Chunk(
                index=len(chunks),
                title=title,
                breadcrumb=([doc_title] + crumb) if doc_title else crumb,
                text=sub,
                char_start=start,
                char_end=end,
            ))
    return _drop_tiny(chunks)

# Piso absoluto: por debajo de esto un fragmento no aporta señal semantica y solo
# compite con contenido curado en los resultados. Medido en el indice real: 245 chunks
# de longitud 0 y 6.739 con menos de 50 chars (10,63%).
FLOOR_CHARS = 50


def _drop_tiny(chunks: list["Chunk"]) -> list["Chunk"]:
    """FIX E2 (2026-08-03): descarta fragmentos por debajo de FLOOR_CHARS.

    MIN_CHARS=200 solo funcionaba como criterio de MERGE (`if ... and chunks`), asi que
    el PRIMER fragmento de un documento sobrevivia suelto por chico que fuera: un doc
    entero de 9 chars se indexaba igual (reproducido). Esto es el hallazgo MEDIO-6 de la
    auditoria integral, abierto desde entonces.
    Se descarta por VACIO/RUIDO, no se borra nada existente: aplica al indexar de aca en
    adelante. Si TODOS los fragmentos son diminutos se conserva el mayor, para no dejar
    un documento sin ninguna representacion en el indice.
    """
    if not chunks:
        return chunks
    keep = [c for c in chunks if len(c.text.strip()) >= FLOOR_CHARS]
    if not keep:
        # Ningun fragmento alcanza el piso. Conservar el mayor SOLO si tiene algo de
        # sustancia (>= FLOOR_CHARS/2); si el documento entero son 9 chars, no se indexa:
        # un chunk de 9 chars no es representacion, es ruido que compite en el ranking.
        mejor = max(chunks, key=lambda c: len(c.text.strip()))
        keep = [mejor] if len(mejor.text.strip()) >= FLOOR_CHARS // 2 else []
    for i, c in enumerate(keep):
        c.index = i
    return keep


def _chunk_plain(text: str, doc_title: str) -> list[Chunk]:
    out = []
    for i, sub in enumerate(_split_large(text, MAX_CHARS, OVERLAP_CHARS)):
        out.append(Chunk(
            index=i, title=doc_title or "(sin título)",
            breadcrumb=[doc_title] if doc_title else [],
            text=sub, char_start=0, char_end=len(text),
        ))
    return _drop_tiny(out)

# Progreso MINIMO que debe ganar el cursor en cada iteracion de _split_large.
# Se elige == MIN_CHARS (200) a proposito: por debajo de eso el fragmento nuevo no
# aporta contenido propio (todo seria overlap del anterior) y el chunker se degrada.
MIN_PROGRESS_CHARS = MIN_CHARS   # 200


def _split_large(text: str, max_chars: int, overlap: int) -> list[str]:
    """Parte un cuerpo grande en fragmentos de <= max_chars con `overlap` de solape.

    FIX F1 (2026-08-04, tarea b0c14345): el `i = max(cut - overlap, i + 1)` original
    GARANTIZABA TERMINACION pero NO PROGRESO. Si el ultimo separador de la ventana caia
    a menos de `overlap` chars del cursor (p.ej. "\\n\\n" en la posicion 30), el salto
    `cut - overlap` era NEGATIVO respecto de i y el `max()` lo clampaba a `i + 1`: el
    cursor avanzaba UN caracter por iteracion y el mismo texto se re-emitia cientos de
    veces, desplazado 1 char. Medido en el indice real: 55 documentos generaban 35.080
    fragmentos donde correspondian 2.958 (-92%). No es un cuelgue, es degradacion
    cuadratica SILENCIOSA: termina, indexa, y contamina el ranking con casi-duplicados.

    El fix: exigir que el corte de un progreso minimo (`cut - i >= overlap + 200`, o sea
    que el cursor nuevo avance >= 200); si no lo da, cortar en el techo duro `end`.

    ================= NO REINTRODUCIR EL `max()` EN EL SALTO =================
    El salto es `i = cut - overlap`, SIN `max(...)`, y esto es deliberado.
    NO volver a escribir `i = max(cut - overlap, i + N)` (con N = 1, MIN_CHARS o
    max_chars - overlap): saltar el cursor POR ENCIMA del `cut` PIERDE para siempre el
    texto que queda entre `cut` y el cursor nuevo, porque ese tramo no lo emite nadie:
    el fragmento ya cerrado termina en `cut` y el siguiente arranca despues.
    Medido 2026-08-04 con un instrumentador de cobertura de caracteres sobre los 55 docs
    reales: la variante con `i + step` (step = max_chars - overlap) PERDIA 69.833 chars
    de texto real en 43 de 55 documentos, en silencio y sin ninguna senal en el indice
    (el texto que sobrevive sigue siendo valido, asi que ninguna sonda de duplicacion
    lo detecta). Tambien mutilaba documentos HOY SANOS: en un doc de parrafos normales
    perdia 838 chars donde el codigo viejo perdia 0.

    El guard usa `overlap + 200` (no `overlap`, no `max_chars - overlap`) porque la
    condicion correcta es sobre el AVANCE DEL CURSOR, que es `(cut - overlap) - i`:
      - `cut - i < overlap`        => avance negativo (el bug original).
      - `cut - i == overlap`       => avance CERO => loop infinito.
      - `cut - i >= overlap + 200` => avance >= 200 chars, y `cut - overlap > i` siempre,
                                      asi que el cursor NUNCA pasa por encima del corte
                                      => overlap EXACTO y CERO perdida de texto.
    Con `max_chars - overlap` como umbral (variante mas agresiva) la terminacion tambien
    se cumple, pero se descartan cortes en separador real perfectamente buenos: medido,
    63,1% de los cortes caen en separador con este umbral vs 44,3% con el agresivo.

    Invariantes (cubiertos por gateway/tests/test_chunker_progreso.py):
      1. cobertura total del original: cero chars perdidos.
      2. ningun fragmento > max_chars (limite del embedder, ver EMBED_LIMIT).
      3. avance del cursor >= MIN_PROGRESS_CHARS en toda iteracion (=> termina).
      4. el caso de parrafos sanos produce lo MISMO que el codigo pre-fix.
    """
    if len(text) <= max_chars:
        return [text]
    out = []
    i = 0
    while i < len(text):
        end = min(i + max_chars, len(text))
        # intentar cortar en párrafo/oración
        if end < len(text):
            cut = text.rfind("\n\n", i, end)
            if cut == -1:
                cut = text.rfind(". ", i, end)
            if cut == -1:
                cut = end
            # el corte tiene que dar progreso real al cursor; si no, techo duro.
            # (cut == end nunca cae aca: end - i == max_chars == 1848 >> 600.)
            if cut - i < overlap + MIN_PROGRESS_CHARS:
                cut = end
            out.append(text[i:cut])
            i = cut - overlap        # SIN max(): ver docstring. cut-overlap > i garantizado.
        else:
            out.append(text[i:end])
            break
    return out

def enrich_text(chunk: Chunk) -> str:
    """Prepend breadcrumb al texto que se envía a embeddings (mejor contexto semántico)."""
    if chunk.breadcrumb:
        crumb = " > ".join(chunk.breadcrumb)
        return f"[{crumb}]\n\n{chunk.text}"
    return chunk.text
