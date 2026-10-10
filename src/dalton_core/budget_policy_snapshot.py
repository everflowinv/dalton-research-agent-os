"""Guarded reads of immutable budget-policy rows in a checkpointed WAL file.

An empty WAL with absent SHM contains no live pages. Linux may retain that
shape after a writer closes. This does not authorize immutable ledger reads:
only the existing registered-policy validator can use this snapshot.
"""
from pathlib import Path
import sqlite3
from typing import Any


def _fingerprint(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _wal_state(path: Path) -> tuple[int, int, int, int, int] | None:
    if not _present(path):
        return None
    if path.is_symlink():
        raise sqlite3.OperationalError('budget policy snapshot refuses a WAL symlink')
    state = _fingerprint(path)
    if state[2] != 0:
        raise sqlite3.OperationalError('budget policy snapshot refuses a nonempty WAL')
    return state


def read_checkpointed_budget_policy(path: str | Path, policy_ref: str) -> dict[str, Any]:
    """Read one canonical policy, never admissions/usage or uncheckpointed pages.

    Both file identities and absence of SHM must hold before opening and after
    the validated read. No sidecars are created, removed or repaired here.
    """
    from .thesis_impact_budget import ThesisImpactBudgetStore

    if str(path) == ':memory:':
        raise ValueError('budget policy snapshot requires an existing database file')
    target = Path(path).absolute()
    wal, shm = Path(str(target) + '-wal'), Path(str(target) + '-shm')
    before = _fingerprint(target)
    with target.open('rb') as handle:
        header = handle.read(100)
    if header[:16] != b'SQLite format 3\x00' or header[18:20] != b'\x02\x02':
        raise sqlite3.OperationalError('budget policy snapshot requires a WAL database')
    if _present(shm):
        raise sqlite3.OperationalError('cold WAL snapshot requires absent WAL/SHM')
    before_wal = _wal_state(wal)
    connection = sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True,
                                 isolation_level=None)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.row_factory = sqlite3.Row
        result = ThesisImpactBudgetStore.policy_from_connection(connection, policy_ref)
        if (_fingerprint(target) != before or _present(shm)
                or _wal_state(wal) != before_wal):
            raise sqlite3.OperationalError('cold WAL authority changed during immutable snapshot')
        return result
    finally:
        connection.close()
