"""Versioned proof of one typed answer supported by an existing exact Figure.

Different retrieval windows are not different facts. Both windows must still be
replayed from registered renderings of the same immutable original bytes; no quote comparison is
silently discarded, and no old Figure or canonical evidence is rewritten.
"""
from __future__ import annotations

import hashlib
import re
from .store import canonical_json, content_hash
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError, normalize_targets

SUPPORT_CONTRACT = 'directed-numeric-figure-support:0.1'
REGISTERED_RENDER_SUPPORT_CONTRACT = 'directed-numeric-figure-support:0.2'
SUPPORT_CONTRACTS = frozenset({SUPPORT_CONTRACT, REGISTERED_RENDER_SUPPORT_CONTRACT})
MEASURE_CONFLICT_CONTRACT = 'directed-numeric-measure-conflict:0.1'
MEASURE_CONFLICT_REASON = ('numeric_measure_identity_conflict: metric conflicts with the verified answer; '
    'keep target open; propose an explicit measure via normal admission, never relabel the old fact.')


class NumericMeasureIdentityConflict(DirectedNumericError):
    def __init__(self, proof):
        super().__init__(MEASURE_CONFLICT_REASON)
        self.proof = proof

FACT_FIELDS = ('metric_ref', 'as_reported_label', 'period', 'value', 'unit',
               'currency', 'scale', 'basis')
_QUOTE = re.compile(r'quote:([0-9]+):([0-9]+):([0-9a-f]{16})\Z')



def _held_registered_read(registry, request, held, match):
    """Only two official local renderers may prove a held historical window.

    The admission's registration remains the new answer's identity. An older
    admission and a newer Figure can name different normalized renderings of
    the same fetched bytes. No caller supplies an alternate registration and
    no substring/number matching relocates the declared quote coordinates.
    """
    original=registry.read(request)
    exact=lambda read: (read['text']==held['citation_text'] and
        hashlib.sha256(read['text'].encode()).hexdigest()[:16]==match.group(3))
    if exact(original):
        return original, None
    expected=request['registration'];authority=expected['source_authority']
    adapter=registry.acquired_document_adapter
    if authority['kind']!='coverage-mission-acquired-document' or adapter is None:
        raise DirectedNumericError('existing Figure quote differs from registered original')
    from .document_research import validate_registration
    for legacy in (False, True):
        registration,_text=adapter.materialize_record(record_id=authority['ref'],
            acquisition_ticket_ref=expected['acquisition_ticket_ref'],legacy=legacy)
        registration=validate_registration(registration)
        if registration['content_hash']==expected['content_hash']:
            continue
        # Raw-source authority, issuer, scope and acquisition are identical;
        # only the official normalized rendering identity may differ.
        keys=('source_ref','document_ref','manifest_hash','source_authority',
              'acquisition_ticket_ref','raw_source','access_policy_ref',
              'retention_policy_ref','terms_policy_ref')
        if any(registration[k]!=expected[k] for k in keys):
            raise DirectedNumericError('held Figure registered rendering binds a different original')
        read=registry.read({**request,'registration':registration})
        if exact(read):
            return read, {'held_read_contract':REGISTERED_RENDER_SUPPORT_CONTRACT,
                'held_registration_ref':registration['id'],
                'held_document_version_hash':registration['content_hash'],
                'held_raw_source_hash':content_hash(registration['raw_source'])}
    raise DirectedNumericError('existing Figure quote differs from registered original')


