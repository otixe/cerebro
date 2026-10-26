"""Office-level denylist: shared vault by default, team offices private."""
import logging
import json
import os
import posixpath
import time
import uuid
from fastapi import HTTPException
import db

STATIC_FOLDER_TEAM = json.loads(os.environ.get("OFFICE_FOLDER_TEAMS", '{"Demo-agent":"demo"}'))
if (not isinstance(STATIC_FOLDER_TEAM, dict) or
    any(not isinstance(n, str) or not n or '/' in n or n in ('.', '..')
        or not isinstance(t, str) or not t or '/' in t for n, t in STATIC_FOLDER_TEAM.items())):
    raise ValueError("Invalid OFFICE_FOLDER_TEAMS")
_CACHE = (0.0, None)


def _parse_aliases(raw):
    out = {}
    for pair in (raw or "").split(","):
        if ":" in pair:
            a, b = (x.strip().lower() for x in pair.split(":", 1))
            if a and b:
                out[a] = b
    return out


# Teams that are THE SAME OFFICE (Otixe, 2026-10-05: «los del equipo de rudeus son los
# mismos que los de la oficina de ariel»). Read once at import: changing it only needs
# a container recreate, not a rebuild. Empty value disables every alias.
TEAM_ALIASES = _parse_aliases(os.getenv("OFFICE_TEAM_ALIASES", ""))


def _office_of(team):
    return TEAM_ALIASES.get(team, team) if team else team


AMBIGUOUS = "\x00ambiguous"  # owner no team can match: deny all but bypass
_log = logging.getLogger("cerebro.office_privacy")


def _snapshot():
    global _CACHE
    now = time.monotonic()
    if _CACHE[1] is not None and now - _CACHE[0] < 60:
        return _CACHE[1]
    try:
        # agent_row() suppresses DB errors: this query must NOT do that.
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, team FROM agents WHERE active=true")
            rows = cur.fetchall()
        agents = {r["name"].lower(): (r["team"] or "").lower() for r in rows}
    except Exception as exc:
        _CACHE = (0.0, None)
        raise HTTPException(503, "office authorization temporarily unavailable") from exc
    _CACHE = (now, agents)
    return agents


def _owners(agents):
    # Static houses win over old/revoked agent names; other live personal folders
    # inherit the team from agents. General folders never appear in agents.
    folders = {name.lower(): (name, team) for name, team in STATIC_FOLDER_TEAM.items()}
    folders.update({name: (name.capitalize(), team) for name, team in agents.items() if name not in folders and team})
    owners = {f"50 Agents/{folder}/": _office_of(team) for folder, team in folders.values()}
    teams = {_office_of(t) for t in agents.values() if t}
    # Unknown Ofi-X has an impossible owner, hence every non-bypass caller denied.
    # Evaluate it in office_denied; do not invent an unbounded list of offices.
    short = {}
    for name, team in agents.items():
        if not team:
            continue
        owners[f"memoria/{name}/"] = _office_of(team)
        owners[f"memoria/{team}-{name}/"] = _office_of(team)
        if name.startswith(f"{team}-") and len(name) > len(team) + 1:
            short.setdefault(name[len(team) + 1:], set()).add(_office_of(team))
    # Short names (memoria/grima/ for saruman-grima). If two active teams share a short
    # name, the static folder owner decides; otherwise nobody but the bypass reads it.
    static = {k.lower(): v for k, v in STATIC_FOLDER_TEAM.items()}
    for sname, steams in short.items():
        key = f"memoria/{sname}/"
        if key in owners:
            continue
        owners[key] = steams.pop() if len(steams) == 1 else (_office_of(static.get(sname)) or AMBIGUOUS)
    return owners, teams


def _identity(agent):
    agents = _snapshot()
    owners, teams = _owners(agents)
    return _office_of(agents.get(agent.lower())), owners, teams


def _bypass(agent):
    return agent.lower() in {x.strip().lower() for x in os.getenv("OFFICE_FULL_READ", "admin").split(",") if x.strip()}


def deny_prefixes(agent) -> list[str]:
    if _bypass(agent):
        return []
    team, owners, teams = _identity(agent)
    denied = [prefix for prefix, owner in owners.items() if owner != team]
    # Every Ofi-<x> that is not the caller's office, x ranging over real offices AND alias
    # names (Ofi-Rudeus must stay denied to third parties even if the folder does not exist).
    denied.extend(f"50 Agents/Ofi-{t.capitalize()}/" for t in teams | set(TEAM_ALIASES) if _office_of(t) != team)
    # Include unknown offices already present on disk; otherwise their legacy
    # search points (without path_ancestors) escape the pre-ranking filter.
    try:
        with os.scandir(os.path.join(os.getenv("VAULT_ROOT", "/vault"), "obsidian-vault", "50 Agents")) as entries:
            denied.extend(f"50 Agents/{e.name}/" for e in entries if e.is_dir() and e.name.lower().startswith("ofi-")
                          and (_office_of(e.name[4:].lower()) not in teams or _office_of(e.name[4:].lower()) != team))
    except OSError as exc:
        raise HTTPException(503, "office authorization temporarily unavailable") from exc
    return sorted(set(denied))


