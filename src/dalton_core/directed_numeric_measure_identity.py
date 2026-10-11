"""Source-reported measures are distinct facts, never aliases for an amount.

Only a replayed independent numeric answer and both registered original rows
can split a historical concept-only Figure identity.  Target wording is audit
provenance, not a way to mint a different measure.
"""
from __future__ import annotations

import re
from .store import canonical_json, content_hash
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError

CONTRACT = 'directed-numeric-source-measure:0.1'


def _row_identity(text, fact):
    from .document_numeric_claim import _fold_words, _candidate_values, numbers_in
    label = _fold_words(fact['as_reported_label'])
    values = _candidate_values(fact)
    rows = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if label in _fold_words(line) and values & numbers_in(line):
            rows.append({'start': offset, 'end': offset+len(line), 'text_hash': content_hash(line),
                         'label': label})
        offset += len(line)
    if len(rows) != 1:
        raise DirectedNumericError('source measure requires one exact reported label/amount row')
    return rows[0]


def measure_support(*, connection, registry, admission, target, figure, search_proof, draft, verifier):
    from .directed_numeric_figure_support import figure_support, NumericMeasureIdentityConflict, _held_registered_read, _QUOTE
    from .directed_numeric_output import quotes
    from .document_research import READ_REQUEST_SCHEMA_VERSION, READ_OPERATION
    try:
        figure_support(connection=connection,registry=registry,admission=admission,target=target,
                       figure=figure,search_proof=search_proof,draft=draft,verifier=verifier)
    except NumericMeasureIdentityConflict as exc:
        conflict = exc.proof
    else:
        raise DirectedNumericError('source measure split requires a proved historical identity collision')
    old, new = conflict['held_fact'], conflict['verified_answer']
    if (set(conflict['differing_fields']) - {'as_reported_label','value'}
            or 'as_reported_label' not in conflict['differing_fields']):
        raise DirectedNumericError('same-measure amount conflict cannot become a different source measure')
    match = _QUOTE.fullmatch(figure['quote_id'])
    request = {k:search_proof['request'][k] for k in ('purpose','research_question','registration','policy_ref','policy_hash')}
    request.update(schema_version=READ_REQUEST_SCHEMA_VERSION,operation=READ_OPERATION,
                   source_start=int(match[1]),source_end=int(match[2]))
    held_read, _ = _held_registered_read(registry,request,figure,match)
    old_row = _row_identity(held_read['text'],old)
    new_text = quotes(registry.verify_search_proof(search_proof))[conflict['answer_quote_id']]
    new_row = _row_identity(new_text,new)
    # Different spelling of the same line, or two labels on one prose/table
    # row, is not proof of separate accounting measures.
    if old_row['label']==new_row['label'] or old_row['text_hash']==new_row['text_hash']:
        raise DirectedNumericError('source measure rows are not distinct')
    identity = {'schema_version':CONTRACT,'company_ref':admission['company_ref'],
        'document_ref':conflict['document_ref'],'source_manifest_hash':conflict['source_manifest_hash'],
        **{k:new[k] for k in ('metric_ref','period','unit','currency','scale','basis')},
        'reported_measure':new_row['label']}
    identity_hash=content_hash(identity)
    body={'schema_version':CONTRACT,'measure_identity':identity,'measure_identity_hash':identity_hash,
        'figure_ref':'mission-document-figure:source-measure:'+identity_hash[:32],
        'conflict_proof':conflict,'held_source_row':old_row,'answer_source_row':new_row}
    return {**body,'content_hash':content_hash(body)}


def record_measure(executor, *, admission, target, figure, search_proof, draft, verifier, expected):
    from .directed_numeric_executor import _admitted_source_binding
    from .claim_index_figures import MissionFigureAuthorityResolver, _FIGURE_COLUMNS
    from .document_numeric_unit_contracts import DIRECTED_FIGURE_VERIFIER_REF
    from .directed_numeric_output import quotes
    mapping=measure_support(connection=executor.connection,registry=executor.registry,admission=admission,
        target=target,figure=figure,search_proof=search_proof,draft=draft,verifier=verifier)
    source=_admitted_source_binding(executor.connection,admission)
    record={'figure_id':mapping['figure_ref'],'company_ref':admission['company_ref'],
        'review_ref':source['review_id'],'document_ref':source['document_ref'],
        'source_manifest_hash':source['source_manifest_hash'],'source_grade':source['source_grade'],
        **{k:expected[k] for k in ('quote_id','metric_ref','as_reported_label','period','value','unit','currency','scale','basis')},
        'citation_text':quotes(search_proof)[expected['quote_id']],
        'verified_by':DIRECTED_FIGURE_VERIFIER_REF,'observed_by':admission['actor_ref'],
        'created_at':executor.clock().isoformat(timespec='microseconds')}
    record['content_hash']=content_hash(record)
    verify_measure(mapping,connection=executor.connection,registry=executor.registry,admission=admission,
        target=target,figure=record,search_proof=search_proof,draft=draft,verifier=verifier,
        persisted=False)
    columns=(*_FIGURE_COLUMNS,'content_hash')
    with executor.authority._missions._transaction() as cur:
        # Retractions remain irreversible, including a retracted old origin.
        MissionFigureAuthorityResolver(executor.connection).verify_figure(figure)
        held=cur.execute('SELECT * FROM coverage_mission_document_figures WHERE figure_id=?',(record['figure_id'],)).fetchone()
        if held is None:
            cur.execute('INSERT INTO coverage_mission_document_figures('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(record[k] for k in columns))
        else:
            record=dict(held)
    verify_measure(mapping,connection=executor.connection,registry=executor.registry,admission=admission,
        target=target,figure=record,search_proof=search_proof,draft=draft,verifier=verifier)
    return record,mapping


def verify_measure(mapping, *, connection, registry, admission, target, figure, search_proof, draft, verifier, persisted=True):
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .directed_numeric_figure_support import FACT_FIELDS
    from .directed_numeric_output import quotes
    resolver=MissionFigureAuthorityResolver(connection)
    original=resolver.figure(mapping['conflict_proof']['figure_ref'])
    rebuilt=measure_support(connection=connection,registry=registry,admission=admission,target=target,
        figure=original,search_proof=search_proof,draft=draft,verifier=verifier)
    if canonical_json(rebuilt)!=canonical_json(mapping):
        raise DirectedNumericError('source measure mapping does not replay')
    if persisted:resolver.verify_figure(figure)
    if (figure['figure_id']!=rebuilt['figure_ref'] or figure['company_ref']!=admission['company_ref']
            or figure['document_ref']!=rebuilt['conflict_proof']['document_ref']
            or figure['source_manifest_hash']!=rebuilt['conflict_proof']['source_manifest_hash']
            or any(figure[k]!=rebuilt['conflict_proof']['verified_answer'][k] for k in FACT_FIELDS)
            or figure['quote_id']!=rebuilt['conflict_proof']['answer_quote_id']
            or figure['citation_text']!=quotes(search_proof)[figure['quote_id']]):
        raise DirectedNumericError('source measure Figure differs from its proved reported row')
    return rebuilt
