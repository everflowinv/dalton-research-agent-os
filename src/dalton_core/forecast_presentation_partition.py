"""Independently prove a disclosed partition from an exact held inline table.

An XBRL axis may have several presentations. Membership comes from complete
source tables, never from amounts, labels or a specification-filtered subset.
"""
from __future__ import annotations
import gzip
import hashlib
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from .store import content_hash
from .sec_filing_units import INLINE, FilingUnitError, _expanded, _source, parse_filing_units

RULE_REF = 'rule:filed-presentation-partition:0.1'


def _partition(raw, filing, rows, expected, *, concept, axis, fixed):
    parsed = parse_filing_units(raw)
    ns = parsed['namespace_bindings']
    qconcept, qaxis = _expanded(concept, ns), _expanded(axis, ns)
    qfixed = {_expanded(k, ns): _expanded(v, ns) for k, v in fixed.items()}
    qexpected = {_expanded(m, ns) for m in expected}
    ends = {r.get('period_end') for r in rows}
    if len(ends) != 1 or not rows:
        raise FilingUnitError('presentation scope requires exact current rows')
    latest = next(iter(ends))
    root = ET.fromstring(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)
    parents = {node: parent for parent in root.iter() for node in parent}
    facts = {f['fact_sha256']: f for f in parsed['facts']}
    tables = {}
    for node in root.iter():
        if node.tag not in {'{'+uri+'}nonFraction' for uri in INLINE}:
            continue
        digest = hashlib.sha256(ET.tostring(node, encoding='unicode').encode()).hexdigest()
        fact = facts.get(digest)
        ctx = parsed['contexts'].get(node.get('contextRef'))
        if ctx is None:
            if _expanded(node.get('name') or '', ns) == qconcept:
                raise FilingUnitError('presentation scope includes an unbound target fact context')
            continue
        if (not fact and ctx['period_end'] == latest
                and set(ctx['dimensions']) == set(qfixed) | {qaxis}
                and all(ctx['dimensions'][k] == v for k, v in qfixed.items())
                and _expanded(node.get('name') or '', ns) == qconcept):
            raise FilingUnitError('presentation scope includes an unsupported numeric fact')
        if not fact or fact['concept'] != qconcept:
            continue
        dims = ctx['dimensions']
        if (ctx['period_end'] == latest and ctx['cik'] == str(filing['cik']).zfill(10)
                and set(dims) == set(qfixed) | {qaxis}
                and all(dims[k] == v for k, v in qfixed.items()) and not ctx['supported']):
            raise FilingUnitError('presentation scope includes an unsupported current context')
        if (ctx['period_end'] != latest
                or ctx['cik'] != str(filing['cik']).zfill(10)
                or set(dims) != set(qfixed) | {qaxis}
                or any(dims[k] != v for k, v in qfixed.items())):
            continue
        table = node
        while table in parents and table.tag.rsplit('}', 1)[-1] != 'table':
            table = parents[table]
        if table.tag.rsplit('}', 1)[-1] != 'table':
            continue
        th = hashlib.sha256(ET.tostring(table, encoding='unicode').encode()).hexdigest()
        tables.setdefault(th, []).append((fact, ctx))

    def member_set(entries):
        return {ctx['dimensions'][qaxis] for _, ctx in entries}

    matches = [h for h, entries in tables.items() if member_set(entries) == qexpected]
    if len(matches) != 1:
        raise FilingUnitError('presentation scope needs one complete source table partition')
    chosen = matches[0]
    selected = tables[chosen]
    groups = {}
    for fact, ctx in selected:
        unit = parsed['units'][fact['unit_ref']]['unit']
        groups.setdefault((ctx['period_start'], ctx['period_end'], unit), set()).add(ctx['dimensions'][qaxis])
    if any(members != qexpected for members in groups.values()):
        raise FilingUnitError('presentation scope has incomplete period/unit membership')

    bindings = []
    other_tables = set()
    for row in rows:
        dims = {_expanded(k, ns): _expanded(v, ns) for k, v in (row.get('dimensions') or {}).items()}
        if (_expanded(row['concept'], ns) != qconcept or set(dims) != set(qfixed) | {qaxis}
                or any(dims[k] != v for k, v in qfixed.items()) or row['period_end'] != latest):
            raise FilingUnitError('presentation row differs from exact concept/context family')
        member = dims[qaxis]
        candidates = []
        for th, entries in tables.items():
            # Every omitted current member must belong to an independently
            # disclosed, disjoint table. An overlapping subtotal is not proof.
            if member in qexpected:
                if th != chosen:
                    continue
            elif th == chosen or member_set(entries) & qexpected:
                continue
            for fact, ctx in entries:
                unit = parsed['units'][fact['unit_ref']]['unit']
                if (ctx['dimensions'] == dims and ctx['period_start'] == row.get('period_start')
                        and ctx['period_end'] == row.get('period_end')
                        and Decimal(fact['value']) == Decimal(str(row['value']))
                        and unit == row.get('unit')):
                    candidates.append((th, fact, ctx, unit))
        if not candidates:
            raise FilingUnitError('presentation scope lacks exact current fact/context/unit proof')
        th, fact, ctx, unit = sorted(candidates, key=lambda x: (x[0], x[1]['fact_sha256']))[0]
        if member not in qexpected:
            other_tables.add(th)
        bindings.append({'line_ref': row.get('line_id'), 'table_hash': th,
                         'fact_hash': fact['fact_sha256'], 'context_ref': fact['context_ref'],
                         'context_hash': ctx['context_sha256'], 'unit': unit,
                         'unit_definition_hash': parsed['units'][fact['unit_ref']]['definition_sha256']})
    if not other_tables:
        raise FilingUnitError('presentation scope has no independently disclosed other partition')
    result = {'rule_ref': RULE_REF, 'raw_sha256': parsed['raw_sha256'],
              'selected_table_hash': chosen, 'other_table_hashes': sorted(other_tables),
              'current_row_bindings': bindings}
    result['content_hash'] = content_hash(result)
    return result


def presentation_partition(connection, filing, rows, expected, *, concept, axis, fixed, source=None):
    """Read original authority afresh; accepts no caller-supplied source proof."""
    databases = list(connection.execute('PRAGMA database_list'))
    main = next((row[2] for row in databases if row[1] == 'main'), None)
    if not main:
        raise FilingUnitError('presentation source requires persistent Core identity')
    proof, raw = _source(connection, Path(main).parent, filing, source=source)
    if hashlib.sha256(raw).hexdigest() != proof["manifest"]["body_sha256"]:
        raise FilingUnitError("presentation original bytes differ from exact source receipt")
    result = _partition(raw, filing, rows, expected, concept=concept, axis=axis, fixed=fixed)
    result.pop('content_hash')
    result['source'] = proof
    result['content_hash'] = content_hash(result)
    return result
