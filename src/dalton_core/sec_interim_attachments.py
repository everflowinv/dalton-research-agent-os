"""Governed original 6-K attachment material, without financial interpretation."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from .store import content_hash
from .sec_native_annual import member, source_member, MAX_MEMBERS, MAX_TOTAL_BYTES
from .sec_native_interim import descriptor, FORMS

RULE_REF = 'sec-interim-attachment-acquisition:0.1'
RECORD_FILE = 'interim-attachments.json'
MAX_SOURCE_MEMBERS = 8
MAX_INDEX_BYTES = 2_000_000


class InterimAttachmentError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise InterimAttachmentError(message)


def _directory(state_dir, ticket_ref):
    _require(isinstance(ticket_ref, str) and re.fullmatch(r'public-web-fetch:[0-9a-f]{24}', ticket_ref) is not None,
             'interim original ticket identity differs')
    return Path(state_dir) / 'fetches' / ticket_ref.split(':', 1)[1]


def _owner_json(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(1_000_001)
        _require(len(raw) <= 1_000_000, 'interim owner metadata byte bound exceeded')
        return json.loads(raw)
    except FileNotFoundError:
        return None


def identity(connection, spool, discovery_ref, *, company_ref, parent_ref):
    """Rebuild issuer and descriptor from the original admitted submissions."""
    from .sec_submissions_pages import _record, envelope_parameters, submissions_payload, verify_archive_parent
    from .sec_issuer_reporting_profile import profile_from_discovery
    from .public_web_core_fetch import url_authority_from_discovery
    d = _record(connection, 'coverage_mission_source_discoveries', 'record_id', discovery_ref)
    e = _record(connection, 'connector_source_envelopes', 'source_envelope_id', d['source_envelope_ref'])
    inv = _record(connection, 'connector_invocations', 'connector_invocation_id', d['connector_invocation_ref'])
    params = envelope_parameters(connection, e)
    _require(d['company_ref'] == company_ref and d['source_ref'] == 'source:sec-edgar'
        and e['source'] == d['source_ref'] and e['operation'] == 'list_filings'
        and e['status'] in ('complete', 'partial')
        and e['content_hash'] == d['source_envelope_hash'] and inv['content_hash'] == d['connector_invocation_hash']
        and e['connector_invocation_ref'] == inv['id'] and d['parameters'] == params
        and d['query_hash'] == content_hash({'operation': 'list_filings', 'parameters': params})
        and params['form'] in FORMS and parent_ref in d['document_refs'],
        'interim discovery issuer/query/invocation binding differs')
    raw = spool.read_object(e['raw_response_hash'])
    _require(len(raw) <= 4_000_000 and hashlib.sha256(raw).hexdigest() == e['raw_response_hash'],
             'interim submissions original bytes differ')
    payload = json.loads(raw)
    _require('cik' not in payload or str(payload['cik']).zfill(10) == params['issuer'], 'interim original issuer differs')
    root_ref = discovery_ref
    if 'filings' not in payload:
        parent_envelope = verify_archive_parent(connection, spool, params)
        roots = connection.execute('SELECT record_id FROM coverage_mission_source_discoveries '
            'WHERE source_envelope_ref=? AND company_ref=? ORDER BY record_id LIMIT 2',
            (parent_envelope['id'], company_ref)).fetchall()
        _require(len(roots) == 1, 'interim archive has no unique exact root discovery')
        root_ref = roots[0]['record_id']
        payload = submissions_payload(payload, params)
    profile = profile_from_discovery(connection, spool, discovery_ref=root_ref,
        company_ref=company_ref, issuer=params['issuer'], as_of=datetime.fromisoformat(d['created_at']).date())
    _require(profile['family'] in ('foreign_20f', 'foreign_40f'), 'interim reporting family differs')
    recent = payload['filings']['recent']
    fields = ('accessionNumber', 'form', 'filingDate', 'reportDate', 'primaryDocument', 'isXBRL')
    size = len(recent['form'])
    _require(size <= 10000 and all(isinstance(recent.get(k), list) and len(recent[k]) == size for k in fields),
             'interim submissions columns differ')
    accession = parent_ref.removeprefix('sec:filing:')
    positions = [i for i, value in enumerate(recent['accessionNumber']) if value == accession]
    _require(len(positions) == 1, 'interim submissions accession is not unique')
    i = positions[0]
    flag = recent['isXBRL'][i]
    _require(type(flag) is int and flag in (0, 1), 'interim original XBRL flag differs')
    filing = descriptor({'accession': accession, 'form': recent['form'][i],
        'filed': recent['filingDate'][i], 'report_date': recent['reportDate'][i],
        'primary_document': recent['primaryDocument'][i], 'is_xbrl': bool(flag)})
    _require(filing['form'] == params['form'], 'interim form differs from admitted query')
    authority = url_authority_from_discovery(connection, spool, url_ref=parent_ref, source_envelope_ref=e['id'])
    return {'company_ref': company_ref, 'issuer': params['issuer'], 'filing': filing,
            'reporting_profile_hash': profile['content_hash'], 'discovery_ref': d['id'],
            'discovery_hash': d['content_hash'], 'primary_authority': authority}


class _Index(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False; self.depth = 0; self.rows = []; self.cells = None; self.cell = None
        self.count = 0
    def handle_starttag(self, tag, attrs):
        self.count += 1
        _require(self.count <= 50000, 'interim attachment index element bound exceeded')
        attrs = dict(attrs)
        if tag == 'table':
            if self.active:
                raise InterimAttachmentError('interim attachment index contains a nested table')
            self.active = attrs.get('summary') in ('Document Format Files', 'Data Files')
        if not self.active: return
        if tag == 'tr':
            _require(self.cells is None, 'interim attachment index row overlaps')
            self.cells = []
        if tag in ('td', 'th') and self.cells is not None:
            _require(self.cell is None, 'interim attachment index cell overlaps')
            self.cell = {'text': '', 'links': []}
        if tag == 'a' and self.cell is not None:
            self.cell['links'].append(attrs.get('href'))
    def handle_data(self, data):
        if self.active and self.cell is not None: self.cell['text'] += data
    def handle_endtag(self, tag):
        if not self.active: return
        if tag in ('td', 'th') and self.cell is not None:
            self.cells.append(self.cell); self.cell = None
        if tag == 'tr' and self.cells is not None:
            _require(self.cell is None, 'interim attachment index cell is unclosed')
            self.rows.append(self.cells); self.cells = None
            _require(len(self.rows) <= MAX_MEMBERS + 2, 'interim attachment index row bound exceeded')
        if tag == 'table':
            _require(self.cell is None and self.cells is None, 'interim attachment index is unclosed')
            self.active = False


def attachment_index(raw, *, issuer, filing, index_url):
    _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_INDEX_BYTES, 'interim attachment index bytes exceed bound')
    expected = f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{filing["accession"].replace("-", "")}/{filing["accession"]}-index.html'
    _require(index_url == expected, 'interim attachment index URL differs')
    parser = _Index(); parser.feed(raw.decode('utf-8', errors='strict')); parser.close()
    _require(not parser.active and parser.cells is None and parser.cell is None, 'interim attachment index is incomplete')
    result = []
    for cells in parser.rows:
        texts = [' '.join(c['text'].split()) for c in cells]
        if texts in (['Seq', 'Description', 'Document', 'Type', 'Size'],): continue
        _require(len(cells) == 5 and len(cells[2]['links']) == 1, 'interim attachment index row shape differs')
        filename = texts[2]
        link = cells[2]['links'][0]
        _require(isinstance(link, str), 'interim attachment link is absent')
        result.append(member({'document': filename, 'document_type': texts[3],
            'sequence': texts[0], 'description': texts[1], 'url': urljoin(index_url, link)},
            issuer=issuer, accession=filing['accession']))
    _require(result and len(result) <= MAX_MEMBERS and len({m['document'] for m in result}) == len(result)
             and filing['primary_document'] in {m['document'] for m in result},
             'interim attachment index lacks primary or repeats a member')
    return result


def attachment_authority(primary, item, *, issuer, filing):
    """Exact same-accession source link; never a guessed URL or broader host."""
    from .public_web_connector import validate_public_web_url_authority, public_web_url_ref
    primary = validate_public_web_url_authority(dict(primary))
    item = member(item, issuer=issuer, accession=filing['accession'])
    expected_primary = f'https://www.sec.gov/Archives/edgar/data/{int(issuer)}/{filing["accession"].replace("-", "")}/{filing["primary_document"]}'
    _require(primary['canonical_url'] == expected_primary and source_member(item, filing['primary_document']),
             'interim attachment primary/source member differs')
    ref = public_web_url_ref(item['url'])
    body = {**{k: v for k, v in primary.items() if k != 'content_hash'},
            'id': 'public-web-url-authority:' + content_hash({'url_ref': ref,
                'source_envelope_hash': primary['discovery_source_envelope_hash']}),
            'url_ref': ref, 'canonical_url': item['url'], 'host': urlsplit(item['url']).hostname, 'url_hash': ref.removeprefix('public-web-url:sha256:'),
            'search_record_ref': ref}
    return validate_public_web_url_authority({**body, 'content_hash': content_hash(body)})


def _raw(spool, digest):
    raw = spool.read_object(digest)
    _require(hashlib.sha256(raw).hexdigest() == digest, 'interim fetched original bytes differ')
    if raw.startswith(b'\x1f\x8b'):
        # Fetch raw hashes bind the compressed object. Parsed index is bounded independently.
        with gzip.GzipFile(fileobj=__import__('io').BytesIO(raw)) as stream:
            raw = stream.read(MAX_INDEX_BYTES + 1)
    return raw


def acquire(fetch, spool, *, source_identity, primary_receipt, out):
    """Normal fetch owner calls only; never converts material into financial facts."""
    from .sec_filings_index import build_sec_exhibit_url_authority
    result = {'rule_ref': RULE_REF, 'status': 'incomplete', 'provider_calls': 0,
              'source_identity': source_identity, 'index_manifest': None, 'members': [],
              'source_manifests': [], 'failure_reason': None, 'financial_periods_inferred': False}
    try:
        filing = source_identity['filing']; primary = source_identity['primary_authority']
        _require(primary_receipt['outcome'] == 'succeeded', 'interim primary fetch is not successful')
        primary_manifest = fetch.manifest(primary_receipt)
        _require(primary_manifest['canonical_url'] == primary['canonical_url'], 'interim primary receipt URL differs')
        authority = build_sec_exhibit_url_authority(primary, filing['accession'] + '-index.html')
        receipt = fetch.fetch(fetch.build_request(authority, created_at=primary_receipt['created_at']))
        result['provider_calls'] += int(receipt.get('provider_calls') or 0)
        _require(receipt['outcome'] == 'succeeded', 'interim attachment index fetch failed')
        index_manifest = fetch.manifest(receipt); result['index_manifest'] = index_manifest
        _require(index_manifest['final_url'] == authority['canonical_url'], 'interim index final URL differs')
        members = attachment_index(_raw(spool, index_manifest['body_sha256']), issuer=source_identity['issuer'],
            filing=filing, index_url=index_manifest['canonical_url'])
        result['members'] = members
        wanted = [m for m in members if source_member(m, filing['primary_document'])]
        _require(len(wanted) <= MAX_SOURCE_MEMBERS, 'interim source member count exceeds per-child bound; complete acquisition required')
        total = 0
        for i, item in enumerate(wanted):
            if item['document'] == filing['primary_document']:
                manifest = primary_manifest
            else:
                authority = attachment_authority(primary, item, issuer=source_identity['issuer'], filing=filing)
                receipt = fetch.fetch(fetch.build_request(authority, created_at=primary_receipt['created_at']))
                result['provider_calls'] += int(receipt.get('provider_calls') or 0)
                _require(receipt['outcome'] == 'succeeded', 'interim original member fetch failed: ' + item['document'])
                manifest = fetch.manifest(receipt)
            _require(manifest['canonical_url'] == item['url'] and manifest['final_url'] == item['url'], 'interim member manifest URL differs')
            total += manifest['body_bytes']
            _require(total <= MAX_TOTAL_BYTES, 'interim original document-set total exceeds bound')
            result['source_manifests'].append({'member': item, 'manifest': manifest})
        result['status'] = 'succeeded'
    except Exception as exc:
        result['failure_reason'] = type(exc).__name__ + ': ' + str(exc)[:400]
    result['content_hash'] = content_hash(result)
    from .public_web_fetch_cli import _write_owner_only
    _write_owner_only(Path(out) / RECORD_FILE, result)
    for i, pair in enumerate(result['source_manifests']):
        if pair['member']['document'] == source_identity['filing']['primary_document']:
            continue  # the normal parent already has its own manifest/review
        _write_owner_only(Path(out) / f'interim-attachment-{i:03d}-manifest.json', pair['manifest'])
    return result


def verified_sources(core, spool, state_dir, *, ticket_ref, parent_document_ref,
                     company_ref, receipt_reader, max_source_chars=6_000_000,
                     max_pdf_pages=100, max_decompressed_bytes=32_000_000):
    """Rebuild index membership and every individual receipt, not just a JSON hash."""
    from .public_web_extraction_source import verified_public_web_source
    from .public_web_core_fetch import validate_public_web_fetch_manifest
    directory = _directory(state_dir, ticket_ref)
    _require(directory is not None, 'interim acquisition ticket identity differs')
    record = _owner_json(directory / RECORD_FILE)
    if record is None: return []
    _require(isinstance(record, dict) and set(record) == {'rule_ref', 'status', 'provider_calls', 'source_identity',
        'index_manifest', 'members', 'source_manifests', 'failure_reason', 'financial_periods_inferred', 'content_hash'}
             and isinstance(record.get('source_identity'), dict)
             and isinstance(record.get('source_manifests'), list) and 1 <= len(record['source_manifests']) <= MAX_SOURCE_MEMBERS
             and record.get('financial_periods_inferred') is False and record.get('content_hash') == content_hash({k: v for k, v in record.items() if k != 'content_hash'})
             and record.get('rule_ref') == RULE_REF and record.get('status') == 'succeeded',
             'interim acquisition record is incomplete or changed')
    summary = _owner_json(directory / 'summary.json')
    _require(isinstance(summary, dict) and summary.get('status') == 'succeeded'
             and summary.get('url_ref') == parent_document_ref and summary.get('discovery_ref') == record['source_identity']['discovery_ref'],
             'interim acquisition parent summary differs')
    expected = identity(core.connection, spool, summary['discovery_ref'], company_ref=company_ref, parent_ref=parent_document_ref)
    _require(expected == record['source_identity'], 'interim original discovery identity differs')
    class ExactReceipts:
        def __getattr__(self, name):
            reader = getattr(receipt_reader, name)
            def read(ref):
                wire = reader(ref)
                _require(wire is None or isinstance(wire, dict) and wire.get('id') == ref
                    and wire.get('content_hash') == content_hash({k: v for k, v in wire.items() if k != 'content_hash'}),
                    'interim Core receipt hash/identity differs')
                return wire
            return read
    exact_receipts = ExactReceipts()
    def verified(value):
        manifest, rendering = verified_public_web_source(core, spool, value, exact_receipts,
            max_source_chars=max_source_chars, max_pdf_pages=max_pdf_pages, max_decompressed_bytes=max_decompressed_bytes)
        _require(rendering['truncated'] is False, 'interim attachment rendering is truncated')
        return manifest, rendering
    parent, _ = verified(_owner_json(directory / 'manifest.json'))
    _require(parent['canonical_url'] == expected['primary_authority']['canonical_url']
        and parent['final_url'] == parent['canonical_url']
        and parent['url_authority_ref'] == expected['primary_authority']['id']
        and parent['url_authority_hash'] == expected['primary_authority']['content_hash']
        and parent['discovery_source_envelope_ref'] == expected['primary_authority']['discovery_source_envelope_ref']
        and parent['discovery_source_envelope_hash'] == expected['primary_authority']['discovery_source_envelope_hash'],
        'interim parent original URL/discovery authority differs')
    index, _ = verified(record['index_manifest'])
    from .sec_filings_index import build_sec_exhibit_url_authority
    index_authority = build_sec_exhibit_url_authority(expected['primary_authority'], expected['filing']['accession'] + '-index.html')
    _require(index['final_url'] == index_authority['canonical_url']
        and index['url_authority_ref'] == index_authority['id'] and index['url_authority_hash'] == index_authority['content_hash'],
        'interim index exact URL authority differs')
    _require(index['discovery_source_envelope_ref'] == parent['discovery_source_envelope_ref']
             and index['discovery_source_envelope_hash'] == parent['discovery_source_envelope_hash'], 'interim index discovery differs')
    members = attachment_index(_raw(spool, index['body_sha256']), issuer=expected['issuer'], filing=expected['filing'], index_url=index['canonical_url'])
    wanted = [m for m in members if source_member(m, expected['filing']['primary_document'])]
    _require(record['members'] == members and len(wanted) <= MAX_SOURCE_MEMBERS
             and [p['member'] for p in record['source_manifests']] == wanted, 'interim original full member set differs')
    result = []; total = 0
    for pair in record['source_manifests']:
        item = pair['member']; manifest, rendering = verified(pair['manifest'])
        _require(manifest['canonical_url'] == item['url'] and manifest['final_url'] == item['url']
            and manifest['discovery_source_envelope_ref'] == parent['discovery_source_envelope_ref']
            and manifest['discovery_source_envelope_hash'] == parent['discovery_source_envelope_hash'],
            'interim attachment URL/discovery differs')
        expected_authority = attachment_authority(expected['primary_authority'], item, issuer=expected['issuer'], filing=expected['filing'])
        _require(manifest['url_authority_ref'] == expected_authority['id']
            and manifest['url_authority_hash'] == expected_authority['content_hash'], 'interim member exact URL authority differs')
        total += manifest['body_bytes']; _require(total <= MAX_TOTAL_BYTES, 'interim document-set byte bound exceeded')
        doc_ref = parent_document_ref if item['document'] == expected['filing']['primary_document'] else f'sec:interim:{expected["filing"]["accession"]}:{item["document"]}'
        body = {'rule_ref': RULE_REF, 'document_ref': doc_ref, 'company_ref': company_ref,
            'parent_document_ref': parent_document_ref, 'source_identity': expected,
            'index_manifest_ref': index['id'], 'index_manifest_hash': index['content_hash'],
            'index_body_sha256': index['body_sha256'], 'member': item,
            'manifest_ref': manifest['id'], 'manifest_hash': manifest['content_hash'],
            'body_sha256': manifest['body_sha256'], 'raw_artifact_version_ref': manifest['raw_artifact_version_ref']}
        result.append({'document_ref': doc_ref, 'manifest': manifest, 'rendering': rendering,
                       'binding': {**body, 'content_hash': content_hash(body)}})
    return result


def register_held(missions, *, state_dir, spool_dir=None, max_parents=2):
    """Normal owner maintenance: bounded original-to-review handoff, zero fetches."""
    from .connector_authority_port import ReadOnlyConnectorReceiptReader
    from .raw_spool import RawSpoolReader, MultiRootRawSpoolReader, RawSpoolError
    from .document_extraction_launcher import _write_attachment_cursor, _secure_dir
    _require(type(max_parents) is int and 1 <= max_parents <= 2, 'interim handoff parent bound differs')
    state = Path(state_dir); cursor_path = state / 'extractions' / 'interim-attachment-maintenance.json'
    result = {'registered': [], 'refused': [], 'scanned': 0}
    try:
        cursor = _owner_json(cursor_path)
        after = cursor.get('after_record_id', '') if isinstance(cursor, dict) else ''
        _require(isinstance(after, str), 'interim handoff cursor identity differs')
    except (OSError, ValueError):
        after = ''  # advisory selection progress confers no source authority
        result['refused'].append({'reason': 'interim handoff cursor unreadable; bounded selection restarted'})
    query = ("SELECT d.* FROM coverage_mission_discovered_documents d "
        "JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref "
        "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
        "WHERE d.source_ref='source:sec-edgar' AND d.status='acquired' AND d.ticket_ref IS NOT NULL "
        "AND d.document_ref LIKE 'sec:filing:%' "
        "AND s.spec_ref IN ('foreign-disclosure-6k','foreign-disclosure-6k-amendment') "
        "AND d.record_id>? ORDER BY d.record_id LIMIT 32")
    rows = missions.connection.execute(query, (after,)).fetchall()
    if not rows and after: rows = missions.connection.execute(query, ('',)).fetchall()
    attempted = 0; last = None; spool = None
    for parent in rows:
        if attempted >= max_parents: break
        last = parent['record_id']; result['scanned'] += 1
        try:
            directory = _directory(state, parent['ticket_ref'])
            record = _owner_json(directory / RECORD_FILE)
        except (OSError, ValueError):
            result['refused'].append({'parent_record_id': last, 'reason': 'interim original acquisition metadata unreadable'})
            continue
        if not isinstance(record, dict) or record.get('status') != 'succeeded': continue
        # Hints can suppress redundant revalidation only when all exact reviews
        # already exist; they cannot confer authority or create a registration.
        pairs = record.get('source_manifests')
        if not isinstance(pairs, list) or not 1 <= len(pairs) <= MAX_SOURCE_MEMBERS:
            result['refused'].append({'parent_record_id': last, 'reason': 'interim handoff member bound differs'}); continue
        wanted = [f"sec:interim:{parent['document_ref'].removeprefix('sec:filing:')}:{p.get('member', {}).get('document', '')}" for p in pairs
                  if p.get('member', {}).get('document') != record.get('source_identity', {}).get('filing', {}).get('primary_document')]
        held = all(missions.connection.execute('SELECT 1 FROM coverage_mission_discovered_documents d '
            'JOIN coverage_mission_document_reviews r ON r.discovered_document_ref=d.record_id '
            'WHERE d.mission_version_ref=? AND d.ticket_ref=? AND d.document_ref=? AND d.status=\'acquired\' LIMIT 1',
            (parent['mission_version_ref'], parent['ticket_ref'], ref)).fetchone() for ref in wanted)
        if held: continue
        attempted += 1
        try:
            if spool is None:
                roots = []; seen = set()
                for path in (spool_dir, state / 'connector-spool', state / 'transcript-spool'):
                    if path is None or Path(path).resolve() in seen: continue
                    seen.add(Path(path).resolve())
                    try: roots.append(RawSpoolReader(path))
                    except RawSpoolError: continue
                _require(roots, 'interim original spool is unavailable')
                spool = roots[0] if len(roots) == 1 else MultiRootRawSpoolReader(roots)
            mission = missions.mission(parent['mission_version_ref'])
            result['registered'].extend(missions.register_acquired_interim_attachments(last,
                state_dir=state, spool=spool, receipt_reader=ReadOnlyConnectorReceiptReader(missions.connection),
                requested_by=mission['autonomy']['automation_principal']))
        except Exception as exc:
            result['refused'].append({'parent_record_id': last, 'reason': type(exc).__name__ + ': ' + str(exc)[:400]})
    if last is not None:
        _secure_dir(cursor_path.parent); _write_attachment_cursor(cursor_path, last)
    return result
