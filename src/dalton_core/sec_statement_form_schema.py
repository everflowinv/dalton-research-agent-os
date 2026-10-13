"""Owner-only, transactional extension of the exact periodic-filings enum.

No filing identity, rowid, canonical bytes, value or hash is rewritten. Unknown
DDL is refused; the ordinary append-only triggers and foreign keys survive.
"""
from __future__ import annotations

import re
from pathlib import Path

TABLE = 'coverage_mission_statement_filings'
FORMS = "'10-Q','10-K','10-Q/A','10-K/A','20-F','20-F/A','40-F','40-F/A'"


def _norm(sql):
    return ' '.join(sql.replace('IF NOT EXISTS ', '').replace('"' + TABLE + '"', TABLE).strip().rstrip(';').split())


def migrate(connection, *, preview=False):
    schema = Path(__file__).with_name('coverage_mission_schema.sql').read_text()
    table_sql = re.search(r'CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings \([\s\S]*?\n\);', schema).group()
    # The frozen schema already declares the native annual contract. Replacing
    # its two-form prefix would duplicate the amended forms on every open.
    if table_sql.count("CHECK(form IN (" + FORMS + "))") != 1:
        raise RuntimeError('statement form migration requires the exact native annual contract')
    new_sql = table_sql
    old_sql = new_sql.replace(FORMS, "'10-Q','10-K'")
    amended_sql = new_sql.replace(FORMS, "'10-Q','10-K','10-Q/A','10-K/A'")
    actual = connection.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table',TABLE)).fetchone()
    if actual is None or _norm(actual[0]) not in {_norm(old_sql), _norm(amended_sql), _norm(new_sql)}:
        raise RuntimeError('statement form migration refuses unknown filing DDL')
    trigger_sql = re.findall(r'CREATE TRIGGER IF NOT EXISTS coverage_mission_statement_filings_[\s\S]*?END;', schema)
    expected = {re.search(r'EXISTS (\w+)', sql).group(1):_norm(sql) for sql in trigger_sql}
    held = {row[0]:_norm(row[1]) for row in connection.execute(
        'SELECT name,sql FROM sqlite_master WHERE type=? AND tbl_name=?', ('trigger',TABLE))}
    if held != expected or any(row[0] is not None for row in connection.execute(
            'SELECT sql FROM sqlite_master WHERE type=? AND tbl_name=?', ('index',TABLE))):
        raise RuntimeError('statement form migration refuses unknown trigger/index DDL')
    if _norm(actual[0]) == _norm(new_sql):
        return
    if preview:
        return
    if connection.in_transaction:
        raise RuntimeError('statement form migration requires no open transaction')
    foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    columns = [r[1] for r in connection.execute(f'PRAGMA table_info({TABLE})')]
    names = 'rowid,' + ','.join(columns)
    replacement = TABLE + '_amended_contract'
    legacy_alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    # Other Core triggers legitimately refer to separate authority tables.
    # Rename only this replacement; do not reparse/rewrite unrelated triggers.
    connection.execute('PRAGMA legacy_alter_table=ON')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(new_sql.replace('IF NOT EXISTS ', '').replace('"' + TABLE + '"', TABLE).replace(TABLE, replacement, 1))
        connection.execute(f'INSERT INTO {replacement}({names}) SELECT {names} FROM {TABLE}')
        for left,right in ((TABLE,replacement),(replacement,TABLE)):
            if connection.execute(f'SELECT {names} FROM {left} EXCEPT SELECT {names} FROM {right} LIMIT 1').fetchone() is not None:
                raise RuntimeError('statement form migration changed an original row')
        connection.execute(f'DROP TABLE {TABLE}')
        connection.execute(f'ALTER TABLE {replacement} RENAME TO {TABLE}')
        for sql in trigger_sql:
            connection.execute(sql)
        related = {TABLE}
        for name, sql in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
            if re.search(r'REFERENCES\s+[\"`\[]?' + TABLE + r'\b', sql, re.I):
                related.add(name)
        for name in related:
            if not re.fullmatch(r'[A-Za-z0-9_]+', name):
                raise RuntimeError('statement form migration refuses unknown referencing table')
            if connection.execute(f'PRAGMA foreign_key_check({name})').fetchone() is not None:
                raise RuntimeError('statement form migration broke related foreign keys')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute(f'PRAGMA foreign_keys={int(foreign_keys)}')
        connection.execute(f'PRAGMA legacy_alter_table={int(legacy_alter)}')
