"""Local EmbeddingGemma by default; Bedrock Titan/Cohere are optional, lazy backends."""
import json
import math
import os
import httpx

# Missing or empty selects local Ollama; invalid values never fall back to cloud.
BACKEND = os.environ.get("EMBEDDING_BACKEND", "ollama").strip().lower() or "ollama"
if BACKEND not in ("ollama", "bedrock"):
    raise ValueError("Unsupported EMBEDDING_BACKEND (expected ollama or bedrock); "
                     "para Cohere usá bedrock + BEDROCK_MODEL_ID")
DIMS = int(os.environ.get("EMBEDDING_DIMS", "768"))
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.titan-embed-text-v2:0")
_IS_COHERE = "cohere" in MODEL_ID.lower()
COHERE_MAX_BATCH = 96
MAX_CHARS = 50000
# Public default is local Gemma. Explicit empty profile preserves legacy Ollama.
GEMMA_PROFILE = "title200-window1400-mean-l2-v1"
PROFILE = (os.environ.get("EMBEDDING_PROFILE", GEMMA_PROFILE)
           if BACKEND == "ollama" else "")
GEMMA_DIGEST = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"

def profile_active() -> bool:
    # Bedrock/Cohere never use the local profile, including its public default.
    if BACKEND != "ollama":
        return False
    if PROFILE not in ("", GEMMA_PROFILE):
        raise ValueError("Unsupported EMBEDDING_PROFILE")
    if PROFILE and (BACKEND != "ollama" or DIMS != 768):
        raise ValueError("Gemma profile requires EMBEDDING_BACKEND=ollama and EMBEDDING_DIMS=768")
    return PROFILE == GEMMA_PROFILE

# ----- Bedrock client -----
_bedrock = None
def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        import boto3
        from botocore.config import Config
        # FIX E5 (2026-08-03, tarea 4f110f83): timeouts EXPLICITOS.
        # Antes no se declaraban => botocore usaba su default de 60s para connect
        # Y 60s para read; con max_attempts=4 el peor caso era ~240s + backoff.
        # Medido en produccion: cuelgues de 61.685 / 90.832 / 139.258 ms, con firma
        # de timeout (multiplos de 60s), no una curva de carga.
        # read_timeout=10 es 25x el p95 real del embed (403 ms): amplio margen, no
        # es una guardia mas estricta que lo guardado.
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(
                retries={"max_attempts": int(os.environ.get("BEDROCK_MAX_ATTEMPTS", "3")),
                         "mode": "standard"},
                connect_timeout=int(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "5")),
                read_timeout=int(os.environ.get("BEDROCK_READ_TIMEOUT", "10")),
            ),
        )
    return _bedrock

# ----- Titan -----
def _embed_titan(text: str) -> list[float]:
    body = json.dumps({"inputText": text[:MAX_CHARS], "dimensions": DIMS, "normalize": True})
    resp = _get_bedrock().invoke_model(modelId=MODEL_ID, body=body)
    return json.loads(resp["body"].read())["embedding"]

# ----- Cohere (multilingual-v3 / english-v3 / v4) -----
def _embed_cohere_batch(texts: list[str], input_type: str) -> list[list[float]]:
    body = json.dumps({
        "texts": [t[:2048] for t in texts],
        "input_type": input_type,
        "embedding_types": ["float"],
        "truncate": "END",
    })
    resp = _get_bedrock().invoke_model(modelId=MODEL_ID, body=body)
    data = json.loads(resp["body"].read())
    emb = data["embeddings"]
    if isinstance(emb, dict):
        return emb["float"]
    return emb

# ----- Ollama -----
def _embed_ollama(text: str) -> list[float]:
    url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
    model = os.environ.get("OLLAMA_MODEL", "embeddinggemma")
    r = httpx.post(f"{url}/api/embeddings", json={"model": model, "prompt": text[:MAX_CHARS]}, timeout=60)
    r.raise_for_status()
    return r.json()["embedding"]

