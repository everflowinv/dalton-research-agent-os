"""Non-provisioning SQLite readers for existing model authorities."""
from pathlib import Path
import sqlite3


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
    with target.open("rb") as stream:
        header = stream.read(100)
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
