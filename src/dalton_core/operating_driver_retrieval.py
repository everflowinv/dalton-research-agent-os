"""Candidate-only lexical search for a paid model's declared source gaps.

These phrases do not make different labels equivalent or certify a quantity.
The operating read owner still reconstructs the exact original need, and the
reader and independent verifier must establish its meaning from the source.
"""
from collections.abc import Mapping

RULE_REF = 'rule:operating-driver-source-retrieval:0.1'


def metric_terms(metric):
    label = metric.get('label') if isinstance(metric, Mapping) else None
    if not isinstance(label, str) or not 0 < len(label.strip()) <= 160:
        raise ValueError('operating retrieval has no bounded declared metric label')
    label = label.strip()
    words = label.lower()
    candidates = []
    if 'volume' in words and ('sales' in words or 'sold' in words):
        candidates += ['sales volume', 'units sold', 'tons sold']
    if 'selling' in words and 'price' in words:
        candidates += ['average selling price', 'selling price']
    if 'natural gas' in words and 'cost' in words:
        candidates += ['cost of natural gas', 'natural gas costs']
    if 'utilization' in words:
        candidates += ['capacity utilization', 'plant utilization', 'utilization']
    unique, seen = [], set()
    for term in [label, *candidates]:
        if term.lower() not in seen:
            seen.add(term.lower()); unique.append(term)
    return unique


def request_terms(request):
    scope = (request.get('acceptance') or {}).get('model_driver_source_request')
    if not isinstance(scope, Mapping) or scope.get('contract_ref') != 'model-spec-joint-required-fields:0.1':
        return None
    metric = scope.get('metric')
    need = request.get('need') or {}
    # This is a lexical material candidate, never numerical authority. A
    # malformed or unrelated request keeps the normal material search path.
    if (scope.get('financial_evidence') is not False or scope.get('repair_authority') is not False
            or scope.get('company_ref') != request.get('subject') or need.get('kind') != 'data'
            or not isinstance(metric, Mapping) or need.get('data_item') != metric.get('label')):
        return None
    return metric_terms(metric)
