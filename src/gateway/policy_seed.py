"""Public generic fallback, not a deployment inventory or vector-coverage proof.

POLICY_SEED_JSON or POLICY_SEED_FILE replaces the complete generic seed. Never
include a private vault's directory listing in a public repository. Dynamic FS
metadata + last-good union and explicit retirements remain in policy_roots.py.
"""
import json
import os
from pathlib import Path

KEYS = ("sessions", "ofi_history", "ofi_archive", "logs")
GENERIC_SEED = {
    "sessions": ["50 Agents/Demo-agent/Sesiones/"],
    "ofi_history": ["50 Agents/Ofi-Demo/Demo-agent/Historico/"],
    "ofi_archive": ["50 Agents/Ofi-Demo/Demo-agent/Archive/"],
    "logs": ["30 Projects/Demo/Progress/"],
    "preserved_raw": [],
    "expect_nonzero_roots": False,  # Generic patterns do not prove populated FS.
    "coverage": "generic fallback only; no PG/Q coverage certified",
    "provenance": "public generic defaults; operator may supply a private seed at runtime",
}


def load_seed():
    raw = os.environ.get("POLICY_SEED_JSON", "")
    filename = os.environ.get("POLICY_SEED_FILE", "")
    if raw and filename:
        raise ValueError("Choose only POLICY_SEED_JSON or POLICY_SEED_FILE")
    value = json.loads(raw) if raw else (json.loads(Path(filename).read_text()) if filename else GENERIC_SEED)
    if not isinstance(value, dict) or set(value) - set(GENERIC_SEED) or not all(k in value for k in KEYS):
        raise ValueError("Invalid policy seed keys")
    for key in KEYS + ("preserved_raw",):
        roots = value.get(key, [])
        if not isinstance(roots, list) or any(not isinstance(r, str) or not r or not r.endswith("/")
                                             or r.startswith("/") or ".." in r.split("/") for r in roots):
            raise ValueError("Invalid policy seed root metadata")
    expect_nonzero_roots = value.get("expect_nonzero_roots", True)
    if type(expect_nonzero_roots) is not bool:
        raise ValueError("Invalid expect_nonzero_roots: expected boolean")
    return {**value, "preserved_raw": value.get("preserved_raw", []),
            "expect_nonzero_roots": expect_nonzero_roots}


POLICY_SEED = load_seed()
