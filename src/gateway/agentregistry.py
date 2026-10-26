"""agentregistry.py - Registro DECLARATIVO de la flota de agentes (determinista).

Escanea los .md del vault que tengan frontmatter YAML (bloque delimitado por --- al
inicio) y un campo `agent:`/`name:`, o que vivan bajo rutas de agentes (50 Agents/*/).
Parsea el frontmatter con yaml.safe_load y expone el registro canonico (modelo, tools,
thinking, runtime, etc.) como JSON estructurado. Cache en memoria con TTL, mismo patron
que notegraph. Read-only sobre el vault. NO toca Postgres ni schema.

API:
  list_agents() -> {"agents": [ {registro...} ], "count": N}
  get_agent(name) -> {registro...} | {"error": "..."}
  stats() -> {"agents": N, "roots": [...], "ttl": N}
"""
import logging
import os
import re
import threading
import time
from pathlib import Path
from archivepolicy import is_registry_excluded_path

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML es dependencia declarada
    yaml = None

# Misma convencion que notegraph: el vault se monta en /vault; las notas viven bajo
# <root>/obsidian-vault.
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", "/vault"))
NOTES_ROOT = VAULT_ROOT / "obsidian-vault"
if not NOTES_ROOT.is_dir() and VAULT_ROOT.is_dir():
    NOTES_ROOT = VAULT_ROOT

# Infrastructure-only directories. Historical/draft policy is shared with retrieval
# via archivepolicy.is_registry_excluded_path(), applied to vault-relative paths.
EXCLUDE = ("node_modules", ".venv", "venv", ".git", "site-packages", "__pycache__",
           ".trash", ".smart-env", ".cache", ".deno")

# Un .md se considera "de agente" si vive bajo 50 Agents/<algo>/ ...
AGENT_DIR_RE = re.compile(r"(^|/)50 Agents/[^/]+/", re.IGNORECASE)
# Delimitador de frontmatter: el archivo abre con --- y cierra con --- (los dos primeros).
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(\n|$)", re.DOTALL)

CACHE_TTL = int(os.environ.get("AGENTREGISTRY_TTL", "60"))
_cache = {"ts": 0.0, "agents": None, "retry_after": 0.0}
_cache_lock = threading.Lock()
_FRONTMATTER_PREFIX = 65536
_REFRESH_RETRY_COOLDOWN = 60

# Campos del frontmatter que materializamos en el registro (todos opcionales salvo el
# nombre, que sale de `name` o `agent`).
_LIST_FIELDS = ("tools", "extension_tools", "runtime")


def _parse_frontmatter(text):
    """Devuelve el dict del frontmatter YAML del .md, o None si no hay/está roto."""
    if yaml is None:
        return None
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_frontmatter(path):
    """Read only the frontmatter when possible, preserving full-text regex semantics."""
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        prefix = stream.read(_FRONTMATTER_PREFIX + 1)
        match = FRONTMATTER_RE.match(prefix)
        if match and (len(prefix) <= _FRONTMATTER_PREFIX or match.end() < len(prefix)):
            return _parse_frontmatter(prefix)
        if not prefix.startswith("---"):
            return None
        # An opening delimiter with no complete match may close after the prefix.
        return _parse_frontmatter(prefix + stream.read())


def _as_list(val):
    """Normaliza un campo a lista de strings. Acepta lista o string (CSV o único)."""
    if val is None:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    if not s:
        return []
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [s]


def _build_record(fm, rel_path):
    """Construye el registro canonico de un agente a partir del frontmatter."""
    name = fm.get("name") or fm.get("agent")
    if name is not None:
        name = str(name).strip()
    rec = {
        "name": name,
        "role": fm.get("role"),
        "model": fm.get("model"),
        "temperature": fm.get("temperature"),
        "thinking": fm.get("thinking"),
        "max_turns": fm.get("max_turns"),
        "tools": _as_list(fm.get("tools")),
        "extension_tools": _as_list(fm.get("extension_tools")),
        "team": fm.get("team") or fm.get("leader") or fm.get("lead"),
        "runtime": _as_list(fm.get("runtime")),
        "active": fm.get("active"),
        "source_path": rel_path,
    }
    return rec


