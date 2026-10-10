"""Evidence-backed synthetic question metadata for current admission fixtures."""
from dalton_core.store import content_hash
from tests.test_store import invocation


def formal_content(store, content, *, question='question:fixture-growth', industry=None):
    result = dict(content)
    identifier = content_hash({'question': question, 'statement': content['statement']})[:16]
    cache = getattr(store, '_question_fixture_claims', {})
    refs = list(content.get('claim_refs') or cache.get(identifier) or ())
    if not refs:
        producer = 'invocation:question-fixture:' + identifier
        store.register_invocation(invocation(producer, 'fixture'))
        evidence = store.register_evidence(evidence={
            'evidence_ref': 'evidence:question-fixture:' + identifier,
            'source_type': 'sec-filing-exhibit', 'source_ref': 'fixture:held-report:' + identifier,
            'retrieved_at': '2026-09-01T00:00:00+00:00',
            'source_lineage': ['fixture:held-report:' + identifier],
            'independence_group': 'fixture:issuer:' + identifier,
        })
        claim = store.register_claim(claim={
            'claim_ref': 'claim:question-fixture:' + identifier, 'subject_ref': 'company:fixture',
            'metric_or_aspect': 'metric:revenue-growth', 'period': '2026Q2',
            'basis': 'synthetic-test-fixture', 'normalized_statement': 'Revenue growth was 4%.',
            'claim_kind': 'quantitative', 'value': 4, 'unit': 'percent', 'producer_invocation_refs': [producer],
        })
        store.relate_evidence(relation={
            'id': 'relation:question-fixture:' + identifier,
            'evidence_version_ref': evidence['evidence_version_id'],
            'claim_version_ref': claim['claim_version_id'], 'relation': 'supports',
        })
        refs = [claim['claim_version_id']]
        cache[identifier] = refs
        store._question_fixture_claims = cache
    framework = None
    if industry:
        from dalton_core.industry_framework import IndustryFrameworkAuthority
        from tests.test_industry_framework import record, simple_comparison
        authority = IndustryFrameworkAuthority(store)
        body = record(comparison=simple_comparison())
        body['industry_ref'] = industry
        framework = authority.publish(body)['id']
    result.update(claim_refs=refs, question_ref=question, stance='house',
                  proposition_direction='supports', view_status='active', passage_refs=[],
                  framework_version_ref=framework, disagreement=None,
                  falsification_conditions=[{'metric_ref': 'metric:revenue-growth', 'operator': 'lt',
                                             'threshold': '0', 'unit': 'percent', 'deadline': '2027-03-31',
                                             'evidence_refs': [refs[0]]}])
    return result
