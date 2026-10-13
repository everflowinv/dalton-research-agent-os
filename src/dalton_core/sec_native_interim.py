"""Original foreign disclosure material; form 6-K never proves a quarter.

Pure source identities for the future governed interim reader. This module
does not fetch, dispatch, register statements or infer financial concepts.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime

from .sec_native_annual import (MAX_MEMBERS, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES,
                                member, source_member)
from .store import content_hash

CONTRACT_REF = 'sec-native-interim-document-set:0.1'
SELECTION_REF = 'sec-native-interim-candidates:0.1'
FORMS = frozenset(('6-K', '6-K/A'))
_FIELDS = frozenset(('accession', 'form', 'filed', 'report_date',
                     'primary_document', 'is_xbrl'))


class NativeInterimError(ValueError):
    pass


def descriptor(value):
    """Preserve the SEC header; its reportDate is not a financial end date."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise NativeInterimError('interim descriptor closed shape differs')
    if (re.fullmatch(r'\d{10}-\d{2}-\d{6}', str(value['accession'])) is None
            or value['form'] not in FORMS or type(value['is_xbrl']) is not bool
            or not isinstance(value['primary_document'], str)
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}', value['primary_document']) is None):
        raise NativeInterimError('interim original descriptor identity differs')
    for key in ('filed', 'report_date'):
        # Some original disclosures have no SEC reportDate. Do not invent one.
        if key == 'report_date' and value[key] == '':
            continue
        try:
            if date.fromisoformat(value[key]).isoformat() != value[key]:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise NativeInterimError('interim original date differs') from exc
    return dict(value)


def candidates(connection, spool, profile, *, company_ref, issuer, as_of,
               date_from, limit=1, cursor=None):
    """One bounded stable page of source candidates, not financial periods.

    A cursor belongs to exactly one admitted submissions root and window.
    A changed root must deliberately start a new selection after consulting
    previously held accession identities; it cannot silently reuse a cursor.
    """
    from .sec_issuer_reporting_profile import verify_profile, MAX_ROOT_BYTES, MAX_RECENT_FILINGS
    if (type(limit) is not int or not 1 <= limit <= 4
            or not isinstance(as_of, date) or isinstance(as_of, datetime)
            or not isinstance(date_from, date) or isinstance(date_from, datetime)
            or date_from > as_of):
        raise NativeInterimError('interim selection window or limit differs')
    profile = verify_profile(connection, spool, profile, company_ref=company_ref,
                             issuer=issuer, as_of=as_of)
    if profile['family'] not in ('foreign_20f', 'foreign_40f'):
        raise NativeInterimError('interim source selection requires a foreign reporting profile')
    reader = spool() if callable(spool) else spool
    raw = reader.read_object(profile['raw_hash'])
    if len(raw) > MAX_ROOT_BYTES or hashlib.sha256(raw).hexdigest() != profile['raw_hash']:
        raise NativeInterimError('interim submissions original bytes differ')
    root = json.loads(raw)
    if str(root.get('cik', '')).zfill(10) != issuer:
        raise NativeInterimError('interim submissions issuer differs')
    recent = root['filings']['recent']
    fields = ('accessionNumber', 'form', 'filingDate', 'reportDate', 'primaryDocument', 'isXBRL')
    size = len(recent['form'])
    if size > MAX_RECENT_FILINGS or any(not isinstance(recent.get(k), list) or len(recent[k]) != size for k in fields):
        raise NativeInterimError('interim submissions columns differ or exceed bound')
    binding = {'contract_ref': SELECTION_REF, 'company_ref': company_ref,
               'issuer': issuer, 'reporting_profile_hash': profile['content_hash'],
               'date_from': date_from.isoformat(), 'as_of': as_of.isoformat()}
    after = None
    if cursor is not None:
        if (not isinstance(cursor, dict) or set(cursor) != {'binding', 'after', 'content_hash'}
                or cursor['binding'] != binding
                or cursor['content_hash'] != content_hash({k: v for k, v in cursor.items() if k != 'content_hash'})
                or not isinstance(cursor['after'], list) or len(cursor['after']) != 2):
            raise NativeInterimError('interim cursor source binding differs')
        after = tuple(cursor['after'])
    chosen = []
    seen = set()
    for i, form in enumerate(recent['form']):
        if form not in FORMS:
            continue
        flag = recent['isXBRL'][i]
        if type(flag) is not int or flag not in (0, 1):
            raise NativeInterimError('interim original XBRL flag differs')
        filing = descriptor({'accession': recent['accessionNumber'][i], 'form': form,
            'filed': recent['filingDate'][i], 'report_date': recent['reportDate'][i],
            'primary_document': recent['primaryDocument'][i], 'is_xbrl': bool(flag)})
        if filing['accession'] in seen:
            raise NativeInterimError('interim submissions repeat an accession')
        seen.add(filing['accession'])
        if date_from.isoformat() <= filing['filed'] <= as_of.isoformat():
            chosen.append(filing)
    chosen.sort(key=lambda f: (f['filed'], f['accession']), reverse=True)
    if after is not None:
        if after not in [(f['filed'], f['accession']) for f in chosen]:
            raise NativeInterimError('interim cursor is not an original selected filing')
        chosen = [f for f in chosen if (f['filed'], f['accession']) < after]
    page = chosen[:limit]
    next_cursor = None
    if len(chosen) > limit:
        body = {'binding': binding, 'after': [page[-1]['filed'], page[-1]['accession']]}
        next_cursor = {**body, 'content_hash': content_hash(body)}
    return {'binding': binding, 'filings': page, 'next_cursor': next_cursor,
            'complete': next_cursor is None, 'financial_periods_inferred': False}


