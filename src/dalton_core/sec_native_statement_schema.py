"""Owner-only exact dispatch enum migration; never edits an existing request."""
import re
from pathlib import Path

TABLE = 'coverage_mission_statement_dispatches'
FORMS = "'10-Q','10-K','20-F','40-F'"


def _norm(value):
    return ' '.join(value.replace('IF NOT EXISTS ', '').replace('"'+TABLE+'"', TABLE).strip().rstrip(';').split())


def migrate(connection, *, preview=False):
    schema = Path(__file__).with_name('coverage_mission_schema.sql').read_text()
    new = re.search(r'CREATE TABLE IF NOT EXISTS '+TABLE+r' \([\s\S]*?\n\);', schema).group()
    old = new.replace(FORMS, "'10-Q','10-K'")
    held = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if held is None or _norm(held[0]) not in {_norm(old), _norm(new)}:
        raise RuntimeError('native statement migration refuses unknown dispatch DDL')
    triggers = re.findall(r'CREATE TRIGGER IF NOT EXISTS '+TABLE+r'_[\s\S]*?END;', schema)
    expected_triggers = {re.search(r'EXISTS (\w+)', x).group(1): _norm(x) for x in triggers}
    existing_triggers = {x[0]:_norm(x[1]) for x in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,))}
    indexes = re.findall(r'CREATE INDEX IF NOT EXISTS \w+\s+ON '+TABLE+r'\([\s\S]*?;', schema)
    expected_indexes = {re.search(r'EXISTS (\w+)', x).group(1):_norm(x) for x in indexes}
    existing_indexes = {x[0]:_norm(x[1]) for x in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (TABLE,))}
    if existing_triggers != expected_triggers or existing_indexes != expected_indexes:
        raise RuntimeError('native statement migration refuses unknown dispatch index/trigger DDL')
    if _norm(held[0]) == _norm(new):
        return
    if preview:
        return
    if connection.in_transaction:
        raise RuntimeError('native statement migration requires no open transaction')
    fk = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    names = 'rowid,' + ','.join(x[1] for x in connection.execute('PRAGMA table_info('+TABLE+')'))
    temp = TABLE+'_native_annual_contract'
    connection.execute('PRAGMA foreign_keys=OFF'); connection.execute('PRAGMA legacy_alter_table=ON')
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(new.replace('IF NOT EXISTS ', '').replace(TABLE, temp, 1))
        connection.execute(f'INSERT INTO {temp}({names}) SELECT {names} FROM {TABLE}')
        for left,right in ((temp,TABLE),(TABLE,temp)):
            if connection.execute(f'SELECT {names} FROM {left} EXCEPT SELECT {names} FROM {right} LIMIT 1').fetchone():
                raise RuntimeError('native statement migration changed an existing request')
        connection.execute('DROP TABLE '+TABLE); connection.execute(f'ALTER TABLE {temp} RENAME TO {TABLE}')
        for sql in triggers + indexes: connection.execute(sql)
        for table,sql in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
            if table==TABLE or re.search(r'REFERENCES\s+["`\[]?'+TABLE+r'\b',sql,re.I):
                if not re.fullmatch(r'[A-Za-z0-9_]+',table) or connection.execute('PRAGMA foreign_key_check('+table+')').fetchone():
                    raise RuntimeError('native statement migration broke related foreign keys')
        connection.commit()
    except BaseException:
        connection.rollback(); raise
    finally:
        connection.execute('PRAGMA foreign_keys='+str(fk)); connection.execute('PRAGMA legacy_alter_table='+str(alter))
