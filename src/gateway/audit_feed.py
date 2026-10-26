"""Self-scoped audit feed sidecar for the Memory Gateway.

Integration (deliberately not performed here):
    import audit_feed
    audit_feed.install_routes(app, verify_agent)

``verify_agent`` is the gateway's real authentication/delegation dependency.  Its
return value is the *effective* principal; this module never accepts an agent,
admin, or scope-grant selector from the caller.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request

import db

_ALLOWED_PARAMS = frozenset({"action", "status", "limit", "cursor"})
_ACTION_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]{0,63}$")
_STATUSES = frozenset({"all", "success", "failure"})
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100
_INVALID_QUERY = "parámetros de consulta inválidos"


def _fingerprint(principal: str, action: Optional[str], status: str) -> str:
    """Bind an unsigned opaque cursor to exactly one self-scoped filter set."""
    material = json.dumps(
        {"principal": principal, "action": action, "status": status},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _encode_cursor(ts: datetime, row_id: int, fingerprint: str) -> str:
    # This is opaque encoding, not a signature. SQL self-scope remains authoritative.
    payload = json.dumps(
        {"ts": ts.isoformat(), "id": int(row_id), "fp": fingerprint},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(raw: str, fingerprint: str) -> tuple[str, int]:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        ts, row_id, supplied_fp = value["ts"], value["id"], value["fp"]
        if not isinstance(ts, str) or not isinstance(row_id, int) or not isinstance(supplied_fp, str):
            raise ValueError("bad cursor types")
        # Validate before supplying it to PostgreSQL's timestamptz coercion.
        datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if row_id < 0 or supplied_fp != fingerprint:
            raise ValueError("cursor does not match filters")
        return ts, row_id
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise HTTPException(422, _INVALID_QUERY)


def _query_params(request: Request) -> tuple[Optional[str], str, int, Optional[str]]:
    query_params = request.query_params
    names = set(query_params)
    if names - _ALLOWED_PARAMS or any(len(query_params.getlist(name)) != 1 for name in names):
        raise HTTPException(422, _INVALID_QUERY)

    action = query_params.get("action")
    if action is not None and not _ACTION_RE.fullmatch(action):
        raise HTTPException(422, _INVALID_QUERY)

    status = query_params.get("status", "all")
    if status not in _STATUSES:
        raise HTTPException(422, _INVALID_QUERY)

    raw_limit = query_params.get("limit", str(_DEFAULT_LIMIT))
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(422, _INVALID_QUERY)
    if not 1 <= limit <= _MAX_LIMIT:
        raise HTTPException(422, _INVALID_QUERY)
    return action, status, limit, query_params.get("cursor")


def _row_value(row, name: str):
    return row[name] if isinstance(row, dict) else getattr(row, name)


def install_routes(app: FastAPI, verify_agent: Callable) -> None:
    """Install the additive GET /audit/self route using the supplied real auth gate."""

    @app.get(
        "/audit/self",
        responses={
            200: {"description": "Página de auditoría de la identidad efectiva"},
            401: {"description": "Credencial ausente o inválida"},
            422: {"description": "Parámetro, filtro o cursor inválido"},
        },
    )
    def audit_self(request: Request, principal: str = Depends(verify_agent)):
        action, status, limit, raw_cursor = _query_params(request)
        fingerprint = _fingerprint(principal, action, status)
        cursor = _decode_cursor(raw_cursor, fingerprint) if raw_cursor else None

        # ts is nullable in the historical schema. Excluding NULL keeps the cursor's
        # strict tuple ordering total and means no row is silently repeated/skipped.
        where = ["agent = %s", "ts IS NOT NULL"]
        params = [principal]
        if action is not None:
            where.append("action = %s")
            params.append(action)
        if status == "success":
            where.append("success IS TRUE")
        elif status == "failure":
            where.append("success IS FALSE")
        if cursor:
            where.append("(ts, id) < (%s::timestamptz, %s)")
            params.extend(cursor)

        sql = (
            "SELECT id, ts, action, result_count, duration_ms, success "
            "FROM audit_log WHERE " + " AND ".join(where) +
            " ORDER BY ts DESC, id DESC LIMIT %s"
        )
        params.append(limit + 1)
        try:
            with db.pg() as conn, conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
        except Exception:
            # Never serialize driver detail; this is an operational endpoint, not a DB probe.
            raise HTTPException(500, "audit feed no disponible")

        page = rows[:limit]
        items = []
        for row in page:
            ts = _row_value(row, "ts")
            items.append({
                "id": int(_row_value(row, "id")),
                "ts": ts.isoformat(),
                "action": _row_value(row, "action"),
                "result_count": _row_value(row, "result_count"),
                "duration_ms": _row_value(row, "duration_ms"),
                "success": _row_value(row, "success"),
            })
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(_row_value(last, "ts"), _row_value(last, "id"), fingerprint)

        # The universal middleware writes the request audit record after this handler.
        # Deliberately no query/filter/cursor metadata: only safe cardinality is retained.
        request.state.audit = {"action": "audit_self", "result_count": len(items)}
        return {"items": items, "next_cursor": next_cursor}
