"""Shared path archival policy used by retrieval and declarative registry.

``is_archived_path`` preserves the retrieval policy's historic substring semantics.
Registry-only exclusions are component-aware because its source is filesystem paths,
not arbitrary indexed source strings: this prevents names such as
``not-a-backup-plan.md`` from hiding an otherwise live agent definition.
"""
from pathlib import PurePosixPath

# Keep this tuple byte-for-byte semantically equivalent to the previous db.py
# retrieval rule. It is intentionally lower-case because matching case-folds once.
ARCHIVED_PATH_PATTERNS = (
    "/historico/", "/archive/", "/99 archive/", ".trash", "/historical/",
    "/_archivo/", "/archivo/", "/_archive/", "/_deprecated/",
)


def is_archived_path(source_path: str) -> bool:
    """Return whether retrieval treats an indexed source path as archived."""
    sp = (source_path or "").lower()
    return any(pattern in sp for pattern in ARCHIVED_PATH_PATTERNS)


# Registry-only filesystem components. These are deliberately components, not
# arbitrary substrings: an active path mentioning "backup" is not an archive.
REGISTRY_ARCHIVE_COMPONENTS = frozenset({
    "historico", "draft", "backup", "listo", "_archivo", "archive", "99 archive",
})


def is_registry_excluded_path(relative_path: str) -> bool:
    """Return whether a vault-relative definition file is historical/draft material.

    Starts with the shared retrieval policy, then covers existing registry-history
    directories that retrieval does not classify. Case-folding follows retrieval.
    ``.bak`` is a file suffix only, never an arbitrary substring.
    """
    normalized = (relative_path or "").replace("\\", "/")
    if is_archived_path("/" + normalized.lstrip("/")):
        return True
    path = PurePosixPath(normalized)
    if path.name.casefold().endswith((".bak", ".bak.md")):
        return True
    return any(part.casefold() in REGISTRY_ARCHIVE_COMPONENTS for part in path.parts[:-1])
