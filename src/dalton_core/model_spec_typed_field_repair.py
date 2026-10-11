"""Exact-parent field corrections, never numeric or evidence rewrites."""
from __future__ import annotations
import copy
from collections.abc import Mapping
from .store import content_hash

CONTRACT_REF = 'model-spec-typed-field-repair:0.1'
PERIODICITY_CODE = 'operating_metric_periodicity'
ACQUISITIONS_CODE = 'acquisitions_forecast_method'
IDENTITY_CODE = 'revenue_driver_identity'
COST_SLOT_PAIR_CODE = 'expense_cost_slot_pair'
COST_SLOT_PAIR_REF = 'model-spec-cost-slot-exclusive-pair:0.1'
ERROR_CODES = (PERIODICITY_CODE, ACQUISITIONS_CODE, IDENTITY_CODE, COST_SLOT_PAIR_CODE)
_MISSING = object()


def proof(body, code, paths, *, detail):
    return {'contract_ref': CONTRACT_REF, 'parent_body_hash': content_hash(body),
            'code': code, 'paths': copy.deepcopy(paths), 'detail': detail}


def cost_slot_pair_scope(state, body, index):
    """A legal template name plus a valid reason is an inconsistent pair.

    Template membership is not proof of an executable economic base. The
    model must choose whether to retain that slot or explicitly leave it open.
    """
    from .driver_template import cost_slot_ids, template_for
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    try:
        line = body['expense_lines'][index]
        slot, reason = line.get('cost_driver_slot'), line.get('cost_driver_unbound_reason')
        classification = template_for(state.get('industry_classification'))['classification']
        if (not isinstance(slot, str) or slot not in cost_slot_ids(classification)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 400):
            return None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return proof(body, COST_SLOT_PAIR_CODE, [
        ['expense_lines', index, 'cost_driver_slot'],
        ['expense_lines', index, 'cost_driver_unbound_reason']], detail={
            'contract_ref': COST_SLOT_PAIR_REF, 'state_hash': state['state_hash'],
            'classification': classification, 'original_slot': slot,
            'instruction': 'Keep the original slot and omit the unbound reason, or use null slot with a nonempty unbound reason. Do not change other fields; a valid slot name alone does not prove its economic base.'})


def _member_refs(state, concept, identity):
    from .company_model_identity_catalog import resolve_identity, cell_identity_ref
    resolved = resolve_identity(state, concept, identity)
    if not resolved['dimensions']:
        raise ValueError('consolidated source is not a member')
    dimensions = identity.get('dimensions')
    if isinstance(dimensions, Mapping) and set(dimensions) == {'catalog_ref'}:
        refs = {dimensions['catalog_ref']}
    else:
        refs = {cell_identity_ref(cell) for cell in state['numeric_context']['cells']
                if cell.get('concept') == concept and cell.get('statement') == resolved['statement']
                and cell.get('dimensions') == resolved['dimensions'] and cell.get('status') == 'filed'}
    return resolved, refs


def revenue_identity_scope(state, body):
    """Only a new, exact source-group projection can authorize identity repair.

    Legacy inputs have no new write gate. Group choices are reconstructed from
    the admitted cells, not trusted from a model-provided marker or label.
    """
    from .model_revenue_group_candidates import CONTRACT_REF as GROUP_REF, candidates
    context = state.get('numeric_context') or {}
    groups = context.get('revenue_group_candidates')
    if not isinstance(groups, Mapping) or groups.get('contract_ref') != GROUP_REF:
        return None
    if content_hash(groups) != content_hash(candidates(context.get('cells') or [])):
        raise ValueError('revenue source-group choices do not replay from this exact input')
    drivers = body.get('revenue_drivers')
    if not isinstance(drivers, list):
        return None
    paths, choices, identities, selected_groups, invalid = [], {}, set(), {}, False
    for index, driver in enumerate(drivers):
        if not isinstance(driver, Mapping) or driver.get('kind') != 'segment':
            continue
        concept = driver.get('basis_concept')
        if not concept:
            continue  # A declared unavailable base stays unavailable, not invented.
        refs = sorted({ref for group in groups['groups'] if group['concept'] == concept
                       for member in group['members']
                       for ref in [member['identity_ref'], *(member.get('equivalent_refs') or [])]})
        paths.append(['revenue_drivers', index, 'basis_identity'])
        choices[str(index)] = refs
        identity = driver.get('basis_identity')
        try:
            if not isinstance(identity, Mapping):
                raise ValueError('missing source identity')
            resolved, selected_refs = _member_refs(state, concept, identity)
            if not selected_refs.intersection(refs):
                raise ValueError('not an exact disclosed member choice')
            matching = {g['group_ref'] for g in groups['groups'] if g['concept'] == concept
                        and any(selected_refs.intersection([m['identity_ref'], *(m.get('equivalent_refs') or [])])
                                for m in g['members'])}
            selected_groups[concept] = selected_groups.get(concept, matching).intersection(matching)
            if not selected_groups[concept]:
                raise ValueError('members selected from unrelated source groups')
            key = content_hash({'concept': concept, **resolved})
            if not resolved['dimensions'] or key in identities:
                raise ValueError('duplicate or consolidated source base')
            identities.add(key)
        except ValueError:
            invalid = True
    if not invalid:
        return None
    # An absent disclosure is a real gap, not an authorized guess/paid repair.
    if any(not refs for refs in choices.values()):
        raise ValueError('segment driver lacks an admitted dimensional revenue source; do not use the consolidated total')
    return proof(body, IDENTITY_CODE, paths, detail={
        'state_hash': state['state_hash'], 'source_groups_hash': content_hash(groups),
        'allowed_catalog_refs': choices,
        'instruction': 'Select exact source-bound member identities; do not infer from labels, invent a residual, or reuse a consolidated total. Preserve all other fields.'})