def office_denied(agent, norm_path) -> bool:
    if _bypass(agent):
        return False
    team, owners, teams = _identity(agent)
    path = (norm_path or "").replace("\\", "/")
    while True:
        path = path.lstrip("/")
        if path.startswith("./"):
            path = path[2:]
        elif path == "obsidian-vault" or path.startswith("obsidian-vault/"):
            path = path[len("obsidian-vault"):].lstrip("/")
        else:
            break
    path = posixpath.normpath(path) + "/"
    low = path.lower()
    if low.startswith("50 agents/ofi-"):
        office = _office_of(low.split("/", 2)[1][4:])
        return office not in teams or office != team
    return any(low.startswith(prefix.lower()) and owner != team for prefix, owner in owners.items())


CHUNK_REF_KEYS = ("src_chunk", "dst_chunk", "chunk_id", "chunk", "superseded_by")


def _chunk_refs(value, acc):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in CHUNK_REF_KEYS and isinstance(v, str) and v:
                acc.add(v)
            else:
                _chunk_refs(v, acc)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _chunk_refs(v, acc)
    return acc


def _chunk_paths(ids):
    """{chunk_id: source_path} for the given ids. DB error => 503 (fail closed)."""
    valid = []
    for i in ids:
        try:
            valid.append(str(uuid.UUID(str(i))))
        except (ValueError, TypeError):
            continue  # not a UUID: stays unresolved => the row fails closed
    if not valid:
        return {}
    try:
        with db.pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT id::text AS id, source_path FROM chunks_meta WHERE id = ANY(%s::uuid[])", (valid,))
            return {r["id"]: r["source_path"] for r in cur.fetchall()}
    except Exception as exc:
        raise HTTPException(503, "office authorization temporarily unavailable") from exc


def _mentions_denied(agent, value, chunk_paths=None) -> bool:
    # A chunk referenced only by UUID counts as its path; unknown UUID fails closed.
    if chunk_paths is not None:
        for ref in _chunk_refs(value, set()):
            path = chunk_paths.get(ref)
            if path is None or office_denied(agent, path):
                return True
    if isinstance(value, str):
        return office_denied(agent, value)
    if isinstance(value, dict):
        return any(_mentions_denied(agent, v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_mentions_denied(agent, v) for v in value)
    return False


def filter_office_output(agent, result, kind):
    """Maintenance endpoints (consolidation, skills mine/proposals, retrieval eval).
    One generic rule, fail closed: any list item that mentions a denied path ANYWHERE
    (source_path, dst_source, evidence refs, hits...) is dropped whole, and global
    totals are removed because they count rows the caller cannot see."""
    if _bypass(agent) or not isinstance(result, dict):
        return result
    refs = _chunk_refs(result, set())
    cp = _chunk_paths(refs) if refs else None
    out = {}
    for key, value in result.items():
        if kind == "retrieval" and key == "per_query" and isinstance(value, list):
            out[key] = [dict(row) if isinstance(row, dict) else row for row in value]  # rows filtered below
        elif isinstance(value, list):
            out[key] = [item for item in value if not _mentions_denied(agent, item, cp)]
        elif isinstance(value, dict) and key not in ("summary", "counts"):
            out[key] = {k: ([i for i in v if not _mentions_denied(agent, i, cp)] if isinstance(v, list) else v)
                        for k, v in value.items()}
        elif key in ("summary", "clusters_found"):
            continue  # global totals include rows the caller cannot see
        elif key in ("count", "counts"):
            continue  # recomputed below from what is actually returned
        else:
            out[key] = value
    if kind == "proposals":
        # A proposal's title/rationale can derive from ANY of its evidence: keep it only
        # if every piece is a memory reference the caller may read (checkpoints are not
        # covered by office privacy, so they fail closed here).
        out["proposals"] = [pr for pr in out.get("proposals", []) if pr.get("evidence") and all(
            isinstance(e, dict) and e.get("kind_group") == "memory" and e.get("ref")
            and not office_denied(agent, e["ref"]) for e in pr["evidence"])]
    if "count" in result:
        main = next((k for k in ("candidates", "decay_candidates", "proposals") if k in out), None)
        out["count"] = len(out[main]) if main else result.get("count")
    if isinstance(result.get("counts"), dict):
        out["counts"] = {k: len(out[k]) for k in result["counts"] if isinstance(out.get(k), list)}
    if kind == "retrieval":
        for row in out.get("per_query", []):
            if isinstance(row, dict):
                row.pop("query", None)
                row.pop("expected_covered", None)
                if isinstance(row.get("hits"), list):
                    row["hits"] = [h for h in row["hits"] if not _mentions_denied(agent, h, cp)]
                    row["retrieved"] = len(row["hits"])
                for k in [k for k, v in row.items() if k != "hits" and _mentions_denied(agent, v)]:
                    row.pop(k)  # any other field naming a private path goes too
    return out


def filter_hits(agent, hits, path_key="source_path"):
    """Safety net for legacy Qdrant points without path_ancestors and list endpoints."""
    kept = [h for h in hits if not office_denied(agent, h.get(path_key) or "")]
    if len(kept) != len(hits):
        _log.warning("office privacy discarded %d hits", len(hits) - len(kept))
    return kept
