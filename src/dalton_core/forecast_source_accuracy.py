"""Source-bound accuracy for closed revenue sums; reported values never change.

Interval consistency is not an exact tie or an invented reconciliation. Old
forecasts without this independently versioned proof retain their strict gate.
"""
from __future__ import annotations
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal, localcontext
import gzip, hashlib, re
from pathlib import Path
from xml.etree import ElementTree as ET
from .store import content_hash
RULE_REF = 'rule:filed-source-accuracy-sum:1'


def _refuse(message):
    from .model_forecast_driver import ForecastModelValidationError
    raise ForecastModelValidationError('filed source accuracy: ' + message)


def radius(value, decimals, precision=None):
    if decimals is not None and precision is not None:
        return None
    if decimals == 'INF' or precision == 'INF':
        return Decimal(0)
    if decimals is not None:
        if not isinstance(decimals, str) or not re.fullmatch(r'-?\d{1,3}', decimals):
            return None
        exponent = -int(decimals)
    elif precision is not None:
        if (not isinstance(precision, str) or not re.fullmatch(r'[1-9]\d{0,2}', precision)
                or Decimal(value) == 0):
            return None
        exponent = abs(Decimal(value)).adjusted() - int(precision) + 1
    else:
        return None
    if abs(exponent) > 1000:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return Decimal('0.5').scaleb(exponent)


def _facts(raw, parsed, accession, concepts):
    from .sec_filing_units import INLINE, MAX_BYTES
    body = gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw
    if len(body) > MAX_BYTES:
        _refuse('original accuracy source exceeds byte limit')
    from .statement_original_revenue_context import _audited_facts
    _audited_facts(raw, parsed, concepts)
    attrs = {}
    for node in ET.fromstring(body).iter():
        if node.tag in {'{' + uri + '}nonFraction' for uri in INLINE}:
            digest = hashlib.sha256(ET.tostring(node, encoding='unicode').encode()).hexdigest()
            attrs[digest] = {k: node.get(k) for k in ('decimals', 'precision')}
    out = []
    for fact in parsed['facts']:
        if fact['concept'] not in concepts or fact['fact_sha256'] not in attrs:
            continue
        context, unit = parsed['contexts'][fact['context_ref']], parsed['units'][fact['unit_ref']]
        if not context['supported'] or not unit.get('unit'):
            from .sec_filing_units import FilingUnitError
            raise FilingUnitError('accuracy source has unsupported relevant context or unit')
        accuracy = attrs[fact['fact_sha256']]
        r = radius(fact['value'], accuracy['decimals'], accuracy['precision'])
        identity = {'accession': accession, 'cik': context['cik'], 'concept': fact['concept'],
                    'period_start': context['period_start'], 'period_end': context['period_end'],
                    'dimensions': context['dimensions'], 'unit': unit['unit']}
        out.append({**identity, 'atomic_ref': content_hash(identity), 'value': fact['value'],
                    **accuracy, 'radius': None if r is None else str(r),
                    'fact_sha256': fact['fact_sha256'], 'context_sha256': context['context_sha256'],
                    'unit_definition_sha256': unit['definition_sha256']})
    return sorted(out, key=str)


def _document(connection, state_dir, filing, source, raw, names):
    from .sec_filing_units import _parse_source_units, _expanded
    from .sec_filing_units import NUMERIC_RULE_REF as NUMERIC_RULE
    parsed = _parse_source_units(connection, state_dir, filing, source, raw, rule_ref=NUMERIC_RULE)
    concepts = {_expanded(name, parsed['namespace_bindings']) for name in names
                if name.split(':')[0] in parsed['namespace_bindings']}
    return {'ingest_id': filing['ingest_id'], 'filing_hash': filing['content_hash'],
            'issuer_cik': str(filing['cik']).zfill(10), 'numeric_parse_rule_ref': NUMERIC_RULE,
            'source': source, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'namespace_bindings': parsed['namespace_bindings'], 'concepts': sorted(concepts),
            'facts': _facts(raw, parsed, filing['accession'], concepts)}