def figure_support(*, connection, registry, admission, target, figure,
                   search_proof, draft, verifier):
    """Derive support only after the caller's strong paid/formal stage replay.

    The caller passes the actual replayed model proofs, never a model's claim
    that a ledger row is equivalent. This helper is shared by recording and
    completion replay; source reads are local registered-document operations.
    """
    from .claim_index_figures import MissionFigureAuthorityResolver
    from .document_numeric_claim import verify_numeric_candidate
    from .directed_numeric_output import quotes, candidate_ref
    from .document_research import READ_REQUEST_SCHEMA_VERSION, READ_OPERATION
    from .model_router import independent_families

    targets = normalize_targets(admission['numeric_authority']['targets'],contract_ref=admission['numeric_authority']['contract_ref'])
    matching = [t for t in targets if t['target_ref'] == target['target_ref']]
    if len(matching) != 1 or canonical_json(matching[0]) != canonical_json(target):
        raise DirectedNumericError('numeric Figure support target identity differs')
    proof = registry.verify_search_proof(search_proof)
    if canonical_json(proof['request']) != canonical_json(admission['request']):
        raise DirectedNumericError('numeric Figure support original binding differs')
    if (not independent_families(verifier['model_family'], draft['model_family'])
            or verifier['output']['verdict'] != 'pass'
            or verifier['output']['candidate_ref'] != candidate_ref(draft['output'])):
        raise DirectedNumericError('numeric Figure support lacks independent target verification')
    rows = [r for r in draft['output']['targets'] if r['target_ref'] == target['target_ref']]
    votes = [r for r in verifier['output']['targets'] if r['target_ref'] == target['target_ref']]
    if (len(rows) != 1 or rows[0]['status'] != 'supported'
            or len(votes) != 1 or votes[0]['verdict'] != 'accept'):
        raise DirectedNumericError('numeric Figure support lacks accepted typed answer')
    candidate = rows[0]['figure']
    if any(candidate.get(k) != target[k] for k in ('metric_ref', 'period', 'unit', 'currency', 'basis')):
        raise DirectedNumericError('numeric Figure support answer differs from admitted definition')
    quote_map = quotes(proof)
    native_proof = None
    if admission['numeric_authority']['contract_ref'] == 'directed-document-operating-numeric:0.1':
        from .operating_source_measurements import registered_measurements
        native_proof = registered_measurements(registry,admission['request']['registration'],
            purpose=admission['request']['purpose'],search_proof=proof,
                periods=list(dict.fromkeys(t['period'] for t in admission['numeric_authority']['targets'])))
    expected = verify_numeric_candidate(candidate, quote_map, numeric_contract_ref=admission['numeric_authority']['contract_ref'],
        operating_source_proof=native_proof)
    resolver = MissionFigureAuthorityResolver(connection)
    held, _ = resolver.verify_figure(figure)
    registration = admission['request']['registration']
    if (held['company_ref'] != admission['company_ref']
            or held['document_ref'] != registration['document_ref']
            or held['source_manifest_hash'] != registration['manifest_hash']):
        raise DirectedNumericError('existing Figure conflicts with exact verified numeric answer')

    # The old Figure's stored citation is not proof that those bytes belonged
    # to this original. Re-read its closed range under the existing read policy.
    match = _QUOTE.fullmatch(held['quote_id'])
    if match is None:
        raise DirectedNumericError('existing Figure quote has no exact registered range')
    start, end = map(int, match.group(1, 2))
    request = {k: proof['request'][k] for k in (
        'purpose', 'research_question', 'registration', 'policy_ref', 'policy_hash')}
    request.update(schema_version=READ_REQUEST_SCHEMA_VERSION, operation=READ_OPERATION,
                   source_start=start, source_end=end)
    old_read, held_registration = _held_registered_read(registry,request,held,match)
    old_text = old_read['text']
    # Each side must prove its own reported number and label. A conflict is
    # diagnostic only after the old fact also binds true original bytes.
    from .claim_index_figures import _numeric_candidate
    from .document_numeric_unit_contracts import figure_numeric_contract
    held_native_proof = None
    from .operating_driver_units import FIGURE_VERIFIER_REF as OPERATING_VERIFIER
    if held['verified_by'] == OPERATING_VERIFIER:
        from .operating_figure_source import read as read_operating_source
        held_native_proof = read_operating_source(connection, held, registry=registry)
    verify_numeric_candidate(_numeric_candidate(held), {held['quote_id']: old_text},
                             numeric_contract_ref=figure_numeric_contract(held['verified_by']),
                             operating_source_proof=held_native_proof)
    current_text = quote_map[expected['quote_id']]
    body = {
        'schema_version': REGISTERED_RENDER_SUPPORT_CONTRACT if held_registration else SUPPORT_CONTRACT,
        'admission_ref': admission['id'], 'admission_hash': admission['content_hash'],
        'target_ref': target['target_ref'], 'target_hash': content_hash(target),
        'metric_definition_hash': target['metric_definition_hash'],
        'figure_ref': held['figure_id'], 'figure_hash': held['content_hash'],
        'company_ref': admission['company_ref'], 'document_ref': registration['document_ref'],
        'document_version_hash': registration['content_hash'],
        'source_manifest_hash': registration['manifest_hash'],
        'source_proof_ref': proof['id'], 'source_proof_hash': proof['content_hash'],
        'held_quote_id': held['quote_id'], 'held_quote_hash': hashlib.sha256(old_text.encode()).hexdigest(),
        'held_read_proof_ref': old_read['id'], 'held_read_proof_hash': old_read['content_hash'],
        'answer_quote_id': expected['quote_id'],
        'answer_quote_hash': hashlib.sha256(current_text.encode()).hexdigest(),
        'verified_fact_hash': content_hash({k: expected[k] for k in FACT_FIELDS}),
        'draft_proof_hash': draft['content_hash'], 'verifier_proof_hash': verifier['content_hash'],
    }
    if held_registration:
        body.update(held_registration)
    differing = [key for key in FACT_FIELDS if held[key] != expected[key]]
    if differing:
        conflict = {**body, 'schema_version': MEASURE_CONFLICT_CONTRACT,
                    'differing_fields': differing,
                    'held_fact': {key: held[key] for key in FACT_FIELDS},
                    'verified_answer': {key: expected[key] for key in FACT_FIELDS}}
        raise NumericMeasureIdentityConflict({**conflict, 'content_hash': content_hash(conflict)})
    return {**body, 'content_hash': content_hash(body)}


