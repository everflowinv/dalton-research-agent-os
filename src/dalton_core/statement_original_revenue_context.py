"""Recover exact revenue facts omitted by a face-statement projection.

This is a source supplement, not a taxonomy alias or a financial bridge. Every
row keeps its own concept, original duration, complete explicit dimensions and
namespace-defined unit. Unsupported facts cannot silently complete a family.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree as ET

from .store import content_hash
from .sec_filing_units import FilingUnitError, INLINE, MAX_BYTES, _expanded, parse_filing_units

# Revenue-source rules are independently replayable. Rules 1/2 use the old
# exact parser; rule 3 opts into the source-defined fixed-zero transformation.
# Rule 4 audits complete elements, including their parser-defined tails. Old
# proof versions retain their original audit and every existing fact hash.
# No-addition preflight retains existing contexts and their paid identities.
LEGACY_RULE_REF = "statement-original-revenue-context:exact-inline-facts:1"
RECONCILIATION_RULE_REF = "statement-original-revenue-context:exact-inline-facts:2"
FIXED_ZERO_RULE_REF = "statement-original-revenue-context:exact-inline-facts:3"
RULE_REF = "statement-original-revenue-context:exact-inline-facts:4"
# Independent statement components; never top-line aliases or REVENUE roles.
RECONCILIATION_CONCEPTS = frozenset({"us-gaap:RevenueNotFromContractWithCustomer"})


def _concepts(parsed, rule_ref):
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    ns = parsed['namespace_bindings']
    allowed = {concept for concept, role in CONCEPT_ROLES.items() if role == REVENUE}
    if rule_ref in {RULE_REF, FIXED_ZERO_RULE_REF, RECONCILIATION_RULE_REF}:
        allowed.update(RECONCILIATION_CONCEPTS)
    elif rule_ref != LEGACY_RULE_REF:
        raise FilingUnitError('original revenue source rule is unsupported')
    return {_expanded(concept, ns): concept for concept in sorted(allowed)
            if concept.split(':')[0] in ns
            and re.fullmatch(r'https?://fasb.org/us-gaap/\d{4}', ns[concept.split(':')[0]])}


def _qname(expanded, ns):
    uri, local = expanded[1:].split('}', 1)
    prefixes = sorted(p for p, value in ns.items() if p and value == uri)
    if not prefixes:
        raise FilingUnitError('revenue context namespace has no exact source prefix')
    return prefixes[0] + ':' + local


def _audited_facts(raw, parsed, concepts, *, complete_elements=False):
    """Every relevant numeric element must survive the strict shared parser."""
    body = gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw
    if len(body) > MAX_BYTES:
        raise FilingUnitError('revenue XML source exceeds byte limit')
    parsed_hashes = {f['fact_sha256'] for f in parsed['facts']}
    pending, stack, excluded = [], [], []
    usable = set()
    relevant = []
    def audit(node):
        digest = hashlib.sha256(ET.tostring(node, encoding='unicode').encode()).hexdigest()
        # A nil fact has no reported amount. It is not a zero or a
        # member available for arithmetic and is never appended.
        nil = node.get('{http://www.w3.org/2001/XMLSchema-instance}nil')
        if nil not in {'true', '1'}:
            if digest not in parsed_hashes:
                raise FilingUnitError('original revenue fact lacks exact numeric/context/unit parse')
            usable.add(digest)
    for event, node in ET.iterparse(io.BytesIO(body), events=('start', 'end', 'start-ns')):
        if event == 'start-ns':
            pending.append(node)
        elif event == 'start':
            scope = dict(stack[-1]) if stack else {}
            scope.update(pending); pending.clear(); stack.append(scope)
            local = node.tag.rsplit('}', 1)[-1] if isinstance(node.tag, str) else ''
            excluded.append((excluded[-1] if excluded else False) or local in {'script', 'style'})
        else:
            if not excluded[-1] and node.tag in {'{' + uri + '}nonFraction' for uri in INLINE}:
                try:
                    name = _expanded(node.get('name') or '', stack[-1])
                except FilingUnitError:
                    if (node.get('name') or '').rsplit(':', 1)[-1] in {c.split('}', 1)[1] for c in concepts}:
                        raise FilingUnitError('original revenue fact has an unbound concept namespace')
                    name = None
                if name in concepts:
                    if complete_elements:
                        # ElementTree's end event guarantees children/text,
                        # but its outside-element tail may arrive in the next
                        # read chunk. The shared parser hashes after parsing;
                        # wait for that same complete representation here.
                        relevant.append(node)
                    else:
                        audit(node)
            stack.pop(); excluded.pop()
    for node in relevant:
        audit(node)
    return usable


def _identity(row, ns):
    return (_expanded(row['concept'], ns), row.get('period_start'), row.get('period_end'),
            content_hash(dict(sorted((_expanded(a, ns), _expanded(v, ns))
                                     for a, v in row.get('dimensions', {}).items()))))


def derive_lines(filing, lines, parsed, raw, *, rule_ref=RULE_REF):
    concepts = _concepts(parsed, rule_ref)
    usable = _audited_facts(raw, parsed, concepts, complete_elements=rule_ref == RULE_REF)
    ns = parsed['namespace_bindings']
    known = {}
    for row in lines:
        if row.get('statement') != 'income' or row.get('dimensions') is None:
            continue
        try:
            known.setdefault(_identity(row, ns), []).append(row)
        except FilingUnitError:
            # Unrelated custom concepts remain untouched, not reclassified.
            continue
    candidates = {}
    for fact in parsed['facts']:
        if fact['concept'] not in concepts or fact['fact_sha256'] not in usable:
            continue
        ctx = parsed['contexts'][fact['context_ref']]
        if ctx['cik'] != str(filing['cik']).zfill(10):
            continue
        if not ctx['supported']:
            raise FilingUnitError('original revenue context has unsupported typed dimensions')
        start, end = ctx['period_start'], ctx['period_end']
        if not start:
            raise FilingUnitError('original revenue context lacks a duration')
        try:
            valid = date.fromisoformat(start) <= date.fromisoformat(end) <= date.fromisoformat(filing['report_date'])
        except (TypeError, ValueError) as exc:
            raise FilingUnitError('original revenue period is invalid') from exc
        if not valid:
            raise FilingUnitError('original revenue duration exceeds exact filing report date')
        unit = parsed['units'][fact['unit_ref']]
        if not isinstance(unit['unit'], str) or not re.fullmatch('[A-Z]{3}', unit['unit']):
            raise FilingUnitError('original revenue fact has no exact currency unit')
        dims = dict(sorted((_qname(a, ns), _qname(v, ns)) for a, v in ctx['dimensions'].items()))
        axis, member = next(iter(dims.items())) if len(dims) == 1 else (None, None)
        concept = concepts[fact['concept']]
        row = {'statement': 'income', 'concept': concept, 'label': concept.split(':', 1)[1],
               'level': 1, 'parent_concept': None, 'is_breakdown': bool(dims),
               'dimension_axis': axis, 'dimension_member': member,
               'dimension_count': len(dims), 'dimensions': dims,
               'period_start': start, 'period_end': end, 'value': fact['value'],
               'unit': fact['unit_ref'], 'balance': None}
        key = _identity(row, ns)
        signature = (Decimal(fact['value']), unit['unit'])
        if key in known:
            for old in known[key]:
                canonical = parsed['units'].get(old['unit'], {}).get('unit', old['unit'])
                if (Decimal(str(old['value'])), canonical) != signature:
                    raise FilingUnitError('original revenue differs from existing exact fact identity')
            continue
        matches = candidates.setdefault(key, [])
        if matches and matches[0][2] != signature:
            raise FilingUnitError('original revenue has conflicting exact fact identities')
        matches.append((row, {'fact': fact, 'context': ctx, 'unit': unit}, signature))
    result = [dict(row) for row in lines]
    evidence = []
    for key, matches in sorted(candidates.items()):
        row, proof, _ = min(matches, key=lambda item: (item[1]['fact']['context_ref'], item[1]['fact']['fact_sha256']))
        evidence.append({'ordinal': len(result), 'line_hash': content_hash(row), **proof})
        result.append(row)
    return result, evidence


def supplemental_revenue_lines(connection, state_dir, filing, lines, *, source=None, rule_ref=RULE_REF):
    from .sec_filing_units import _source
    if source is None:
        try:
            binding, raw = _source(connection, state_dir, filing)
        except (ValueError, KeyError, TypeError, OSError):
            # Legacy contexts without a held original keep their existing
            # authority; this supplement cannot fabricate new rows for them.
            return [dict(row) for row in lines], None
    else:
        binding, raw = _source(connection, state_dir, filing, source=source)
    if not raw.startswith(b'\x1f\x8b') and b'nonFraction' not in raw:
        return [dict(row) for row in lines], None
    from .sec_filing_units import NUMERIC_RULE_REF as NUMERIC_RULE, LEGACY_RULE_REF as LEGACY_NUMERIC_RULE
    parsed = parse_filing_units(raw, rule_ref=NUMERIC_RULE if rule_ref in {RULE_REF, FIXED_ZERO_RULE_REF} else LEGACY_NUMERIC_RULE)
    result, evidence = derive_lines(filing, lines, parsed, raw, rule_ref=rule_ref)
    if not evidence:
        return result, None
    proof = {'rule_ref': rule_ref, 'source': binding, 'raw_sha256': parsed['raw_sha256'],
             'parsed_hash': content_hash(parsed), 'base_line_count': len(lines),
             'base_lines_hash': content_hash(lines), 'evidence': evidence,
             'evidence_hash': content_hash(evidence)}
    return result, proof


def validate_revenue_supplement(connection, record, filing):
    proof = record.get('supplementary_original_revenue')
    if proof is None:
        return
    if record.get('kind') != 'complete_snapshot' or proof.get('rule_ref') not in {RULE_REF, FIXED_ZERO_RULE_REF, RECONCILIATION_RULE_REF, LEGACY_RULE_REF}:
        raise FilingUnitError('original revenue supplement contract/kind differs')
    n = proof.get('base_line_count')
    original = [dict(row) for row in record['lines']]
    # The caller has already strongly replayed this complete unit overlay.
    for ev in (record.get('supplementary_units') or {}).get('evidence', []):
        row = original[ev['ordinal']]
        row['unit'] = row.pop('source_unit_ref'); row.pop('unit_definition_hash')
    if type(n) is not int or not 0 <= n < len(original) or content_hash(original[:n]) != proof.get('base_lines_hash'):
        raise FilingUnitError('original revenue prefix binding differs')
    paths = [row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main' and row[2]]
    if len(paths) != 1:
        raise FilingUnitError('original revenue replay requires existing Core file')
    replay, expected = supplemental_revenue_lines(connection, Path(paths[0]).parent, filing,
                                                  original[:n], source=proof['source'], rule_ref=proof['rule_ref'])
    if replay != original or expected != proof:
        raise FilingUnitError('original revenue complete source/fact replay differs')


def prepare_model_revenue_inputs(missions, state_dir, company_ref):
    """Bounded normal-child preflight; old immutable Works are never rewritten.

    A refreshed source changes the state and the child exits stale before any
    model send. The next normal lane tick selects that new state. A no-addition
    check retains the old context and source identity, including paid replays.
    """
    from .statement_context import _latest, prepare_context, StatementContextAuthority
    filings = sorted(missions.statement_filings(company_ref), key=lambda f: (f['filed'], f['ingest_id']))[-8:]
    authority = None
    refreshed = []
    for filing in filings:
        previous = None
        ref = _latest(missions.connection, filing['ingest_id'])
        if ref is not None:
            # Only an inspection skip; all material consumers still use the
            # ordinary context_record strong reader for every source binding.
            row = missions.connection.execute('SELECT record_json,content_hash FROM statement_context_versions WHERE version_id=?', (ref,)).fetchone()
            record = json.loads(row['record_json'])
            previous = record
            asserted = record.pop('content_hash', None)
            if (asserted != row['content_hash'] or asserted != content_hash(record)
                    or record.get('status') != 'available' or record.get('version_id') != ref
                    or record.get('ingest_id') != filing['ingest_id']):
                raise FilingUnitError('original revenue preflight context hash differs')
            if (record.get('supplementary_original_revenue') or {}).get('rule_ref') == RULE_REF:
                continue
        record = prepare_context(missions.connection, state_dir, filing['ingest_id'])
        if record.get('status') != 'available' or not record.get('supplementary_original_revenue'):
            continue
        if previous is not None and previous.get('mapping_hash') == record.get('mapping_hash'):
            # A newer additive rule with no new material must not invalidate
            # an older exact context or buy the old successful request again.
            continue
        if authority is None:
            authority = StatementContextAuthority(missions.store)
        held = authority.backfill(state_dir, filing['ingest_id'])
        if held['status'] != 'available' or held['version_id'] != record['version_id']:
            raise FilingUnitError('original revenue changed during preflight')
        refreshed.append(held['version_id'])
    return {'refreshed': bool(refreshed), 'context_refs': refreshed}
