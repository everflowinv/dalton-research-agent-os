"""Recover explicitly presented native balance facts lost by single-role parsing.

The original balance structure must show the same exact date and amount as an
original full-context fact. No balance role is inferred from a concept name.
Original rows remain in place; new rows require the native XML fact proof too.
"""
from __future__ import annotations

import copy
import math
import re
from decimal import Decimal, InvalidOperation

from .store import content_hash

CONTRACT = 'native-statement-membership:0.1'
FIELD = 'supplementary_native_membership'


def _amount(value):
    # A floating parser presentation cell is a comparison, never the stored
    # fact value. Accept only exactly represented integers in its safe range.
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > 2 ** 53:
            return None
    if not isinstance(value, (str, int, float)) or len(str(value)) > 128:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def candidates(parsed, lines):
    """Pure candidates from explicit missing balance presentations, not proof."""
    from .sec_financials_normalise import (canonical_ref, fact_dimensions,
        fact_period, normalise_statement)
    statements = parsed.get('statements') or {}
    structure = (statements.get('balance') or {}).get('structure') or []
    facts = [(role, index, row) for role, value in sorted(statements.items())
             for index, row in enumerate(value.get('facts') or [])]
    present = {(r['concept'], r.get('period_start'), r['period_end'])
               for r in lines if r['statement'] == 'balance'
               and r.get('dimensions') == {} and not r.get('is_breakdown')}
    selected = {}
    for index, row in enumerate(structure):
        if (row.get('abstract') or row.get('is_breakdown') or row.get('dimension_axis')
                or row.get('dimension_member') or row.get('dimension')):
            continue
        concept = canonical_ref(row.get('concept'))
        if not concept:
            continue
        for end, value in sorted(row.items()):
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}', end) is None:
                continue
            from datetime import date
            try:
                date.fromisoformat(end)
            except ValueError:
                continue
            amount = _amount(value)
            key = (concept, None, end)
            if amount is None or key in present:
                continue
            matches = [(role, n, fact) for role, n, fact in facts
                       if canonical_ref(fact.get('concept')) == concept
                       and fact_period(fact) == (None, end)
                       and fact_dimensions(fact) == {}]
            # Conflicting values or units cannot be selected by choosing the
            # candidate that happens to match the presentation.
            identities = {(_amount(f.get('value')), f.get('unit_ref')) for _, _, f in matches}
            if len(identities) != 1 or not matches:
                continue
            found_amount, unit = next(iter(identities))
            if found_amount != amount or not unit:
                continue
            role, fact_index, fact = matches[0]
            normalized = normalise_statement(statement='balance', structure=[row],
                facts=[fact], include_note_segments=False)['lines']
            if len(normalized) != 1 or normalized[0].get('dimensions') != {}:
                continue
            entry = {'line': normalized[0], 'structure_ordinal': index,
                     'structure_hash': content_hash(row), 'fact_statement': role,
                     'fact_ordinal': fact_index, 'fact_hash': content_hash(fact)}
            if key in selected and selected[key]['line'] != entry['line']:
                raise ValueError('native balance membership has conflicting presentations')
            selected.setdefault(key, entry)
    return [selected[key] for key in sorted(selected)]


def supplement(connection, state_dir, filing, parsed, lines):
    from .sec_native_annual import ANNUAL_FORMS
    if filing['form'] not in ANNUAL_FORMS:
        return lines, None
    entries = candidates(parsed, lines)
    if not entries:
        return lines, None
    from .sec_native_filing_units import _document_set, _match_documents
    evidence, docset = _document_set(connection, state_dir, filing)
    additions = [entry['line'] for entry in entries]
    _, matched = _match_documents(docset, additions)
    if {m['ordinal'] for m in matched} != set(range(len(additions))):
        raise ValueError('native balance membership lacks complete original fact proof')
    body = {'contract_ref': CONTRACT, 'filing_hash': filing['content_hash'],
            'raw_ref': evidence['raw_parse_ref'], 'document_set_hash': docset['content_hash'],
            'base_count': len(lines), 'base_hash': content_hash(lines), 'entries': entries}
    return [*lines, *additions], {**body, 'content_hash': content_hash(body)}


def validate(connection, record, filing):
    proof = record.get(FIELD)
    if proof is None:
        return
    if (record.get('kind') != 'complete_snapshot' or not isinstance(proof, dict)
            or proof.get('contract_ref') != CONTRACT
            or proof.get('content_hash') != content_hash({k:v for k,v in proof.items() if k != 'content_hash'})):
        raise ValueError('native balance membership proof differs')
    original = copy.deepcopy(record['lines'])
    # Native unit evidence is independently replayed by context_record first.
    # Restore only its declared unit overlays for the pre-unit membership proof.
    for match in (record.get('supplementary_native_units') or {}).get('evidence', []):
        line = original[match['ordinal']]
        if line.get('unit_definition_hash') != match['content_hash']:
            raise ValueError('native balance membership unit binding differs')
        line['unit'] = line.pop('source_unit_ref')
        line.pop('unit_definition_hash')
    count = proof.get('base_count')
    if type(count) is not int or count < 0 or count >= len(original):
        raise ValueError('native balance membership prefix differs')
    prefix = original[:count]
    if content_hash(prefix) != proof['base_hash']:
        raise ValueError('native balance membership changed preceding rows')
    from pathlib import Path
    from .statement_context import _raw_filing
    paths = [r[2] for r in connection.execute('PRAGMA database_list') if r[1] == 'main' and r[2]]
    if len(paths) != 1:
        raise ValueError('native balance membership needs the original Core location')
    state = Path(paths[0]).parent
    raw_ref, parsed = _raw_filing(state, filing)
    if raw_ref != proof['raw_ref']:
        raise ValueError('native balance membership raw source differs')
    expected, expected_proof = supplement(connection, state, filing, parsed, prefix)
    if original != expected or proof != expected_proof:
        raise ValueError('native balance membership exact source replay differs')