def verify_support(mapping, **kwargs):
    """Require the entire persisted support mapping to replay byte-for-byte."""
    expected = figure_support(**kwargs)
    if canonical_json(mapping) != canonical_json(expected):
        raise DirectedNumericError('numeric Figure support mapping does not replay')
    return expected


def stage_exact_figure(executor, *, figure, actor_ref, idempotency_key, resolver):
    """Reuse only an already staged, byte-identical immutable Figure bundle.

    Staging is not canonical completion: the caller still makes and verifies
    this admission's ordinary policy commit and canonical target mapping.
    """
    from .claim_index_figures import build_figure_candidate, promote_figure
    bundle = build_figure_candidate(executor.connection, figure=figure,
                                    actor_ref=actor_ref, resolver=resolver)
    request_hash = content_hash({
        'material_hash': bundle['material']['content_hash'], 'numeric_spec_hash': None,
        'source_verification_hash': bundle['source_verification']['content_hash'],
        'numeric_verification_hash': bundle['numeric_verification']['content_hash'],
        'evidence_hash': bundle['evidence']['content_hash'],
        'claim_hash': bundle['claim']['content_hash'], 'figure_hash': figure['content_hash'],
    })
    row = executor.staging.connection.execute(
        'SELECT idempotency_key FROM candidate_stage_requests WHERE request_hash=? '
        'ORDER BY idempotency_key LIMIT 1', (request_hash,)).fetchone()
    if row is not None:
        # Check the entire stored authority against the current strong Core
        # derivation, not just the presence of a staging request or its key.
        for table, key, field, identifier in (
            ('candidate_source_materials', 'material_id', 'material', 'id'),
            ('candidate_verifications', 'verification_id', 'source_verification', 'id'),
            ('candidate_verifications', 'verification_id', 'numeric_verification', 'id'),
            ('candidate_evidence_versions', 'version_id', 'evidence', 'id'),
            ('candidate_claim_versions', 'version_id', 'claim', 'id'),
            ('candidate_figures', 'figure_id', 'figure', 'figure_id'),
        ):
            expected = bundle[field]
            held = executor.staging.connection.execute(
                f'SELECT record_json,content_hash FROM {table} WHERE {key}=?',
                (expected[identifier],)).fetchone()
            if (held is None or held['record_json'] != canonical_json(expected)
                    or held['content_hash'] != expected['content_hash']):
                raise DirectedNumericError('existing Figure staging bundle differs')
        idempotency_key = row['idempotency_key']
    staged = promote_figure(executor.authority.store, executor.staging, figure=figure,
        actor_ref=actor_ref, idempotency_key=idempotency_key, resolver=resolver)
    expected_result = {
        'write_status': staged['staging']['write_status'],
        'candidate_evidence_ref': bundle['evidence']['id'],
        'candidate_evidence_hash': bundle['evidence']['content_hash'],
        'candidate_claim_ref': bundle['claim']['id'],
        'candidate_claim_hash': bundle['claim']['content_hash'],
        'source_verification_ref': bundle['source_verification']['id'],
        'numeric_verification_ref': bundle['numeric_verification']['id'],
    }
    if canonical_json(staged['staging']) != canonical_json(expected_result):
        raise DirectedNumericError('existing Figure staging result differs')
    return staged