def prepare_accuracy(connection, state_dir, spec, table):
    """Read only original accessions used by declared additive identity sums."""
    from .sec_filing_units import _source, _annual_document_set, FilingUnitError
    from .company_model_annual_projection import verify_statement_filing
    declarations = {d['ref']: d for d in spec.get('revenue_drivers') or []}
    names = set()
    for plan in spec.get('driver_formulas') or []:
        ds = [declarations.get(ref) for ref in plan.get('driver_refs') or []]
        if (plan.get('operator') == 'sum' and len(ds) > 1 and all(ds)
                and all(d.get('basis_identity') for d in ds)
                and len({d.get('basis_concept') for d in ds}) == 1):
            names.add(ds[0]['basis_concept'])
    if not names:
        return None
    accessions = set()
    for line in [*(table.get('filed_identity_lines') or []), *(table.get('filed_lines') or [])]:
        if line.get('concept') in names:
            for cell in [*(line.get('cells') or {}).values(), *(line.get('duration_facts') or [])]:
                accessions.update(cell.get('source_accessions') or [])
    sources, unavailable = [], []
    for row in connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? ORDER BY accession', (spec['company_ref'],)):
        filing = dict(row)
        if filing['accession'] not in accessions:
            continue
        verify_statement_filing(connection, filing, allow_legacy_metadata=True)
        try:
            primary, raw = _source(connection, state_dir, filing)
            documents = [(primary, raw)]
            if filing['form'] == '10-K':
                try:
                    documents.append(_annual_document_set(connection, state_dir, filing, primary=primary, primary_raw=raw))
                except FilingUnitError:
                    pass
            sources.extend(_document(connection, state_dir, filing, source, original, names)
                           for source, original in documents)
        except FilingUnitError as exc:
            unavailable.append({'accession': filing['accession'], 'reason': str(exc)})
    proof = {'rule_ref': RULE_REF, 'company_ref': spec['company_ref'],
             'sources': sorted(sources, key=str), 'unavailable_sources': unavailable}
    return {**proof, 'content_hash': content_hash(proof)}


def validate_shape(proof, company_ref):
    if (not isinstance(proof, Mapping) or proof.get('rule_ref') != RULE_REF
            or proof.get('company_ref') != company_ref
            or proof.get('content_hash') != content_hash({k: v for k, v in proof.items() if k != 'content_hash'})):
        _refuse('proof identity or hash differs')
    for source in proof.get('sources') or []:
        if 'issuer_cik' in source and (not isinstance(source['issuer_cik'], str)
                or not re.fullmatch(r'\d{10}', source['issuer_cik'])):
            _refuse('source issuer identity is invalid')
        for fact in source['facts']:
            r = radius(fact['value'], fact['decimals'], fact['precision'])
            if fact['radius'] != (None if r is None else str(r)):
                _refuse('radius differs from reported numeric accuracy')
            identity = {k: fact[k] for k in ('accession', 'cik', 'concept', 'period_start', 'period_end', 'dimensions', 'unit')}
            if content_hash(identity) != fact['atomic_ref']:
                _refuse('atomic fact identity differs')


