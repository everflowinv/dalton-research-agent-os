"""Content-addressed, bounded raw response spool for connector replay."""

from __future__ import annotations

import hashlib
import fcntl
import gzip
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_SINK_RE = re.compile(r"^raw-sink:([0-9a-f]{64})$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SPOOL_DIRECTORY = "connector-spool"
_MAX_TOTAL_BYTES_ENV = "DALTON_RAW_SPOOL_MAX_TOTAL_BYTES"
_ARCHIVE_AFTER_SECONDS_ENV = "DALTON_RAW_SPOOL_ARCHIVE_AFTER_SECONDS"
_DEFAULT_ARCHIVE_AFTER_SECONDS = 7 * 24 * 60 * 60


class RawSpoolError(Exception):
    pass


class RawSpoolCapacityError(RawSpoolError):
    pass


class RawSpoolLimitExceeded(RawSpoolError):
    pass


def _positive_environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RawSpoolError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise RawSpoolError(f"{name} must be a positive integer")
    return value


def _optional_nonnegative_environment_integer(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RawSpoolError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise RawSpoolError(f"{name} must be a non-negative integer")
    return value


def _spool_root(data_dir: str | Path) -> Path:
    """Resolve the public spool directory without stranding old installations.

    Historically every caller passed ``<state>/connector-spool`` and this
    module appended another ``connector-spool``. New spools use the path the
    caller named. If the historical nested directory already exists, keep
    using it so a deploy cannot make the existing evidence disappear.
    """

    supplied = Path(data_dir).expanduser().resolve()
    if supplied.name != _SPOOL_DIRECTORY:
        return supplied / _SPOOL_DIRECTORY
    historical = supplied / _SPOOL_DIRECTORY
    direct_objects = supplied / "objects"
    historical_objects = historical / "objects"
    if direct_objects.is_dir() and historical_objects.is_dir():
        direct_has_objects = any(direct_objects.glob("*/*"))
        historical_has_objects = any(historical_objects.glob("*/*"))
        if direct_has_objects and historical_has_objects:
            raise RawSpoolError(
                "raw spool has objects in both the direct and historical nested roots"
            )
        if historical_has_objects:
            return historical
        return supplied
    if historical.is_dir() and not direct_objects.exists():
        return historical
    return supplied


def _read_object(objects: Path, content_hash: str) -> bytes:
    if not isinstance(content_hash, str) or _HASH_RE.fullmatch(content_hash) is None:
        raise RawSpoolError("content_hash must be lowercase SHA-256")
    shard = objects / content_hash[:2]
    if not shard.is_dir() or shard.is_symlink():
        raise RawSpoolError("raw object not found")
    plain = shard / content_hash
    archived = shard / f"{content_hash}.gz"
    if plain.is_file() and not plain.is_symlink():
        try:
            return _read_plain_file(plain)
        except FileNotFoundError:
            # Archive publication is fsynced before the plain name is removed.
            # A reader crossing that rename/unlink boundary can use the archive.
            pass
    if archived.is_file() and not archived.is_symlink():
        try:
            raw = _read_archived_file(archived)
        except FileNotFoundError:
            # Restore publishes and fsyncs the plain object before removing the
            # archive. Retry that now-present name once; never bounce between
            # names if another archive/restore transition immediately follows.
            try:
                return _read_plain_file(plain)
            except (FileNotFoundError, OSError) as exc:
                raise RawSpoolError("raw object changed during archive restore") from exc
        except (OSError, EOFError, gzip.BadGzipFile) as exc:
            raise RawSpoolError("archived raw object is corrupt") from exc
        if hashlib.sha256(raw).hexdigest() != content_hash:
            raise RawSpoolError("archived raw object content hash mismatch")
        return raw
    raise RawSpoolError("raw object not found")


def _read_plain_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _read_archived_file(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as compressed, gzip.GzipFile(fileobj=compressed) as stream:
        return stream.read()


@dataclass(frozen=True, slots=True)
class RawObject:
    content_hash: str
    size_bytes: int
    storage_locator: str

    def to_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "storage_locator": self.storage_locator,
        }


class BoundedRawSink:
    """Write-only adapter capability; intentionally exposes no filesystem path."""

    __slots__ = (
        "_file", "_spool", "_temporary", "_limit", "_size", "_digest",
        "_closed", "_sink_ref",
    )

    def __init__(
        self,
        spool: "RawSpool",
        sink_ref: str,
        temporary: Path,
        file_handle: BinaryIO,
        limit: int,
    ) -> None:
        self._spool = spool
        self._sink_ref = sink_ref
        self._temporary = temporary
        self._file = file_handle
        self._limit = limit
        self._size = 0
        self._digest = hashlib.sha256()
        self._closed = False

    @property
    def sink_ref(self) -> str:
        return self._sink_ref

    @property
    def size_bytes(self) -> int:
        return self._size

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self._closed:
            raise RawSpoolError("raw sink is closed")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("raw sink accepts bytes only")
        chunk = bytes(data)
        if self._size + len(chunk) > self._limit:
            self.abort()
            raise RawSpoolLimitExceeded(
                f"raw response exceeds max_response_bytes={self._limit}"
            )
        written = self._file.write(chunk)
        if written != len(chunk):
            self.abort()
            raise RawSpoolError("short write to raw spool")
        self._digest.update(chunk)
        self._size += written
        return written

    def finalize(self) -> RawObject:
        if self._closed:
            raise RawSpoolError("raw sink is closed")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._closed = True
        digest = self._digest.hexdigest()
        try:
            return self._spool._finalize(self._temporary, digest, self._size)
        finally:
            # Keep the cross-process activity lock through publication. A
            # collector must not unlink the partial between close and link.
            self._file.close()
            self._spool._release_reservation(self._sink_ref)

    def abort(self) -> None:
        if not self._closed:
            try:
                self._temporary.unlink(missing_ok=True)
            finally:
                self._file.close()
                self._closed = True
        self._spool._release_reservation(self._sink_ref)

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            try:
                self._file.close()
            except Exception:
                pass


class RawSpoolReader:
    """Read existing content-addressed objects without creating or chmoding paths.

    ``data_dir`` has the same meaning as for RawSpool, including its existing
    connector-spool suffix. No write or reservation methods are exposed.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self._root = _spool_root(data_dir)
        self._objects = self._root / "objects"
        if (not self._objects.is_dir() or self._root.is_symlink()
                or self._objects.is_symlink()):
            raise RawSpoolError("existing raw object directory is unavailable")

    def object_exists(self, content_hash: str) -> bool:
        if not isinstance(content_hash, str) or _HASH_RE.fullmatch(content_hash) is None:
            raise RawSpoolError("content_hash must be lowercase SHA-256")
        directory = self._objects / content_hash[:2]
        paths = (directory / content_hash, directory / f"{content_hash}.gz")
        return (directory.is_dir() and not directory.is_symlink()
                and any(path.is_file() and not path.is_symlink() for path in paths))

    def read_object(self, content_hash: str) -> bytes:
        return _read_object(self._objects, content_hash)

    def total_bytes(self) -> int:
        """Physical bytes used by plain and compressed objects, read-only."""

        return sum(
            path.stat().st_size for path in self._objects.glob("*/*")
            if path.is_file() and not path.is_symlink()
        )


class RawSpool:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_total_bytes: int,
        archive_after_seconds: int | None = None,
    ) -> None:
        if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int):
            raise RawSpoolError("max_total_bytes must be a positive integer")
        if max_total_bytes < 1:
            raise RawSpoolError("max_total_bytes must be a positive integer")
        max_total_bytes = _positive_environment_integer(
            _MAX_TOTAL_BYTES_ENV, max_total_bytes
        )
        if archive_after_seconds is None:
            archive_after_seconds = _optional_nonnegative_environment_integer(
                _ARCHIVE_AFTER_SECONDS_ENV
            )
        if (archive_after_seconds is not None
                and (isinstance(archive_after_seconds, bool)
                     or not isinstance(archive_after_seconds, int)
                     or archive_after_seconds < 0)):
            raise RawSpoolError("archive_after_seconds must be a non-negative integer")
        self._root = _spool_root(data_dir)
        self._tmp = self._root / "tmp"
        self._objects = self._root / "objects"
        self._max_total_bytes = max_total_bytes
        self._archive_after_seconds = archive_after_seconds
        self._lock = threading.RLock()
        self._open_reservations: dict[str, int] = {}
        for directory in (self._root, self._tmp, self._objects):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    @contextmanager
    def _temporary_directory_lock(self):
        """Serialize partial creation/claim with collection across processes."""
        descriptor = os.open(self._tmp, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def open_sink(self, sink_ref: str, *, max_response_bytes: int) -> BoundedRawSink:
        match = _SINK_RE.fullmatch(sink_ref)
        if match is None:
            raise RawSpoolError("raw sink ref is not a closed opaque handle")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise RawSpoolError("max_response_bytes must be a positive integer")
        with self._lock, self._temporary_directory_lock():
            used = self.total_bytes()
            reserved = sum(self._open_reservations.values())
            projected = used + reserved + max_response_bytes
            archival = {"archived": 0, "freed_bytes": 0}
            if projected > self._max_total_bytes and self._archive_after_seconds is not None:
                archival = self._archive_old_objects_locked(
                    target_free_bytes=projected - self._max_total_bytes,
                    min_age_seconds=self._archive_after_seconds,
                )
                used = self.total_bytes()
                projected = used + reserved + max_response_bytes
            if projected > self._max_total_bytes:
                raise RawSpoolCapacityError(
                    "raw spool high-water mark reached: "
                    f"used_bytes={used} reserved_bytes={reserved} "
                    f"requested_bytes={max_response_bytes} "
                    f"max_total_bytes={self._max_total_bytes} "
                    f"archived_objects={archival['archived']} "
                    f"archive_freed_bytes={archival['freed_bytes']} "
                    f"capacity_env={_MAX_TOTAL_BYTES_ENV}"
                )
            temporary = self._tmp / f"{match.group(1)}.partial"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(temporary, flags, 0o600)
            except FileExistsError as exc:
                raise RawSpoolError("raw sink already has an unfinished partial") from exc
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                temporary.unlink(missing_ok=True)
                raise
            self._open_reservations[sink_ref] = max_response_bytes
        handle = os.fdopen(descriptor, "wb", buffering=0)
        return BoundedRawSink(
            self, sink_ref, temporary, handle, max_response_bytes
        )

    def _finalize(self, temporary: Path, digest: str, size_bytes: int) -> RawObject:
        shard = self._objects / digest[:2]
        shard.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(shard, 0o700)
        target = shard / digest
        archived = shard / f"{digest}.gz"
        try:
            if archived.is_file() and not archived.is_symlink():
                restored = _read_object(self._objects, digest)
                if len(restored) != size_bytes or hashlib.sha256(restored).hexdigest() != digest:
                    raise RawSpoolError("archived content-addressed object collision")
            else:
                os.link(temporary, target)
                os.chmod(target, 0o600)
        except FileExistsError:
            if target.stat().st_size != size_bytes:
                raise RawSpoolError("content-addressed object size collision")
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        directory_fd = os.open(shard, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return RawObject(
            content_hash=digest,
            size_bytes=size_bytes,
            storage_locator=f"spool:objects/{digest[:2]}/{digest}",
        )

    def total_bytes(self) -> int:
        with self._lock:
            total = 0
            for path in self._objects.glob("*/*"):
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            return total

    def archive_old_objects(
        self, *, target_free_bytes: int = 0, min_age_seconds: int | None = None,
    ) -> dict[str, int]:
        """Losslessly compress old objects and report physical bytes reclaimed.

        The original is unlinked only after the compressed copy has been
        fsynced, published atomically and read back to the same SHA-256 name.
        Readers transparently open either representation.
        """

        if (isinstance(target_free_bytes, bool)
                or not isinstance(target_free_bytes, int)
                or target_free_bytes < 0):
            raise RawSpoolError("target_free_bytes must be a non-negative integer")
        age = (
            self._archive_after_seconds
            if min_age_seconds is None and self._archive_after_seconds is not None
            else (_DEFAULT_ARCHIVE_AFTER_SECONDS
                  if min_age_seconds is None else min_age_seconds)
        )
        if isinstance(age, bool) or not isinstance(age, int) or age < 0:
            raise RawSpoolError("min_age_seconds must be a non-negative integer")
        with self._lock, self._temporary_directory_lock():
            return self._archive_old_objects_locked(
                target_free_bytes=target_free_bytes, min_age_seconds=age
            )

    def _archive_old_objects_locked(
        self, *, target_free_bytes: int, min_age_seconds: int,
    ) -> dict[str, int]:
        cutoff = time.time() - min_age_seconds
        candidates = []
        for path in self._objects.glob("*/*"):
            if (not path.is_file() or path.is_symlink()
                    or _HASH_RE.fullmatch(path.name) is None):
                continue
            metadata = path.stat()
            if metadata.st_mtime <= cutoff:
                candidates.append((metadata.st_mtime, path, metadata.st_size))
        candidates.sort(key=lambda item: (item[0], str(item[1])))
        archived_count = 0
        freed = 0
        for _, source, original_size in candidates:
            temporary = self._tmp / f"archive-{source.name}.gz.partial"
            destination = source.with_name(f"{source.name}.gz")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            digest = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(descriptor, "wb") as output:
                    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as archive:
                        source_descriptor = os.open(
                            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        )
                        with os.fdopen(source_descriptor, "rb") as stream:
                            while chunk := stream.read(1024 * 1024):
                                digest.update(chunk)
                                size += len(chunk)
                                archive.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                compressed_size = temporary.stat().st_size
                if (digest.hexdigest() != source.name or size != original_size):
                    raise RawSpoolError("raw object changed while it was being archived")
                if compressed_size >= original_size:
                    temporary.unlink()
                    continue
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
                self._fsync_directory(source.parent)
                restored = _read_archived_file(destination)
                if hashlib.sha256(restored).hexdigest() != source.name or len(restored) != size:
                    destination.unlink(missing_ok=True)
                    raise RawSpoolError("archived raw object failed read-back verification")
                source.unlink()
                self._fsync_directory(source.parent)
                archived_count += 1
                freed += original_size - compressed_size
                if target_free_bytes and freed >= target_free_bytes:
                    break
            finally:
                temporary.unlink(missing_ok=True)
        return {"archived": archived_count, "freed_bytes": freed}

    def restore_archived_objects(self) -> dict[str, int]:
        """Restore every compressed object for rollback to an older runtime."""

        restored_count = 0
        added = 0
        with self._lock, self._temporary_directory_lock():
            for archived in sorted(self._objects.glob("*/*.gz")):
                digest = archived.name.removesuffix(".gz")
                if (not archived.is_file() or archived.is_symlink()
                        or _HASH_RE.fullmatch(digest) is None):
                    continue
                raw = _read_archived_file(archived)
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise RawSpoolError("archived raw object content hash mismatch")
                target = archived.with_name(digest)
                temporary = self._tmp / f"restore-{digest}.partial"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if target.exists():
                        if target.is_symlink() or target.read_bytes() != raw:
                            raise RawSpoolError("plain raw object collides with archive")
                        temporary.unlink()
                    else:
                        os.replace(temporary, target)
                        os.chmod(target, 0o600)
                    self._fsync_directory(target.parent)
                    archived_size = archived.stat().st_size
                    archived.unlink()
                    self._fsync_directory(target.parent)
                    restored_count += 1
                    added += len(raw) - archived_size
                finally:
                    temporary.unlink(missing_ok=True)
        return {"restored": restored_count, "added_bytes": added}

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def gc_orphans(self) -> int:
        removed = 0
        with self._lock, self._temporary_directory_lock():
            for path in self._tmp.glob("*.partial"):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                except FileNotFoundError:
                    continue
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue  # another downloader still owns this file
                    opened = os.fstat(descriptor)
                    try:
                        current = path.lstat()
                    except FileNotFoundError:
                        continue
                    if (not stat.S_ISREG(opened.st_mode)
                            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                        continue
                    path.unlink()
                    self._open_reservations.pop(
                        f"raw-sink:{path.name.removesuffix('.partial')}", None)
                    removed += 1
                finally:
                    os.close(descriptor)
        return removed

    def _release_reservation(self, sink_ref: str) -> None:
        with self._lock:
            self._open_reservations.pop(sink_ref, None)

    def object_exists(self, content_hash: str) -> bool:
        if not isinstance(content_hash, str) or _HASH_RE.fullmatch(content_hash) is None:
            raise RawSpoolError("content_hash must be lowercase SHA-256")
        shard = self._objects / content_hash[:2]
        paths = (shard / content_hash, shard / f"{content_hash}.gz")
        return any(path.is_file() and not path.is_symlink() for path in paths)

    def read_object(self, content_hash: str) -> bytes:
        return _read_object(self._objects, content_hash)


class MultiRootRawSpoolReader:
    """The objects of one lane, read from whichever root that lane wrote them to.

    P13ap moved the reading side onto "wherever this lane's child actually
    wrote": the AlphaEngine and fetch children are handed the writer's spool,
    the feed and Guidepoint children are not and fall back to their own default
    under the state directory.  Live on 2026-09-18 every one of the 522 queued
    corpus documents' bytes were in ``<state>/connector-spool`` while the
    reading side held only the transcript spool.

    Which root answered is not a question about what is true: an object is
    named by its own hash, and every caller re-hashes what it gets against the
    manifest that named it before a word of it is quoted.  Read-only by
    construction -- there is no write or reservation method here to pick a root
    for.
    """

    def __init__(self, roots) -> None:
        self._roots = list(roots)
        if not self._roots:
            raise RawSpoolError("a multi-root raw spool reader needs at least one root")

    def read_object(self, content_hash: str) -> bytes:
        first = None
        for root in self._roots:
            try:
                return root.read_object(content_hash)
            except Exception as exc:  # noqa: BLE001 - the next root may hold it
                first = first or exc
        raise first

    def object_exists(self, content_hash: str) -> bool:
        return any(root.object_exists(content_hash) for root in self._roots)


#: Everything a caller may hand a correction or candidate authority to read
#: content-addressed objects back out of.  Named rather than duck-typed so that
#: "which objects may a Claim be cited from" stays a closed question.
READABLE_RAW_SPOOLS = (RawSpool, RawSpoolReader, MultiRootRawSpoolReader)


__all__ = [
    "BoundedRawSink", "MultiRootRawSpoolReader", "READABLE_RAW_SPOOLS",
    "RawObject", "RawSpool", "RawSpoolCapacityError",
    "RawSpoolError", "RawSpoolLimitExceeded", "RawSpoolReader",
]