def canonical_commit_support(store, *, bundle, committed, idempotency_key=None):
    """Bind a reused canonical admission without pretending a new row exists."""
    import json
    from .research_auto_commit import validate_policy_commit_decision
    from .research_review import validate_claim_version_v0_2, validate_evidence_version_v0_2
    if idempotency_key is None:
        # Freeze the first canonical origin at write time. Later compatible
        # rows can never change the source selected by an existing receipt.
        row = store.connection.execute(
            'SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=? '
            'AND candidate_evidence_ref=? AND candidate_claim_ref=? ORDER BY rowid LIMIT 1',
            (committed['review_decision_ref'], bundle['evidence']['id'], bundle['claim']['id'])).fetchone()
    else:
        row = store.connection.execute(
            'SELECT * FROM reviewed_candidate_commits WHERE idempotency_key=?',
            (idempotency_key,)).fetchone()
    if row is None:
        raise DirectedNumericError('numeric canonical admission is absent')
    decision = validate_policy_commit_decision(json.loads(row['decision_json']))
    saved = json.loads(row['result_json'])
    request_hash = content_hash({'decision_hash': decision['content_hash'],
        'evidence_hash': bundle['evidence']['content_hash'], 'claim_hash': bundle['claim']['content_hash']})
    if (row['review_decision_ref'] != committed['review_decision_ref']
            or row['candidate_evidence_ref'] != bundle['evidence']['id']
            or row['candidate_claim_ref'] != bundle['claim']['id']
            or canonical_json(decision) != row['decision_json']
            or canonical_json(saved) != row['result_json'] or row['request_hash'] != request_hash
            or saved['review_decision_ref'] != committed['review_decision_ref']
            or saved['claim_version_ref'] != committed['claim_version_ref']
            or saved['evidence_version_ref'] != committed['evidence_version_ref']
            or saved['idempotency_key'] != row['idempotency_key']):
        raise DirectedNumericError('numeric canonical support is not bound to the exact Figure')
    for table,key,json_key,validator,ref in (
        ('claim_versions','claim_version_id','claim_json',validate_claim_version_v0_2,committed['claim_version_ref']),
        ('evidence_versions','evidence_version_id','evidence_json',validate_evidence_version_v0_2,committed['evidence_version_ref']),
    ):
        held = store.connection.execute(f'SELECT {json_key},content_hash FROM {table} WHERE {key}=?', (ref,)).fetchone()
        if held is None:
            raise DirectedNumericError('numeric canonical support authority is absent')
        value = validator(json.loads(held[json_key]))
        if canonical_json(value) != held[json_key] or value['content_hash'] != held['content_hash']:
            raise DirectedNumericError('numeric canonical support authority bytes differ')
    if store._claim_status(store.connection, committed['claim_version_ref']) not in {'proposed', 'corroborated'}:
        raise DirectedNumericError('numeric canonical support is no longer usable')
    return {'schema_version': SUPPORT_CONTRACT, 'idempotency_key': row['idempotency_key'],
            'request_hash': request_hash, 'authorization_hash': decision['content_hash']}