def build_document_set(*, issuer, filing, reporting_profile_hash, members, originals):
    filing = descriptor(filing)
    if (not isinstance(reporting_profile_hash, str)
            or re.fullmatch(r'[0-9a-f]{64}', reporting_profile_hash) is None
            or not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS
            or not isinstance(originals, dict)):
        raise NativeInterimError('interim original index or profile bound differs')
    index = [member(m, issuer=issuer, accession=filing['accession']) for m in members]
    names = [m['document'] for m in index]
    if len(set(names)) != len(names) or filing['primary_document'] not in names:
        raise NativeInterimError('interim original index repeats or lacks the primary')
    required = {m['document'] for m in index if source_member(m, filing['primary_document'])}
    if set(originals) != required:
        raise NativeInterimError('interim source originals are missing or outside the attachment index')
    stored = []
    total = 0
    for name in sorted(required):
        raw = originals[name]
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MEMBER_BYTES:
            raise NativeInterimError('interim original member exceeds byte bound or is missing')
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise NativeInterimError('interim original document set exceeds total bound')
        stored.append({'document': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                       'size_bytes': len(raw), 'base64': base64.b64encode(raw).decode('ascii')})
    body = {'contract_ref': CONTRACT_REF, 'issuer': issuer, 'filing': filing,
            'reporting_profile_hash': reporting_profile_hash, 'members': index,
            'originals': stored, 'admission': 'material_only'}
    return {**body, 'content_hash': content_hash(body)}


def verify_document_set(value, *, issuer, filing, reporting_profile_hash, expected_hash):
    """Rebuild exact bytes; expected_hash must come from the strong source receipt."""
    if (not isinstance(value, dict) or set(value) != {'contract_ref', 'issuer', 'filing',
            'reporting_profile_hash', 'members', 'originals', 'admission', 'content_hash'}
            or value['contract_ref'] != CONTRACT_REF or value['admission'] != 'material_only'
            or value['content_hash'] != expected_hash
            or not isinstance(value['originals'], list) or len(value['originals']) > MAX_MEMBERS):
        raise NativeInterimError('interim document set strong identity differs')
    raws = {}
    for original in value['originals']:
        if (not isinstance(original, dict) or set(original) != {'document', 'sha256', 'size_bytes', 'base64'}
                or original['document'] in raws or not isinstance(original['base64'], str)
                or len(original['base64']) > 4 * ((MAX_MEMBER_BYTES + 2) // 3)):
            raise NativeInterimError('interim stored original closed identity differs')
        try:
            raw = base64.b64decode(original['base64'], validate=True)
        except (ValueError, TypeError) as exc:
            raise NativeInterimError('interim stored original encoding differs') from exc
        if len(raw) != original['size_bytes'] or hashlib.sha256(raw).hexdigest() != original['sha256']:
            raise NativeInterimError('interim stored original bytes differ')
        raws[original['document']] = raw
    rebuilt = build_document_set(issuer=issuer, filing=filing,
        reporting_profile_hash=reporting_profile_hash, members=value['members'], originals=raws)
    if rebuilt != value:
        raise NativeInterimError('interim source receipt/document-set binding differs')
    return rebuilt
