"""Request-level ranking policy, reused unchanged across archive fallback.

Fallback seed ships as Python under COPY *.py. Primary roots include fixed-depth
FS metadata discovery with bounded refresh; union retains legacy/PG-only seed
and last-good roots. Neither FS nor seed certifies Q-only/log payload coverage.
"""
import re
from dataclasses import dataclass
from qdrant_client.models import FieldCondition, MatchAny
from archivepolicy import ARCHIVED_PATH_PATTERNS, is_archived_path
from policy_seed import POLICY_SEED
from policy_roots import ROOT_CACHE
from policy_retirements import RETIREMENTS

INVENTORY = POLICY_SEED
SESSIONS_REGEX = r'(^|/)sesiones(/|$)'
OFI_NONCURRENT_REGEX = r'(^|/)ofi-[^/]+/([^/]+/)*(historico|archive)(/|$)'
# Mirrors path_ancestors: remove ONE exact prefix, then leading slashes and
# empty components. A surviving obsidian-vault component is a legacy spelling.
NORMALIZED_SOURCE_SQL = ("regexp_replace(ltrim(regexp_replace(source_path, "
                         "'^obsidian-vault/', ''), '/'), '/+', '/', 'g')")
ARCHIVE_REGEX = '|'.join(re.escape(p) for p in ARCHIVED_PATH_PATTERNS)


def ancestors(path):
    sp = path or ''
    if sp.startswith('obsidian-vault/'):
        sp = sp[len('obsidian-vault/'):]
    parts = [p for p in sp.lstrip('/').split('/') if p]
    return ['/'.join(parts[:i])+'/' for i in range(1, len(parts))]


def like_prefix(root):
    # No interpolated values; escape LIKE metacharacters including legacy '_'s.
    return root.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')+'%'


def _roots(key, snapshot):
    # Seed also contains legacy/PG-only roots; never drop them on a healthy FS
    # scan. All paths are exact, new discoveries retain observed casing.
    roots = tuple(sorted((set(INVENTORY[key]) | set(snapshot[key])) - set(RETIREMENTS[key])))
    if len(roots) != len(set(roots)) or any(not r or not r.endswith('/') for r in roots):
        raise ValueError('Invalid policy metadata snapshot: ' + key)
    return roots


@dataclass(frozen=True)
class CandidatePolicy:
    include_sessions: bool
    include_logs: bool
    requested_archived: bool
    sessions: tuple[str, ...]
    ofi_noncurrent: tuple[str, ...]
    logs: tuple[str, ...]
    archive_roots: tuple[str, ...]

    def qdrant_conditions(self, *, pass_include_archived):
        must, must_not = [], []
        condition = FieldCondition(key='path_ancestors', match=MatchAny(any=list(self.sessions)))
        (must if self.include_sessions else must_not).append(condition)
        # ORIGINAL caller flag, NOT fallback flag: noncurrent offices do not
        # re-enter accidentally when the general archive fallback is invoked.
        if not self.requested_archived and self.ofi_noncurrent:
            must_not.append(FieldCondition(key='path_ancestors', match=MatchAny(any=list(self.ofi_noncurrent))))
        if not self.include_logs and self.logs:
            must_not.append(FieldCondition(key='path_ancestors', match=MatchAny(any=list(self.logs))))
        if not pass_include_archived and self.archive_roots:
            must_not.append(FieldCondition(key='path_ancestors', match=MatchAny(any=list(self.archive_roots))))
        return must, must_not

    def pg_conditions(self, *, pass_include_archived):
        conds = [f'{NORMALIZED_SOURCE_SQL} ' + ('~*' if self.include_sessions else '!~*') + ' %(policy_sessions)s']
        params = {'policy_sessions': SESSIONS_REGEX}
        if not self.requested_archived:
            conds.append(f'{NORMALIZED_SOURCE_SQL} !~* %(policy_ofi)s')
            params['policy_ofi'] = OFI_NONCURRENT_REGEX
        if not self.include_logs and self.logs:
            conds.append(f'NOT ({NORMALIZED_SOURCE_SQL} LIKE ANY(%(policy_logs)s))')
            params['policy_logs'] = [like_prefix(r) for r in self.logs]
        if not pass_include_archived:
            # Existing archive policy is raw source substring matching, casefolded.
            # No content/YAML classification and no registry-only components.
            conds.append('LOWER(source_path) !~ %(policy_archive)s')
            params['policy_archive'] = ARCHIVE_REGEX
        return conds, params


def build_candidate_policy(*, include_sessions=False, include_logs=False, include_archived=False):
    snapshot = ROOT_CACHE.snapshot() # once per request, never per fallback/helper
    sessions = _roots('sessions', snapshot)
    ofi = _roots('ofi_history', snapshot)+_roots('ofi_archive', snapshot)
    logs = _roots('logs', snapshot)
    archive = set()
    # Known legacy archived folders excluded PRE-pool, alongside archived Bool.
    # Exact spelling comes from ancestry of known roots, no lowercase payloads.
    for root in sessions+ofi+logs:
        for ancestor in ancestors(root+'__metadata__.md'):
            if is_archived_path('obsidian-vault/'+ancestor):
                archive.add(ancestor)
                break
    return CandidatePolicy(bool(include_sessions), bool(include_logs), bool(include_archived),
                           sessions, ofi, logs, tuple(sorted(archive)))