def _at(body, path):
    value = body
    for part in path[:-1]:
        value = value[part]
    return value.get(path[-1], _MISSING)


def validate_parent(body, scope, *, state):
    from .company_model_spec import CompanyModelSpecError
    def refuse():
        raise CompanyModelSpecError('typed repair scope is not the exact closed parent error', code='semantic')
    if (not isinstance(scope, Mapping) or set(scope) != {'contract_ref', 'parent_body_hash', 'code', 'paths', 'detail'}
            or scope['contract_ref'] != CONTRACT_REF or scope['parent_body_hash'] != content_hash(body)
            or scope['code'] not in ERROR_CODES or not isinstance(scope['paths'], list) or not scope['paths']):
        refuse()
    try:
        if scope['code'] == COST_SLOT_PAIR_CODE:
            path = scope['paths'][0]
            if (len(scope['paths']) != 2 or len(path) != 3 or path[0] != 'expense_lines'
                    or content_hash(scope) != content_hash(cost_slot_pair_scope(state, body, path[1]))):
                refuse()
            return
        if scope['code'] == IDENTITY_CODE:
            if content_hash(scope) != content_hash(revenue_identity_scope(state, body)):
                refuse()
            return
        indices = set()
        for path in scope['paths']:
            if not isinstance(path, list):
                refuse()
            if scope['code'] == PERIODICITY_CODE:
                if len(path) != 3 or path[0] != 'operating_metrics' or path[-1] != 'periodicity' or len(scope['paths']) != 1:
                    refuse()
                index = path[1]
            else:
                if len(path) != 4 or path[:2] != ['financial_statement_structure', 'lines'] or path[-1] not in ('forecast_method', 'forecast_base_ref'):
                    refuse()
                index = path[2]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                refuse()
            indices.add(index)
            if scope['code'] == PERIODICITY_CODE:
                if _at(body, path) in ('quarterly', 'annual'):
                    refuse()
            else:
                line = body['financial_statement_structure']['lines'][index]
                if (line.get('role') != 'acquisitions' or line.get('kind') != 'filed'
                        or line.get('statement') != 'cash' or line.get('forecast_method') != 'zero_one_off'):
                    refuse()
        if scope['code'] == ACQUISITIONS_CODE and (
                len(indices) != 1 or len(scope['paths']) != 2
                or {p[-1] for p in scope['paths']} != {'forecast_method', 'forecast_base_ref'}):
            refuse()
    except (KeyError, IndexError, TypeError, ValueError):
        refuse()


