"""A source-proved scale clarification, never an alias for a different fact.

Legacy Figures remain immutable. A local, independently verified completion
may append a new typed Figure only when scale is the sole missing field.
"""
from .store import canonical_json, content_hash
from .directed_numeric_research import DirectedNumericError

CONTRACT = 'directed-numeric-source-unit-clarification:0.1'
PREFIX = 'mission-document-figure:source-unit:'
TABLE = 'mission_document_numeric_source_units'


def _identity(conflict):
    new=conflict['verified_answer']
    return {'schema_version': CONTRACT, 'company_ref': conflict['company_ref'],
        'document_ref': conflict['document_ref'], 'source_manifest_hash': conflict['source_manifest_hash'],
        'quote_id':conflict['answer_quote_id'], 'quote_hash':conflict['answer_quote_hash'],
        **{k: new[k] for k in ('metric_ref', 'period', 'as_reported_label', 'unit', 'currency', 'scale', 'basis')}}


def candidate_figures(connection, admission, search_proof, draft, verifier):
    """A dispatch hint from strong formals, not permission to write a Figure.

    The local child must still replay original reads, financial definitions,
    independent verification and every fee before its only zero-cost Work.
    """
    from .coverage_mission import _ref, _period_key
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .directed_numeric_figure_support import FACT_FIELDS
    from .directed_numeric_output import candidate_ref, quotes
    from .document_numeric_claim import verify_numeric_candidate
    from .directed_numeric_research import CONTRACT_REF
    from .model_router import independent_families
    if (verifier['output']['verdict']!='pass'
            or verifier['output']['candidate_ref']!=candidate_ref(draft['output'])
            or not independent_families(draft['model_family'],verifier['model_family'])):
        return []
    accepted={t['target_ref'] for t in verifier['output']['targets'] if t['verdict']=='accept'}
    targets={t['target_ref']:t for t in admission['numeric_authority']['targets']}
    result=[]
    for item in draft['output']['targets']:
        if item['target_ref'] not in accepted or item['status']!='supported':continue
        fact=verify_numeric_candidate(item['figure'],quotes(search_proof),numeric_contract_ref=CONTRACT_REF)
        target=targets[item['target_ref']]
        if any(fact[k]!=target[k] for k in ('metric_ref','period','unit','currency','basis')):continue
        legacy_ref=_ref('mission-document-figure',{'company_ref':admission['company_ref'],
            'metric_ref':fact['metric_ref'],'period':_period_key(fact['period']),
            'document_ref':admission['request']['registration']['document_ref']})
        row=connection.execute('SELECT * FROM coverage_mission_document_figures WHERE figure_id=?',(legacy_ref,)).fetchone()
        if row is None:continue
        held,_=MissionFigureAuthorityResolver(connection).verify_figure(dict(row))
        if (held['source_manifest_hash']!=admission['request']['registration']['manifest_hash']
                or held['scale'] is not None or fact['scale'] is None
                or held['unit']!='currency' or held['currency'] is None
                or any(held[k]!=fact[k] for k in FACT_FIELDS if k!='scale')):continue
        result.append({'target_ref':item['target_ref'],'figure_ref':held['figure_id'],'figure_hash':held['content_hash']})
    return result


