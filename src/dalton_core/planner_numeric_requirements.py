"""Keep source-verified numeric rejections visible through prompt compression.

This is planning feedback, not new numeric authority or semantic matching.
"""
from collections.abc import Mapping, Sequence
from typing import Any
from .store import content_hash

BINDING_FIELDS = ('admission_ref', 'admission_hash', 'rejection_ref', 'rejection_hash')
KEEP_PER_COMPANY = 4
QUESTION_CHARS = 600


def numeric_requirements(feedback: Sequence[Mapping[str, Any]], *, limit: int | None = KEEP_PER_COMPANY) -> dict[str, Any]:
    """The producer already replays numeric_rejection; never infer from prose."""
    selected = {}
    for observation in sorted(feedback, key=lambda x: (str(x.get('created_at') or ''), str(x.get('id') or '')), reverse=True):
        binding = observation.get('replaces_numeric_rejection')
        if observation.get('numeric_strategy_required') is not True or not isinstance(binding, Mapping):
            continue
        if set(binding) != set(BINDING_FIELDS) or not all(isinstance(binding[k], str) and binding[k] for k in BINDING_FIELDS):
            raise ValueError('numeric requirement has invalid rejection binding')
        if any(len(binding[k]) != 64 or any(c not in '0123456789abcdef' for c in binding[k]) for k in ('admission_hash', 'rejection_hash')):
            raise ValueError('numeric requirement has invalid rejection hash')
        question, document = observation.get('question'), observation.get('document_ref')
        if not isinstance(question, str) or not question.strip() or not isinstance(document, str) or not document:
            raise ValueError('numeric requirement lost original question/document')
        row = {'question': question[:QUESTION_CHARS], 'question_hash': content_hash(question.strip()),
               'document_ref': document, 'replaces_numeric_rejection': dict(binding)}
        if len(question) > QUESTION_CHARS:
            row['question_excerpted'] = True
        version = observation.get('document_authority_hash')
        if isinstance(version, str):
            row['document_version_hash'] = version
        prior = selected.get(binding['rejection_ref'])
        if prior is not None and prior != row:
            raise ValueError('one numeric rejection has inconsistent planning scope')
        selected[binding['rejection_ref']] = row
    rows = list(selected.values())
    kept = rows if limit is None else rows[:limit]
    omitted = rows[len(kept):]
    return {'requirements': kept, 'omitted': len(omitted),
            **({'omitted_hash': content_hash(omitted)} if omitted else {})}


