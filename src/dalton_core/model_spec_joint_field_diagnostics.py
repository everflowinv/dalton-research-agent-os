"""Finite exact-parent annotation diagnostics, never invented financial inputs.

Legacy independent-settings reports remain unchanged. Only an otherwise closed
body with missing expense explanations or an empty optional bound-slot reason
can use this contract. Missing declared execution is a source task, not a paid
annotation repair. The diagnostic view is never accepted as a specification.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from .store import canonical_json, content_hash
from . import model_spec_complete_diagnostics as independent

CONTRACT_REF = 'model-spec-joint-required-fields:0.1'
ERROR_CODE = 'joint_required_fields'
SOURCE_CODE = 'declared_driver_source_required'
PREFLIGHT_CODE = 'joint_fields_source_mismatch'
MAX_FINDINGS = 20
_MISSING = object()


def _value(body, path):
    node = body
    for key in path[:-1]:
        node = node[key]
    return {'present': path[-1] in node, 'value': copy.deepcopy(node.get(path[-1]))}


def classification_from_prompt(prompt):
    from .driver_template import cost_slot_ids, template_for
    headers = [s for s in prompt.splitlines() if s.startswith('COST DRIVER TEMPLATE (')]
    if len(headers) != 1 or not headers[0].endswith('):'):
        raise ValueError('joint fields lack the original cost template')
    classification = headers[0][len('COST DRIVER TEMPLATE ('):-2]
    if template_for(classification)['classification'] != classification:
        raise ValueError('original cost classification differs')
    block = prompt.split(headers[0], 1)[1].split('\n\n', 1)[0]
    slots = [line.split('\t')[0] for line in block.splitlines()
             if len(line.split('\t')) == 4 and not line.startswith('<cost slot id>')]
    if slots != list(cost_slot_ids(classification)):
        raise ValueError('original cost slot catalog differs')
    return classification


def diagnose(body, *, state_hash, classification, output_schema=None):
    from .company_model_spec import OUTPUT_SCHEMA, MAX_EXPENSE_LINES, checked_current_output_schema
    schema=checked_current_output_schema(OUTPUT_SCHEMA if output_schema is None else output_schema)
    from .driver_template import cost_slot_ids
    report = {'contract_ref': CONTRACT_REF, 'parent_body_hash': content_hash(body),
              'state_hash': state_hash, 'classification': classification,
              'status': 'blocked', 'reason': 'closed_shape_or_refs_unproved',
              'findings': [], 'missing_driver_refs': []}
    if schema!=OUTPUT_SCHEMA:report['output_schema']=schema
    if not isinstance(body, dict):
        return report
    view = copy.deepcopy(body)
    expenses = view.get('expense_lines')
    if not isinstance(expenses, list) or not 1 <= len(expenses) <= MAX_EXPENSE_LINES:
        return report
    findings = []
    for index, line in enumerate(expenses):
        if not isinstance(line, dict):
            return report
        because = line.get('because', _MISSING)
        if because is _MISSING or isinstance(because, str) and not because.strip():
            path = ['expense_lines', index, 'because']
            findings.append({'rule': 'required_expense_explanation', 'paths': [path],
                             'original_values': [_value(body, path)],
                             'choices': 'a nonempty source-consistent explanation of at most 400 characters'})
            line['because'] = 'Diagnostic shape placeholder; never a model answer.'
        slot = line.get('cost_driver_slot')
        reason = line.get('cost_driver_unbound_reason', _MISSING)
        if slot is not None and isinstance(reason, str) and not reason.strip():
            if slot not in cost_slot_ids(classification):
                return report
            path = ['expense_lines', index, 'cost_driver_unbound_reason']
            findings.append({'rule': 'empty_optional_bound_slot_reason', 'paths': [path],
                             'original_values': [_value(body, path)],
                             'choices': 'omit the empty optional reason; retain the exact original bound slot'})
            del line['cost_driver_unbound_reason']
    if not independent._shape(view, schema) or not independent._closed_refs(view):
        return report
    # Never turn the diagnostic view into an accepted financial proposal.
    settings = independent.diagnose(view,output_schema=schema)
    if settings['status'] != 'complete':
        return {**report, 'reason': 'independent_settings_unproved_or_overflow'}
    if not findings:
        return report  # Preserve all legacy contracts for an ordinary settings error.
    for item in settings['findings']:
        findings.append({**item, 'original_values': [_value(body, p) for p in item['paths']]})
    used = {ref for formula in view.get('driver_formulas', [])
            for ref in formula.get('driver_refs', [])}
    drivers = view['revenue_drivers']
    if not used <= {d['ref'] for d in drivers} or any(
            f['output_ref'] not in {line['ref'] for line in view['financial_statement_structure']['lines']}
            for f in view.get('driver_formulas', [])):
        return report
    missing = sorted(d['ref'] for d in drivers if d['ref'] not in used)
    identity = (len(drivers) == 1 and drivers[0]['kind'] == 'mix'
                and drivers[0].get('basis_concept') == view['revenue_anchor_concept'])
    if 'driver_formulas' not in view and identity:
        missing = []  # The original parser's explicit single-total compatibility.
    source_gap = [d['ref'] for d in drivers if d['ref'] in missing
                  and d['kind'] == 'external' and d.get('basis_concept') is None]
    return {**report, 'status': ('overflow' if len(findings) > MAX_FINDINGS else
                'source_required' if source_gap else 'blocked' if missing else 'complete'),
            'reason': ('declared_driver_has_no_bound_execution_base' if source_gap else
                       'declared_driver_execution_unproved' if missing else None),
            'finding_count': len(findings), 'findings': findings if len(findings) <= MAX_FINDINGS else [],
            'missing_driver_refs': missing, 'source_required_driver_refs': sorted(source_gap),
            'cash_base_contract_ref': settings['cash_base_contract_ref']}


def from_state(body, state, *, output_schema=None):
    from .driver_template import template_for
    from .company_model_spec import output_schema_for_state
    return diagnose(body, state_hash=state['state_hash'],
                    classification=template_for(state.get('industry_classification'))['classification'],
                    output_schema=output_schema_for_state(state) if output_schema is None else output_schema)


def error_for(body, state, *, output_schema=None):
    from .company_model_spec import CompanyModelSpecError
    report = from_state(body, state,output_schema=output_schema)
    if report['status'] == 'complete' and report['findings']:
        # These exact filed sources are outside every allowed annotation or
        # settings path. Do not spend the one repair slot when the current
        # authority already proves that those unchanged fields will fail.
        from .company_financial_statement_structure import _presentation_filed, _same_net_income_source
        from .company_model_inputs import _declared_statement_hints
        try:
            filed, _ = _presentation_filed(state, statement_hints=_declared_statement_hints(
                {'financial_statement_structure': body['financial_statement_structure']}))
        except ValueError:
            filed = None  # Preserve the original malformed/absent state refusal.
        mismatches = []
        for line in body['financial_statement_structure']['lines']:
            if filed is None or line['kind'] != 'filed':
                continue
            source = filed.get(line['concept'])
            if source is None or (
                source['statement'] != line['statement']
                and not _same_net_income_source(line, source, line['concept'])
            ) or source['period_basis'] != line['period_kind']:
                mismatches.append({'line_ref': line['ref'], 'concept': line['concept'],
                    'requested_statement': line['statement'], 'requested_period_kind': line['period_kind'],
                    'held_statement': None if source is None else source['statement'],
                    'held_period_kind': None if source is None else source['period_basis']})
        if mismatches:
            return CompanyModelSpecError('Annotation repair cannot change missing or incompatible filed sources: '
                + ', '.join(f"{x['line_ref']} needs {x['requested_statement']}/{x['requested_period_kind']}; "
                            f"held {x['held_statement']}/{x['held_period_kind']}" for x in mismatches),
                code=PREFLIGHT_CODE, proof={'diagnostics': report, 'source_mismatches': mismatches})
    if report['status'] == 'overflow':
        return CompanyModelSpecError('joint required field diagnostics exceed the fixed finding limit', proof=report)
    if report['status'] == 'source_required':
        metrics = [f"{m['ref']} ({m['unit']})" for m in body['operating_metrics']
                   if m.get('disclosed') is False]
        quarters = sorted({c['period_end'] for c in (state.get('numeric_context') or {}).get('cells', [])
                           if c.get('status') == 'filed' and c.get('period_shape') == 'quarter'})[-8:]
        # Put the executable acquisition need first so the ordinary lane's
        # bounded failure reason carries it into the next planner prompt.
        hint = ('Declared driver source required: ' + ', '.join(report['source_required_driver_refs'])
                + '; read held quarterly/annual filing operating disclosures for '
                + ', '.join(metrics[:4]) + '; historical quarter ends ' + ', '.join(quarters)
                + '. Preserve reported facts; obtain exact quantities/prices with period, unit and source, '
                  'then the normal model must declare an executable plan. Do not buy an explanation-only correction.')
        return CompanyModelSpecError(hint, code=SOURCE_CODE, proof=report)
    if report['status'] == 'complete' and report['findings']:
        return CompanyModelSpecError('joint required fields: ' + ', '.join(f['rule'] for f in report['findings']),
                                     code=ERROR_CODE, proof=report)
    return None


def validate_parent(body, proof):
    from .company_model_spec import CompanyModelSpecError
    expected = diagnose(body, state_hash=proof.get('state_hash'), classification=proof.get('classification'),output_schema=proof.get('output_schema'))
    if expected['status'] != 'complete' or not expected['findings'] or canonical_json(expected) != canonical_json(proof):
        raise CompanyModelSpecError('joint diagnostic proof differs from its exact parent')


def validate_frozen_parent(body, proof, *, state_hash, root_prompt):
    if proof.get('state_hash') != state_hash or proof.get('classification') != classification_from_prompt(root_prompt):
        raise ValueError('joint diagnostic input binding differs')
    from .company_model_spec import OUTPUT_SCHEMA,repair_output_schema_from_prompt
    if proof.get('output_schema',OUTPUT_SCHEMA)!=repair_output_schema_from_prompt(root_prompt):
        raise ValueError('joint diagnostic schema differs from its authenticated root input')
    validate_parent(body, proof)


def assert_only_allowed_changed(original, repaired, proof):
    from .company_model_spec import CompanyModelSpecError
    validate_parent(original, proof)
    frozen = copy.deepcopy(repaired)
    try:
        paths = set()
        for finding in proof['findings']:
            values = []
            for path, old in zip(finding['paths'], finding['original_values'], strict=True):
                if tuple(path) in paths or _value(original, path) != old:
                    raise ValueError('ambiguous path')
                paths.add(tuple(path))
                values.append(_value(repaired, path))
                node = frozen
                for key in path[:-1]:
                    node = node[key]
                if old['present']:
                    node[path[-1]] = copy.deepcopy(old['value'])
                else:
                    node.pop(path[-1], None)
            rule = finding['rule']
            if rule == 'required_expense_explanation':
                value = values[0]['value']
                if not values[0]['present'] or not isinstance(value, str) or not value.strip() or len(value) > 400:
                    raise ValueError('required explanation missing')
            elif rule == 'empty_optional_bound_slot_reason':
                if values[0]['present']:
                    raise ValueError('optional empty reason must be omitted')
            elif rule in {'shares_annual_semantics', 'eps_annual_semantics'}:
                if not values[0]['present'] or values[0]['value'] not in finding['choices'][0]:
                    raise ValueError('invalid annual choice')
            elif rule == 'operating_cash_base_reference':
                method, base, because = [v['value'] for v in values]
                if not all(v['present'] for v in values) or method != 'unavailable' or base is not None or not isinstance(because, str) or not because.strip() or len(because) > 400:
                    raise ValueError('invalid cash choice')
            else:
                raise ValueError('unknown rule')
        if canonical_json(frozen) != canonical_json(original):
            raise ValueError('whole body drift')
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CompanyModelSpecError('joint repair changed fields outside its exact allowed paths') from exc


def open_source_request(*, missions, model, state, mission, call, state_dir):
    """A regular bounded material need, not numerical evidence or retry authority.

    Only the exact already-successful original model Work can open this need.
    Current mission/source input is rechecked; failed, unsettled and repaired
    parents cannot create this entry. Acquisition keeps its normal owner gates.
    """
    import hashlib
    from .company_model_spec import parse_response, build_prompt, TASK_HASH, MAX_OPERATING_METRICS
    from .planner_paid_output_recovery import _authority_connection
    from .model_spec_maintenance_repair import _load, _proof
    from .model_spec_source_sign_projection import _permission, _settled_success_history
    from .material_requests import open_request
    if content_hash({k: v for k, v in state.items() if k != 'state_hash'}) != state['state_hash']:
        raise ValueError('declared driver source request current state hash differs')
    _permission(missions, mission, state['company_ref'])
    with _authority_connection(model.scheduler_db) as connection:
        work, held = _load(connection, call['work_order_ref'])
        from .company_model_cli import validate_model_spec_request_identity
        identity = validate_model_spec_request_identity(work['metadata'].get('model_spec_request_identity', {}))
        if (not isinstance(identity, Mapping) or identity.get('state_hash') != state['state_hash']
                or identity.get('task_hash') != TASK_HASH or work['question'] != build_prompt(state)
                or work['metadata'].get('mission_version_ref') != mission['id']
                or work['metadata'].get('mission_version_hash') != mission['content_hash']
                or work['metadata'].get('structured_output_repair') is not None
                or _proof(held) != _proof(call) or held['text'] != call['text']):
            raise ValueError('declared driver source request original/input differs')
        siblings = connection.execute(
            "SELECT work_order_id FROM scheduler_work_orders WHERE work_order_id >= 'work:cockpit-model_spec-' "
            "AND work_order_id < 'work:cockpit-model_spec.' AND "
            "json_extract(work_order_json,'$.metadata.structured_output_repair.root_original.work_order_ref')=? LIMIT 1",
            (work['id'],)).fetchall()
        if siblings:
            raise ValueError('declared driver source request requires the unrepaired original root')
        report = from_state(parse_response(held['text']), state)
        if report['status'] != 'source_required':
            raise ValueError('declared driver source request lacks the exact diagnostic')
        fees = _settled_success_history(model, connection, work, held, mission)
    anchor = {'contract_ref': CONTRACT_REF, 'company_ref': state['company_ref'],
              'state_hash': state['state_hash'], 'root_original': _proof(held),
              'original_text_sha256': hashlib.sha256(held['text'].encode()).hexdigest(),
              'diagnostics_hash': content_hash(report), 'paid_history_hash': content_hash(fees),
              'mission_version_ref': mission['id'], 'mission_version_hash': mission['content_hash'],
              'financial_evidence': False, 'repair_authority': False}
    plans = source_request_plans(parse_response(held['text']), state, anchor)
    output = []
    for plan in plans:
        request = open_request(state_dir, **plan)
        output.append({'id': request['id'], 'status': request['status'],
                       'write_status': request['write_status'],
                       'anchor': plan['acceptance']['model_driver_source_request'], 'need': plan['need']})
    return output


def source_request_plans(body, state, anchor):
    """Pure queue descriptors only; opening them requires the strong owner path.

    These are acquisition tasks, not an assertion that the quantities are
    disclosed or an authority to put passages into model inputs. The normal
    reader/numeric/source-period lineage must still prove those inputs.
    """
    from .company_model_spec import MAX_OPERATING_METRICS
    report = from_state(body, state)
    metrics = [m for m in body['operating_metrics'] if m.get('disclosed') is False]
    quarters = sorted({(c['period_start'], c['period_end'])
                       for c in (state.get('numeric_context') or {}).get('cells', [])
                       if c.get('status') == 'filed' and c.get('period_shape') == 'quarter'
                       and c.get('period_start') and c.get('period_end')})[-8:]
    if report['status'] != 'source_required' or not metrics or not quarters:
        raise ValueError('declared driver source request has no explicit metric/period need')
    plans = []
    # Separate exact metrics/quarters: a paragraph for one metric or one
    # quarter must not close an omnibus eight-quarter acquisition need.
    # Both dimensions are bounded by the existing input/schema contracts.
    for metric in metrics[:MAX_OPERATING_METRICS]:
        for start, end in quarters:
            need = {'kind': 'data', 'doc_kinds': ['sec_10q', 'sec_10k', 'transcript'],
                'data_item': metric['label'],
                'question': ('Read original operating disclosures for declared driver(s) '
                    + ', '.join(report['source_required_driver_refs']) + ': '
                    + metric['label'] + ' [' + metric['ref'] + '; ' + metric['unit'] + ']'
                    + '. Exact historical quarter ' + start + '/' + end
                    + '. Obtain the exact disclosed quantity/price/cost/utilization, full scope and source; '
                      'preserve unavailable metrics. No inferred residual, proxy or invented formula '
                      'is a company actual.')}
            scope = {**anchor, 'metric': copy.deepcopy(metric), 'period_start': start, 'period_end': end}
            plans.append({'subject': state['company_ref'], 'need': need,
                'period': 'quarter_ending:' + end,
                'requester': {'lane': 'company_model_spec', 'product_ref': anchor['root_original']['work_order_ref'],
                    'run_ref': state['state_hash'], 'section': 'declared_driver_history'},
                'source_priority': ['indexed_corpus', 'sec_edgar', 'alphaengine', 'web'],
                'acceptance': {'min_passages': 1, 'about_company': True,
                    'model_driver_source_request': scope,
                    'model_input_admission': 'Material availability is not numerical/model authority; exact source, period, full dimensions, units and financial validation remain required.'}})
    return plans
