"""
sanitize.py — Funciones PURAS de saneamiento para el write-path del memory-gateway.

Objetivo (blast radius alto, ADDITIVE-ONLY):
  - redact_private_blocks: quita bloques <private>...</private> antes de que lleguen a disco/indice.
  - scan_secrets / redact_secrets: detecta y redacta SOLO secretos de ALTA CONFIANZA.
  - sanitize: pipeline (private-blocks -> secret-redaction) usado por add()/add_markdown().

Diseno conservador: patrones de alta confianza para NO romper escrituras legitimas.
Nunca expone el valor completo de un secreto (solo prefijo + longitud).

Stdlib-only (re). Ejecutar `python3 sanitize.py` corre el self-test (PASS/FAIL).
"""
import ast
import re

__all__ = [
    "redact_private_blocks",
    "scan_secrets",
    "redact_secrets",
    "sanitize",
    "repair_surrogates",
    "repair_surrogates_deep",
]

# --------------------------------------------------------------------------
# Private blocks
# --------------------------------------------------------------------------
_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def redact_private_blocks(text):
    """Reemplaza <private>...</private> (DOTALL, non-greedy, case-insensitive)
    por '[REDACTED]'. Devuelve (clean, n) con n = cantidad de bloques redactados."""
    if not text:
        return text, 0
    clean, n = _PRIVATE_RE.subn("[REDACTED]", text)
    return clean, n


# --------------------------------------------------------------------------
# Secretos de ALTA CONFIANZA
# Cada entrada: (kind, regex, value_group)  value_group=0 => redacta match completo.
# --------------------------------------------------------------------------
_PATTERNS = [
    # Bloque PEM de clave privada (RSA/EC/OPENSSH/DSA/... o sin algoritmo)
    ("private_key_block", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL), 0),
    # AWS Access Key ID
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    # GitHub Personal Access Token (classic)
    ("github_pat_classic", re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), 0),
    # GitHub fine-grained PAT
    ("github_pat_fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), 0),
    # Slack token
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    # JWT (tres segmentos base64url separados por punto, prefijo eyJ)
    ("jwt", re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),
    # Asignacion generica key/secret/token/... = <valor largo>. Redacta SOLO grupo 2 (valor).
    ("generic_assignment", re.compile(
        r"(?i)(api[_-]?key|secret|token|password|passwd|x-merlin-key)"
        r"\s*[:=]\s*[\"']?([\w-]{20,}(?:\.[\w-]+)*)"), 2),
]


def _preview(val):
    """Preview seguro: prefijo + '...' + longitud. Nunca el valor completo."""
    val = (val or "").strip()
    n = len(val)
    keep = 4 if n > 6 else 1
    return f"{val[:keep]}…[{n}]"


# Markdown is often not a Python module.  Fallback candidates are deliberately
# small: when a statement crosses either limit it is not exempted (fail closed).
_MAX_FALLBACK_STATEMENT_LINES = 64
_MAX_FALLBACK_STATEMENT_CHARS = 16_384


def _line_offsets(source):
    return [0] + [match.end() for match in re.finditer("\\n", source)]


def _source_index(source, _cost=None):
    """Build line starts and UTF-8 byte-column maps once for one parsed source."""
    lines = source.splitlines(keepends=True)
    starts = []
    byte_columns = []
    offset = 0
    for line in lines:
        starts.append(offset)
        columns = {0: 0}
        byte_offset = 0
        for char_offset, char in enumerate(line, 1):
            byte_offset += len(char.encode("utf-8"))
            columns[byte_offset] = char_offset
        byte_columns.append(columns)
        offset += len(line)
        if _cost is not None:
            _cost["index_characters"] += len(line)
    return starts, byte_columns


def _node_span_in_source(index, node):
    """Return a node's [start, end) source span from a shared source index."""
    starts, byte_columns = index
    start_line = node.lineno - 1
    end_line = node.end_lineno - 1
    start = starts[start_line] + byte_columns[start_line][node.col_offset]
    end = starts[end_line] + byte_columns[end_line][node.end_col_offset]
    return start, end


