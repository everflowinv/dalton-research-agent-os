"""Content-addressed, bounded raw response spool for connector replay."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


_SINK_RE = re.compile(r"^raw-sink:([0-9a-f]{64})$")


class RawSpoolError(Exception):
    pass


class RawSpoolCapacityError(RawSpoolError):
    pass


class RawSpoolLimitExceeded(RawSpoolError):
    pass


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
        self._file.close()
        self._closed = True
        digest = self._digest.hexdigest()
        try:
            return self._spool._finalize(self._temporary, digest, self._size)
        finally:
            self._spool._release_reservation(self._sink_ref)

    def abort(self) -> None:
        if not self._closed:
            try:
                self._file.close()
            finally:
                self._closed = True
        try:
            self._temporary.unlink()
        except FileNotFoundError:
            pass
        self._spool._release_reservation(self._sink_ref)

    def __del__(self) -> None:
        if not getattr(self, "_closed", True):
            try:
                self._file.close()
            except Exception:
                pass


class RawSpool:
    def __init__(self, data_dir: str | Path, *, max_total_bytes: int) -> None:
        if isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int):
            raise RawSpoolError("max_total_bytes must be a positive integer")
        if max_total_bytes < 1:
            raise RawSpoolError("max_total_bytes must be a positive integer")
        self._root = Path(data_dir).resolve() / "connector-spool"
        self._tmp = self._root / "tmp"
        self._objects = self._root / "objects"
        self._max_total_bytes = max_total_bytes
        self._lock = threading.RLock()
        self._open_reservations: dict[str, int] = {}
        for directory in (self._root, self._tmp, self._objects):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

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
        with self._lock:
            projected = (
                self.total_bytes()
                + sum(self._open_reservations.values())
                + max_response_bytes
            )
            if projected > self._max_total_bytes:
                raise RawSpoolCapacityError("raw spool high-water mark reached")
            temporary = self._tmp / f"{match.group(1)}.partial"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(temporary, flags, 0o600)
            except FileExistsError as exc:
                raise RawSpoolError("raw sink already has an unfinished partial") from exc
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
        try:
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

    def gc_orphans(self) -> int:
        removed = 0
        with self._lock:
            for path in self._tmp.glob("*.partial"):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
                    self._open_reservations.pop(
                        f"raw-sink:{path.name.removesuffix('.partial')}", None
                    )
                    removed += 1
        return removed

    def _release_reservation(self, sink_ref: str) -> None:
        with self._lock:
            self._open_reservations.pop(sink_ref, None)

    def object_exists(self, content_hash: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise RawSpoolError("content_hash must be lowercase SHA-256")
        path = self._objects / content_hash[:2] / content_hash
        return path.is_file() and not path.is_symlink()

    def read_object(self, content_hash: str) -> bytes:
        if not self.object_exists(content_hash):
            raise RawSpoolError("raw object not found")
        return (self._objects / content_hash[:2] / content_hash).read_bytes()


__all__ = [
    "BoundedRawSink", "RawObject", "RawSpool", "RawSpoolCapacityError",
    "RawSpoolError", "RawSpoolLimitExceeded",
]