def read(connection, figure_ref):
    """Read an immutable typed view; the legacy Figure remains the origin."""
    import json
    from .claim_index_figures import MissionFigureAuthorityResolver, _FIGURE_COLUMNS
    from .directed_numeric_local_reconciliation import TABLE as AUTHORIZATIONS
    row = connection.execute('SELECT * FROM '+TABLE+' WHERE figure_ref=?', (figure_ref,)).fetchone()
    if row is None:
        from .claim_index_figures import FigureNotFound
        raise FigureNotFound('source unit Figure authority is absent')
    value, mapping = json.loads(row['record_json']), json.loads(row['proof_json'])
    if (canonical_json(value)!=row['record_json'] or canonical_json(mapping)!=row['proof_json']
            or set(value)!={*_FIGURE_COLUMNS,'content_hash'}
            or value['content_hash']!=row['content_hash']
            or content_hash({k:value[k] for k in _FIGURE_COLUMNS})!=row['content_hash']
            or mapping['content_hash']!=row['proof_hash']
            or content_hash({k:v for k,v in mapping.items() if k!='content_hash'})!=row['proof_hash']
            or mapping['schema_version']!=CONTRACT or mapping['figure_ref']!=figure_ref
            or value['figure_id']!=figure_ref):
        raise DirectedNumericError('source unit Figure authority hash differs')
    conflict=mapping['conflict_proof']
    if (conflict['differing_fields']!=['scale'] or conflict['held_fact']['scale'] is not None
            or conflict['verified_answer']['scale'] is None
            or conflict['held_fact']['unit']!='currency' or conflict['held_fact']['currency'] is None
            or content_hash({k:v for k,v in conflict.items() if k!='content_hash'})!=conflict['content_hash']
            or content_hash(mapping['identity'])!=mapping['identity_hash']
            or mapping['identity']!=_identity(conflict)
            or figure_ref!=PREFIX+mapping['identity_hash'][:32]):
        raise DirectedNumericError('source unit clarification identity differs')
    if conflict['figure_ref'].startswith(PREFIX):
        raise DirectedNumericError('unit clarification cannot use another clarification as origin')
    origin=MissionFigureAuthorityResolver(connection).figure(conflict['figure_ref'])
    MissionFigureAuthorityResolver(connection).verify_figure(origin)
    from .directed_numeric_figure_support import FACT_FIELDS
    if (origin['content_hash']!=conflict['figure_hash']
            or any(origin[k]!=conflict['held_fact'][k] for k in FACT_FIELDS)
            or any(origin[k]!=conflict[k] for k in ('company_ref','document_ref','source_manifest_hash'))):
        raise DirectedNumericError('source unit origin Figure differs')
    authorization=connection.execute('SELECT record_json,content_hash FROM '+AUTHORIZATIONS+' WHERE authorization_ref=?',
        (row['authorization_ref'],)).fetchone()
    if authorization is None:
        raise DirectedNumericError('source unit local authorization is absent')
    auth=json.loads(authorization['record_json'])
    if (canonical_json(auth)!=authorization['record_json'] or auth['content_hash']!=authorization['content_hash']
            or content_hash({k:v for k,v in auth.items() if k!='content_hash'})!=authorization['content_hash']
            or auth['admission_ref']!=conflict['admission_ref'] or auth['admission_hash']!=conflict['admission_hash']
            or auth['id']!=row['authorization_ref']
            or {'kind':'source_unit_clarification','proof':mapping} not in auth['repair_evidence']):
        raise DirectedNumericError('source unit authorization binding differs')
    # Full paid/source replay remains mandatory in the numeric receipt reader.
    # This resolver also refuses a self-rehashed typed value that differs from
    # the exact independently verified fact stored in that authorization.
    from .document_numeric_unit_contracts import DIRECTED_FIGURE_VERIFIER_REF
    import hashlib
    if (any(value[k]!=conflict['verified_answer'][k] for k in FACT_FIELDS)
            or value['company_ref']!=conflict['company_ref']
            or value['document_ref']!=conflict['document_ref']
            or value['source_manifest_hash']!=conflict['source_manifest_hash']
            or value['quote_id']!=conflict['answer_quote_id']
            or hashlib.sha256(value['citation_text'].encode()).hexdigest()!=conflict['answer_quote_hash']
            or value['verified_by']!=DIRECTED_FIGURE_VERIFIER_REF):
        raise DirectedNumericError('source unit Figure differs from authorized typed fact')
    return value


def support(**kwargs):
    from .directed_numeric_figure_support import figure_support, NumericMeasureIdentityConflict
    try:
        figure_support(**kwargs)
    except NumericMeasureIdentityConflict as exc:
        conflict = exc.proof
    else:
        raise DirectedNumericError('unit clarification requires an exact legacy scale conflict')
    old, new = conflict['held_fact'], conflict['verified_answer']
    if (conflict['differing_fields'] != ['scale'] or old['scale'] is not None
            or new['scale'] is None or old['unit'] != 'currency'
            or old['currency'] is None):
        raise DirectedNumericError('unit clarification cannot change value, basis, currency or known scale')
    identity = _identity(conflict)
    digest = content_hash(identity)
    body = {'schema_version': CONTRACT, 'identity': identity, 'identity_hash': digest,
        'figure_ref': PREFIX + digest[:32], 'conflict_proof': conflict}
    return {**body, 'content_hash': content_hash(body)}


