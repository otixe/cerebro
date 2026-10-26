"""Fixed-depth directory-only metadata glob; no backend/file-content discovery.
Monotonic union seed + last-good + current FS (Saruman cda1024b822e1e91).
Only explicit versioned retirements remove roots. FS discovery is NOT Q coverage.
"""
import fnmatch
import json
import logging
import os
import threading
import time
from pathlib import Path
from policy_seed import POLICY_SEED
from policy_retirements import RETIREMENTS

LOG = logging.getLogger(__name__)
KEYS = ('sessions', 'ofi_history', 'ofi_archive', 'logs')
VAULT = Path(os.environ.get('VAULT_ROOT', '/vault')) / 'obsidian-vault'
# Recon maximum observed directory depth6 +ONE fixed margin (approved).
MAX_DIRECTORY_DEPTH = 7
TTL_SECONDS = 240 # startup + periodic/request refresh, strictly <=5min interval
ANCHORS = tuple(json.loads(os.environ.get('POLICY_ROOT_ANCHORS',
    '["50 Agents", "99 Archive", "30 Projects", "harness", "07 Session Memory", "Session Memory"]')))
if any(not isinstance(a, str) or not a or '/' in a or a in ('.', '..') for a in ANCHORS):
    raise ValueError('Invalid POLICY_ROOT_ANCHORS')
LOG_FOLDERS = {'progress', 'continuity', '07 session memory', 'session memory'}


class DiscoveryError(Exception):
    pass


def freeze(data):
    frozen = {}
    for key in KEYS:
        roots = data[key]
        if any(not isinstance(r, str) or not r or not r.endswith('/') for r in roots):
            raise DiscoveryError('invalid root metadata')
        frozen[key] = tuple(sorted(set(roots)))
    return frozen


def directory_glob(vault):
    """Glob selectors over ONLY directories at fixed depth, no **/rglob/files.
    Explicit scandir avoids stdlib glob's SILENT suppression of permission/OSError.
    No symlink traversal (escape/loops); preserve observed spelling for KEYWORD.
    """
    result = {k: set() for k in KEYS}
    stack = []
    with os.scandir(vault) as entries:
        for entry in entries:
            if any(fnmatch.fnmatchcase(entry.name, a) for a in ANCHORS) and entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), (entry.name,)))
    while stack:
        directory, parts = stack.pop()
        leaf = parts[-1].casefold() # classifier only; NEVER lowercase emitted path
        root = '/'.join(parts)+'/'
        if leaf == 'sesiones':
            result['sessions'].add(root)
        if any(p.casefold().startswith('ofi-') and len(p) > 4 for p in parts[:-1]):
            if leaf == 'historico': result['ofi_history'].add(root)
            if leaf == 'archive': result['ofi_archive'].add(root)
        if leaf in LOG_FOLDERS:
            result['logs'].add(root)
        # Raw descendants are covered by Session Memory ancestor, NEVER Rawglobal.
        if len(parts) >= MAX_DIRECTORY_DEPTH:
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), parts+(entry.name,)))
    return freeze(result)


class RootCache:
    def __init__(self, seed=POLICY_SEED, *, vault=VAULT, scan=directory_glob,
                 clock=time.monotonic, retirements=RETIREMENTS):
        self.seed = freeze(seed)
        # Private/legacy seeds remain strict unless explicitly configured otherwise.
        self.expect_nonzero_roots = seed.get('expect_nonzero_roots', True)
        if type(self.expect_nonzero_roots) is not bool:
            raise ValueError('Invalid expect_nonzero_roots: expected boolean')
        self.retirements = freeze(retirements)
        self.vault, self.scan, self.clock = vault, scan, clock
        self.last_good = self._retire(self.seed)
        self.fs_counts = {k: 0 for k in KEYS}
        self.attempted_at = None
        self.good_at = None
        self.error = 'not_refreshed'
        self.generation = 0
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None

    def _retire(self, roots):
        return {k: tuple(r for r in roots[k] if r not in self.retirements[k]) for k in KEYS}

    def refresh(self, *, force=False):
        with self.lock:
            now = self.clock()
            if not force and self.attempted_at is not None and now-self.attempted_at < TTL_SECONDS:
                return {k: self.last_good[k] for k in KEYS}
            try:
                fresh = freeze(self.scan(self.vault))
                # Generic fallback patterns are not observed FS inventory. Once
                # roots are actually observed, total/class loss remains degraded.
                if not any(fresh.values()) and (any(self.fs_counts.values()) or
                        (self.expect_nonzero_roots and any(self.last_good.values()))):
                    raise DiscoveryError('zero roots after nonzero inventory')
                for key in KEYS:
                    if self.fs_counts[key] > 0 and not fresh[key]:
                        raise DiscoveryError('zero roots for previously nonzero '+key)
                merged = {k: tuple(sorted(set(self.seed[k]) | set(self.last_good[k]) | set(fresh[k]))) for k in KEYS}
                self.last_good = self._retire(merged)
                self.fs_counts = {k: len(fresh[k]) for k in KEYS}
                self.good_at = self.clock()
                self.error = None
                self.generation += 1
            except Exception as exc:
                # Do NOT log exception text/path/content: class and safe reason.
                self.error = str(exc) if isinstance(exc, DiscoveryError) else type(exc).__name__
                LOG.error('candidate_policy_inventory ERROR: %s; fallback=last_good_or_seed', self.error)
            self.attempted_at = self.clock()
            return {k: self.last_good[k] for k in KEYS}

    def snapshot(self):
        return self.refresh()

    def detail(self):
        with self.lock:
            now = self.clock()
            age = None if self.attempted_at is None else max(0, now-self.attempted_at)
            error = self.error or ('refresh_stale' if age is not None and age > 300 else None)
            return {'status': 'degraded' if error else 'ok', 'fallback': bool(error),
                    'error': error, 'source': 'seed' if self.good_at is None else 'seed+last_good+FS',
                    'generation': self.generation, 'ttl_seconds': TTL_SECONDS,
                    'max_directory_depth': MAX_DIRECTORY_DEPTH,
                    'seed_counts': {k: len(self.seed[k]) for k in KEYS},
                    'fs_counts': dict(self.fs_counts),
                    'effective_counts': {k: len(self.last_good[k]) for k in KEYS},
                    'retired_counts': {k: len(self.retirements[k]) for k in KEYS},
                    'last_attempt_age_seconds': age,
                    'last_good_age_seconds': None if self.good_at is None else max(0, now-self.good_at),
                    'q_payload_coverage_certified': False}

    def start(self):
        self.refresh(force=True)
        with self.lock:
            if self.thread is not None and self.thread.is_alive(): return
            self.stop_event.clear()
            def worker():
                while not self.stop_event.wait(TTL_SECONDS):
                    self.refresh(force=True)
            self.thread = threading.Thread(target=worker, name='candidate-policy-directory-refresh', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None: self.thread.join(timeout=2)


ROOT_CACHE = RootCache()