def validate_originals(connection, record):
    """Re-read exact original receipts; a supplied/rehashed proof is no authority."""
    proof = (record.get('execution_contract') or {}).get('source_accuracy')
    if proof is None:
        return
    validate_shape(proof, record['company_ref'])
    from .sec_filing_units import _source
    from .company_model_annual_projection import verify_statement_filing
    filename = next((str(row[2]) for row in connection.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not filename:
        _refuse('source accuracy requires physical original authority')
    state_dir = Path(filename).parent
    for stored in proof['sources']:
        row = connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?', (stored['ingest_id'],)).fetchone()
        if row is None:
            _refuse('original filing is unavailable')
        filing = dict(row)
        verify_statement_filing(connection, filing, allow_legacy_metadata=True)
        if filing['content_hash'] != stored['filing_hash'] or filing['company_ref'] != record['company_ref']:
            _refuse('original filing identity drifted')
        if ('issuer_cik' in stored
                and stored['issuer_cik'] != str(filing['cik']).zfill(10)):
            _refuse('original filing issuer identity drifted')
        source, raw = _source(connection, state_dir, filing, source=stored['source'])
        from .sec_filing_units import _parse_source_units
        from .sec_filing_units import LEGACY_RULE_REF as LEGACY_NUMERIC_RULE
        parsed = _parse_source_units(connection, state_dir, filing, source, raw,
                                     rule_ref=stored.get('numeric_parse_rule_ref', LEGACY_NUMERIC_RULE))
        expected = {**stored, 'source': source, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
                    'namespace_bindings': parsed['namespace_bindings'],
                    'facts': _facts(raw, parsed, filing['accession'], set(stored['concepts']))}
        if expected != stored:
            _refuse('original fact/context/unit/accuracy does not replay')


def _issuer_cik(proof, source):
    # New proofs bind issuer to the verified filing, independent of public
    # company naming. Old SEC-ref proofs retain their exact matching rule.
    cik = source.get('issuer_cik')
    if cik is not None:
        return cik if isinstance(cik, str) and re.fullmatch(r'\d{10}', cik) else None
    company = str(proof.get('company_ref') or '')
    return company[len('company:sec-cik:'):] if re.fullmatch(r'company:sec-cik:\d{10}', company) else None


def _atomic(proof, *, concept, dimensions, unit, accession, start, end, value):
    from .sec_filing_units import _expanded
    matches = []
    for source in proof['sources']:
        try:
            expanded = _expanded(concept, source['namespace_bindings'])
            dims = {_expanded(a, source['namespace_bindings']): _expanded(m, source['namespace_bindings']) for a, m in dimensions.items()}
        except ValueError:
            continue
        for f in source['facts']:
            if (f['concept'] == expanded and f['dimensions'] == dims and f['accession'] == accession
                    and f['cik'] == _issuer_cik(proof, source)
                    and f['period_start'] == start and f['period_end'] == end
                    and f['unit'].casefold() == unit.casefold()):
                matches.append(f)
    if (not matches or any(f['radius'] is None or Decimal(f['value']) != Decimal(value) for f in matches)
            or len({f['atomic_ref'] for f in matches}) != 1):
        return None
    # Repeated renderings share one uncertainty variable and cannot widen it.
    f = min(matches, key=lambda f: Decimal(f['radius']))
    return {f['atomic_ref']: {'value': Decimal(value), 'radius': Decimal(f['radius']), 'coefficient': Decimal(1)}}


def expression(proof, driver, cell):
    dimensions = (driver.get('statement_identity') or {}).get('dimensions') or {}
    def operand(c, accession):
        return _atomic(proof, concept=driver['concept'], dimensions=dimensions, unit=driver['unit'],
                       accession=accession, start=c['period_start'], end=c['period_end'], value=c['value'])
    if cell.get('basis') == 'reported' and len(cell.get('accessions') or []) == 1 and not cell.get('derived_from'):
        return operand(cell, cell['accessions'][0])
    parts = cell.get('derived_from')
    if cell.get('basis') == 'derived_from_cumulative' and parts is None and len(cell.get('accessions') or []) == 2:
        from .sec_filing_units import _expanded
        candidates = {}
        earlier_end = (date.fromisoformat(cell['period_start']) - timedelta(days=1)).isoformat()
        for source in proof['sources']:
            try:
                concept = _expanded(driver['concept'], source['namespace_bindings'])
                dims = {_expanded(a, source['namespace_bindings']): _expanded(m, source['namespace_bindings']) for a, m in dimensions.items()}
            except ValueError:
                continue
            for f in source['facts']:
                if (f['concept'] == concept and f['dimensions'] == dims
                        and f['cik'] == _issuer_cik(proof, source)
                        and f['accession'] in cell['accessions'] and f['unit'].casefold() == driver['unit'].casefold()
                        and f['period_end'] in {earlier_end, cell['period_end']}):
                    candidates.setdefault(f['period_end'], set()).add((f['period_start'], f['period_end'], f['value'], f['unit'], f['accession']))
        # A 9M filing also contains its direct Q3. Match the two cumulative
        # sources by their identical fiscal start, not by whichever amounts fit.
        pairs = [(a, b) for a in candidates.get(earlier_end, ())
                 for b in candidates.get(cell['period_end'], ()) if a[0] == b[0]]
        if len(pairs) == 1:
            parts = [dict(zip(('period_start', 'period_end', 'value', 'unit', 'accession'), p))
                     for p in pairs[0]]
    if cell.get('basis') != 'derived_from_cumulative' or not isinstance(parts, list) or len(parts) != 2:
        return None
    earlier, later = parts
    if (earlier['period_start'] != later['period_start'] or later['period_end'] != cell['period_end']
            or date.fromisoformat(earlier['period_end']) + timedelta(days=1) != date.fromisoformat(cell['period_start'])
            or earlier['unit'].casefold() != later['unit'].casefold()
            or later['unit'].casefold() != driver['unit'].casefold()
            or Decimal(later['value']) - Decimal(earlier['value']) != Decimal(cell['value'])
            or set(cell.get('accessions') or []) != {p['accession'] for p in parts}):
        return None
    a, b = operand(earlier, earlier['accession']), operand(later, later['accession'])
    return None if a is None or b is None else combine([(b, 1), (a, -1)])


def combine(expressions):
    out = {}
    for expression_, coefficient in expressions:
        for ref, atomic in expression_.items():
            if ref in out and any(out[ref][k] != atomic[k] for k in ('value', 'radius')):
                _refuse('shared fact has conflicting values or accuracy')
            out.setdefault(ref, {**atomic, 'coefficient': Decimal(0)})
            out[ref]['coefficient'] += Decimal(coefficient) * atomic['coefficient']
    return {ref: a for ref, a in out.items() if a['coefficient']}


def assess(expressions):
    with localcontext() as ctx:
        values = [a[k] for a in expressions.values() for k in ('value', 'radius')]
        ctx.prec = max(80, max((v.adjusted() for v in values), default=0)
                       - min((v.as_tuple().exponent for v in values), default=0) + 20)
        delta = sum((a['value'] * a['coefficient'] for a in expressions.values()), Decimal(0))
        bound = sum((a['radius'] * abs(a['coefficient']) for a in expressions.values()), Decimal(0))
        interval = [str(delta - bound), str(delta + bound)]
    return {'status': 'exact' if delta == 0 else 'within_disclosed_accuracy' if abs(delta) <= bound else 'refused',
            'reported_delta': str(delta), 'accuracy_bound': str(bound),
            'difference_interval': interval,
            'atomic_terms': [{k: str(v) if isinstance(v, Decimal) else v for k, v in {'atomic_ref': ref, **a}.items()}
                             for ref, a in sorted(expressions.items())],
            'meaning': 'Reported values unchanged; precision consistency is not an exact tie or a reconciliation amount.'}


def history_assessment(proof, inputs, target, end):
    if proof is None or len(inputs) < 2 or len({d['concept'] for d in inputs + [target]}) != 1:
        return None
    identities = [(d.get('statement_identity') or {}).get('dimensions') or {} for d in inputs]
    if (any(not d for d in identities) or len({content_hash(d) for d in identities}) != len(inputs)
            or (target.get('statement_identity') or {}).get('dimensions')):
        return None
    axes = set(identities[0]);varying = [axis for axis in axes if len({d.get(axis) for d in identities}) > 1]
    from .economic_invariants import ADDITIVE_SEGMENT_AXES
    if len(axes) != 1 or len(varying) != 1 or varying[0] not in ADDITIVE_SEGMENT_AXES or any(set(d) != axes for d in identities):
        return None
    if any(any(d[a] != identities[0][a] for a in axes - {varying[0]}) for d in identities):
        return None
    cells = [next((c for c in d.get('history') or [] if c['period_end'] == end), None) for d in inputs + [target]]
    if (any(c is None for c in cells) or len({(c['period_start'], c['period_end']) for c in cells}) != 1
            or len({tuple(sorted(c.get('accessions') or [])) for c in cells}) != 1
            or any(d.get('statement') != target.get('statement') for d in inputs)):
        return None
    es = [expression(proof, d, c) for d, c in zip(inputs + [target], cells)]
    if any(e is None for e in es):
        return None
    return {'period_end': end, **assess(combine([(e, 1 if i < len(inputs) else -1) for i, e in enumerate(es)]))}


def family_assessment(proof, group, rows):
    """Only a complete flat source family can use reported numeric accuracy."""
    from .economic_invariants import _incomplete_members, ADDITIVE_SEGMENT_AXES
    if (proof is None or group.get('axis') not in ADDITIVE_SEGMENT_AXES
            or group.get('bridge') or group.get('reconciling_items') or _incomplete_members(group)):
        return None
    start, end = group['period'].split('..')
    family = [r for r in rows if r.get('concept') == group['line']
              and r.get('period_start') == start and r.get('period_end') == end]
    members = [str(p['member']) for p in group['parts']]
    if not members or len(set(members)) != len(members):
        return None
    selected = []
    for part in group['parts']:
        matches = [r for r in family if r.get('dimensions') == {group['axis']: part['member']}]
        if len(matches) != 1 or Decimal(matches[0]['value']) != Decimal(part['value']):
            return None
        selected.append(matches[0])
    # A different fixed-axis family remains separate, never a peer of this
    # declared one-dimensional partition. The full scope retains those rows.
    actual = [r for r in family if set(r.get('dimensions') or {}) == {group['axis']}]
    if len(actual) != len(selected):
        return None  # hidden/fixed-axis/parent-child members never disappear
    totals = [r for r in family if not r.get('dimensions') and not r.get('is_breakdown')]
    if len(totals) != 1 or Decimal(totals[0]['value']) != Decimal(group['total']):
        return None
    selected.append(totals[0])
    if len({r['unit'].casefold() for r in selected}) != 1:
        return None
    expressions = []
    for row in selected:
        sources = [s for s in proof['sources'] if s['ingest_id'] == row.get('ingest_id')]
        accessions = {f['accession'] for s in sources for f in s['facts']}
        if len(accessions) != 1:
            return None
        expression_ = _atomic({**proof, 'sources': sources}, concept=row['concept'],
            dimensions=row.get('dimensions') or {}, unit=row['unit'], accession=next(iter(accessions)),
            start=start, end=end, value=row['value'])
        if expression_ is None:
            return None
        expressions.append((expression_, -1 if row is totals[0] else 1))
    return {'concept': group['line'], 'period_start': start, 'period_end': end, 'axis': group['axis'],
            **assess(combine(expressions))}


def evaluate_segment_accuracy(record, subject, grouped_rows, selected, original):
    """Preserve all global checks; replace only source-proven sum failures."""
    from .economic_invariants import _segment_sum, InvariantResult, InvariantReport, SEGMENT_SUM, FAIL, PASS
    proof = (record.get('execution_contract') or {}).get('source_accuracy')
    if proof is None:
        return original
    validate_shape(proof, record['company_ref'])
    verdicts, diagnostics = [], []
    for key, group in selected:
        verdict = _segment_sum([group])
        if verdict.status == FAIL:
            assessment = family_assessment(proof, group, grouped_rows[key])
            if assessment is not None and assessment['status'] == 'within_disclosed_accuracy':
                diagnostics.append(assessment)
                verdict = InvariantResult(SEGMENT_SUM, PASS, checked=verdict.checked,
                    reason='Same-source complete member sum is consistent with disclosed numeric accuracy; not an exact tie.',
                    findings=(str(assessment),))
        verdicts.append(verdict)
    if not diagnostics:
        return original
    failed = [v for v in verdicts if v.status == FAIL]
    segment = InvariantResult(SEGMENT_SUM, FAIL if failed else PASS,
        checked=sum(v.checked for v in verdicts),
        reason='Reported values and differences are retained; source accuracy is not a reconciliation amount.',
        findings=tuple(f for v in (failed if failed else verdicts) for f in v.findings))
    return InvariantReport(output_kind=original.output_kind, output_ref=original.output_ref,
        company_ref=original.company_ref, results=tuple(segment if v.invariant == SEGMENT_SUM else v for v in original.results),
        rule_ref=original.rule_ref,
        subject_hash=content_hash({'subject': subject, 'source_accuracy_hash': proof['content_hash'], 'accuracy_checks': diagnostics}))