def verify(mapping, *, figure, persisted=True, **kwargs):
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .directed_numeric_figure_support import FACT_FIELDS
    from .directed_numeric_output import quotes
    resolver = MissionFigureAuthorityResolver(kwargs['connection'])
    original = resolver.figure(mapping['conflict_proof']['figure_ref'])
    rebuilt = support(figure=original, **kwargs)
    if canonical_json(rebuilt) != canonical_json(mapping):
        raise DirectedNumericError('source unit clarification does not replay')
    if persisted:resolver.verify_figure(figure)
    if (figure['figure_id'] != rebuilt['figure_ref']
            or figure['company_ref'] != kwargs['admission']['company_ref']
            or figure['document_ref'] != rebuilt['conflict_proof']['document_ref']
            or figure['source_manifest_hash'] != rebuilt['conflict_proof']['source_manifest_hash']
            or any(figure[k] != rebuilt['conflict_proof']['verified_answer'][k] for k in FACT_FIELDS)
            or figure['quote_id'] != rebuilt['conflict_proof']['answer_quote_id']
            or figure['citation_text'] != quotes(kwargs['search_proof'])[figure['quote_id']]):
        raise DirectedNumericError('clarified Figure differs from exact source-reported answer')
    return rebuilt


def record(executor, *, works, figure, expected, **kwargs):
    from .directed_numeric_local_reconciliation import verify_reconciliation_work
    from .directed_numeric_executor import _admitted_source_binding
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .document_numeric_unit_contracts import DIRECTED_FIGURE_VERIFIER_REF
    from .directed_numeric_output import quotes
    auth = verify_reconciliation_work(executor, kwargs['admission'], works)
    mapping = support(figure=figure, connection=executor.connection, registry=executor.registry, **kwargs)
    from .directed_numeric_figure_support import FACT_FIELDS
    if (any(expected[k] != mapping['conflict_proof']['verified_answer'][k] for k in FACT_FIELDS)
            or expected['quote_id'] != mapping['conflict_proof']['answer_quote_id']):
        raise DirectedNumericError('unit clarification write differs from its independently verified answer')
    if {'kind': 'source_unit_clarification', 'proof': mapping} not in auth['repair_evidence']:
        raise DirectedNumericError('source unit write lacks its exact local authorization')
    source = _admitted_source_binding(executor.connection, kwargs['admission'])
    admission = kwargs['admission']
    value = {'figure_id': mapping['figure_ref'], 'company_ref': admission['company_ref'],
        'review_ref': source['review_id'], 'document_ref': source['document_ref'],
        'source_manifest_hash': source['source_manifest_hash'], 'source_grade': source['source_grade'],
        **{k: expected[k] for k in ('quote_id','metric_ref','as_reported_label','period','value','unit','currency','scale','basis')},
        'citation_text': quotes(kwargs['search_proof'])[expected['quote_id']],
        'verified_by': DIRECTED_FIGURE_VERIFIER_REF, 'observed_by': admission['actor_ref'],
        'created_at': executor.clock().isoformat(timespec='microseconds')}
    value['content_hash'] = content_hash(value)
    verify(mapping, figure=value, persisted=False, connection=executor.connection, registry=executor.registry, **kwargs)
    with executor.authority._transaction() as cur:
        MissionFigureAuthorityResolver(executor.connection).verify_figure(figure)
        held = cur.execute('SELECT * FROM '+TABLE+' WHERE figure_ref=?',
                           (value['figure_id'],)).fetchone()
        if held is None:
            cur.execute('INSERT INTO '+TABLE+'(figure_ref,authorization_ref,record_json,content_hash,proof_json,proof_hash) VALUES(?,?,?,?,?,?)',
                (value['figure_id'],auth['id'],canonical_json(value),value['content_hash'],canonical_json(mapping),mapping['content_hash']))
        else:
            value = read(executor.connection, value['figure_id'])
    verify(mapping, figure=value, connection=executor.connection, registry=executor.registry, **kwargs)
    return value, mapping