def _scan():
    """Escanea el vault y arma la lista de registros de agentes."""
    root = NOTES_ROOT
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.md") if not any(x in str(p) for x in EXCLUDE)]
    records = []
    for p in files:
        try:
            rel = str(p.relative_to(root))
        except Exception:
            continue
        if is_registry_excluded_path(rel) or not AGENT_DIR_RE.search("/" + rel):
            continue
        try:
            fm = _read_frontmatter(p)
        except Exception:
            continue
        if fm is None:
            continue
        fm_type = str(fm.get("type") or "").strip().lower()
        is_agent_def = fm_type in ("soul", "agent")
        # Se registra SOLO si es una DEFINICION de agente (type: soul|agent) bajo
        # 50 Agents/. Excluye las notas con `agent:` de AUTORIA (progreso/diagnostico).
        if not is_agent_def:
            continue
        rec = _build_record(fm, rel)
        # Sin nombre resoluble no sirve como entrada del registro.
        if not rec.get("name"):
            continue
        records.append(rec)
    records.sort(key=lambda r: (str(r.get("name") or "").lower(), r.get("source_path") or ""))
    return records


def _refresh_locked():
    """Refresh while holding _cache_lock; publish only a complete successful scan."""
    try:
        try:
            agents = _scan()
        except Exception:
            # A stale cache remains usable; bound retry attempts without changing
            # the timestamp of the last successful scan. Cold-start errors propagate.
            if _cache["agents"] is not None:
                _cache["retry_after"] = time.time() + _REFRESH_RETRY_COOLDOWN
            raise
        _cache["agents"] = agents
        _cache["ts"] = time.time()
        _cache["retry_after"] = 0.0
    finally:
        _cache_lock.release()


def _refresh_background():
    try:
        _refresh_locked()
    except Exception:
        logging.exception("agentregistry background refresh failed; retaining previous cache")


def _agents(force=False):
    agents = _cache["agents"]
    if not force and agents is not None:
        if (time.time() - _cache["ts"] > CACHE_TTL
                and time.time() >= _cache["retry_after"]
                and _cache_lock.acquire(blocking=False)):
            try:
                # Recheck after acquiring: another refresh might have just completed.
                if (time.time() - _cache["ts"] > CACHE_TTL
                        and time.time() >= _cache["retry_after"]):
                    threading.Thread(target=_refresh_background, daemon=True,
                                     name="agentregistry-refresh").start()
                else:
                    _cache_lock.release()
            except Exception:
                _cache_lock.release()
                raise
        return agents
    _cache_lock.acquire()
    try:
        if force or _cache["agents"] is None:
            _refresh_locked()
        else:
            _cache_lock.release()
    except Exception:
        # _refresh_locked releases on both success and error.
        raise
    return _cache["agents"]


def list_agents():
    ags = _agents()
    return {"agents": ags, "count": len(ags)}


def get_agent(name):
    if not name or not str(name).strip():
        return {"error": "name vacio"}
    q = str(name).strip().lower()
    ags = _agents()
    matches = [a for a in ags if str(a.get("name") or "").lower() == q]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        srcs = [m.get("source_path") for m in matches[:10]]
        return {"error": "ambiguo: %r matchea %d agentes: %s" % (name, len(matches), srcs)}
    return {"error": "no encontrado: %r no matchea ningun agente del vault" % name}


def stats():
    ags = _agents()
    roots = sorted({(a.get("source_path") or "").split("/")[0] for a in ags if a.get("source_path")})
    return {
        "agents": len(ags),
        "roots": roots,
        "notes_root": str(NOTES_ROOT),
        "ttl": CACHE_TTL,
    }


if __name__ == "__main__":
    import json
    ags = _agents(force=True)
    st = stats()
    print("[agentregistry self-test] stats:", json.dumps(st))
    assert st["agents"] > 0, "FAIL: no se encontraron agentes en %s" % NOTES_ROOT
    print("[agentregistry self-test] agentes:", json.dumps([a.get("name") for a in ags])[:800])
    # get_agent case-insensitive
    first = ags[0].get("name")
    r = get_agent(first)
    assert r.get("name"), "FAIL: get_agent(%r) deberia resolver" % first
    r2 = get_agent(str(first).upper())
    assert r2.get("name"), "FAIL: get_agent deberia ser case-insensitive"
    # inexistente da error
    r3 = get_agent("__no_existe_zzz_qqq__")
    assert r3.get("error"), "FAIL: deberia dar error para agente inexistente"
    # vacio da error
    r4 = get_agent("")
    assert r4.get("error"), "FAIL: name vacio deberia dar error"
    print("[agentregistry self-test] PASS")
