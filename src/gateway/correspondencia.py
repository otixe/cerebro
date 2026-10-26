"""Correspondencia F1: lógica y rutas REST aditivas para el gateway.

Integración futura (no aplicada por este sidecar):
    import correspondencia
    correspondencia.install_routes(app, verify_agent)

La identidad siempre llega de ``verify_agent``. Ningún endpoint acepta remitente.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import errno
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Literal, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, model_validator

import db
import sanitize

# ── RESOLVEDOR DE BUZONES: FUENTE ÚNICA EN common/correspondencia_core.py ─────
# Estas ocho definiciones VIVÍAN acá. Se movieron a `common/correspondencia_core.py`
# porque el aviso automático de asignación de tablero corre en TRES puertas
# (gateway, mcp y el BFF de Sinapsis) y este archivo sólo está horneado en la
# primera. Los bytes del módulo común son los que estaban acá, extraídos por AST por
# `build_common_core.py` -- no una copia escrita a mano.
#
# Se REBINDEAN con los MISMOS nombres, así que los ~40 usos que hay más abajo en
# este archivo (`_registry(cur)`, `normalize_mailbox(cur, x)`, `_insert_deliveries(...)`)
# no se tocan. UNA implementación, cuatro puertas.
import correspondencia_core as _core

ROOT_HEX_LENGTH = _core.ROOT_HEX_LENGTH
ACTIVE_IDENTITY_ALIASES = _core.ACTIVE_IDENTITY_ALIASES
individual_mailbox = _core.individual_mailbox
_agent_rows = _core._agent_rows
_registry = _core._registry
normalize_mailbox = _core.normalize_mailbox
mailbox_normalizer = _core.mailbox_normalizer
_insert_deliveries = _core._insert_deliveries


def _new_root(cur):
    """Delegado a `_core`; traduce el fallo al contrato REST de siempre (503).

    El módulo común no puede lanzar `HTTPException`: se hornea en las imágenes de
    mcp y sinapsis, donde fastapi no está en el grafo de import. Lanza
    `RootAllocationError` y acá se convierte al MISMO 503 con el MISMO detalle, así
    que ningún cliente ve una diferencia.
    """
    try:
        return _core._new_root(cur)
    except _core.RootAllocationError as exc:
        raise HTTPException(503, str(exc))

VALID_PRIORITIES = {"urgente", "medio", "bajo"}
VALID_TYPES = {"inform", "request"}
VALID_READ_VIAS = {"panel", "mcp", "rest"}
BLOB_ROOT = Path(os.environ.get("CORRESPONDENCIA_BLOB_ROOT", "/correspondencia-blobs"))
# Correcciones históricas confirmadas por el recon; difflib no puede inferir ariel->rudeus.
KNOWN_RECIPIENT_CORRECTIONS = {"@ariel": "@rudeus/rudeus", "@zeta": "@alpha/zeta"}

# ── Alias de IDENTIDAD ACTIVOS (transición de nombre; orden de Otixe 2026-09-15) ──
# Mapea alias -> NOMBRE del agente en `agents`, NO un buzón literal. Motivo: el
# buzón se DERIVA (`individual_mailbox`) de team+is_lead, así que si mañana Otixe
# renombra el equipo, el alias sigue a la IDENTIDAD y no a una cadena que quedó vieja.
#
# ⚠️ Lista explícita A PROPÓSITO, y NO se deriva de KNOWN_RECIPIENT_CORRECTIONS:
# ese mapa tiene también "@zeta", que NADIE pidió activar. Derivar de él activaría
# @zeta de arrastre. KNOWN_RECIPIENT_CORRECTIONS sigue siendo SÓLO la sugerencia
# del 422 (comportamiento intacto); esta constante es la que RESUELVE de verdad.
#
# Es un alias de IDENTIDAD, no de equipo: `@ariel/*` NO inventa un equipo nuevo
# (ver `resolve_recipients`, que lo rechaza con 422 y dice por qué).
# (movida a common/correspondencia_core.py -- ver el rebind arriba: ACTIVE_IDENTITY_ALIASES)
_CODE_RE = re.compile(r"^[0-9a-f]{16}(?:\.[1-9][0-9]*)?$")

# Igual que _CODE_RE pero admite un PREFIJO de la raiz (F11): la lectura de UNO acepta
# prefijo como TODAS las tools del tablero. No relaja el alfabeto, solo el largo.
_CODE_PREFIX_RE = re.compile(r"^[0-9a-f]{4,16}(?:\.[1-9][0-9]*)?$")

# Largo del preview del cuerpo en la bandeja (F11). Espeja gateway/conversations.PREVIEW_CHARS
# y mcp_server._BUS_PREVIEW_CHARS: UN numero para las tres ventanas, para que el contrato
# "metadata + preview, cuerpo opt-in" sea el MISMO en el bus y en Correspondencia.
PREVIEW_CHARS = 120


def _content_cols(include_content: bool, alias: str = "m") -> str:
    """Columnas del cuerpo para la bandeja (F11). Mismo contrato que
    gateway/conversations._content_cols: sin `include_content` el cuerpo NO se
    selecciona -- no viaja por la red -- y en su lugar van su LARGO real y un preview de
    PREVIEW_CHARS con los espacios colapsados. El contador + el corte son la senal de que
    falta contenido: asi un preview recortado no se puede confundir con un mensaje corto.
    """
    preview = (f"left(regexp_replace({alias}.contenido, '\\s+', ' ', 'g'), "
               f"{int(PREVIEW_CHARS)}) AS contenido_preview")
    body = f"{alias}.contenido" if include_content else "NULL::text AS contenido"
    return f"{body}, char_length({alias}.contenido) AS contenido_len, {preview}"

# ─────────────────────────────────────────────────────────────────────────────
# Endurecimiento F4: tope de tamaño, tipo por contenido, contenido activo, cuota.
#
# Todo se resuelve por request (no al importar) para que un valor inválido cierre
# SÓLO los adjuntos en vez de tumbar el gateway entero; el resultado sigue siendo
# fail-closed porque un valor ilegible NO cae al default: rechaza la subida.
# ─────────────────────────────────────────────────────────────────────────────
ADJUNTO_MAX_BYTES_DEFAULT = 25 * 1024 * 1024                # 25 MiB por adjunto
ADJUNTO_CUOTA_MENSAJE_BYTES_DEFAULT = 64 * 1024 * 1024      # 64 MiB por mensaje
ADJUNTO_CUOTA_MENSAJE_COUNT_DEFAULT = 20                    # 20 adjuntos por mensaje
ADJUNTO_CUOTA_REMITENTE_BYTES_DEFAULT = 256 * 1024 * 1024   # 256 MiB por remitente
ADJUNTO_CUOTA_REMITENTE_COUNT_DEFAULT = 2000                # 2000 adjuntos por remitente
_SNIFF_BYTES = 4096
_ENV_UINT_RE = re.compile(r"[0-9]{1,19}")


def _env_positive_int(name: str, default: int) -> int:
    """Entero positivo de entorno. Un valor ilegible NO cae al default: cierra."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    if not _ENV_UINT_RE.fullmatch(raw):
        raise HTTPException(500, f"configuración de adjuntos inválida: {name}")
    value = int(raw)
    if value <= 0:
        raise HTTPException(500, f"configuración de adjuntos inválida: {name}")
    return value


def _env_flag(name: str) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise HTTPException(500, f"configuración de adjuntos inválida: {name}")


def attachment_limits() -> dict:
    """Topes efectivos. Se resuelven por request; nunca se memoizan."""
    return {
        "max_bytes": _env_positive_int("CORRESPONDENCIA_ADJUNTO_MAX_BYTES",
                                       ADJUNTO_MAX_BYTES_DEFAULT),
        "mensaje_bytes": _env_positive_int("CORRESPONDENCIA_ADJUNTO_CUOTA_MENSAJE_BYTES",
                                           ADJUNTO_CUOTA_MENSAJE_BYTES_DEFAULT),
        "mensaje_count": _env_positive_int("CORRESPONDENCIA_ADJUNTO_CUOTA_MENSAJE_COUNT",
                                           ADJUNTO_CUOTA_MENSAJE_COUNT_DEFAULT),
        "remitente_bytes": _env_positive_int("CORRESPONDENCIA_ADJUNTO_CUOTA_REMITENTE_BYTES",
                                             ADJUNTO_CUOTA_REMITENTE_BYTES_DEFAULT),
        "remitente_count": _env_positive_int("CORRESPONDENCIA_ADJUNTO_CUOTA_REMITENTE_COUNT",
                                             ADJUNTO_CUOTA_REMITENTE_COUNT_DEFAULT),
        "mime_strict": _env_flag("CORRESPONDENCIA_ADJUNTO_MIME_STRICT"),
    }


# Firmas binarias por prefijo. `activo` marca lo que un navegador o un SO podría
# EJECUTAR; nada de eso se sirve nunca con su tipo original.
_MAGIC_PREFIX: tuple[tuple[bytes, str, bool], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", False),
    (b"\xff\xd8\xff", "image/jpeg", False),
    (b"GIF87a", "image/gif", False),
    (b"GIF89a", "image/gif", False),
    (b"BM", "image/bmp", False),
    (b"II*\x00", "image/tiff", False),
    (b"MM\x00*", "image/tiff", False),
    (b"\x00\x00\x01\x00", "image/vnd.microsoft.icon", False),
    (b"%PDF-", "application/pdf", False),
    (b"PK\x03\x04", "application/zip", False),
    (b"PK\x05\x06", "application/zip", False),
    (b"PK\x07\x08", "application/zip", False),
    (b"\x1f\x8b", "application/gzip", False),
    (b"BZh", "application/x-bzip2", False),
    (b"\xfd7zXZ\x00", "application/x-xz", False),
    (b"\x28\xb5\x2f\xfd", "application/zstd", False),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed", False),
    (b"Rar!\x1a\x07", "application/vnd.rar", False),
    (b"OggS", "application/ogg", False),
    (b"ID3", "audio/mpeg", False),
    (b"fLaC", "audio/flac", False),
    (b"\x1aE\xdf\xa3", "video/x-matroska", False),
    (b"SQLite format 3\x00", "application/vnd.sqlite3", False),
    (b"\x7fELF", "application/x-elf", True),
    (b"MZ", "application/x-dosexec", True),
    (b"\x00asm", "application/wasm", True),
    (b"\xcf\xfa\xed\xfe", "application/x-mach-binary", True),
    (b"\xce\xfa\xed\xfe", "application/x-mach-binary", True),
    (b"\xfe\xed\xfa\xcf", "application/x-mach-binary", True),
    (b"\xfe\xed\xfa\xce", "application/x-mach-binary", True),
    # Colisión conocida: el fat Mach-O y el .class de Java comparten CAFEBABE.
    # Los dos son ejecutables, así que el veredicto `activo` no depende de cuál sea.
    (b"\xca\xfe\xba\xbe", "application/x-mach-binary", True),
    (b"#!", "application/x-executable", True),
)

# Tipos que un navegador ejecuta o interpreta si se los sirve con su tipo real.
ACTIVE_MIME = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "application/xml",
    "text/xml", "text/javascript", "application/javascript",
    "application/x-javascript", "application/ecmascript", "text/ecmascript",
    "application/x-elf", "application/x-dosexec", "application/x-mach-binary",
    "application/x-executable", "application/wasm", "application/x-sh",
    "application/vnd.ms-htmlhelp", "application/x-shockwave-flash",
})

INERT_MIME = "application/octet-stream"

# Tokens que delatan marcado ejecutable en un head textual. Deliberadamente
# sobre-inclusivo: `activo` sólo endurece cómo se SIRVE, nunca rechaza, así que
# un falso positivo no cuesta nada y un falso negativo sí.
_ACTIVE_TEXT_TOKENS = (
    "<script", "<iframe", "<object", "<embed", "<svg", "<html", "<head", "<body",
    "<!doctype html", "<?php", "<%", "javascript:", "srcdoc=", "<foreignobject",
    "<animate", "<handler", "<base ", "<meta http-equiv", "<link ", "<applet",
)
_ACTIVE_TEXT_RE = re.compile(r"on[a-z]{3,20}\s*=")
_XHTML_NS = "http://www.w3.org/1999/xhtml"


