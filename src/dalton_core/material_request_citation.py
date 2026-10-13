"""Exact immutable MaterialRequest identity for prose, never financial evidence.

Only this request token is exempt from number scanning. It says nothing about
acquisition success, retry rights, source freshness, or a monetary amount.
"""
import json
import re
from pathlib import Path

from .store import canonical_json, content_hash

CONTRACT = 'material-request-prose-citation:exact-subject-record:1'


def request_citation_resolver(state_dir, subject_ref):
    """Operation-local exact-PK reader: at most twenty section gap identities."""
    from .material_requests import SCHEMA_VERSION, ledger_path, request_digest
    from .planner_paid_output_recovery import _authority_connection
    cache = {}

    def resolve(ref):
        if ref in cache:
            return cache[ref]
        if len(cache) >= 20:
            return None
        cache[ref] = None
        if not isinstance(ref, str) or not re.fullmatch(r'material-request:[0-9a-f]{32}', ref):
            return None
        path = ledger_path(Path(state_dir))
        if not path.is_file():
            return None
        with _authority_connection(path) as connection:
            row = connection.execute('SELECT * FROM material_requests WHERE request_id=?', (ref,)).fetchone()
            if row is None:
                return None
            if len(row['record_json'].encode('utf-8')) > 32768:
                return None
            try:
                body = json.loads(row['record_json'])
                digest = request_digest(body['subject'], body['need'], body['period'])
                projections = (
                    ('digest',digest),('subject_ref',body['subject']),('need_kind',body['need']['kind']),
                    ('period',body['period']),('requester_lane',body['requester']['lane']),
                    ('deadline',body['deadline']),('actor_ref',body['actor_ref']),('created_at',body['created_at']))
            except (KeyError, TypeError, ValueError):
                return None
            if (body.get('schema_version') != SCHEMA_VERSION
                    or canonical_json(body) != row['record_json'] or body.get('id') != ref
                    or body.get('content_hash') != row['content_hash']
                    or content_hash({k:v for k,v in body.items() if k!='content_hash'}) != row['content_hash']
                    or digest != body.get('digest') or ref != 'material-request:' + digest[:32]
                    or body.get('subject') != subject_ref
                    or any(row[column] != value for column,value in projections)):
                return None
            cache[ref] = body
        return cache[ref]
    return resolve


def without_verified_request_tokens(body, gap_items, resolve_request):
    """Mask complete, authority-resolved references belonging to this section."""
    if resolve_request is None:
        return body
    verified = set()
    for item in gap_items:
        ref = item.get('request_ref')
        if item.get('kind') != 'not_acquired' or not ref:
            continue
        record = resolve_request(ref)
        if (not isinstance(record, dict) or record.get('id') != ref
                or record.get('content_hash') != content_hash({k:v for k,v in record.items() if k!='content_hash'})):
            continue
        if item.get('period') is not None and item['period'] != record.get('period'):
            continue
        verified.add(ref)
    if not verified:
        return body
    # Exact known tokens only; a sentence's final full stop is punctuation,
    # while an identifier suffix (including .123) remains an unknown ID.
    pattern = re.compile(r'(?<![A-Za-z0-9_:/.-])(?:' + '|'.join(re.escape(ref) for ref in sorted(verified))
                         + r')(?![A-Za-z0-9_:/-]|\.[A-Za-z0-9])')
    return pattern.sub(lambda match: ' ' * len(match.group()), body)
