"""Non-provisioning SQLite readers for existing model authorities."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Iterator


def _read_header_without_releasing_process_locks(target: Path) -> bytes:
    # Diagnostic experiment: a raw close in this process releases SQLite's
    # POSIX locks even when another sqlite3 connection still owns the file.
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; f=open(sys.argv[1],'rb'); sys.stdout.buffer.write(f.read(100)); f.close()",
         str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=5, check=False,
    )
    if result.returncode:
        raise sqlite3.OperationalError("isolated database header read failed")
    return result.stdout


def connect_read_only(path: str | Path) -> sqlite3.Connection:
    """Open an existing file, never an URI supplied by the caller.

    Do not use immutable=1: these authorities can have live WAL commits.
    SQLite may create WAL/SHM even in mode=ro when a writable directory is
    available. Refuse WAL files without already provisioned sidecars instead
    of creating them or silently ignoring the WAL. Provisioning/recovery is
    the owner's separate writable operation, not a page-read side effect.
    """
    if str(path) == ":memory:":
        raise ValueError("read_only requires an existing database file")
    target = Path(path).absolute()
    header = _read_header_without_releasing_process_locks(target)
    if header[:16] == b"SQLite format 3\x00" and header[18:20] == b"\x02\x02":
        if not all(Path(str(target) + suffix).is_file() for suffix in ("-wal", "-shm")):
            raise sqlite3.OperationalError("read_only WAL requires existing WAL/SHM; no sidecars were created")
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def connect_cold_wal_snapshot(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Read a cleanly checkpointed, sidecar-free WAL database without writes.

    ``connect_read_only`` deliberately refuses this shape: SQLite's ordinary
    read-only WAL open needs a SHM file and may create one when the directory
    is writable.  A cold database has no uncheckpointed WAL to consult, so a
    short immutable snapshot is safe provided the file and absence of both
    sidecars remain unchanged for the entire read.  This narrow helper is for
    immutable records such as registered policy versions, not live ledgers.
    """

    if str(path) == ":memory:":
        raise ValueError("cold WAL snapshot requires an existing database file")
    target = Path(path).absolute()
    sidecars = tuple(Path(str(target) + suffix) for suffix in ("-wal", "-shm"))
    header = _read_header_without_releasing_process_locks(target)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x02\x02":
        raise sqlite3.OperationalError("cold WAL snapshot requires a WAL database")
    if any(item.exists() for item in sidecars):
        raise sqlite3.OperationalError("cold WAL snapshot requires absent WAL/SHM")
    before = target.stat()
    fingerprint = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    connection = sqlite3.connect(
        target.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        yield connection
        after = target.stat()
        if (
            (
                after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns,
            )
            != fingerprint
            or any(item.exists() for item in sidecars)
        ):
            raise sqlite3.OperationalError(
                "cold WAL authority changed during immutable snapshot"
            )
    finally:
        connection.close()