def _sniff_text(head: bytes) -> tuple[Optional[str], bool]:
    """Clasifica un head textual. Devuelve (mime|None, activo).

    El head llega cortado en 4 KiB, así que un multibyte partido al final es
    esperable y NO debe degradar la clasificación: se descartan hasta 3 bytes
    de cola antes de declarar que no es texto.
    """
    text = None
    for trim in range(0, 4):
        candidate = head[:len(head) - trim] if trim else head
        try:
            text = candidate.decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, False
    stripped = text.lstrip("\ufeff \t\r\n\v\f")
    low = stripped.lower()
    active = (any(token in low for token in _ACTIVE_TEXT_TOKENS)
              or bool(_ACTIVE_TEXT_RE.search(low)))
    if "<svg" in low:
        return "image/svg+xml", True
    if f'xmlns="{_XHTML_NS}"' in low or f"xmlns='{_XHTML_NS}'" in low:
        return "application/xhtml+xml", True
    if low.startswith(("<!doctype html", "<html", "<head", "<body")):
        return "text/html", True
    if low.startswith(("<script", "<?php", "<%", "<iframe", "<object", "<embed")):
        return "text/html", True
    if low.startswith(("<?xml", "<!doctype")):
        return "application/xml", True
    if low.startswith("<") and re.match(r"<[a-z!/?][a-z0-9:._-]*", low):
        return "text/html", True
    if stripped[:1] in "{[":
        try:
            json.loads(stripped)
            return "application/json", active
        except ValueError:
            pass
    if not stripped:
        return None, False
    return "text/plain", active


def sniff_content_type(head: bytes, total_size: int) -> dict:
    """Determina el tipo REAL por contenido. Fail-closed: 0 bytes no es un tipo.

    Nunca mira el ``tipo_mime`` del cliente ni el nombre del archivo. Un
    ``application/octet-stream`` acá es una CLASIFICACIÓN positiva ("bytes
    opacos", inerte) alcanzada después de leer bytes reales, no un default de
    ignorancia.
    """
    if total_size <= 0 or not head:
        raise HTTPException(422, "adjunto vacío: no hay contenido cuyo tipo determinar")
    for prefix, mime, active in _MAGIC_PREFIX:
        if head.startswith(prefix):
            return {"tipo": mime, "activo": active, "via": "magic-prefijo"}
    if head.startswith(b"RIFF") and len(head) >= 12:
        form = head[8:12]
        if form == b"WEBP":
            return {"tipo": "image/webp", "activo": False, "via": "magic-riff"}
        if form == b"WAVE":
            return {"tipo": "audio/wav", "activo": False, "via": "magic-riff"}
        if form == b"AVI ":
            return {"tipo": "video/x-msvideo", "activo": False, "via": "magic-riff"}
        return {"tipo": INERT_MIME, "activo": False, "via": "magic-riff-desconocido"}
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"avif", b"avis"}:
            return {"tipo": "image/avif", "activo": False, "via": "magic-ftyp"}
        if brand in {b"heic", b"heix", b"hevc", b"mif1", b"msf1"}:
            return {"tipo": "image/heic", "activo": False, "via": "magic-ftyp"}
        return {"tipo": "video/mp4", "activo": False, "via": "magic-ftyp"}
    if len(head) >= 262 and head[257:262] == b"ustar":
        return {"tipo": "application/x-tar", "activo": False, "via": "magic-offset"}
    text_mime, text_active = _sniff_text(head)
    if text_mime is not None:
        return {"tipo": text_mime, "activo": text_active, "via": "texto"}
    return {"tipo": INERT_MIME, "activo": False, "via": "opaco"}


def _generic_declaration(declared: str) -> bool:
    """Un declarado genérico es AUSENCIA de afirmación, no una contradicción."""
    return declared.strip().lower() in {"", INERT_MIME, "application/binary",
                                        "binary/octet-stream", "*/*",
                                        "application/x-www-form-urlencoded",
                                        "multipart/form-data"}


# Grupos donde declarado y detectado nombran lo mismo con distinta etiqueta, o
# donde el contenedor real (zip, elf, texto) es indistinguible del sub-formato.
_MIME_EQUIVALENTS: tuple[frozenset, ...] = (
    frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}),
    frozenset({"image/tiff", "image/x-tiff"}),
    frozenset({"image/vnd.microsoft.icon", "image/x-icon", "image/icon"}),
    frozenset({"application/zip", "application/x-zip-compressed",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "application/vnd.openxmlformats-officedocument.presentationml.presentation",
               "application/vnd.oasis.opendocument.text",
               "application/vnd.oasis.opendocument.spreadsheet",
               "application/epub+zip", "application/java-archive"}),
    frozenset({"application/gzip", "application/x-gzip"}),
    frozenset({"application/xml", "text/xml"}),
    frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
    frozenset({"application/x-elf", "application/x-executable",
               "application/x-sharedlib", "application/x-pie-executable"}),
    frozenset({"application/x-dosexec", "application/x-msdownload",
               "application/vnd.microsoft.portable-executable",
               "application/x-msdos-program"}),
    frozenset({"text/plain", "text/markdown", "text/csv", "text/x-log",
               "application/json", "application/x-ndjson", "text/x-python",
               "text/x-shellscript", "text/tab-separated-values"}),
)


def _mime_mismatch(declared: str, detected: str) -> bool:
    declared = (declared or "").strip().lower()
    detected = (detected or "").strip().lower()
    if _generic_declaration(declared) or declared == detected:
        return False
    for group in _MIME_EQUIVALENTS:
        if declared in group and detected in group:
            return False
    return True


# Sufijos que un SO o navegador puede ejecutar al abrir el archivo descargado.
_ACTIVE_SUFFIXES = frozenset({
    ".html", ".htm", ".xhtml", ".xht", ".shtml", ".svg", ".svgz", ".xml", ".xsl",
    ".xslt", ".js", ".mjs", ".cjs", ".jsx", ".php", ".phtml", ".asp", ".aspx",
    ".jsp", ".cgi", ".pl", ".py", ".rb", ".sh", ".bash", ".zsh", ".ps1", ".psm1",
    ".bat", ".cmd", ".com", ".exe", ".dll", ".scr", ".msi", ".vbs", ".vbe",
    ".wsf", ".wsh", ".hta", ".jar", ".class", ".swf", ".wasm", ".so", ".dylib",
    ".desktop", ".lnk", ".url", ".scf", ".reg", ".elf", ".run", ".appimage",
})


def _active_suffix(name: str) -> bool:
    """¿El NOMBRE promete ejecución? Independiente de la detección por contenido.

    Un `.js` de texto plano no tiene magic bytes ni marcado, así que por contenido
    es indistinguible de un `.txt`: se sirve inerte igual, pero sin esto conservaba
    el nombre ejecutable en el disco de quien lo baja.
    """
    lowered = (name or "").strip().lower()
    return any(lowered.endswith(suffix) for suffix in _ACTIVE_SUFFIXES)


def _neutralize_filename(name: str, active: bool) -> str:
    """El disposition de contenido activo lleva sufijo inerte: no arma la trampa."""
    if not active:
        return name
    if name.lower().endswith(".bin"):
        return name
    return name + ".bin"


