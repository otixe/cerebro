"""Durable, private backups for vault overwrites.

This module deliberately has no HTTP/DB dependency.  The gateway supplies an
already-normalised vault path and owns authorization; this module owns only the
filesystem transaction.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class BackupStoreError(RuntimeError):
    """A safe pre-replace step failed; the caller must leave the original alone."""


class DurabilityUncertainError(BackupStoreError):
    """Replacement happened but fsync of its directory failed."""


class BackupCorruptError(BackupStoreError):
    """A requested historical version is not safe to restore."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _secure_dir(path: Path, *, _managed: bool = True) -> None:
    """Ensure one managed store directory exists, is private, and has a durable entry.

    The immediate parent is synced on *every* successful call, including when the
    directory already existed or another process won the mkdir race.  Existence is
    not proof that a different process completed its durability boundary.  Recursive
    parent preparation never chmods unrelated ancestors; only the requested managed
    directory may have its mode tightened.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        parent = path.parent
        if parent == path:
            raise BackupStoreError("backup store parent does not exist")
        _secure_dir(parent, _managed=False)
        try:
            os.mkdir(path, 0o700)
            st = os.lstat(path)
        except FileExistsError:
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise BackupStoreError("could not inspect backup store directory") from exc
        except OSError as exc:
            raise BackupStoreError("could not create backup store directory") from exc
    except OSError as exc:
        raise BackupStoreError("could not inspect backup store directory") from exc

    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise BackupStoreError("backup store is not a real directory")
    if _managed:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise BackupStoreError("cannot secure backup store permissions") from exc
    # This directory entry is relevant to the transaction even if we did not
    # create it: a concurrent creator may not have reached its fsync yet.
    try:
        _fsync_dir(path.parent)
    except OSError as exc:
        raise BackupStoreError("could not durably confirm backup store directory") from exc


def _write_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.close(fd)


class VaultBackupStore:
    """Opaque version store keyed by the SHA-256 of an absolute normalized path."""

    def __init__(self, root: str | None, *, vault_root: str | None = None,
                 vault_write_base: str | None = None):
        if not root:
            raise BackupStoreError("VAULT_BACKUP_ROOT is required for overwrites")
        # Validate lexical aliases and symlink aliases before _open()/mkdir/lock.
        # `commonpath`, unlike a string prefix, distinguishes /vault from
        # /vault-backups and works for a symlink resolving into the vault.
        root_real = os.path.realpath(root)
        for protected in (vault_root, vault_write_base):
            if protected:
                protected_real = os.path.realpath(protected)
                try:
                    inside = os.path.commonpath((root_real, protected_real)) == protected_real
                except ValueError:  # e.g. different drives on non-POSIX platforms
                    inside = False
                if inside:
                    raise BackupStoreError("VAULT_BACKUP_ROOT must be outside the vault")
        self.root = Path(root_real)

    def _open(self) -> None:
        # A configured path cannot silently become a container overlay/tmp fallback.
        _secure_dir(self.root)
        _secure_dir(self.root / "locks")
        _secure_dir(self.root / "versions")

    @staticmethod
    def _path_key(normalized_path: str) -> str:
        return hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()

    def _version_dir(self, normalized_path: str) -> Path:
        return self.root / "versions" / self._path_key(normalized_path)

    @contextlib.contextmanager
    def lock(self, normalized_path: str) -> Iterator[None]:
        """Cooperative inter-process lock for the whole read/backup/replace chain."""
        self._open()
        key = self._path_key(normalized_path)
        lock_path = self.root / "locks" / (key + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _new_version(self) -> str:
        return secrets.token_hex(24)

    def backup_bytes(self, normalized_path: str, data: bytes) -> dict:
        """Persist bytes before a target write.  A version is visible only after metadata commits."""
        self._open()
        directory = self._version_dir(normalized_path)
        _secure_dir(directory)
        version = self._new_version()
        payload = directory / (version + ".bin")
        metadata = directory / (version + ".json")
        digest = hashlib.sha256(data).hexdigest()
        record = {
            "version": version,
            "path": normalized_path,
            "sha256": digest,
            "bytes": len(data),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_exclusive(payload, data)
            # Metadata is committed last.  A stranded .bin is deliberately not listable.
            fd, tmp_name = tempfile.mkstemp(prefix=".metadata-", suffix=".tmp", dir=directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb", closefd=False) as f:
                    f.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    f.flush()
                    os.fsync(f.fileno())
                os.close(fd)
                fd = -1
                os.replace(tmp_name, metadata)
                _fsync_dir(directory)
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        except (OSError, ValueError) as exc:
            raise BackupStoreError("could not durably preserve the previous version") from exc
        return record

    def list_versions(self, normalized_path: str) -> list[dict]:
        self._open()
        directory = self._version_dir(normalized_path)
        if not directory.is_dir() or directory.is_symlink():
            return []
        result: list[dict] = []
        for entry in directory.glob("*.json"):
            if entry.is_symlink():
                continue
            try:
                record = json.loads(entry.read_text("utf-8"))
                version = record["version"]
                if not isinstance(version, str) or len(version) != 48 or any(c not in "0123456789abcdef" for c in version):
                    continue
                if entry.name != version + ".json" or record.get("path") != normalized_path:
                    continue
                payload = directory / (version + ".bin")
                if payload.is_symlink() or not payload.is_file() or payload.stat().st_size != record.get("bytes"):
                    continue
                result.append({k: record[k] for k in ("version", "sha256", "bytes", "created_at")})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, key=lambda item: (item["created_at"], item["version"]))

    def read_version(self, normalized_path: str, version: str) -> bytes:
        if not isinstance(version, str) or len(version) != 48 or any(c not in "0123456789abcdef" for c in version):
            raise BackupCorruptError("invalid backup version")
        self._open()
        directory = self._version_dir(normalized_path)
        metadata, payload = directory / (version + ".json"), directory / (version + ".bin")
        try:
            if metadata.is_symlink() or payload.is_symlink():
                raise BackupCorruptError("backup version is unsafe")
            record = json.loads(metadata.read_text("utf-8"))
            if record.get("version") != version or record.get("path") != normalized_path:
                raise BackupCorruptError("backup metadata does not match requested path")
            data = payload.read_bytes()
            if record.get("bytes") != len(data) or record.get("sha256") != hashlib.sha256(data).hexdigest():
                raise BackupCorruptError("backup integrity check failed")
            return data
        except FileNotFoundError as exc:
            raise BackupCorruptError("backup version not found") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise BackupCorruptError("backup version cannot be read safely") from exc


def replace_bytes(target: str, data: bytes, original_mode: int | None) -> None:
    """Atomic same-filesystem replacement; raise honestly if final directory fsync fails."""
    path = Path(target)
    fd, temp_name = tempfile.mkstemp(prefix=".vault-write-", dir=path.parent)
    try:
        os.fchmod(fd, (original_mode if original_mode is not None else 0o600) & 0o777)
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.close(fd)
        fd = -1
        os.replace(temp_name, path)
        try:
            _fsync_dir(path.parent)
        except OSError as exc:
            raise DurabilityUncertainError("write replaced the file but final durability confirmation failed") from exc
    except OSError as exc:
        raise BackupStoreError("could not replace vault file") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
