"""Deadline-bound candidate SELECTs without touching the authority connection.

A disposable read-only connection owns its busy/progress settings. Authority
transactions, paid settlement and existing writer handlers remain untouched.
"""
from contextlib import closing, contextmanager
import sqlite3
import time
from .readonly_sqlite import connect_read_only


class SourceReadDeadline(RuntimeError):
    pass


def bounded_rows(connection, sql, parameters=(), *, deadline=None):
    if deadline is None:
        return connection.execute(sql, parameters).fetchall()
    if time.monotonic() >= deadline:
        raise SourceReadDeadline('source candidate read deadline exhausted')
    if not sql.lstrip().upper().startswith('SELECT '):
        raise ValueError('source candidate budget only supports SELECT')
    filenames = [row[2] for row in connection.execute('PRAGMA database_list')
                 if row[1] == 'main' and row[2]]
    if not filenames:
        # In-memory/offline callers keep their original connection and handler.
        rows = connection.execute(sql, parameters).fetchall()
    else:
        if connection.in_transaction:
            raise SourceReadDeadline('source candidate read awaits authority commit')
        with closing(connect_read_only(filenames[0])) as reader:
            reader.row_factory = connection.row_factory
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            reader.execute(f'PRAGMA busy_timeout={min(100, remaining_ms)}')
            reader.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                rows = reader.execute(sql, parameters).fetchall()
            except sqlite3.OperationalError as exc:
                if (getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                        or (str(exc) in {'database is locked', 'database table is locked'})):
                    raise SourceReadDeadline('source candidate SELECT interrupted or busy') from exc
                raise
    if time.monotonic() >= deadline:
        raise SourceReadDeadline('source candidate read deadline exhausted')
    return rows


@contextmanager
def bounded_read_view(connection, *, deadline=None):
    """A whole pure decision read shares one deadline and one RO snapshot.

    The final check also catches a legacy reader swallowing SQLite INTERRUPT.
    The authority connection, its transactions and progress handler are intact.
    """
    if deadline is None:
        yield connection
        return
    if time.monotonic() >= deadline:
        raise SourceReadDeadline('source decision read deadline exhausted')
    filenames = [row[2] for row in connection.execute('PRAGMA database_list')
                 if row[1] == 'main' and row[2]]
    if not filenames:
        yield connection
        if time.monotonic() >= deadline:
            raise SourceReadDeadline('source decision read deadline exhausted')
        return
    if connection.in_transaction:
        raise SourceReadDeadline('source decision read awaits authority commit')
    with closing(connect_read_only(filenames[0])) as reader:
        reader.row_factory = connection.row_factory
        reader.execute(f'PRAGMA busy_timeout={min(100, max(1, int((deadline-time.monotonic())*1000)))}')
        reader.execute('BEGIN')
        expired = False
        def progress():
            nonlocal expired
            expired = time.monotonic() >= deadline
            return int(expired)
        reader.set_progress_handler(progress, 1000)
        failed = False
        try:
            yield reader
        except sqlite3.OperationalError as exc:
            failed = True
            if (getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                    or str(exc) in {'database is locked', 'database table is locked'}):
                raise SourceReadDeadline('source decision SELECT interrupted or busy') from exc
            raise
        except BaseException:
            failed = True
            raise
        finally:
            if not failed and (expired or time.monotonic() >= deadline):
                raise SourceReadDeadline('source decision read deadline exhausted')