def _sniff_open_fd(fd: int) -> dict:
    """Sniffing desde el descriptor YA anclado. Fail-closed: si falla ⇒ activo.

    Lee del MISMO fd que después se streamea, así que lo clasificado y lo servido
    son el mismo inode: no hay ventana para cambiar el archivo entre las dos cosas.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        head = os.read(fd, _SNIFF_BYTES)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        return {"tipo": INERT_MIME, "activo": True, "via": "sniff-fallido"}
    try:
        return sniff_content_type(head, len(head))
    except HTTPException:
        return {"tipo": INERT_MIME, "activo": True, "via": "sniff-indeterminado"}
    except Exception:
        return {"tipo": INERT_MIME, "activo": True, "via": "sniff-error"}


def _declared_content_length(request: Request, limit: int) -> Optional[int]:
    """Content-Length declarado. Ilegible ⇒ 400; excedido ⇒ 413 sin leer un byte."""
    raw = None
    try:
        raw = request.headers.get("content-length")
    except Exception:
        raw = None
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if not _ENV_UINT_RE.fullmatch(raw):
        raise HTTPException(400, "Content-Length ilegible: no se puede determinar el tamaño")
    declared = int(raw)
    if declared > limit:
        raise HTTPException(413, f"adjunto de {declared} bytes supera el tope de {limit} bytes")
    return declared


def _attachment_usage(cur, message_id, sender: str) -> dict:
    """Consumo vigente por mensaje y por remitente, contando bytes por blob ÚNICO.

    Cuenta ``DISTINCT blob_hash`` para que la deduplicación se respete: readjuntar
    el mismo contenido no vuelve a gastar cuota, igual que no vuelve a gastar disco.
    """
    cur.execute(
        "SELECT count(*) AS refs, "
        "COALESCE((SELECT sum(b.tamano) FROM correspondencia_blobs b WHERE b.hash IN ("
        "  SELECT DISTINCT a2.blob_hash FROM correspondencia_adjuntos a2 "
        "  WHERE a2.mensaje_id=%s AND a2.retirado_en IS NULL)),0) AS bytes "
        "FROM correspondencia_adjuntos a WHERE a.mensaje_id=%s AND a.retirado_en IS NULL",
        (message_id, message_id),
    )
    per_message = dict(cur.fetchone() or {"refs": 0, "bytes": 0})
    cur.execute(
        "SELECT count(*) AS refs, "
        "COALESCE((SELECT sum(b.tamano) FROM correspondencia_blobs b WHERE b.hash IN ("
        "  SELECT DISTINCT a2.blob_hash FROM correspondencia_adjuntos a2 "
        "  WHERE lower(a2.subido_por)=lower(%s) AND a2.retirado_en IS NULL)),0) AS bytes "
        "FROM correspondencia_adjuntos a "
        "WHERE lower(a.subido_por)=lower(%s) AND a.retirado_en IS NULL",
        (sender, sender),
    )
    per_sender = dict(cur.fetchone() or {"refs": 0, "bytes": 0})
    return {
        "mensaje_refs": int(per_message.get("refs") or 0),
        "mensaje_bytes": int(per_message.get("bytes") or 0),
        "remitente_refs": int(per_sender.get("refs") or 0),
        "remitente_bytes": int(per_sender.get("bytes") or 0),
    }


def _blob_already_counted(cur, message_id, sender: str, content_hash: str) -> dict:
    """¿Este blob ya está contado? Si sí, el delta de bytes es 0 (dedup)."""
    cur.execute(
        "SELECT "
        "  EXISTS (SELECT 1 FROM correspondencia_adjuntos WHERE mensaje_id=%s "
        "          AND blob_hash=%s AND retirado_en IS NULL) AS en_mensaje, "
        "  EXISTS (SELECT 1 FROM correspondencia_adjuntos WHERE lower(subido_por)=lower(%s) "
        "          AND blob_hash=%s AND retirado_en IS NULL) AS en_remitente",
        (message_id, content_hash, sender, content_hash),
    )
    row = dict(cur.fetchone() or {})
    return {"en_mensaje": bool(row.get("en_mensaje")),
            "en_remitente": bool(row.get("en_remitente"))}


def _enforce_quota(cur, message_id, sender: str, content_hash: str, size: int,
                   limits: dict) -> dict:
    """Cuota por mensaje y por remitente. Excederla RECHAZA; nunca borra nada."""
    usage = _attachment_usage(cur, message_id, sender)
    counted = _blob_already_counted(cur, message_id, sender, content_hash)
    message_delta = 0 if counted["en_mensaje"] else size
    sender_delta = 0 if counted["en_remitente"] else size
    checks = (
        ("adjuntos_por_mensaje", usage["mensaje_refs"] + 1, limits["mensaje_count"], "refs"),
        ("bytes_por_mensaje", usage["mensaje_bytes"] + message_delta, limits["mensaje_bytes"], "bytes"),
        ("adjuntos_por_remitente", usage["remitente_refs"] + 1, limits["remitente_count"], "refs"),
        ("bytes_por_remitente", usage["remitente_bytes"] + sender_delta, limits["remitente_bytes"], "bytes"),
    )
    for name, proposed, cap, unit in checks:
        if proposed > cap:
            raise HTTPException(429, json.dumps({
                "error": "cuota_de_adjuntos_excedida",
                "cuota": name,
                "unidad": unit,
                "en_uso": usage["mensaje_refs"] if name == "adjuntos_por_mensaje"
                          else usage["mensaje_bytes"] if name == "bytes_por_mensaje"
                          else usage["remitente_refs"] if name == "adjuntos_por_remitente"
                          else usage["remitente_bytes"],
                "incremento": 1 if unit == "refs" else (message_delta if "mensaje" in name else sender_delta),
                "resultante": proposed,
                "tope": cap,
                "remitente": sender,
                "dedup_aplicado": counted,
                "nada_se_borro": True,
            }, ensure_ascii=False, sort_keys=True))
    return {"uso_previo": usage, "dedup": counted,
            "delta_mensaje": message_delta, "delta_remitente": sender_delta}


def _reject_if_quota_exhausted(usage: dict, sender: str, limits: dict) -> None:
    """Pre-chequeo antes de recibir bytes: sólo rechaza lo que el autoritativo
    rechazaría IGUAL, cualquiera sea el contenido que venga.

    El hash real se conoce recién al terminar de recibir, así que acá NO se sabe
    si el incremento de bytes va a ser ``size`` o **0** por dedup. Entonces se
    evalúa con el incremento MÍNIMO posible: 0 bytes (el blob puede estar ya
    contado) y 1 ref (una referencia nueva se inserta siempre, con dedup o sin
    él). Con el mínimo, la condición es NECESARIA: si acá rechaza, sobre el mismo
    uso ``_enforce_quota`` rechazaría también. Por eso ``usage == cap`` con delta
    0 --readjuntar el MISMO contenido-- no se rechaza acá: el autoritativo lo
    acepta y un pre-chequeo no puede contradecirlo.

    El tope para contenido NUEVO no se debilita: lo aplica ``_enforce_quota``
    dentro de la transacción con ``proposed > cap``, y los bytes que se reciben
    antes de llegar ahí están acotados por el tope por adjunto
    (``limits["max_bytes"]``, cortado durante el streaming) y se desenlazan en el
    ``finally`` de ``attach``. Lo que este pre-chequeo ahorra es disco, no
    autoridad.
    """
    exhausted = (
        ("adjuntos_por_mensaje", usage["mensaje_refs"], limits["mensaje_count"], "refs",
         usage["mensaje_refs"] + 1 > limits["mensaje_count"]),
        ("bytes_por_mensaje", usage["mensaje_bytes"], limits["mensaje_bytes"], "bytes",
         usage["mensaje_bytes"] > limits["mensaje_bytes"]),
        ("adjuntos_por_remitente", usage["remitente_refs"], limits["remitente_count"], "refs",
         usage["remitente_refs"] + 1 > limits["remitente_count"]),
        ("bytes_por_remitente", usage["remitente_bytes"], limits["remitente_bytes"], "bytes",
         usage["remitente_bytes"] > limits["remitente_bytes"]),
    )
    for name, in_use, cap, unit, over in exhausted:
        if over:
            raise HTTPException(429, json.dumps({
                "error": "cuota_de_adjuntos_excedida",
                "cuota": name, "unidad": unit, "en_uso": in_use, "tope": cap,
                "remitente": sender, "etapa": "pre-recepcion",
                "nada_se_escribio": True, "nada_se_borro": True,
            }, ensure_ascii=False, sort_keys=True))


def root_collision_probability(roots: int) -> float:
    """Aproximación de cumpleaños para un espacio de raíces de 64 bits."""
    space = 16 ** ROOT_HEX_LENGTH
    return 1.0 - math.exp(-(roots * (roots - 1)) / (2.0 * space))


# (movida a common/correspondencia_core.py -- ver el rebind arriba: individual_mailbox)


# (movida a common/correspondencia_core.py -- ver el rebind arriba: _agent_rows)


# (movida a common/correspondencia_core.py -- ver el rebind arriba: _registry)


# (movida a common/correspondencia_core.py -- ver el rebind arriba: normalize_mailbox)


def equivalent_mailboxes(cur, canonical: str) -> list[str]:
    """Formas persistidas equivalentes; permite leer historia sin reescribirla."""
    normalized = normalize_mailbox(cur, canonical)
    _, _, aliases = _registry(cur)
    return sorted({normalized, *(alias for alias, target in aliases.items()
                                 if target == normalized)})


def caller_identity(cur, agent: str) -> tuple[dict, str, bool]:
    cur.execute(
        "SELECT name, team, is_lead, kind, role FROM agents "
        "WHERE active=true AND lower(name)=lower(%s)",
        (agent,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(403, "la identidad autenticada no tiene un buzón activo")
    data = dict(row)
    mailbox = individual_mailbox(data)
    is_human_admin = data.get("kind") == "human" and data.get("role") == "admin"
    return data, mailbox, bool(is_human_admin)


def resolve_recipients(cur, requested: list[str]) -> list[str]:
    individuals, teams, aliases = _registry(cur)
    candidates = sorted(set(individuals) | set(teams) | set(aliases))
    resolved: list[str] = []
    for raw in requested:
        original = (raw or "").strip().lower()
        # @equipo/* se reconoce ANTES del alias @equipo y conserva fan-out.
        if original in teams:
            resolved.extend(teams[original])
            continue
        # Un alias de IDENTIDAD no es un equipo. Sin esta rama el 422 igual salía,
        # pero con el mensaje equivocado ("no tiene equipo", falso: la identidad
        # destino SÍ tiene equipo), y un error que miente cuesta más que uno que falta.
        if original.endswith("/*") and original[:-2] in ACTIVE_IDENTITY_ALIASES:
            target = normalize_mailbox(cur, original[:-2])
            hint = (f"; para el equipo completo usá {target.split('/')[0]}/*"
                    if "/" in target else "")
            raise HTTPException(
                422,
                f"{original} no existe: {original[:-2]} es un alias de IDENTIDAD de "
                f"{target}, no un equipo{hint}")
        value = aliases.get(original, original)
        if value in individuals:
            resolved.append(value)
            continue
        if original.endswith("/*") and normalize_mailbox(cur, original[:-2]) in individuals:
            raise HTTPException(422, f"{original[:-2]} no tiene equipo; el comodín /* no aplica")
        known = KNOWN_RECIPIENT_CORRECTIONS.get(original)
        known = normalize_mailbox(cur, known) if known else None
        suggestion = ([known] if known in individuals else
                      difflib.get_close_matches(original, candidates, n=1, cutoff=0.35))
        detail = f"buzón inexistente: {original!r}"
        if suggestion:
            detail += f"; quizás quisiste decir {suggestion[0]!r}"
        raise HTTPException(422, detail)
    result = sorted(set(resolved))
    if not result:
        raise HTTPException(422, "debe existir al menos un destinatario")
    return result


def _validate_content(value: str, field: str) -> str:
    clean, _repaired = sanitize.repair_surrogates((value or "").strip())
    if not clean:
        raise HTTPException(422, f"{field} no puede estar vacío")
    if "\x00" in clean:
        raise HTTPException(422, f"{field} contiene NUL")
    return clean


# ── VERSION VIEJA (F11, 2026-09-18) — SE CONSERVA MARCADA, NO SE BORRA ───────
# def _validate_refs_task(cur, refs_task: Optional[uuid.UUID]) -> None:
#     if refs_task is None:
#         return
#     cur.execute("SELECT 1 FROM tasks WHERE id=%s", (refs_task,))
#     if cur.fetchone() is None:
#         raise HTTPException(422, "refs_task no existe")
# Por que se reemplaza: el tipo `Optional[uuid.UUID]` del modelo hacia que un PREFIJO de
# 8 hex -- el formato que imprime TODO el tablero -- muriera en pydantic con
# `422 uuid_parsing` ANTES de llegar aca, y el mensaje entero se DESCARTABA (medido dos
# veces el 2026-09-17 escribiendo el mismo mensaje). El bus viejo YA aceptaba prefijo
# (app._resolve_bus_refs_task); Correspondencia era la unica asimetria.
def _validate_refs_task(cur, refs_task) -> Optional[uuid.UUID]:
    """Resuelve UUID completo o prefijo hexadecimal UNICO de tarea; vacio => None.

    DEVUELVE el UUID resuelto: el caller debe persistir el retorno, no `req.refs_task`
    (que ahora puede ser un prefijo). Ambiguo o inexistente => 422 que DICE cual es el
    problema; un `uuid_parsing` generico no le sirve a nadie."""
    if refs_task is None:
        return None
    value = str(refs_task).strip()
    if not value:
        return None
    if len(value) < 8 or not re.fullmatch(r"[0-9a-fA-F-]+", value):
        raise HTTPException(
            422, "refs_task debe ser un UUID de tarea o un prefijo hexadecimal de al "
                 f"menos 8 caracteres; recibi {value!r}")
    cur.execute("SELECT id FROM tasks WHERE id::text=%s OR id::text LIKE %s LIMIT 4",
                (value, value + "%"))
    rows = cur.fetchall()
    if not rows:
        raise HTTPException(422, f"refs_task no existe: {value!r}")
    if len(rows) > 1:
        muestra = ", ".join(str(row["id"])[:12] for row in rows[:3])
        total = f"al menos {len(rows)}" if len(rows) == 4 else str(len(rows))
        raise HTTPException(
            422, f"refs_task ambiguo: el prefijo {value!r} matchea {total} tareas "
                 f"({muestra}); pasa mas caracteres o el UUID completo")
    return rows[0]["id"]


# (movida a common/correspondencia_core.py -- ver el rebind arriba: _new_root)


# (movida a common/correspondencia_core.py -- ver el rebind arriba: _insert_deliveries)


class PublishRequest(BaseModel):
    model_config = {"extra": "forbid"}
    destinatarios: list[str] = Field(..., min_length=1)
    asunto: str = Field(..., min_length=1)
    prioridad: Literal["urgente", "medio", "bajo"] = "medio"
    contenido: str = Field(..., min_length=1)
    tipo: Literal["inform", "request"] = "inform"
    # F11: era `Optional[uuid.UUID]` -> pydantic rechazaba un prefijo de 8 con
    # `422 uuid_parsing` y descartaba el mensaje. Ahora entra como texto y lo resuelve
    # _validate_refs_task (UUID completo o prefijo unico), igual que el tablero.
    refs_task: Optional[str] = None
    ref_vault: Optional[str] = None


class ReplyRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str = Field(..., min_length=1)
    contenido: str = Field(..., min_length=1)
    tipo: Literal["inform", "request"] = "inform"
    prioridad: Optional[Literal["urgente", "medio", "bajo"]] = None
    # F11: idem PublishRequest -- responder tambien descartaba el mensaje con un prefijo.
    refs_task: Optional[str] = None
    ref_vault: Optional[str] = None


class ReadRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str = Field(..., min_length=1)
    via_lectura: Literal["panel", "mcp", "rest"] = "rest"


class OkRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str = Field(..., min_length=1)
    emoji: str = "👍"


class AckRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigos: list[str] = Field(default_factory=list)
    raiz: Optional[str] = None
    emoji: str = "👍"

    @model_validator(mode="after")
    def require_selector(self):
        if not self.codigos and not (self.raiz or "").strip():
            raise ValueError("indica codigos y/o raiz")
        return self


class ReactionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str = Field(..., min_length=1)
    emoji: str = Field(..., min_length=1, max_length=32)
    retirar: bool = False


class ReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    codigo: str = Field(..., min_length=1)
    motivo: str = Field(..., min_length=1, max_length=2000)


def publish(agent: str, req: PublishRequest) -> dict:
    subject = _validate_content(req.asunto, "asunto")
    content = _validate_content(req.contenido, "contenido")
    ref_vault = (req.ref_vault or "").strip() or None
    with db.pg() as conn, conn.cursor() as cur:
        caller_identity(cur, agent)
        recipients = resolve_recipients(cur, req.destinatarios)
        # F11: se PERSISTE el retorno (uuid resuelto), no `req.refs_task`, que ahora
        # puede ser un prefijo. Guardar el prefijo crudo reventaria el INSERT.
        ref_task = _validate_refs_task(cur, req.refs_task)
        message_id, root = _new_root(cur)
        opened = req.tipo == "request"
        cur.execute(
            "INSERT INTO correspondencia_mensajes "
            "(id,codigo,raiz,remitente,asunto,prioridad,contenido,tipo,abierto,refs_task,ref_vault) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (message_id, root, root, agent, subject, req.prioridad, content,
             req.tipo, opened, ref_task, ref_vault),
        )
        _insert_deliveries(cur, message_id, recipients)
        conn.commit()
    return {"id": str(message_id), "codigo": root, "raiz": root,
            "destinatarios": recipients, "entregas": len(recipients),
            "abierto": opened}


def _reply_audiences(cur, roots: set[str], caller_box: str) -> dict[str, list[str]]:
    """Audiencia nueva por raíz, limitada a buzones individuales activos.

    Las entregas históricas sólo aportan candidatos: no se modifican ni pierden su
    autorización histórica. El registro activo se bloquea con ``FOR SHARE``
    hasta el commit de la respuesta para ordenar una revocación concurrente frente
    al snapshot que efectivamente se persiste.
    """
    if not roots:
        return {}
    cur.execute(
        "SELECT m.raiz,e.buzon FROM correspondencia_entregas e "
        "JOIN correspondencia_mensajes m ON m.id=e.mensaje_id "
        "WHERE m.raiz=ANY(%s) GROUP BY m.raiz,e.buzon",
        (list(roots),),
    )
    historical = [dict(row) for row in cur.fetchall()]
    cur.execute(
        "SELECT codigo,remitente FROM correspondencia_mensajes "
        "WHERE codigo=raiz AND raiz=ANY(%s)",
        (list(roots),),
    )
    root_senders = [dict(row) for row in cur.fetchall()]

    # Un único snapshot para normalizar, filtrar y deduplicar la nueva audiencia.
    individuals, _, aliases = _registry(cur, lock=True)
    active_by_name = {
        str(identity["name"]).lower(): mailbox
        for mailbox, identity in individuals.items()
    }
    normalized: dict[str, list[str]] = {root: [] for root in roots}
    for row in historical:
        raw = str(row["buzon"]).strip().lower()
        mailbox = aliases.get(raw, raw)  # normalización antes de filtro/dedupe
        if mailbox in individuals:
            normalized[str(row["raiz"])].append(mailbox)
    for row in root_senders:
        mailbox = active_by_name.get(str(row["remitente"]).lower())
        if mailbox:
            normalized[str(row["codigo"])].append(mailbox)
    return {
        root: sorted({mailbox for mailbox in mailboxes if mailbox != caller_box})
        for root, mailboxes in normalized.items()
    }


def _reply_audience(cur, root: str, caller_box: str) -> list[str]:
    return _reply_audiences(cur, {root}, caller_box).get(root, [])


def reply(agent: str, req: ReplyRequest) -> dict:
    code = (req.codigo or "").strip().lower()
    if not _CODE_RE.fullmatch(code):
        raise HTTPException(422, "codigo inválido")
    content = _validate_content(req.contenido, "contenido")
    ref_vault = (req.ref_vault or "").strip() or None
    with db.pg() as conn, conn.cursor() as cur:
        _, caller_box, _ = caller_identity(cur, agent)
        cur.execute(
            "SELECT id,raiz FROM correspondencia_mensajes WHERE codigo=%s", (code,)
        )
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "mensaje no encontrado")
        root = target["raiz"]
        # Mecanismo atómico: todas las respuestas del hilo bloquean la misma fila raíz.
        cur.execute(
            "SELECT id,remitente,asunto,prioridad,abierto FROM correspondencia_mensajes "
            "WHERE codigo=%s AND raiz=%s FOR UPDATE",
            (root, root),
        )
        root_row = cur.fetchone()
        if not root_row:
            raise HTTPException(409, "hilo sin raíz válida")
        if not root_row["abierto"]:
            raise HTTPException(409, "el hilo está cerrado; requiere solicitar revisión")
        cur.execute(
            "SELECT 1 FROM correspondencia_entregas e "
            "JOIN correspondencia_mensajes m ON m.id=e.mensaje_id "
            "WHERE m.raiz=%s AND e.buzon=ANY(%s) LIMIT 1",
            (root, equivalent_mailboxes(cur, caller_box)),
        )
        participant = cur.fetchone() is not None or str(root_row["remitente"]).lower() == agent.lower()
        if not participant:
            raise HTTPException(403, "no participás de este hilo")
        cur.execute(
            "SELECT COALESCE(MAX(split_part(codigo,'.',2)::bigint),0)+1 AS n "
            "FROM correspondencia_mensajes WHERE raiz=%s AND codigo<>raiz",
            (root,),
        )
        number = int(cur.fetchone()["n"])
        new_code = f"{root}.{number}"
        ref_task = _validate_refs_task(cur, req.refs_task)  # F11: prefijo -> uuid
        # Misma fuente que expone la bandeja para la confirmación de la UI.
        recipients = _reply_audience(cur, root, caller_box)
        if not recipients:
            raise HTTPException(409, "el hilo no tiene destinatarios activos fuera del caller")
        message_id = uuid.uuid4()
        priority = req.prioridad or root_row["prioridad"]
        opened = req.tipo == "request"
        cur.execute(
            "INSERT INTO correspondencia_mensajes "
            "(id,codigo,raiz,remitente,asunto,prioridad,contenido,tipo,abierto,refs_task,ref_vault) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (message_id, new_code, root, agent, root_row["asunto"], priority,
             content, req.tipo, opened, ref_task, ref_vault),
        )
        _insert_deliveries(cur, message_id, recipients)
        # Responder completa explícitamente la entrega referenciada del caller.
        cur.execute(
            "UPDATE correspondencia_entregas SET estado='archivado', "
            "archivado_en=COALESCE(archivado_en,now()) "
            "WHERE mensaje_id=%s AND buzon=ANY(%s) AND estado<>'archivado'",
            (target["id"], equivalent_mailboxes(cur, caller_box)),
        )
        conn.commit()
    return {"id": str(message_id), "codigo": new_code, "raiz": root,
            "asunto": root_row["asunto"], "destinatarios": recipients,
            "entregas": len(recipients), "abierto": opened}


def _feedback_for(cur, messages: list[dict]) -> dict[str, dict]:
    """Reacciones activas, ausentes y apertura de revisión, por UUID de mensaje."""
    message_ids = [row["id"] for row in messages]
    if not message_ids:
        return {}
    # UN snapshot del registro para TODA la función (F10): antes se releía `agents`
    # una vez por reacción y otra por CADA entrega de items+enviados. Va acá y no
    # dentro del loop porque entre `cur.execute` y `fetchall` el cursor tiene un
    # result set pendiente que otra consulta pisaría.
    normalize = mailbox_normalizer(cur)
    cur.execute(
        "SELECT mensaje_id,buzon,emoji,es_ok,creado_en FROM correspondencia_reacciones "
        "WHERE mensaje_id=ANY(%s) AND retirado_en IS NULL ORDER BY creado_en,buzon",
        (message_ids,),
    )
    reactions: dict[str, list[dict]] = {str(mid): [] for mid in message_ids}
    reacted: dict[str, set[str]] = {str(mid): set() for mid in message_ids}
    for row in cur.fetchall():
        key = str(row["mensaje_id"])
        mailbox = normalize(row["buzon"])
        reaction = {"buzon": mailbox, "emoji": row["emoji"],
                    "gesto": "ok" if row["es_ok"] else "expresiva",
                    "creado_en": row["creado_en"]}
        if not any(item["buzon"] == mailbox and item["emoji"] == row["emoji"]
                   and item["gesto"] == reaction["gesto"] for item in reactions[key]):
            reactions[key].append(reaction)
        reacted[key].add(mailbox)
    cur.execute(
        "SELECT mensaje_id,buzon FROM correspondencia_entregas "
        "WHERE mensaje_id=ANY(%s) ORDER BY buzon", (message_ids,)
    )
    deliveries: dict[str, list[str]] = {str(mid): [] for mid in message_ids}
    for row in cur.fetchall():
        key = str(row["mensaje_id"])
        mailbox = normalize(row["buzon"])
        if mailbox not in deliveries[key]:
            deliveries[key].append(mailbox)
    feedback = {
        key: {"reacciones": reactions[key],
              "sin_reaccion": [box for box in deliveries[key] if box not in reacted[key]],
              "revision": None}
        for key in reactions
    }
    by_root: dict[str, list[str]] = {}
    for message in messages:
        by_root.setdefault(str(message["raiz"]), []).append(str(message["id"]))
    cur.execute(
        "SELECT agent,query,ts FROM audit_log "
        "WHERE action='correspondencia_revision_abierta' ORDER BY ts DESC"
    )
    for row in cur.fetchall():
        try:
            record = json.loads(row["query"] or "{}")
            root = str(record.get("codigo") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for key in by_root.get(root, []):
            if feedback[key]["revision"] is None:
                feedback[key]["revision"] = {"abierto_por": row["agent"],
                                               "motivo": record.get("motivo"),
                                               "abierto_en": row["ts"]}
    return feedback


def _enrich(cur, rows: list[dict], own_box: str, received_ids: set[str]) -> None:
    """Decora mensajes con reacciones/adjuntos/escalado/audiencia/acciones y la senal de
    truncado del preview.

    EXTRAIDO de inbox() en F11 (mismas lineas, mismo orden, un solo batch por consulta)
    para que la lectura de UNO no pueda DIVERGIR del render de la bandeja: con dos copias
    de este loop, un campo agregado en una ventana falta en la otra y el defecto es
    invisible hasta que alguien lo busca."""
    if not rows:
        return
    feedback = _feedback_for(cur, rows)
    ids = [row["id"] for row in rows]
    attachments = _attachment_rows(cur, ids)
    escalation = _escalation_for(cur, ids)
    audience_by_root = _reply_audiences(cur, {str(row["raiz"]) for row in rows}, own_box)
    for item in rows:
        item.update(feedback.get(str(item["id"]),
                                 {"reacciones": [], "sin_reaccion": [], "revision": None}))
        item["adjuntos"] = attachments.get(str(item["id"]), [])
        # Campo aditivo (F5): el emisor ve que su urgente no fue atendido sin
        # que exista un mensaje nuevo. Lista vacia = sin escalado.
        item["escalado"] = escalation.get(str(item["id"]), [])
        # Campo aditivo: la UI lo muestra tal cual; no replica esta regla.
        item["audiencia_respuesta"] = audience_by_root.get(str(item["raiz"]), [])
        # F11: senal EXPLICITA de que el preview esta recortado. Sin el contador, un
        # cuerpo cortado es indistinguible de un mensaje corto.
        item["contenido_truncado"] = int(item.get("contenido_len") or 0) > PREVIEW_CHARS
        has_delivery = str(item["id"]) in received_ids
        item["acciones"] = {
            "ok": {"etiqueta": "OK (procesado; archiva)", "archiva": True,
                   "disponible": has_delivery},
            # ETIQUETAS TEXTUALES DEL ORIGINAL, CON TILDES: las muestra el panel. Un
            # "Reaccion" sin tilde es una regresion visible para Otixe, no un detalle.
            "reaccion_expresiva": {
                "etiqueta": "Reacción expresiva (no archiva)", "archiva": False,
                "disponible": has_delivery, "permite_retirar": True},
            "solicitar_revision": {
                "etiqueta": "Solicitar revisión", "requiere_motivo": True,
                "disponible": item["tipo"] == "inform" and not item["abierto"]},
        }


def inbox(agent: str, requested_mailbox: Optional[str], archive: bool,
          include_content: bool = True) -> dict:
    """Bandeja del caller.

    `include_content` (F11) DEFAULT True A PROPOSITO: el panel de Sinapsis
    (CorrespondenciaView.tsx:924/1029) renderiza `message.contenido` directo de esta
    respuesta, asi que voltear el default aca dejaria los cuerpos en blanco en la UI de
    Otixe. El opt-OUT (`include_content=false`) lo usa el consumidor que SI se rompia por
    el tamano: la tool MCP, cuya salida se TRUNCABA (medido 2026-09-18: 719.538 chars).
    El flip del default REST queda para una medicion aparte, con la UI adaptada."""
    with db.pg() as conn, conn.cursor() as cur:
        _, own_box, is_admin = caller_identity(cur, agent)
        raw_target = (requested_mailbox or "").strip().lower() or None
        target = normalize_mailbox(cur, raw_target) if raw_target else None
        if target and not is_admin and target != own_box:
            raise HTTPException(403, "no podés leer el buzón de otra identidad")
        if target:
            individuals, _, _ = _registry(cur)
            if target not in individuals:
                raise HTTPException(422, "buzón individual inexistente")
        clauses = ["e.estado='archivado'" if archive else "e.estado<>'archivado'"]
        params: list[object] = []
        effective = target if is_admin else own_box
        if effective:
            clauses.append("e.buzon=ANY(%s)")
            params.append(equivalent_mailboxes(cur, effective))
        # UN snapshot antes de la consulta de items (F10): normalizar por fila hacía
        # un SELECT a `agents` por entrega. Antes del execute a propósito.
        normalize = mailbox_normalizer(cur)
        cur.execute(
            "SELECT m.id,m.codigo,m.raiz,m.remitente,m.asunto,m.prioridad,"
            + _content_cols(include_content) + ","
            "m.tipo,m.abierto,m.refs_task,m.ref_vault,m.creado_en,e.buzon,e.estado,"
            "e.leido_en,e.archivado_en,e.via_lectura "
            "FROM correspondencia_entregas e JOIN correspondencia_mensajes m "
            "ON m.id=e.mensaje_id WHERE " + " AND ".join(clauses) +
            " ORDER BY m.creado_en DESC,m.codigo DESC",
            tuple(params),
        )
        items_by_id: dict[str, dict] = {}
        for row in cur.fetchall():
            item = dict(row)
            item["buzon"] = normalize(item["buzon"])
            items_by_id.setdefault(str(item["id"]), item)
        items = list(items_by_id.values())
        cur.execute(
            "SELECT id,codigo,raiz,remitente,asunto,prioridad,"
            + _content_cols(include_content, "correspondencia_mensajes") + ","
            "tipo,abierto,"
            "refs_task,ref_vault,creado_en FROM correspondencia_mensajes "
            "WHERE lower(remitente)=lower(%s) ORDER BY creado_en DESC,codigo DESC",
            (agent,),
        )
        sent = [dict(row) for row in cur.fetchall()]
        received_ids = {str(item["id"]) for item in items}
        # F11: el loop de decorado vive en _enrich (mismas lineas, extraidas) para que la
        # lectura de UNO use EXACTAMENTE este render. Ver comentario en _enrich.
        _enrich(cur, items + sent, own_box, received_ids)
    return {"buzon": effective or "*", "vista": "archivo" if archive else "bandeja",
            "count": len(items), "items": items,
            "enviados_count": len(sent), "enviados": sent,
            # F11: espeja `content_included` de /bus/thread. Un cliente puede SABER si
            # lo que recibio tiene cuerpos sin adivinar por el contenido.
            "contenido_incluido": bool(include_content),
            "preview_chars": PREVIEW_CHARS}


def _resolve_code(cur, raw: str) -> str:
    """Codigo exacto, o PREFIJO unico. Exacto SIEMPRE gana: la raiz `<16hex>` es prefijo
    de sus propias respuestas `<16hex>.n`, asi que sin esta precedencia pedir la raiz de
    un hilo con respuestas seria eternamente 'ambiguo'."""
    cur.execute("SELECT codigo FROM correspondencia_mensajes WHERE codigo=%s", (raw,))
    row = cur.fetchone()
    if row:
        return row["codigo"]
    cur.execute("SELECT codigo FROM correspondencia_mensajes WHERE codigo LIKE %s "
                "ORDER BY codigo LIMIT 4", (raw + "%",))
    matches = [r["codigo"] for r in cur.fetchall()]
    if not matches:
        raise HTTPException(404, f"no existe mensaje con codigo {raw!r}")
    if len(matches) > 1:
        total = f"al menos {len(matches)}" if len(matches) == 4 else str(len(matches))
        raise HTTPException(
            422, f"codigo ambiguo: el prefijo {raw!r} matchea {total} mensajes "
                 f"({', '.join(matches[:3])}); pasa el codigo completo")
    return matches[0]


def mark_read(agent: str, req: ReadRequest) -> dict:
    """LECTURA DE UNO: devuelve UN mensaje COMPLETO por su codigo (acepta prefijo unico)
    y marca leida la entrega del caller si existe.

    ANTES (F1..F10) esta funcion SOLO marcaba leido: devolvia
    {codigo,estado,leido_en,archivado_en} y NINGUN cuerpo, asi que la unica puerta al
    CONTENIDO era `/correspondencia/bandeja`, que trae TODO lo no archivado. Medido
    2026-09-18 en la bandeja de saruman (7 recibidos + 204 enviados): 902.957 bytes por
    REST y 719.538 chars de render MCP, con la respuesta MCP **truncada** -- o sea el
    mensaje buscado podia quedar FUERA del corte y el instrumento fallaba en su funcion,
    no solo era caro.
    Ahora Correspondencia tiene el MISMO contrato que /bus/thread: metadata + preview por
    default y cuerpo por pedido explicito, con la lectura registrada en audit_log.

    Las 4 claves viejas (codigo/estado/leido_en/archivado_en) SE CONSERVAN en el nivel
    superior porque el panel las tipa (web/src/lib/api.ts:1971); todo lo nuevo es
    ADITIVO. `mensaje` trae el item con el MISMO shape que la bandeja (via _enrich).

    AUTORIZACION: entrega propia (cualquier buzon equivalente), o ser el REMITENTE, o
    admin. La rama del remitente es nueva y NO amplia lo visible: un mensaje que mandaste
    vos ya aparecia con cuerpo en tu propia bandeja (`enviados`); sin esta rama, volcar la
    bandeja a preview dejaba TUS enviados sin ninguna puerta al cuerpo."""
    raw = (req.codigo or "").strip().lower()
    if not _CODE_PREFIX_RE.fullmatch(raw):
        raise HTTPException(422, "codigo invalido: hex de la raiz (4..16) con .n opcional")
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, is_admin = caller_identity(cur, agent)
        code = _resolve_code(cur, raw)
        equivalent = equivalent_mailboxes(cur, mailbox)
        # ── LINEA VIEJA (se conserva): el UPDATE es el MISMO, cambia que ya no es el
        # unico camino ni la unica salida. Antes: `if not row: 404`.
        cur.execute(
            "UPDATE correspondencia_entregas e SET "
            "leido_en=COALESCE(e.leido_en,now()), via_lectura=COALESCE(e.via_lectura,%s), "
            "estado=CASE WHEN e.estado='pendiente' THEN 'leido' ELSE e.estado END "
            "FROM correspondencia_mensajes m WHERE e.mensaje_id=m.id "
            "AND m.codigo=%s AND e.buzon=ANY(%s) RETURNING e.estado,e.leido_en,e.archivado_en",
            (req.via_lectura, code, equivalent),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT id,codigo,raiz,remitente,asunto,prioridad,"
            + _content_cols(True, "correspondencia_mensajes") + ","
            "tipo,abierto,refs_task,ref_vault,creado_en "
            "FROM correspondencia_mensajes WHERE codigo=%s", (code,))
        message = cur.fetchone()
        if not message:
            raise HTTPException(404, f"no existe mensaje con codigo {code!r}")
        message = dict(message)
        es_remitente = str(message["remitente"]).lower() == agent.lower()
        if not row and not es_remitente and not is_admin:
            raise HTTPException(403, "no tenes entrega de este mensaje ni lo enviaste vos")
        cur.execute(
            "SELECT buzon,estado,leido_en,archivado_en,via_lectura FROM correspondencia_entregas "
            "WHERE mensaje_id=%s AND buzon=ANY(%s) ORDER BY buzon", (message["id"], equivalent))
        propias = [dict(r) for r in cur.fetchall()]
        normalize = mailbox_normalizer(cur)
        for entrega in propias:
            entrega["buzon"] = normalize(entrega["buzon"])
        if propias:
            message["buzon"] = propias[0]["buzon"]
            message["estado"] = propias[0]["estado"]
            message["leido_en"] = propias[0]["leido_en"]
            message["archivado_en"] = propias[0]["archivado_en"]
            message["via_lectura"] = propias[0]["via_lectura"]
        else:  # sos el remitente (o admin): no hay entrega TUYA que marcar
            message["buzon"] = None
            message["estado"] = None
            message["leido_en"] = None
            message["archivado_en"] = None
            message["via_lectura"] = None
        _enrich(cur, [message], mailbox, {str(message["id"])} if propias else set())
        conn.commit()
    return {"codigo": code, "estado": (row or message).get("estado"),
            "leido_en": (row or message).get("leido_en"),
            "archivado_en": (row or message).get("archivado_en"),
            # ── TODO LO DE ABAJO ES ADITIVO (F11) ──────────────────────────────────
            "solicitado": raw,
            "marcado_leido": bool(row),
            "es_remitente": es_remitente,
            "contenido_incluido": True,
            "contenido_chars": int(message.get("contenido_len") or 0),
            "entregas_propias": propias,
            "mensaje": message}


def _ack_rows(cur, mailbox: str, codes: list[str], root: Optional[str], emoji: str) -> list[str]:
    mailbox = normalize_mailbox(cur, mailbox)
    equivalent = equivalent_mailboxes(cur, mailbox)
    selectors: list[str] = []
    params: list[object] = [equivalent]
    clean_codes = sorted({c.strip().lower() for c in codes if c.strip()})
    if clean_codes:
        selectors.append("m.codigo=ANY(%s)")
        params.append(clean_codes)
    clean_root = (root or "").strip().lower()
    if clean_root:
        selectors.append("m.raiz=%s")
        params.append(clean_root)
    cur.execute(
        "SELECT e.id,e.mensaje_id,m.codigo FROM correspondencia_entregas e "
        "JOIN correspondencia_mensajes m ON m.id=e.mensaje_id "
        "WHERE e.buzon=ANY(%s) AND e.estado<>'archivado' AND (" +
        " OR ".join(selectors) + ") FOR UPDATE OF e",
        tuple(params),
    )
    rows = list(cur.fetchall())
    unique = {str(row["mensaje_id"]): row for row in rows}
    for row in unique.values():
        cur.execute(
            "SELECT 1 FROM correspondencia_reacciones WHERE mensaje_id=%s "
            "AND buzon=ANY(%s) AND es_ok=true AND retirado_en IS NULL LIMIT 1",
            (row["mensaje_id"], equivalent),
        )
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO correspondencia_reacciones "
                "(id,mensaje_id,buzon,emoji,es_ok) VALUES (%s,%s,%s,%s,true)",
                (uuid.uuid4(), row["mensaje_id"], mailbox, emoji),
            )
        cur.execute(
            "UPDATE correspondencia_entregas SET estado='archivado', "
            "archivado_en=COALESCE(archivado_en,now()) "
            "WHERE mensaje_id=%s AND buzon=ANY(%s)",
            (row["mensaje_id"], equivalent),
        )
    return [row["codigo"] for row in unique.values()]


def acknowledge(agent: str, req: AckRequest) -> dict:
    emoji = _validate_content(req.emoji, "emoji")
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, _ = caller_identity(cur, agent)
        archived = _ack_rows(cur, mailbox, req.codigos, req.raiz, emoji)
        conn.commit()
    return {"archivados": len(archived), "codigos": archived}


def acknowledge_one(agent: str, req: OkRequest) -> dict:
    batch = AckRequest(codigos=[req.codigo], emoji=req.emoji)
    result = acknowledge(agent, batch)
    if result["archivados"] == 0:
        # Idempotencia: ya archivado sigue siendo éxito sólo si la entrega existe.
        with db.pg() as conn, conn.cursor() as cur:
            _, mailbox, _ = caller_identity(cur, agent)
            cur.execute(
                "SELECT 1 FROM correspondencia_entregas e JOIN correspondencia_mensajes m "
                "ON m.id=e.mensaje_id WHERE m.codigo=%s AND e.buzon=ANY(%s)",
                ((req.codigo or "").strip().lower(), equivalent_mailboxes(cur, mailbox)),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "entrega no encontrada en tu buzón")
    return result


def react(agent: str, req: ReactionRequest) -> dict:
    """Reacción expresiva: nunca archiva y retirar preserva la fila histórica."""
    code = (req.codigo or "").strip().lower()
    if not _CODE_RE.fullmatch(code):
        raise HTTPException(422, "codigo inválido")
    emoji = _validate_content(req.emoji, "emoji")
    if len(emoji) > 32:
        raise HTTPException(422, "emoji supera 32 caracteres")
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, _ = caller_identity(cur, agent)
        cur.execute(
            "SELECT m.id,e.estado,e.archivado_en FROM correspondencia_mensajes m "
            "JOIN correspondencia_entregas e ON e.mensaje_id=m.id "
            "WHERE m.codigo=%s AND e.buzon=ANY(%s) FOR UPDATE OF m",
            (code, equivalent_mailboxes(cur, mailbox)),
        )
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "entrega no encontrada en tu buzón")
        # Sin índice nuevo/DDL: un advisory lock por triple serializa dos reacciones
        # concurrentes y vuelve idempotente el SELECT+INSERT.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"{target['id']}|{mailbox}|{emoji}",),
        )
        cur.execute(
            "SELECT id FROM correspondencia_reacciones WHERE mensaje_id=%s AND buzon=ANY(%s) "
            "AND emoji=%s AND es_ok=false AND retirado_en IS NULL ORDER BY creado_en LIMIT 1",
            (target["id"], equivalent_mailboxes(cur, mailbox), emoji),
        )
        active = cur.fetchone()
        created = retired = 0
        if req.retirar:
            if active:
                cur.execute(
                    "UPDATE correspondencia_reacciones SET retirado_en=now() "
                    "WHERE id=%s AND retirado_en IS NULL", (active["id"],)
                )
                retired = cur.rowcount
        elif not active:
            cur.execute(
                "INSERT INTO correspondencia_reacciones "
                "(id,mensaje_id,buzon,emoji,es_ok) VALUES (%s,%s,%s,%s,false)",
                (uuid.uuid4(), target["id"], mailbox, emoji),
            )
            created = 1
        conn.commit()
    return {"codigo": code, "gesto": "reaccion_expresiva", "emoji": emoji,
            "archiva": False, "creadas": created, "retiradas": retired,
            "estado_entrega": target["estado"], "archivado_en": target["archivado_en"]}


def request_review(agent: str, req: ReviewRequest) -> dict:
    """Abre un inform cerrado y registra autor+motivo en audit_log, sin DDL."""
    code = (req.codigo or "").strip().lower()
    if not _CODE_RE.fullmatch(code):
        raise HTTPException(422, "codigo inválido")
    reason = _validate_content(req.motivo, "motivo")
    with db.pg() as conn, conn.cursor() as cur:
        _, caller_box, _ = caller_identity(cur, agent)
        cur.execute(
            "SELECT id,raiz FROM correspondencia_mensajes WHERE codigo=%s", (code,)
        )
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "mensaje no encontrado")
        cur.execute(
            "SELECT id,remitente,tipo,abierto FROM correspondencia_mensajes "
            "WHERE codigo=%s AND raiz=%s FOR UPDATE", (target["raiz"], target["raiz"])
        )
        root = cur.fetchone()
        if not root:
            raise HTTPException(409, "hilo sin raíz válida")
        cur.execute(
            "SELECT 1 FROM correspondencia_entregas WHERE mensaje_id=%s AND buzon=ANY(%s)",
            (root["id"], equivalent_mailboxes(cur, caller_box)),
        )
        participant = cur.fetchone() is not None or str(root["remitente"]).lower() == agent.lower()
        if not participant:
            raise HTTPException(403, "no participás de este hilo")
        if root["tipo"] != "inform":
            raise HTTPException(409, "solicitar revisión sólo aplica a un inform")
        if root["abierto"]:
            raise HTTPException(409, "el inform ya está abierto para revisión")
        cur.execute("UPDATE correspondencia_mensajes SET abierto=true WHERE id=%s", (root["id"],))
        record = json.dumps({"codigo": target["raiz"], "motivo": reason},
                            ensure_ascii=False, sort_keys=True)
        cur.execute(
            "INSERT INTO audit_log (agent,action,query,success) VALUES "
            "(%s,'correspondencia_revision_abierta',%s,true)",
            (agent, record),
        )
        conn.commit()
    return {"codigo": code, "raiz": target["raiz"], "abierto": True,
            "abierto_por": agent, "motivo": reason}




def _thread_participant(cur, root: str, agent: str, mailbox: str) -> bool:
    """Sólo una identidad actualmente activa puede usar su participación histórica."""
    cur.execute(
        "SELECT 1 FROM correspondencia_mensajes m "
        "LEFT JOIN correspondencia_entregas e ON e.mensaje_id=m.id "
        "WHERE m.raiz=%s AND (lower(m.remitente)=lower(%s) OR e.buzon=ANY(%s)) LIMIT 1",
        (root, agent, equivalent_mailboxes(cur, mailbox)),
    )
    return cur.fetchone() is not None


def _attachment_target(cur, code: str) -> dict:
    cur.execute("SELECT id,codigo,raiz FROM correspondencia_mensajes WHERE codigo=%s", (code,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "mensaje no encontrado")
    return dict(row)


def _validated_blob_hash(content_hash: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash or ""):
        raise HTTPException(500, "hash de adjunto inválido")
    return content_hash


def blob_path(content_hash: str, root: Optional[Path] = None) -> Path:
    """Ruta diagnóstica solamente; el I/O de blobs nunca abre esta ruta."""
    content_hash = _validated_blob_hash(content_hash)
    root = root or BLOB_ROOT
    return root / content_hash[:2] / content_hash[2:4] / content_hash


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_root_fd(root: Optional[Path] = None) -> int:
    try:
        fd = os.open(os.fspath(root or BLOB_ROOT), _DIR_FLAGS)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "blob root no es directorio")
        return fd
    except OSError as exc:
        raise HTTPException(503, "blob root no disponible") from exc


def _open_dir_at(parent_fd: int, name: str, *, create: bool) -> int:
    if not re.fullmatch(r"(?:[0-9a-f]{2}|\.incoming)", name):
        raise HTTPException(503, "componente de blob inválido")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise HTTPException(503, "directorio de blob no disponible") from exc
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTDIR, "componente no es directorio")
        return fd
    except OSError as exc:
        raise HTTPException(503, "directorio de blob no disponible") from exc


def _regular_fd(fd: int, expected_size: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise HTTPException(503, "blob deduplicado inconsistente")
    return info


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_blob_fd_at(root_fd: int, content_hash: str, expected_size: int) -> tuple[int, os.stat_result]:
    content_hash = _validated_blob_hash(content_hash)
    first = second = fd = -1
    try:
        first = _open_dir_at(root_fd, content_hash[:2], create=False)
        second = _open_dir_at(first, content_hash[2:4], create=False)
        try:
            fd = os.open(content_hash, _FILE_FLAGS, dir_fd=second)
        except OSError as exc:
            raise HTTPException(503, "blob no disponible") from exc
        info = _regular_fd(fd, expected_size)
        if _digest_fd(fd) != content_hash:
            raise HTTPException(503, "blob deduplicado inconsistente")
        result, fd = fd, -1
        return result, info
    finally:
        if fd >= 0:
            _close_fd(fd)
        if second >= 0:
            _close_fd(second)
        if first >= 0:
            _close_fd(first)


class _IncomingBlob:
    def __init__(self, root_fd: int, incoming_fd: int, temp_fd: int, temp_name: str,
                 content_hash: str, size: int, head: bytes = b"", limit: int = 0):
        self.root_fd = root_fd
        self.incoming_fd = incoming_fd
        self.temp_fd = temp_fd
        self.temp_name = temp_name
        self.content_hash = content_hash
        self.size = size
        self.head = head
        self.limit = limit
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.unlink(self.temp_name, dir_fd=self.incoming_fd)
            os.fsync(self.incoming_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        _close_fd(self.temp_fd)
        _close_fd(self.incoming_fd)
        _close_fd(self.root_fd)


async def _receive_blob(request: Request, root: Optional[Path] = None,
                        limit: Optional[int] = None) -> _IncomingBlob:
    """Recibe bytes en un inode exclusivo bajo descriptores confinados.

    El tope se aplica DURANTE el streaming: el chunk que lo excede no se escribe,
    así que el archivo completo nunca se materializa. El temporal se desenlaza en
    el mismo ``except BaseException`` que ya cubría cancelaciones y errores, así
    que un 413 no deja huérfanos.
    """
    if limit is None:
        limit = attachment_limits()["max_bytes"]
    declared = _declared_content_length(request, limit)
    root_fd = incoming_fd = temp_fd = -1
    temp_name = f"{uuid.uuid4().hex}.part"
    try:
        root_fd = _open_root_fd(root)
        incoming_fd = _open_dir_at(root_fd, ".incoming", create=True)
        temp_fd = os.open(
            temp_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=incoming_fd,
        )
        digest = hashlib.sha256()
        size = 0
        head = bytearray()
        async for chunk in request.stream():
            if not chunk:
                continue
            if size + len(chunk) > limit:
                raise HTTPException(
                    413,
                    f"adjunto supera el tope de {limit} bytes "
                    f"(cortado en {size + len(chunk)} sin materializar el archivo)",
                )
            if len(head) < _SNIFF_BYTES:
                head += chunk[: _SNIFF_BYTES - len(head)]
            view = memoryview(chunk)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError(errno.EIO, "escritura incompleta")
                view = view[written:]
            digest.update(chunk)
            size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"adjunto supera el tope de {limit} bytes")
        if size <= 0:
            raise HTTPException(422, "adjunto vacío: cero bytes recibidos")
        if declared is not None and size > declared:
            raise HTTPException(400, "se recibieron más bytes que los declarados en Content-Length")
        os.fsync(temp_fd)
        info = _regular_fd(temp_fd, size)
        if info.st_nlink != 1:
            raise HTTPException(503, "temporal de blob inconsistente")
        result = _IncomingBlob(root_fd, incoming_fd, temp_fd, temp_name, digest.hexdigest(), size,
                               head=bytes(head), limit=limit)
        root_fd = incoming_fd = temp_fd = -1
        return result
    except BaseException:
        if incoming_fd >= 0:
            try:
                os.unlink(temp_name, dir_fd=incoming_fd)
                os.fsync(incoming_fd)
            except OSError:
                pass
        raise
    finally:
        if temp_fd >= 0:
            _close_fd(temp_fd)
        if incoming_fd >= 0:
            _close_fd(incoming_fd)
        if root_fd >= 0:
            _close_fd(root_fd)


def _verify_existing_blob_at(root_fd: int, content_hash: str, expected_size: int) -> None:
    fd = -1
    try:
        fd, _ = _open_blob_fd_at(root_fd, content_hash, expected_size)
    finally:
        if fd >= 0:
            _close_fd(fd)


def _publish_blob_once(incoming: _IncomingBlob) -> bool:
    """Publica con linkat sin reemplazo; un EEXIST sólo acepta el blob coherente."""
    content_hash = _validated_blob_hash(incoming.content_hash)
    first = second = -1
    try:
        source = os.stat(incoming.temp_name, dir_fd=incoming.incoming_fd, follow_symlinks=False)
        opened = os.fstat(incoming.temp_fd)
        if (not stat.S_ISREG(source.st_mode) or
                (source.st_dev, source.st_ino) != (opened.st_dev, opened.st_ino) or
                source.st_size != incoming.size):
            raise HTTPException(503, "temporal de blob inconsistente")
        first = _open_dir_at(incoming.root_fd, content_hash[:2], create=True)
        second = _open_dir_at(first, content_hash[2:4], create=True)
        created = False
        try:
            os.link(
                incoming.temp_name, content_hash,
                src_dir_fd=incoming.incoming_fd, dst_dir_fd=second,
                follow_symlinks=False,
            )
            created = True
            os.fsync(second)
        except FileExistsError:
            pass
        except OSError as exc:
            raise HTTPException(503, "no se pudo publicar blob") from exc
        _verify_existing_blob_at(incoming.root_fd, content_hash, incoming.size)
        return created
    finally:
        if second >= 0:
            _close_fd(second)
        if first >= 0:
            _close_fd(first)


def _safe_header_name(name: str) -> str:
    clean = "".join(ch for ch in name if ch not in "\r\n\x00" and ord(ch) >= 32).strip()
    return clean or "adjunto"


class _FDStream:
    def __init__(self, fd: int):
        self.fd = fd

    async def __aiter__(self):
        try:
            while True:
                chunk = os.read(self.fd, 1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            _close_fd(fd)

    def __del__(self):
        self.close()


def _blob_stream_response(content_hash: str, expected_size: int, name: str):
    """Sirve SIEMPRE inerte. El tipo se decide por CONTENIDO, nunca por ``tipo_mime``.

    El sniffing sale del mismo descriptor anclado que después se streamea, así que
    cubre también los blobs YA EXISTENTES cuyo ``tipo_mime`` en base es la
    declaración sin validar del cliente.
    """
    root_fd = fd = -1
    try:
        root_fd = _open_root_fd()
        fd, info = _open_blob_fd_at(root_fd, content_hash, expected_size)
        sniffed = _sniff_open_fd(fd)
        stream = _FDStream(fd)
        fd = -1
    finally:
        if fd >= 0:
            _close_fd(fd)
        if root_fd >= 0:
            _close_fd(root_fd)
    active = (bool(sniffed["activo"]) or sniffed["tipo"] in ACTIVE_MIME
              or _active_suffix(str(name)))
    original = _safe_header_name(_neutralize_filename(str(name), active))
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", original).strip() or "adjunto"
    if active and not fallback.lower().endswith(".bin"):
        fallback += ".bin"
    disposition = "attachment; filename=\"" + fallback.replace('"', "_") + "\"; filename*=UTF-8''" + quote(original, safe="")
    return StreamingResponse(
        stream,
        media_type=INERT_MIME,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(info.st_size),
            "Content-Type": INERT_MIME,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": ("default-src 'none'; sandbox; "
                                        "frame-ancestors 'none'; base-uri 'none'; "
                                        "form-action 'none'; object-src 'none'; "
                                        "script-src 'none'"),
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cross-Origin-Embedder-Policy": "require-corp",
            "Cache-Control": "private, no-store",
            "X-Download-Options": "noopen",
            "X-Permitted-Cross-Domain-Policies": "none",
            "Referrer-Policy": "no-referrer",
            "X-Adjunto-Tipo-Detectado": sniffed["tipo"],
            "X-Adjunto-Contenido-Activo": "1" if active else "0",
        },
        background=BackgroundTask(stream.close),
    )


async def attach(agent: str, code: str, name: str, mime: str, request: Request) -> dict:
    clean_code = (code or "").strip().lower()
    clean_name, _ = sanitize.repair_surrogates((name or "").strip())
    clean_mime, _ = sanitize.repair_surrogates((mime or "application/octet-stream").strip())
    if not _CODE_RE.fullmatch(clean_code):
        raise HTTPException(422, "codigo inválido")
    if not clean_name or "\x00" in clean_name:
        raise HTTPException(422, "nombre de adjunto inválido")
    if not clean_mime or "\x00" in clean_mime:
        raise HTTPException(422, "tipo_mime inválido")

    limits = attachment_limits()

    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, _ = caller_identity(cur, agent)
        target = _attachment_target(cur, clean_code)
        if not _thread_participant(cur, target["raiz"], agent, mailbox):
            raise HTTPException(403, "no participás de este hilo")
        # Pre-chequeo con el incremento MÍNIMO posible (0 bytes por dedup, 1 ref
        # siempre): ahorra disco rechazando sólo lo que el autoritativo rechazaría
        # igual. El veredicto lo da _enforce_quota dentro de la transacción.
        _reject_if_quota_exhausted(_attachment_usage(cur, target["id"], agent),
                                   agent, limits)

    incoming = await _receive_blob(request, limit=limits["max_bytes"])
    content_hash, size = incoming.content_hash, incoming.size
    # Tipo por CONTENIDO. Lo que se persiste en `tipo_mime` es lo detectado; la
    # declaración del cliente sólo sirve para reportar la discrepancia.
    sniffed = sniff_content_type(incoming.head, size)
    detected_mime = sniffed["tipo"]
    active_content = bool(sniffed["activo"]) or detected_mime in ACTIVE_MIME
    mismatch = _mime_mismatch(clean_mime, detected_mime)
    if mismatch and limits["mime_strict"]:
        incoming.close()
        raise HTTPException(415, json.dumps({
            "error": "tipo_declarado_no_coincide_con_el_contenido",
            "tipo_declarado": clean_mime, "tipo_detectado": detected_mime,
            "deteccion": sniffed["via"], "contenido_activo": active_content,
            "modo": "estricto", "nada_se_escribio": True,
        }, ensure_ascii=False, sort_keys=True))
    attachment_id = uuid.uuid4()
    blob_created = False
    try:
        with db.pg() as conn, conn.cursor() as cur:
            _, mailbox, _ = caller_identity(cur, agent)
            target = _attachment_target(cur, clean_code)
            if not _thread_participant(cur, target["raiz"], agent, mailbox):
                raise HTTPException(403, "no participás de este hilo")
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (content_hash,))
            quota = _enforce_quota(cur, target["id"], agent, content_hash, size, limits)
            cur.execute("SELECT hash,tamano FROM correspondencia_blobs WHERE hash=%s", (content_hash,))
            known = cur.fetchone()
            if known and int(known["tamano"]) != size:
                raise HTTPException(503, "metadata de blob inconsistente")
            blob_created = _publish_blob_once(incoming)
            if not known:
                cur.execute(
                    "INSERT INTO correspondencia_blobs (hash,tamano,creado_por) VALUES (%s,%s,%s)",
                    (content_hash, size, agent),
                )
            cur.execute(
                "INSERT INTO correspondencia_adjuntos "
                "(id,mensaje_id,nombre,tipo_mime,tamano,blob_hash,subido_por) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (attachment_id, target["id"], clean_name, detected_mime, size, content_hash, agent),
            )
            conn.commit()
        return {"id": str(attachment_id), "mensaje_id": str(target["id"]),
                "codigo": clean_code, "nombre": clean_name, "tipo_mime": detected_mime,
                "tamano": size, "hash": content_hash, "subido_por": agent,
                "blob_creado": blob_created,
                "tipo_declarado": clean_mime, "tipo_detectado": detected_mime,
                "tipo_mime_mismatch": mismatch, "deteccion": sniffed["via"],
                "contenido_activo": active_content,
                "servido_como": INERT_MIME,
                "tope_bytes": limits["max_bytes"],
                "cuota": {"uso_previo": quota["uso_previo"],
                          "dedup": quota["dedup"],
                          "tope_mensaje_bytes": limits["mensaje_bytes"],
                          "tope_mensaje_count": limits["mensaje_count"],
                          "tope_remitente_bytes": limits["remitente_bytes"],
                          "tope_remitente_count": limits["remitente_count"]}}
    finally:
        incoming.close()


def _attachment_rows(cur, message_ids: list) -> dict[str, list[dict]]:
    result = {str(message_id): [] for message_id in message_ids}
    if not message_ids:
        return result
    cur.execute(
        "SELECT id,mensaje_id,nombre,tipo_mime,tamano,blob_hash AS hash,subido_por,"
        "creado_en,retirado_en FROM correspondencia_adjuntos WHERE mensaje_id=ANY(%s) "
        "ORDER BY creado_en,id", (message_ids,)
    )
    for raw in cur.fetchall():
        row = dict(raw)
        key = str(row["mensaje_id"])
        row["id"] = str(row["id"])
        row["mensaje_id"] = key
        result.setdefault(key, []).append(row)
    return result


def list_attachments(agent: str, code: str) -> dict:
    clean_code = (code or "").strip().lower()
    if not _CODE_RE.fullmatch(clean_code):
        raise HTTPException(422, "codigo inválido")
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, _ = caller_identity(cur, agent)
        target = _attachment_target(cur, clean_code)
        if not _thread_participant(cur, target["raiz"], agent, mailbox):
            raise HTTPException(403, "no participás de este hilo")
        rows = _attachment_rows(cur, [target["id"]]).get(str(target["id"]), [])
    return {"codigo": clean_code, "count": len(rows), "adjuntos": rows}


def download_attachment(agent: str, attachment_id: uuid.UUID):
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, _ = caller_identity(cur, agent)
        cur.execute(
            "SELECT a.id,a.nombre,a.tipo_mime,a.tamano,a.blob_hash AS hash,a.retirado_en,m.raiz "
            "FROM correspondencia_adjuntos a JOIN correspondencia_mensajes m "
            "ON m.id=a.mensaje_id WHERE a.id=%s", (attachment_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "adjunto no encontrado")
        row = dict(row)
        if not _thread_participant(cur, row["raiz"], agent, mailbox):
            raise HTTPException(403, "no participás de este hilo")
        if row["retirado_en"] is not None:
            raise HTTPException(410, "adjunto retirado")
    return _blob_stream_response(row["hash"], int(row["tamano"]), str(row["nombre"]))


def retire_attachment(agent: str, attachment_id: uuid.UUID) -> dict:
    with db.pg() as conn, conn.cursor() as cur:
        _, mailbox, is_admin = caller_identity(cur, agent)
        cur.execute(
            "SELECT a.id,a.blob_hash AS hash,a.subido_por,a.retirado_en,m.raiz "
            "FROM correspondencia_adjuntos a JOIN correspondencia_mensajes m "
            "ON m.id=a.mensaje_id WHERE a.id=%s FOR UPDATE OF a", (attachment_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "adjunto no encontrado")
        row = dict(row)
        if not _thread_participant(cur, row["raiz"], agent, mailbox):
            raise HTTPException(403, "no participás de este hilo")
        if not is_admin and str(row["subido_por"]).lower() != agent.lower():
            raise HTTPException(403, "sólo quien subió el adjunto puede retirarlo")
        cur.execute(
            "UPDATE correspondencia_adjuntos SET retirado_en=COALESCE(retirado_en,now()) "
            "WHERE id=%s RETURNING retirado_en", (attachment_id,)
        )
        retired = cur.fetchone()["retirado_en"]
        conn.commit()
    return {"id": str(attachment_id), "retirado_en": retired,
            "blob_conservado": True, "hash": row["hash"]}


# ─────────────────────────────────────────────────────────────────────────────
# F5 (recorte A/B/C): resumen, escalado visible y digest. TODO es *pull*.
#
# Ninguna de estas funciones crea mensajes ni entregas: son SELECT sobre lo que
# ya existe. La no-recursividad no se vigila, es estructural — no hay INSERT en
# correspondencia_mensajes en ningún camino de F5 (el escalador vive fuera del
# gateway y también es sólo UPDATE).
#
# `escalada_en`/`escalado_causa` son columnas ADITIVAS. El código no asume que el
# DDL ya corrió: si faltan, degrada a 0 escalados en vez de romper la bandeja.
# Eso hace que el orden de despliegue no pueda causar una caída silenciosa, y es
# falsable (test: base sin DDL ⇒ bandeja/resumen/digest siguen respondiendo).
# ─────────────────────────────────────────────────────────────────────────────
# Definición ÚNICA de "entrega urgente NO procesada", compartida por resumen y
# escalador (el escalador la repite en SQL porque corre fuera del gateway).
#
# 🔴 CORRECCIÓN AL DISEÑO (Palantír, 2026-09-12 §1/§4): el diseño afirma que el
# CHECK de `correspondencia_entregas` hace `estado='pendiente'` equivalente a
# `leido_en IS NULL AND archivado_en IS NULL`. Es FALSO: el CHECK desplegado sólo
# exige `archivado_en IS NULL` para 'pendiente' (medido con \d el 2026-09-12).
# Hoy no hay filas 'pendiente' con `leido_en` seteado (medido: 0) porque mark_read
# pasa a 'leido', pero la base NO lo garantiza. Se usa la conjunción explícita:
# nunca se trata como "no atendida" una entrega con evidencia de lectura.
NO_PROCESADA_SQL = ("e.estado='pendiente' AND e.leido_en IS NULL "
                    "AND e.archivado_en IS NULL")
# La causa la ESCRIBE el escalador derivándola del umbral efectivo
# ('actividad_4h' con el default, 'actividad_3h' con --horas-actividad 3). Acá no se
# enumeran valores —una lista de dos literales envejecería en silencio en cuanto
# alguien moviera un umbral—: se deja la FORMA, que es la misma que valida el CHECK
# del DDL. El gateway no valida la causa (no la escribe): la publica tal cual.
ESCALATION_CAUSE_PATTERN = r"^(actividad|edad)_[0-9]{1,5}h$"
PRIORITY_ORDER = ("urgente", "medio", "bajo")
RESUMEN_MAX_CODIGOS = 3


def escalation_columns_present(cur) -> bool:
    """¿Está aplicado el DDL de F5? Lookup de catálogo, sin memoizar.

    No se memoiza a propósito: un flag de proceso mentiría en un test que usa
    varias bases con el mismo import, y el costo es un acceso al catálogo que
    Postgres ya tiene en caché.
    """
    cur.execute(
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='correspondencia_entregas' "
        "AND column_name IN ('escalada_en','escalado_causa')"
    )
    row = cur.fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0]) == 2


def _escalation_for(cur, message_ids: list) -> dict[str, list[dict]]:
    """Escalados por UUID de mensaje, para que el EMISOR los vea en su item enviado.

    Campo aditivo: no cambia ningún contrato existente y no genera un mensaje
    nuevo. Si el DDL de F5 no está aplicado, devuelve vacío.
    """
    if not message_ids or not escalation_columns_present(cur):
        return {}
    normalize = mailbox_normalizer(cur)  # UN snapshot, no uno por escalado (F10)
    cur.execute(
        "SELECT mensaje_id,buzon,escalada_en,escalado_causa FROM correspondencia_entregas "
        "WHERE mensaje_id=ANY(%s) AND escalada_en IS NOT NULL ORDER BY escalada_en,buzon",
        (message_ids,),
    )
    out: dict[str, list[dict]] = {}
    for row in cur.fetchall():
        out.setdefault(str(row["mensaje_id"]), []).append(
            {"buzon": normalize(row["buzon"]),
             "escalada_en": row["escalada_en"],
             "causa": row["escalado_causa"]})
    return out


def summary(agent: str) -> dict:
    """A — teaser barato del buzón propio: conteos y ≤3 códigos urgentes.

    NO reusa inbox(): esa función trae contenido, reacciones, adjuntos y audiencia
    de respuesta. Acá son dos consultas agregadas y NINGUNA columna de cuerpo
    (`contenido`) ni de asunto: un resumen que cite el asunto ya es una bandeja.

    🔴 DOS UNIVERSOS, Y CADA CAMPO DICE CUÁL ES EL SUYO (corregido). Antes
    `urgentes` contaba NO ARCHIVADAS (o sea incluía leídas) y `urgentes_codigos`
    listaba sólo las NO LEÍDAS: dos poblaciones distintas en el mismo teaser, y el
    lector natural resta y saca una conclusión falsa ("tengo 5 urgentes y me muestra
    3, debe ser el tope"). Los universos son:
      · `criterio`            → no archivadas (pendiente+leido): pendientes,
                                urgentes, medios, bajos, urgentes_escalados;
      · `criterio_sin_leer`   → conjunción estricta de NO PROCESADA: sin_leer,
                                urgentes_sin_leer y urgentes_sin_leer_codigos.
    El campo de códigos se llama `urgentes_sin_leer_codigos` justamente para que el
    nombre arrastre su universo, y tiene el conteo hermano del MISMO universo
    (`urgentes_sin_leer`) para que "3 códigos de 5" sea comparable con algo.
    """
    with db.pg() as conn, conn.cursor() as cur:
        _, own_box, _ = caller_identity(cur, agent)
        boxes = equivalent_mailboxes(cur, own_box)
        escalated = ("count(*) FILTER (WHERE m.prioridad='urgente' "
                     "AND e.escalada_en IS NOT NULL)" if escalation_columns_present(cur)
                     else "0")
        cur.execute(
            "SELECT count(*) AS pendientes,"
            " count(*) FILTER (WHERE m.prioridad='urgente') AS urgentes,"
            " count(*) FILTER (WHERE m.prioridad='urgente' AND "
            f"{NO_PROCESADA_SQL}) AS urgentes_sin_leer,"
            f" {escalated} AS urgentes_escalados,"
            " count(*) FILTER (WHERE m.prioridad='medio') AS medios,"
            " count(*) FILTER (WHERE m.prioridad='bajo') AS bajos,"
            f" count(*) FILTER (WHERE {NO_PROCESADA_SQL}) AS sin_leer "
            "FROM correspondencia_entregas e JOIN correspondencia_mensajes m "
            "ON m.id=e.mensaje_id WHERE e.buzon=ANY(%s) AND e.estado<>'archivado'",
            (boxes,),
        )
        counts = dict(cur.fetchone())
        order = ("e.escalada_en DESC NULLS LAST, m.creado_en ASC"
                 if escalation_columns_present(cur) else "m.creado_en ASC")
        cur.execute(
            "SELECT m.codigo FROM correspondencia_entregas e "
            "JOIN correspondencia_mensajes m ON m.id=e.mensaje_id "
            f"WHERE e.buzon=ANY(%s) AND {NO_PROCESADA_SQL} AND m.prioridad='urgente' "
            f"ORDER BY {order} LIMIT %s",
            (boxes, RESUMEN_MAX_CODIGOS),
        )
        codes = [row["codigo"] for row in cur.fetchall()]
    return {"buzon": own_box,
            # Universo de pendientes/urgentes/medios/bajos/urgentes_escalados.
            "criterio": "entregas no archivadas (pendiente+leido)",
            # Universo de sin_leer/urgentes_sin_leer/urgentes_sin_leer_codigos.
            "criterio_sin_leer": "pendiente AND leido_en IS NULL AND archivado_en IS NULL",
            "pendientes": int(counts["pendientes"]),
            "sin_leer": int(counts["sin_leer"]),
            "urgentes": int(counts["urgentes"]),
            "urgentes_sin_leer": int(counts["urgentes_sin_leer"]),
            "urgentes_escalados": int(counts["urgentes_escalados"]),
            "medios": int(counts["medios"]),
            "bajos": int(counts["bajos"]),
            "urgentes_sin_leer_codigos": codes,
            "max_codigos": RESUMEN_MAX_CODIGOS}


def digest(agent: str, requested_mailbox: Optional[str]) -> dict:
    """C — digest agrupado por prioridad. Sin contenido ni reacciones expandidas.

    Mismo 403/422 que inbox() para el buzón de un tercero: la regla de acceso no
    se reescribe acá, se repite tal cual para que un cambio en una y no en la otra
    salte en el test de regresión.
    """
    with db.pg() as conn, conn.cursor() as cur:
        _, own_box, is_admin = caller_identity(cur, agent)
        raw_target = (requested_mailbox or "").strip().lower() or None
        target = normalize_mailbox(cur, raw_target) if raw_target else None
        if target and not is_admin and target != own_box:
            raise HTTPException(403, "no podés leer el buzón de otra identidad")
        if target:
            individuals, _, _ = _registry(cur)
            if target not in individuals:
                raise HTTPException(422, "buzón individual inexistente")
        has_escalation = escalation_columns_present(cur)
        columns = ("e.escalada_en,e.escalado_causa" if has_escalation
                   else "NULL::timestamptz AS escalada_en,NULL::text AS escalado_causa")
        clauses = ["e.estado<>'archivado'"]
        params: list[object] = []
        effective = target if is_admin else own_box
        if effective:
            clauses.append("e.buzon=ANY(%s)")
            params.append(equivalent_mailboxes(cur, effective))
        normalize = mailbox_normalizer(cur)  # UN snapshot, no uno por fila (F10)
        cur.execute(
            "SELECT m.codigo,m.raiz,m.asunto,m.remitente,m.prioridad,m.creado_en,"
            f"m.tipo,e.buzon,e.estado,{columns} "
            "FROM correspondencia_entregas e JOIN correspondencia_mensajes m "
            "ON m.id=e.mensaje_id WHERE " + " AND ".join(clauses) +
            " ORDER BY CASE m.prioridad WHEN 'urgente' THEN 0 WHEN 'medio' THEN 1 "
            "ELSE 2 END, m.creado_en, m.codigo",
            tuple(params),
        )
        groups: dict[str, dict] = {name: {"count": 0, "escalados": 0, "items": []}
                                   for name in PRIORITY_ORDER}
        total = 0
        for row in cur.fetchall():
            item = dict(row)
            priority = str(item["prioridad"])
            bucket = groups.setdefault(priority, {"count": 0, "escalados": 0, "items": []})
            bucket["items"].append({"codigo": item["codigo"], "raiz": item["raiz"],
                                    "asunto": item["asunto"], "remitente": item["remitente"],
                                    "prioridad": priority, "tipo": item["tipo"],
                                    "buzon": normalize(item["buzon"]),
                                    "estado": item["estado"], "creado_en": item["creado_en"],
                                    "escalada_en": item["escalada_en"],
                                    "escalado_causa": item["escalado_causa"]})
            bucket["count"] += 1
            if item["escalada_en"] is not None:
                bucket["escalados"] += 1
            total += 1
    return {"buzon": effective or "*", "vista": "digest", "count": total,
            "prioridades": list(PRIORITY_ORDER), "escalado_disponible": has_escalation,
            **groups}


def mailboxes(agent: str) -> dict:
    """Registro de buzones canónicos de las identidades ACTIVAS. Sólo lectura.

    GATE ADMIN ESTRUCTURAL, no una comprobación agregada encima: `caller_identity`
    marca `is_admin` sólo con kind='human' AND role='admin'. La credencial del BFF es
    kind='agent', así que NUNCA puede ser admin. Consecuencia buscada: si esta ruta no
    está declarada en DELEGATION_ROUTES del BFF, el caller que llega acá es `sinapsis`
    y la respuesta es 403 — no una lista de buzones vista desde otra identidad. Una
    ruta nueva no nace delegada, y el modo de falla de olvidarse de declararla tiene
    que ser RUIDOSO: un 200 con el registro equivocado es indistinguible del correcto.

    La respuesta ecoa `identidad`/`propio`/`es_admin` para que el cliente CRUCE contra
    su sesión en vez de confiar en el status. Hoy el 403 alcanza; el eco sobrevive a
    que mañana esa credencial cambie de forma.

    No inventa permiso: leer el registro de identidades activas no habilita nada sobre
    el contenido de ningún buzón. `/correspondencia/bandeja` sigue decidiendo por sí.
    """
    with db.pg() as conn, conn.cursor() as cur:
        data, own_box, is_admin = caller_identity(cur, agent)
        if not is_admin:
            raise HTTPException(
                403, "sólo un admin humano puede listar los buzones de la flota")
        individuals, _, aliases = _registry(cur)
        alias_por_destino: dict[str, list[str]] = {}
        for alias, destino in aliases.items():
            alias_por_destino.setdefault(destino, []).append(alias)
        buzones = []
        for mailbox in sorted(individuals):
            row = individuals[mailbox]
            buzones.append({
                "buzon": mailbox,
                "nombre": str(row["name"]).strip().lower(),
                "equipo": str(row.get("team") or "").strip().lower() or None,
                "kind": str(row.get("kind") or "agent").strip().lower(),
                "es_lider": bool(row.get("is_lead")),
                "alias": sorted(alias_por_destino.get(mailbox, [])),
                "propio": mailbox == own_box,
            })
    return {"identidad": str(data["name"]).strip().lower(), "propio": own_box,
            "es_admin": is_admin, "count": len(buzones), "buzones": buzones}


def install_routes(app: FastAPI, verify_agent: Callable) -> None:
    """Instala rutas REST. El gate decidirá si integra esta llamada al gateway."""

    @app.post("/correspondencia/adjuntos")
    async def adjuntar_ep(request: Request, codigo: str = Query(...),
                          nombre: str = Query(...),
                          tipo_mime: str = Query("application/octet-stream"),
                          agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_adjunto_subir",
                               "query": f"codigo={codigo} nombre_chars={len(nombre)}"}
        return await attach(agent, codigo, nombre, tipo_mime, request)

    @app.get("/correspondencia/adjuntos")
    def adjuntos_ep(request: Request, codigo: str = Query(...),
                    agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_adjunto_listar", "query": codigo}
        return list_attachments(agent, codigo)

    @app.get("/correspondencia/adjuntos/{attachment_id}/descargar")
    def descargar_adjunto_ep(request: Request, attachment_id: uuid.UUID,
                             agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_adjunto_descargar",
                               "query": str(attachment_id)}
        return download_attachment(agent, attachment_id)

    @app.post("/correspondencia/adjuntos/{attachment_id}/retirar")
    def retirar_adjunto_ep(request: Request, attachment_id: uuid.UUID,
                           agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_adjunto_retirar",
                               "query": str(attachment_id)}
        return retire_attachment(agent, attachment_id)

    @app.post("/correspondencia/publicar")
    def publicar_ep(request: Request, req: PublishRequest,
                    agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_publicar"}
        return publish(agent, req)

    @app.post("/correspondencia/responder")
    def responder_ep(request: Request, req: ReplyRequest,
                     agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_responder", "query": req.codigo}
        return reply(agent, req)

    @app.get("/correspondencia/bandeja")
    def bandeja_ep(request: Request, buzon: Optional[str] = Query(None),
                   archivo: bool = Query(False),
                   incluir_contenido: bool = Query(True),
                   agent: str = Depends(verify_agent)):
        """`incluir_contenido` (F11) es OPT-OUT: default True es CONTRATO REST/BFF
        con clientes existentes que renderizan `contenido`; no cambiarlo sin migrarlos.
        El panel adaptado pide explícitamente `false`: con eso viajan
        `contenido_len` + `contenido_preview` (120 chars) y el cuerpo se pide de a UNO
        por /correspondencia/leer, que audita la lectura."""
        request.state.audit = {"action": "correspondencia_bandeja",
                               "query": f"buzon={buzon or 'self'} archivo={archivo} "
                                        f"incluir_contenido={bool(incluir_contenido)}"}
        return inbox(agent, buzon, archivo, include_content=bool(incluir_contenido))

    @app.get("/correspondencia/resumen")
    def resumen_ep(request: Request, agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_resumen", "query": "self"}
        return summary(agent)

    @app.get("/correspondencia/digest")
    def digest_ep(request: Request, buzon: Optional[str] = Query(None),
                  agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_digest",
                               "query": f"buzon={buzon or 'self'}"}
        return digest(agent, buzon)

    @app.get("/correspondencia/buzones")
    def buzones_ep(request: Request, agent: str = Depends(verify_agent)):
        """Registro para el selector de admin. Sin parámetros: no hay nada que elegir
        antes de saber qué existe, y un filtro acá sería una segunda autorización."""
        request.state.audit = {"action": "correspondencia_buzones", "query": "registro"}
        return mailboxes(agent)

    @app.post("/correspondencia/leer")
    def leer_ep(request: Request, req: ReadRequest,
                agent: str = Depends(verify_agent)):
        """Lectura de UNO: devuelve el mensaje COMPLETO y marca leida la entrega propia.
        La lectura del CUERPO queda en audit_log como DELIBERADA (mismo criterio que
        /bus/thread con include_content=true): el objetivo es que traer un cuerpo por
        comodidad deje de ser indistinguible de traerlo por necesidad."""
        request.state.audit = {"action": "correspondencia_leer", "query": req.codigo}
        data = mark_read(agent, req)
        # El middleware lee request.state.audit DESPUES del handler, asi que aca ya se
        # puede registrar QUE se leyo y CUANTO cuerpo se entrego.
        request.state.audit["query"] = (
            f"codigo={data.get('codigo')} solicitado={req.codigo} "
            f"contenido_chars={data.get('contenido_chars')} "
            f"cuerpo_entregado=1 via={req.via_lectura}")[:480]
        return data

    @app.post("/correspondencia/ok")
    def ok_ep(request: Request, req: OkRequest,
              agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_ok", "query": req.codigo}
        return acknowledge_one(agent, req)

    @app.post("/correspondencia/ack")
    def ack_ep(request: Request, req: AckRequest,
               agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_ack",
                               "query": f"codigos={len(req.codigos)} raiz={req.raiz or '-'}"}
        return acknowledge(agent, req)


    @app.post("/correspondencia/reaccion")
    def reaccion_ep(request: Request, req: ReactionRequest,
                    agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_reaccion", "query": req.codigo}
        return react(agent, req)

    @app.post("/correspondencia/solicitar-revision")
    def solicitar_revision_ep(request: Request, req: ReviewRequest,
                              agent: str = Depends(verify_agent)):
        request.state.audit = {"action": "correspondencia_solicitar_revision",
                               "query": req.codigo}
        return request_review(agent, req)
