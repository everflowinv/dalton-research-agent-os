"""Read SQLite headers without closing a database descriptor in this process.

POSIX close() releases every advisory lock this process holds on that inode,
including locks owned by open sqlite3 connections. Read the small header in an
isolated interpreter instead; cache only unchanged file identities, not data.
"""
from collections import OrderedDict
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import RLock

_MAX_HEADERS = 128
_HEADERS: OrderedDict[tuple, bytes] = OrderedDict()
_HEADER_LOCK = RLock()
_READ_SCRIPT = "import sys; f=open(sys.argv[1],'rb'); sys.stdout.buffer.write(f.read(100)); f.close()"


def _fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    state = path.stat()
    return (state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns)


def read_sqlite_header(path: Path) -> bytes:
    """Read at most 100 bytes, with no same-process raw database FD or writes."""
    target = path.absolute()
    before = _fingerprint(target)
    key = (str(target), *before)
    with _HEADER_LOCK:
        header = _HEADERS.get(key)
        if header is not None:
            _HEADERS.move_to_end(key)
            return header
    try:
        result = subprocess.run(
            [sys.executable, '-I', '-c', _READ_SCRIPT, str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise sqlite3.OperationalError('isolated SQLite header read failed') from exc
    if result.returncode or _fingerprint(target) != before:
        raise sqlite3.OperationalError('SQLite file changed or was unavailable during header read')
    header = result.stdout
    with _HEADER_LOCK:
        # Replace prior identities of this path; bounded entries contain only
        # file metadata and headers, never open descriptors or authority rows.
        for prior in tuple(_HEADERS):
            if prior[0] == str(target):
                del _HEADERS[prior]
        _HEADERS[key] = header
        while len(_HEADERS) > _MAX_HEADERS:
            _HEADERS.popitem(last=False)
    return header