def validate_numeric_requirement(inquiry: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    """Only explicit rejection identity or exact question binds a requirement."""
    from .directed_numeric_source_capabilities import NUMERIC_STRATEGIES
    strategy = inquiry.get('directed_document')
    if not isinstance(strategy, Mapping):
        return
    requirements = []
    for company in state.get('companies', ()):
        # Validation uses full feedback, not just the bounded prompt excerpt.
        feedback = company.get('document_research_feedback') or ()
        rows = (numeric_requirements(feedback, limit=None)['requirements'] if feedback else
                (company.get('numeric_reading_requirements') or {}).get('requirements') or ())
        requirements.extend({**row, 'company_ref': company.get('company_ref')} for row in rows)
    binding = strategy.get('replaces_numeric_rejection')
    if isinstance(binding, Mapping):
        known = [r for r in requirements if r['replaces_numeric_rejection']['rejection_ref'] == binding.get('rejection_ref')
                 or r['replaces_numeric_rejection']['admission_ref'] == binding.get('admission_ref')]
        if not known:
            return  # Historical retries still use the original strong admission gate.
        match = [r for r in known if r['replaces_numeric_rejection'] == binding]
        if len(match) != len(known) or any(r['company_ref'] != inquiry.get('company_ref') for r in match):
            raise ValueError('numeric rejection binding is not exact in the research state')
        if any(r['document_ref'] != strategy.get('document_ref') or
               (r.get('document_version_hash') is not None and r['document_version_hash'] != strategy.get('document_version_hash')) for r in match):
            raise ValueError('numeric rejection binding names another original version')
    else:
        match = [r for r in requirements if r['company_ref'] == inquiry.get('company_ref') and r['question_hash'] == content_hash(str(inquiry.get('question') or '').strip())
                 and r['document_ref'] == strategy.get('document_ref')
                 and (r.get('document_version_hash') is None or r['document_version_hash'] == strategy.get('document_version_hash'))]
    if match:
        if strategy.get('strategy_version') not in NUMERIC_STRATEGIES:
            raise ValueError('the bound numeric requirement cannot use a qualitative strategy')
        if binding is None:
            raise ValueError('the bound numeric requirement needs its exact rejection binding')


def numeric_waiting_targets(feedback: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bounded planning diagnostics from the strong numeric-feedback reader.

    This helper does not establish numeric authority. The caller supplies only
    feedback whose admission, receipt and successful formal result were replayed.
    Completed targets supersede older waiting for the same exact definition.
    """
    seen, pending = set(), []
    for observation in sorted(feedback, key=lambda x: (str(x.get('created_at') or ''), str(x.get('id') or '')), reverse=True):
        if observation.get('outcome') not in {'numeric_waiting_evidence', 'numeric_targets_completed'}:
            continue
        body = dict(observation)
        digest = body.pop('content_hash', None)
        # The feedback reader adds these already-verified rejection bindings.
        body.pop('numeric_strategy_required', None)
        body.pop('replaces_numeric_rejection', None)
        if digest != content_hash(body):
            raise ValueError('numeric waiting feedback hash differs')
        targets = {row['target_ref']: row for row in observation['numeric_targets']}
        completed = {row['target_ref'] for row in observation['completed_targets']}
        missing = {row['target_ref']: row for row in observation['missing']}
        for ref, target in targets.items():
            identity = (ref, content_hash(target), observation.get('answer_contract_ref'))
            if identity in seen:
                continue
            seen.add(identity)
            if ref in completed:
                continue
            item = missing.get(ref)
            if item is None:
                raise ValueError('numeric waiting target lacks completion or missing proof')
            reason = item['reason']
            version = observation.get('document_version_hash')
            if not isinstance(reason, str) or not reason.strip() or not isinstance(version, str) or len(version) != 64:
                raise ValueError('numeric waiting diagnostic lacks exact source or reason')
            row = {'target_ref': ref, 'reason': reason[:180],
                   'document_ref': observation['document_ref'], 'document_version_hash': version,
                   'numeric_result_hash': observation['numeric_result_hash']}
            for key in ('answer_contract_ref', 'source_grade', 'document_date', 'attribution'):
                if key in observation: row[key] = observation[key]
            if observation.get('target_revision_candidate') is True:
                row['target_revision_candidate'] = True
            if len(reason) > 180:
                row.update(reason_excerpted=True, full_reason_hash=content_hash(reason))
            pending.append(row)
    kept, omitted = pending[:KEEP_PER_COMPANY], pending[KEEP_PER_COMPANY:]
    return {'targets': kept, 'omitted': len(omitted),
            **({'omitted_hash': content_hash(omitted)} if omitted else {})}


def numeric_completed_context(feedback: Sequence[Mapping[str, Any]], *,
                              company_ref: str) -> dict[str, Any]:
    """Retain recent proved target scopes with their exact original source versions.

    These are planning hints, not new Figures or answers to another question.
    The upstream reader has replayed the admission and final successful result.
    Different rejection bindings and metric definitions remain unresolved.
    """
    from .directed_numeric_research import normalize_targets
    if not isinstance(company_ref, str) or not company_ref:
        return {'sources': [], 'omitted': 0}
    candidates, seen = [], set()
    for observation in sorted(feedback, key=lambda x: (str(x.get('created_at') or ''), str(x.get('id') or '')), reverse=True):
        if observation.get('outcome') != 'numeric_targets_completed':
            continue
        if observation.get('company_ref') != company_ref:
            continue
        body = dict(observation)
        digest = body.pop('content_hash', None)
        body.pop('numeric_strategy_required', None)
        body.pop('replaces_numeric_rejection', None)
        if digest != content_hash(body):
            raise ValueError('numeric completed feedback hash differs')
        header = {key: observation[key] for key in (
            'document_ref', 'document_version_hash', 'numeric_result_hash', 'admission_ref', 'admission_hash')}
        for key in ('answer_contract_ref', 'source_grade', 'document_date', 'attribution'):
            if key in observation: header[key] = observation[key]
        for key in ('document_version_hash', 'numeric_result_hash', 'admission_hash'):
            if not isinstance(header[key], str) or len(header[key]) != 64 or any(c not in '0123456789abcdef' for c in header[key]):
                raise ValueError('numeric completed feedback lacks an exact hash')
        if not isinstance(header['admission_ref'], str) or not header['admission_ref']:
            raise ValueError('numeric completed feedback lacks an admission')
        targets = normalize_targets(observation['numeric_targets'])
        completed = observation['completed_targets']
        bindings = {row['target_ref']: row for row in completed}
        if observation.get('missing') or len(bindings) != len(completed) or set(bindings) != {t['target_ref'] for t in targets}:
            raise ValueError('numeric completed feedback lacks every target')
        for target in targets:
            binding = bindings[target['target_ref']]
            if binding.get('metric_definition_hash') != target['metric_definition_hash']:
                raise ValueError('numeric completed Figure definition differs')
            figure_ref, figure_hash = binding.get('figure_ref'), binding.get('figure_hash')
            if (not isinstance(figure_ref, str) or not figure_ref.startswith('mission-document-figure:')
                    or not isinstance(figure_hash, str) or len(figure_hash) != 64
                    or any(c not in '0123456789abcdef' for c in figure_hash)):
                raise ValueError('numeric completed target lacks a Figure binding')
            row = {**target, 'figure_ref': figure_ref, 'figure_hash': figure_hash}
            answers = {answer['target_ref']: answer for answer in observation.get('completed_answers', ())}
            answer = answers.get(target['target_ref'])
            if answer is not None:
                if (answer['figure_ref'] != figure_ref or answer['figure_hash'] != figure_hash
                        or answer['metric_definition_hash'] != target['metric_definition_hash']
                        or any(answer[key] != target[key] for key in ('period', 'unit', 'currency', 'basis'))):
                    raise ValueError('numeric completed answer scope differs')
                row['verified_answer'] = {key: answer[key] for key in (
                    'value', 'unit', 'currency', 'scale', 'period', 'basis', 'as_reported_label',
                    'source_grade', 'quote_id', 'citation_excerpt', 'citation_excerpt_start', 'citation_excerpt_is_complete', 'citation_text_hash')}
            identity = (header['document_ref'], header['document_version_hash'], content_hash(row))
            if identity not in seen:
                seen.add(identity)
                candidates.append({'source': header, 'target': row})
    kept, omitted = candidates[:KEEP_PER_COMPANY], candidates[KEEP_PER_COMPANY:]
    sources = {}
    for item in kept:
        key = content_hash(item['source'])
        sources.setdefault(key, {**item['source'], 'targets': []})['targets'].append(item['target'])
    return {'sources': list(sources.values()), 'omitted': len(omitted),
            **({'omitted_hash': content_hash(omitted)} if omitted else {})}