# ----- Optional EmbeddingGemma profile (Ariel reindex_local.vector contract) -----
def _embed_gemma(text: str, title: str | None = None) -> list[float]:
    if not isinstance(text, str) or (title is not None and not isinstance(title, str)):
        raise ValueError("Gemma text/title must be strings")
    url = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    # Each vector validates the exact model used by the migration, without cache/alias guessing.
    tags = httpx.get(f"{url}/api/tags", timeout=180)
    tags.raise_for_status()
    models = tags.json().get("models")
    if not isinstance(models, list):
        raise ValueError("Gemma tags response missing models")
    matches = [m for m in models if isinstance(m, dict) and m.get("name") == "embeddinggemma:latest"]
    if len(matches) != 1 or matches[0].get("digest") != GEMMA_DIGEST:
        raise ValueError("Gemma model digest drift/missing/ambiguous")
    prefix = "title: " + (title or "none")[:200] + " | text: "
    sums = [0.0] * 768
    count = 0
    for offset in range(0, max(1, len(text)), 1400 * 8):
        inputs = [prefix + text[j:j + 1400]
                  for j in range(offset, min(max(1, len(text)), offset + 1400 * 8), 1400)]
        response = httpx.post(f"{url}/api/embed", json={"model": "embeddinggemma", "input": inputs,
                             "truncate": False, "options": {"num_thread": 4, "num_ctx": 2048, "num_batch": 2048}}, timeout=180)
        response.raise_for_status()
        vectors = response.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise ValueError("Gemma embedding batch cardinality invalid")
        for vector in vectors:
            if (not isinstance(vector, list) or len(vector) != 768 or
                not all(type(z) in (int, float) and math.isfinite(z) for z in vector)):
                raise ValueError("Gemma vector must contain 768 finite numbers")
            # Match vector() exactly: sum raw window vectors, then normalize once.
            # Arithmetic mean has the same direction, but dividing first changes float rounding.
            for i, z in enumerate(vector):
                sums[i] += z
            count += 1
    norm = math.sqrt(sum(z * z for z in sums))
    if not count or not math.isfinite(norm) or norm <= 0:
        raise ValueError("Gemma pooled vector has zero/nonfinite norm")
    return [z / norm for z in sums]

# ----- Public API -----
def embed(text: str, input_type: str = "search_document", title: str | None = None) -> list[float]:
    """Embed de UN texto. Levanta ValueError si el texto esta vacio.

    FIX E5 (2026-08-03, tarea 4f110f83): antes devolvia un VECTOR CERO en silencio.
    Medido contra el indice vivo: buscar con vector cero devuelve HTTP 200 con 5
    resultados, low_confidence=false y score exactamente 0.0000 — el agente recibe
    datos IRRELEVANTES con apariencia de acierto (falso verde semantico), que es
    peor que un error porque nada lo delata.
    OJO:  NO cambia — el indexer DEPENDE de que un chunk vacio produzca
    vector cero para no desalinear los indices del batch.
    """
    if profile_active():
        return _embed_gemma(text, title)
    if not text or not text.strip():
        raise ValueError("embed(): texto vacio (usar embed_batch para lotes con huecos)")
    if BACKEND == "ollama":
        return _embed_ollama(text)
    if _IS_COHERE:
        return _embed_cohere_batch([text], input_type)[0]
    return _embed_titan(text)

def embed_batch(texts: list[str], input_type: str = "search_document", titles: list[str | None] | None = None) -> list[list[float]]:
    if profile_active():
        if titles is None:
            titles = [None] * len(texts)
        if len(titles) != len(texts):
            raise ValueError("Gemma titles/texts cardinality differs")
        return [_embed_gemma(text, title) for text, title in zip(texts, titles)]
    if not texts:
        return []
    if BACKEND == "bedrock" and _IS_COHERE:
        out: list[list[float]] = []
        for i in range(0, len(texts), COHERE_MAX_BATCH):
            chunk = texts[i:i + COHERE_MAX_BATCH]
            nonempty = [(j, t) for j, t in enumerate(chunk) if t and t.strip()]
            res: list[list[float]] = [[0.0] * DIMS for _ in chunk]
            if nonempty:
                vecs = _embed_cohere_batch([t for _, t in nonempty], input_type)
                for (j, _), v in zip(nonempty, vecs):
                    res[j] = v
            out.extend(res)
        return out
    return [embed(t, input_type) for t in texts]
