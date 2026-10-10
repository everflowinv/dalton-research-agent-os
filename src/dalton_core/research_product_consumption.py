"""Verified unit input receipts: bookkeeping, never a research version authority.

Only original identities actually present in a frozen model input are consumed.
The SQLite journal is append-only and scoped to one state/target/product/unit.
Published versions, quoted references and arrival high-water marks are not
substitutes for these receipts. All planning readers are strictly read-only.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .store import canonical_json, content_hash

SCHEMA_VERSION = 'verified-unit-input-consumption:0.1'
JOURNAL = 'research-product-input-consumption.sqlite'


def scope(product: str, target: str, unit: str, company_refs: Sequence[str]) -> dict[str, Any]:
    if product not in ('dossier', 'industry_framework') or not company_refs or not all(isinstance(x, str) and x for x in (target, unit, *company_refs)):
        raise ValueError('unit input consumption requires a product, exact target and source companies')
    return {'product': product, 'target': target, 'unit': unit,
            'company_refs': sorted(set(company_refs))}


def _open_ro(state: Path):
    path = Path(state) / JOURNAL
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return db


def _receipt(raw: str, digest: str, expected_scope: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(raw)
    if (value.get('schema_version') != SCHEMA_VERSION
            or value.get('role') != 'verified_input_consumption'
            or value.get('scope') != dict(expected_scope)
            or value.get('content_hash') != digest
            or content_hash({k: v for k, v in value.items() if k != 'content_hash'}) != digest):
        raise ValueError('verified unit input receipt identity/hash differs')
    return value


def has_input(state: Path, binding: Mapping[str, Any], input_hash: str) -> bool:
    db = _open_ro(state)
    if db is None:
        return False
    try:
        row = db.execute('SELECT record_json,content_hash FROM unit_input_receipts WHERE scope_hash=? AND input_hash=?',
                         (content_hash(binding), input_hash)).fetchone()
        if row is None:
            return False
        value = _receipt(row[0], row[1], binding)
        if value['snapshot']['input_hash'] != input_hash:
            raise ValueError('unit input consumption hash binding differs')
        return True
    finally:
        db.close()


def pending_arrivals(state: Path, binding: Mapping[str, Any], arrivals: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    db = _open_ro(state)
    if db is None:
        return [dict(x) for x in arrivals]
    try:
        consumed = set()
        candidates = {int(x['arrival_id']): x for x in arrivals}
        ids = sorted({int(x['arrival_id']) for x in arrivals})
        for start in range(0, len(ids), 300):
            batch = ids[start:start+300]
            rows = db.execute('SELECT a.arrival_id,r.record_json,r.content_hash FROM unit_read_arrivals a '
                'JOIN unit_input_receipts r ON r.receipt_id=a.receipt_id WHERE a.scope_hash=? '
                f"AND a.arrival_id IN ({','.join('?' for _ in batch)})", (content_hash(binding), *batch)).fetchall()
            for arrival, raw, digest in rows:
                record = _receipt(raw, digest, binding)
                proof = next((x for x in record['snapshot']['arrivals'] if x['arrival_id'] == arrival), None)
                if proof is None or any(proof.get(k) != candidates[arrival].get(k) for k in ('document_ref', 'digest', 'company_ref')):
                    raise ValueError('unit input consumption arrival binding differs')
                consumed.add(arrival)
        return [dict(x) for x in arrivals if x['arrival_id'] not in consumed]
    finally:
        db.close()


def latest(state: Path, binding: Mapping[str, Any]) -> dict[str, Any] | None:
    db = _open_ro(state)
    if db is None:
        return None
    try:
        row = db.execute('SELECT record_json,content_hash FROM unit_input_receipts WHERE scope_hash=? '
                         'ORDER BY captured_at DESC,receipt_id DESC LIMIT 1', (content_hash(binding),)).fetchone()
        return None if row is None else _receipt(row[0], row[1], binding)
    finally:
        db.close()


def snapshot(state: Path, connection: Any, binding: Mapping[str, Any], *,
             material: Sequence[Mapping[str, Any]], input_data: Mapping[str, Any],
             read_cache: dict | None = None) -> dict[str, Any]:
    """Capture exact read identities before sending; no global arrival cursor.

    A passage binds its indexed original hash. Bare Claim or dossier lineage
    does not prove an original hash and therefore never consumes that original.
    """
    from . import evidence_index as ei
    moment = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    refs = sorted({str(row['ref']) for row in material if row.get('ref')})
    if len(refs) > 4096:
        raise ValueError('unit input reference resource limit exceeded')
    # Only an exact indexed passage proves the original body actually read.
    # Bare Claim lineage and a presently unique indexed body do not prove
    # which original version the historical Claim was extracted from.
    actual = input_data.get('parse_input', {}).get('material') if isinstance(input_data.get('parse_input'), Mapping) else input_data.get('material')
    if actual is None or list(actual) != list(material):
        raise ValueError('consumption material differs from the frozen input')
    documents: dict[str, str] = {}
    for ref in refs:
        if ref.startswith('doc-passage:'):
            passage = ei.passage(state, ref)
            if passage:
                documents[passage['document_ref']] = passage['document_sha256']
    arrivals = []
    with ei._readonly(state) as index:
        if index is not None:
            for ref, digest in sorted(documents.items()):
                rows = index.execute('SELECT * FROM material_arrivals WHERE document_ref=? AND digest=?', (ref, digest)).fetchall()
                for row in rows:
                    item = dict(row)
                    if item['company_ref'] in binding['company_refs']:
                        arrived = datetime.fromisoformat(item['indexed_at'].replace('Z', '+00:00'))
                        if arrived <= datetime.fromisoformat(moment):
                            arrivals.append(item)
    semantic_input = copy.deepcopy(dict(input_data))
    # The previous generated prose is context, not fresh research material.
    semantic_input.pop('prior_body', None)
    semantic_input.pop('prompt_sha', None)
    if isinstance(semantic_input.get('parse_input'), dict):
        semantic_input['parse_input'].pop('prior_body', None)
        if semantic_input.get('unit') not in ('demand_drivers', 'supply_and_cost'):
            semantic_input['parse_input'].pop('classification', None)
    return {'captured_at': moment, 'input_hash': content_hash(semantic_input),
            'exact_input_hash': content_hash(input_data), 'read_refs': refs,
            'read_documents': [{'document_ref': k, 'text_sha256': v} for k,v in sorted(documents.items())],
            'arrivals': sorted(arrivals, key=lambda r: r['arrival_id']),
            'policy_model_baselines': model_baselines(state, binding['company_refs'], read_cache=read_cache)}


def record(state: Path, binding: Mapping[str, Any], captured: Mapping[str, Any], *,
           result: str, producer_refs: Sequence[str], verifier_ref: str,
           independent: bool, verified: bool) -> dict[str, Any]:
    if (not verified or not independent or result not in ('published', 'verified_no_new_evidence')
            or not producer_refs or not all(isinstance(r, str) and r for r in producer_refs)
            or not isinstance(verifier_ref, str) or not verifier_ref):
        raise ValueError('unit input consumption requires completed independent verification')
    if not re.fullmatch('[0-9a-f]{64}', str(captured.get('input_hash', ''))):
        raise ValueError('unit input consumption input hash is invalid')
    if set(captured) - {'captured_at', 'input_hash', 'exact_input_hash', 'read_refs', 'read_documents', 'arrivals', 'baseline_at', 'policy_model_baselines'}:
        raise ValueError('unit input consumption snapshot has unknown fields')
    for name in ('input_hash', 'exact_input_hash'):
        if not re.fullmatch('[0-9a-f]{64}', str(captured.get(name, ''))):
            raise ValueError('unit input consumption snapshot hash is invalid')
    moment = datetime.fromisoformat(str(captured['captured_at']).replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('unit input consumption snapshot time requires a timezone')
    originals = {(x['document_ref'], x['text_sha256']) for x in captured['read_documents']}
    for row in captured['arrivals']:
        if ((row['document_ref'], row['digest']) not in originals
                or row['company_ref'] not in binding['company_refs']
                or datetime.fromisoformat(row['indexed_at'].replace('Z', '+00:00')) > moment):
            raise ValueError('unit input consumption arrival was not in the read snapshot')
    value = {'schema_version': SCHEMA_VERSION, 'role': 'verified_input_consumption',
             'scope': dict(binding), 'snapshot': dict(captured), 'result': result,
             'producer_refs': list(producer_refs), 'verifier_ref': verifier_ref,
             'recorded_at': datetime.now(timezone.utc).isoformat(timespec='microseconds')}
    value['content_hash'] = content_hash(value)
    path = Path(state) / JOURNAL
    db = sqlite3.connect(path, timeout=30)
    path.chmod(0o600)
    try:
        db.executescript('CREATE TABLE IF NOT EXISTS unit_input_receipts ('
            'receipt_id TEXT PRIMARY KEY,scope_hash TEXT NOT NULL,input_hash TEXT NOT NULL,'
            'captured_at TEXT NOT NULL,record_json TEXT NOT NULL,content_hash TEXT NOT NULL,'
            'UNIQUE(scope_hash,input_hash));'
            'CREATE TABLE IF NOT EXISTS unit_read_arrivals ('
            'scope_hash TEXT NOT NULL,arrival_id INTEGER NOT NULL,receipt_id TEXT NOT NULL,'
            'PRIMARY KEY(scope_hash,arrival_id));'
            "CREATE TRIGGER IF NOT EXISTS no_receipt_update BEFORE UPDATE ON unit_input_receipts BEGIN SELECT RAISE(ABORT,'input receipts are append-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS no_receipt_delete BEFORE DELETE ON unit_input_receipts BEGIN SELECT RAISE(ABORT,'input receipts are append-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS no_arrival_update BEFORE UPDATE ON unit_read_arrivals BEGIN SELECT RAISE(ABORT,'read arrivals are append-only'); END;"
            "CREATE TRIGGER IF NOT EXISTS no_arrival_delete BEFORE DELETE ON unit_read_arrivals BEGIN SELECT RAISE(ABORT,'read arrivals are append-only'); END;")
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT record_json,content_hash FROM unit_input_receipts WHERE scope_hash=? AND input_hash=?',
                           (content_hash(binding), captured['input_hash'])).fetchone()
        if prior:
            db.rollback()
            return _receipt(prior[0], prior[1], binding)
        receipt_id = 'unit-input-consumption:' + value['content_hash']
        db.execute('INSERT INTO unit_input_receipts VALUES(?,?,?,?,?,?)',
            (receipt_id, content_hash(binding), captured['input_hash'], captured['captured_at'], canonical_json(value), value['content_hash']))
        for arrival in captured['arrivals']:
            db.execute('INSERT OR IGNORE INTO unit_read_arrivals VALUES(?,?,?)',
                       (content_hash(binding), arrival['arrival_id'], receipt_id))
        db.commit()
        path.chmod(0o600)
        return value
    finally:
        db.close()



def model_baselines(state: Path, companies: Sequence[str], *, read_cache: dict | None = None) -> dict[str, Any]:
    """Strong per-version NTM EPS context for policy, never an original read.

    The existing annual/quarterly EPS projection performs the arithmetic; a
    pair of immutable model IDs/hashes brackets that read. This metadata is
    excluded from semantic prompt identity, and consumes no document arrival.
    """
    from contextlib import contextmanager
    from . import material_sufficiency as ms
    from .cockpit_v2.charts_api import ChartReader, model_payload
    from .valuation_forward import our_eps
    from .model_forecast_driver import validate_forecast_model
    class Reader(ChartReader):
        @contextmanager
        def core(self):
            with super().core() as db:
                if db is not None:
                    db.execute('PRAGMA query_only=ON')
                yield db
    output = {}
    db = ms.open_core_ro(state)
    if db is None:
        return output
    try:
        for company in companies:
            try:
                row = db.execute('SELECT version_id,content_hash,record_json FROM forecast_model_versions '
                    'WHERE company_ref=? ORDER BY version_number DESC LIMIT 1', (company,)).fetchone()
                if not row:
                    continue
                key = ('policy_model_baseline', str(state), company, row['version_id'], row['content_hash'])
                if read_cache is not None and key in read_cache:
                    output[company] = read_cache[key]
                    continue
                wire = validate_forecast_model(json.loads(row['record_json']))
                if wire['id'] != row['version_id'] or wire['content_hash'] != row['content_hash'] or wire['company_ref'] != company:
                    continue
                payload = model_payload(Reader(ms.core_path(state)), {'company': company})
                eps = our_eps(payload, company)
                after = db.execute('SELECT version_id,content_hash FROM forecast_model_versions '
                    'WHERE company_ref=? ORDER BY version_number DESC LIMIT 1', (company,)).fetchone()
                if (not after or tuple(after) != (row['version_id'], row['content_hash'])
                        or payload.get('version_ref') != row['version_id'] or eps.get('status') != 'available'
                        or not eps.get('quarters') or not eps.get('method') or not eps.get('unit')):
                    continue
                value = {'ntm_eps': eps['value'], 'quarters': list(eps['quarters']),
                    'method': eps['method'], 'unit': eps['unit'], 'company_ref': company,
                    'version_ref': row['version_id'], 'content_hash': row['content_hash']}
                output[company] = value
                if read_cache is not None:
                    read_cache[key] = value
            except Exception:  # unreadable/legacy EPS is explicitly unavailable
                continue
    finally:
        db.close()
    return output
