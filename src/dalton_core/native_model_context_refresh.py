"""Free, source-scoped native context refresh; refusal caches grant no authority."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import canonical_json, content_hash

CONTRACT = 'native-pending-model-context-refresh:0.1'


def scope(connection, state_dir, filing):
    from .statement_context import _latest, _native_unit_normalizer_hash
    from .acquired_sec_filings import SPOOL_ROOTS
    from .raw_spool import RawSpoolReader, RawSpoolError
    refs = filing.get('source_record_refs')
    if refs is None:
        refs = json.loads(filing['source_record_refs_json'])
    objects = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith('raw-sink:'):
            continue
        digest = ref[9:]
        if re.fullmatch('[a-f0-9]{64}', digest) is None:
            raise ValueError('native refresh raw object identity differs')
        for name in SPOOL_ROOTS:
            try:
                reader = RawSpoolReader(Path(state_dir) / name)
            except RawSpoolError:
                objects.append({'spool': name, 'ref': ref, 'present': False})
                continue
            for suffix in ('', '.gz'):
                path = reader._objects / digest[:2] / (digest + suffix)
                try:
                    stat = path.lstat()
                except FileNotFoundError:
                    stat = None
                objects.append({'spool': name, 'ref': ref, 'locator': str(path),
                    'stat': None if stat is None else [stat.st_dev, stat.st_ino,
                        stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]})
    # Kept native evidence contains the complete original member hashes and
    # locators. Hash it independently here; this cache never certifies its
    # numeric validity, which the actual refresh and all consumers reprove.
    evidence = connection.execute('SELECT record_json,content_hash FROM '
        'coverage_mission_statement_native_evidence WHERE ingest_id=?',
        (filing['ingest_id'],)).fetchone()
    held = None
    if evidence is not None:
        held = json.loads(evidence['record_json'])
        if (canonical_json(held) != evidence['record_json']
                or held.get('content_hash') != evidence['content_hash']
                or content_hash({k:v for k,v in held.items() if k != 'content_hash'}) != evidence['content_hash']):
            raise ValueError('native refresh immutable source evidence hash differs')
    version = _latest(connection, filing['ingest_id'])
    current = None if version is None else connection.execute(
        'SELECT content_hash FROM statement_context_versions WHERE version_id=?', (version,)).fetchone()
    body = {'contract_ref': CONTRACT, 'ingest_id': filing['ingest_id'],
        'company_ref': filing['company_ref'], 'filing_hash': filing['content_hash'],
        'raw_refs': refs, 'original_objects': objects,
        'native_evidence': held, 'context_ref': version,
        'context_hash': None if current is None else current['content_hash'],
        'normalizer_hash': _native_unit_normalizer_hash()}
    return {**body, 'content_hash': content_hash(body)}


def previous_refusal(connection, wanted):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='statement_context_versions'").fetchone() is None:
        return None
    rows = connection.execute("SELECT * FROM statement_context_versions "
        "WHERE ingest_id=? AND status='refused' ORDER BY created_at DESC,version_id DESC LIMIT 8",
        (wanted['ingest_id'],)).fetchall()
    for row in rows:
        record = json.loads(row['record_json'])
        if record.get('model_refresh_scope') != wanted:
            continue
        identity = {k:v for k,v in record.items() if k not in {'version_id','content_hash'}}
        if (canonical_json(record) != row['record_json']
                or record.get('content_hash') != row['content_hash']
                or content_hash({k:v for k,v in record.items() if k != 'content_hash'}) != row['content_hash']
                or record.get('version_id') != row['version_id']
                or row['version_id'] != 'statement-context:' + content_hash(identity)[:32]
                or record.get('status') != 'refused'
                or record.get('ingest_id') != wanted['ingest_id']
                or record.get('company_ref') != wanted['company_ref']
                or record.get('filing_content_hash') != wanted['filing_hash']):
            raise ValueError('native refresh refusal cache authority differs')
        return {'ingest_id': row['ingest_id'], 'version_id': row['version_id'],
                'scope_hash': wanted['content_hash'], 'reason': record['reason'], 'reused': True}
    return None