def _outer_call_spans(source, base_offset=0, _cost=None):
    """Parse *source* once and return exact outer assignment callable spans.

    Only ``Assign`` and ``AnnAssign`` RHS callables qualify.  Keyword values and
    arbitrary nested call contexts therefore cannot create an exception.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError):
        return None
    index = _source_index(source, _cost)
    spans = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        start, end = _node_span_in_source(index, node.value.func)
        spans.add((base_offset + start, base_offset + end))
    return spans


def _fallback_statement_sources(text):
    """Return bounded Python-looking statements from mixed Markdown in one pass.

    The line scanner never searches windows.  It accepts only a physical block
    beginning with an assignment-shaped line and ending at balanced delimiters;
    prose, malformed blocks, and blocks over either documented cap are omitted,
    so their matches remain redacted.
    """
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(text)
    candidates = []
    index = 0
    while index < len(lines):
        line = lines[index]
        # A conservative statement start; full-Python input took the AST path.
        if not re.match(r"^\s*(?:[\w.]+\s*(?::[^=\n]+)?\s*=)", line, re.UNICODE):
            index += 1
            continue
        start = index
        depth = 0
        chars = 0
        while index < len(lines):
            current = lines[index]
            chars += len(current)
            # Quotes are intentionally not interpreted here: a premature stop
            # merely loses an exemption, never leaks a secret.
            depth += current.count("(") + current.count("[") + current.count("{")
            depth -= current.count(")") + current.count("]") + current.count("}")
            index += 1
            if (index - start > _MAX_FALLBACK_STATEMENT_LINES or
                    chars > _MAX_FALLBACK_STATEMENT_CHARS):
                while index < len(lines) and depth > 0:
                    depth += lines[index].count("(") + lines[index].count("[") + lines[index].count("{")
                    depth -= lines[index].count(")") + lines[index].count("]") + lines[index].count("}")
                    index += 1
                break
            if depth <= 0:
                candidate = "".join(lines[start:index])
                # Avoid building/parsing fallback work without a relevant match.
                if _PATTERNS[-1][1].search(candidate):
                    candidates.append((offsets[start], candidate))
                break
    return candidates


def _generic_call_exception_spans(text, _cost=None):
    """Compute exceptions once per sanitize invocation; no payload is retained."""
    full = _outer_call_spans(text, _cost=_cost)
    if full is not None:
        return full
    spans = set()
    # Mixed Markdown: each bounded statement is parsed once, never O(n²) windows.
    for offset, candidate in _fallback_statement_sources(text):
        parsed = _outer_call_spans(candidate, offset, _cost)
        if parsed:
            spans.update(parsed)
    return spans


def _detected_matches(text, _cost=None):
    """Materialize matches once so scan/redaction share one bounded analysis."""
    exceptions = _generic_call_exception_spans(text, _cost)
    matches = []
    for kind, rx, grp in _PATTERNS:
        for match in rx.finditer(text):
            span = (match.start(grp), match.end(grp)) if grp else (match.start(), match.end())
            if kind == "generic_assignment" and span in exceptions:
                continue
            matches.append((kind, span, match.group(grp) if grp else match.group(0)))
    return matches


def _findings(matches, _cost=None):
    findings = [{"kind": kind, "span": [start, end], "preview": _preview(value)}
                for kind, (start, end), value in matches]
    if _cost is not None:
        _cost["finding_items"] = _cost.get("finding_items", 0) + len(findings)
    return findings


def scan_secrets(text, _cost=None):
    """Detect high-confidence secrets without exposing their complete values.

    ``_cost`` is optional deterministic test instrumentation, like the redactor
    hook; production callers neither provide nor retain it.
    """
    return _findings(_detected_matches(text, _cost), _cost) if text else []


def _resolve_priority_spans(matches, _cost=None):
    """Keep non-overlapping spans in detector order in O(n log n).

    ``matches`` retains `_PATTERNS`/``finditer`` order, so this preserves the
    original detector priority exactly. A coordinate-compressed Fenwick tree
    records accepted half-open intervals: querying the candidate's elementary
    segments detects an overlap without pairwise scans. The returned spans are
    then source ordered solely for one-pass output construction.
    """
    if not matches:
        return []
    coordinates = sorted({point for _kind, span, _value in matches for point in span})
    positions = {point: index for index, point in enumerate(coordinates)}
    # Range add / range sum Fenwick trees over elementary [coordinate_i,
    # coordinate_i+1) segments.  Each accepted interval adds one coverage unit.
    size = len(coordinates)
    first = [0] * (size + 2)
    second = [0] * (size + 2)

    def add(tree, index, value):
        while index <= size:
            tree[index] += value
            index += index & -index

    def prefix(tree, index):
        total = 0
        while index:
            total += tree[index]
            index -= index & -index
        return total

    def range_add(left, right, value):
        # Fenwick indices are one-based and right is inclusive.
        add(first, left, value)
        add(first, right + 1, -value)
        add(second, left, value * (left - 1))
        add(second, right + 1, -value * right)

    def range_sum(index):
        return prefix(first, index) * index - prefix(second, index)

    accepted = []
    for _kind, span, _value in matches:
        start, end = span
        left = positions[start] + 1
        right = positions[end]
        if _cost is not None:
            _cost["overlap_comparisons"] += 1
        if range_sum(right) - range_sum(left - 1):
            continue
        range_add(left, right, 1)
        accepted.append(span)
    return sorted(accepted)


def redact_secrets(text, _cost=None):
    """Redact exact detector spans, preserving generic-assignment priority.

    ``_cost`` is test-only deterministic instrumentation; it is never retained
    or exposed by the write path.
    """
    if not text:
        return text, []
    matches = _detected_matches(text, _cost)
    findings = _findings(matches, _cost)
    spans = _resolve_priority_spans(matches, _cost)
    parts = []
    cursor = 0
    for start, end in spans:
        parts.extend((text[cursor:start], "[REDACTED:secret]"))
        cursor = end
    parts.append(text[cursor:])
    clean = "".join(parts)
    if _cost is not None:
        _cost["output_characters"] += len(text) + len(spans) * len("[REDACTED:secret]")
    return clean, findings


def sanitize(text):
    """Pipeline: private-blocks y luego secret-redaction.
    Devuelve (clean, report) con report = {private_redacted:int, secrets:[...]}."""
    clean, n = redact_private_blocks(text)
    clean, findings = redact_secrets(clean)
    report = {"private_redacted": n, "secrets": findings}
    return clean, report


# --------------------------------------------------------------------------
# Unicode surrogate repair for JSON/JSONB write paths
# --------------------------------------------------------------------------
def repair_surrogates(text, replacement="\ufffd"):
    """Replace only unpaired UTF-16 surrogate code points in *text*.

    Valid pairs (for example the source spelling ``\\ud83d\\ude00``) and every
    non-surrogate code point are preserved exactly. Returns ``(clean, n)`` where
    *n* is the number of repaired orphan code points. Non-strings pass through.
    """
    if not isinstance(text, str):
        return text, 0
    clean = []
    repaired = 0
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 < len(text) and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                clean.append(text[index:index + 2])
                index += 2
                continue
            clean.append(replacement)
            repaired += 1
        elif 0xDC00 <= codepoint <= 0xDFFF:
            clean.append(replacement)
            repaired += 1
        else:
            clean.append(text[index])
        index += 1
    return "".join(clean), repaired


def repair_surrogates_deep(obj, replacement="\ufffd"):
    """Pure recursive surrogate repair for a JSON-like object.

    String values, dictionary keys, list items, tuple items, and arbitrary
    nesting are covered; new containers are built. Other scalar types pass
    through unchanged. If distinct dict keys differ only by orphan surrogates,
    repair can map both to U+FFFD and normal last-key-wins dict semantics apply.
    This accepted pathological loss is preferable to losing the whole checkpoint.
    """
    if isinstance(obj, str):
        return repair_surrogates(obj, replacement)
    if isinstance(obj, dict):
        clean = {}
        repaired = 0
        for key, value in obj.items():
            clean_key, key_count = repair_surrogates_deep(key, replacement)
            clean_value, value_count = repair_surrogates_deep(value, replacement)
            clean[clean_key] = clean_value
            repaired += key_count + value_count
        return clean, repaired
    if isinstance(obj, list):
        clean = []
        repaired = 0
        for value in obj:
            clean_value, value_count = repair_surrogates_deep(value, replacement)
            clean.append(clean_value)
            repaired += value_count
        return clean, repaired
    if isinstance(obj, tuple):
        clean = []
        repaired = 0
        for value in obj:
            clean_value, value_count = repair_surrogates_deep(value, replacement)
            clean.append(clean_value)
            repaired += value_count
        return tuple(clean), repaired
    return obj, 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
if __name__ == "__main__":
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")

    # ---- POSITIVOS: cada patron debe detectarse y redactarse ----
    # private blocks
    clean, n = redact_private_blocks("hola <private>SECRETO_AQUI</private> chau")
    check("private_block_redacted", n == 1 and "SECRETO_AQUI" not in clean and "[REDACTED]" in clean)

    clean, n = redact_private_blocks("<PRIVATE>a</PRIVATE> y <private>b\nc</private>")
    check("private_block_multi_ci_dotall", n == 2 and "a" not in clean.replace("[REDACTED]", ""))

    # AWS
    aws = "creds AKIAIOSFODNN7EXAMPLE end"
    f = scan_secrets(aws)
    clean, _ = redact_secrets(aws)
    check("aws_akia", any(x["kind"] == "aws_access_key_id" for x in f) and "AKIAIOSFODNN7EXAMPLE" not in clean)

    # GitHub classic ghp_ + 36
    ghp = "ghp_" + "a" * 36
    f = scan_secrets(f"token {ghp} x")
    clean, _ = redact_secrets(f"token {ghp} x")
    check("github_ghp", any(x["kind"] == "github_pat_classic" for x in f) and ghp not in clean)

    # GitHub fine-grained
    gpat = "github_pat_" + "A1b2C3d4E5" * 3  # 30 chars tras prefijo
    f = scan_secrets(gpat)
    clean, _ = redact_secrets(gpat)
    check("github_pat_fine", any(x["kind"] == "github_pat_fine" for x in f) and gpat not in clean)

    # Slack
    slack = "xoxb-123456789012-abcdEFGH"
    f = scan_secrets(slack)
    clean, _ = redact_secrets(slack)
    check("slack_token", any(x["kind"] == "slack_token" for x in f) and "xoxb-123456789012" not in clean)

    # JWT
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36P"
    f = scan_secrets(jwt)
    clean, _ = redact_secrets(jwt)
    check("jwt", any(x["kind"] == "jwt" for x in f) and jwt not in clean)

    # Generic assignment (redacta SOLO el valor)
    ga = 'api_key = "SUPERSECRETVALUE1234567890"'
    f = scan_secrets(ga)
    clean, _ = redact_secrets(ga)
    check("generic_api_key", any(x["kind"] == "generic_assignment" for x in f)
          and "SUPERSECRETVALUE1234567890" not in clean
          and "api_key" in clean and "[REDACTED:secret]" in clean)

    ga2 = "token: abcdefghij0123456789XYZ"
    clean, f = redact_secrets(ga2)
    check("generic_token", any(x["kind"] == "generic_assignment" for x in f)
          and "abcdefghij0123456789XYZ" not in clean and "token" in clean)

    ga3 = "x-merlin-key=ABCDEFGHIJKLMNOPQRSTUVWX"
    clean, f = redact_secrets(ga3)
    check("generic_x_merlin_key", any(x["kind"] == "generic_assignment" for x in f)
          and "ABCDEFGHIJKLMNOPQRSTUVWX" not in clean)

    # sanitize pipeline combinado
    combo = "nota <private>oculto</private> con AKIAIOSFODNN7EXAMPLE y fin"
    clean, report = sanitize(combo)
    check("sanitize_pipeline", report["private_redacted"] == 1
          and len(report["secrets"]) >= 1
          and "oculto" not in clean and "AKIAIOSFODNN7EXAMPLE" not in clean)

    # preview no expone valor completo
    f = scan_secrets("AKIAIOSFODNN7EXAMPLE")
    check("preview_no_full_value", f and "AKIAIOSFODNN7EXAMPLE" not in f[0]["preview"] and "…" in f[0]["preview"])

    # ---- NEGATIVOS: NO deben matchear ----
    check("neg_normal_text", scan_secrets("Esta es una nota normal sobre el proyecto Cerebro.") == [])

    check("neg_lone_uuid", scan_secrets("id 550e8400-e29b-41d4-a716-446655440000 listo") == [])

    check("neg_category_failure", scan_secrets("category=failure") == [])

    check("neg_short_token", scan_secrets("token: hunter2") == [])

    check("neg_password_prose", scan_secrets("la password es importante para la seguridad") == [])

    check("neg_no_private_block", redact_private_blocks("texto sin bloques privados")[1] == 0)

    # ---- Resumen ----
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("-" * 50)
    print(f"RESULT: {passed}/{total} PASS")
    if passed != total:
        print("SELF-TEST FAILED")
        raise SystemExit(1)
    print("ALL PASS")