def assert_only_typed_fields_changed(original, repaired, scope, *, state):
    from .company_model_spec import CompanyModelSpecError
    def refuse():
        raise CompanyModelSpecError('typed field repair changed an unrelated field or its exact parent', code='semantic')
    validate_parent(original, scope, state=state)
    frozen = copy.deepcopy(repaired)
    try:
        if scope['code'] == COST_SLOT_PAIR_CODE:
            index = scope['paths'][0][1]
            line = repaired['expense_lines'][index]
            if not isinstance(line, Mapping):
                refuse()
            slot, reason = line.get('cost_driver_slot', _MISSING), line.get('cost_driver_unbound_reason', _MISSING)
            if not ((slot == scope['detail']['original_slot'] and reason is _MISSING)
                    or (slot is None and isinstance(reason, str) and reason.strip() and len(reason) <= 400)):
                refuse()
        for path in scope['paths']:
            old, new = _at(original, path), _at(repaired, path)
            if scope['code'] == PERIODICITY_CODE and new not in ('quarterly', 'annual'):
                refuse()
            if scope['code'] == ACQUISITIONS_CODE and path[-1] == 'forecast_method' and new not in (
                    'carry_forward_mean', 'year_over_year_growth', 'seasonal_factor', 'share_of_line', 'unavailable'):
                refuse()
            if scope['code'] == IDENTITY_CODE:
                refs = scope['detail']['allowed_catalog_refs'][str(path[1])]
                if not isinstance(new, Mapping):
                    refuse()
                concept = original['revenue_drivers'][path[1]]['basis_concept']
                _, selected_refs = _member_refs(state, concept, new)
                if not selected_refs.intersection(refs):
                    refuse()
            target = frozen
            for part in path[:-1]:
                target = target[part]
            if old is _MISSING:
                target.pop(path[-1], None)
            else:
                target[path[-1]] = copy.deepcopy(old)
    except (KeyError, IndexError, TypeError, ValueError):
        refuse()
    if content_hash(frozen) != content_hash(original):
        refuse()


def validate_frozen_parent(body, scope, *, state_hash, root_prompt):
    """Reprove transported scope from the immutable paid root's input.

    The producer rederives the choices from full state; admission independently
    uses the choices frozen in that root Work, never caller-supplied choices.
    The repaired response still undergoes full-state and financial validation.
    """
    import json
    from .company_model_spec import CompanyModelSpecError
    from .model_revenue_group_candidates import CONTRACT_REF as GROUP_REF
    def refuse():
        raise CompanyModelSpecError("typed repair proof differs from the frozen parent input", code="semantic")
    if not isinstance(scope, Mapping) or scope.get("code") not in ERROR_CODES:
        refuse()
    code = scope["code"]
    if code == COST_SLOT_PAIR_CODE:
        from .driver_template import cost_slot_ids, template_for
        headers = [line for line in root_prompt.splitlines() if line.startswith('COST DRIVER TEMPLATE (')]
        if len(headers) != 1 or not headers[0].endswith('):'):
            refuse()
        classification = headers[0][len('COST DRIVER TEMPLATE ('):-2]
        if template_for(classification)['classification'] != classification:
            refuse()
        block = root_prompt.split(headers[0], 1)[1].split('\n\n', 1)[0]
        slots = [line.split('\t')[0] for line in block.splitlines()
                 if len(line.split('\t')) == 4 and not line.startswith('<cost slot id>')]
        if slots != list(cost_slot_ids(classification)):
            refuse()
        validate_parent(body, scope, state={'state_hash': state_hash, 'industry_classification': classification})
        return
    if code != IDENTITY_CODE:
        validate_parent(body, scope, state={})
        detail = (
            "Correct this temporal enum only; do not change metric, source, period, or any other field."
            if code == PERIODICITY_CODE else
            "Choose a supported cash-line method and base, or unavailable with null base; preserve every filed authority and amount."
        )
        if content_hash(scope) != content_hash(proof(body, code, scope["paths"], detail=detail)):
            refuse()
        return
    contexts = [json.loads(line[len("CONTEXT="):]) for line in root_prompt.splitlines()
                if line.startswith("CONTEXT=")]
    if len(contexts) != 1:
        refuse()
    groups = contexts[0].get("revenue_group_candidates")
    if not isinstance(groups, Mapping) or groups.get("contract_ref") != GROUP_REF:
        refuse()
    paths, choices = [], {}
    for index, driver in enumerate(body.get("revenue_drivers") or []):
        if not isinstance(driver, Mapping) or driver.get("kind") != "segment" or not driver.get("basis_concept"):
            continue
        refs = sorted({ref for group in groups["groups"] if group["concept"] == driver["basis_concept"]
                       for member in group["members"]
                       for ref in [member["identity_ref"], *(member.get("equivalent_refs") or [])]})
        if not refs:
            refuse()
        paths.append(["revenue_drivers", index, "basis_identity"])
        choices[str(index)] = refs
    if not paths:
        refuse()
    expected = proof(body, code, paths, detail={
        "state_hash": state_hash, "source_groups_hash": content_hash(groups),
        "allowed_catalog_refs": choices,
        "instruction": "Select exact source-bound member identities; do not infer from labels, invent a residual, or reuse a consolidated total. Preserve all other fields."})
    if content_hash(scope) != content_hash(expected):
        refuse()
