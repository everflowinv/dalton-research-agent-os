"""Reprove a transported financial correction before its normal paid admission.

The capability is installed by the financial validation owner, never accepted
from a request binding. It adds no slot, budget, policy or changed field.
"""
from copy import deepcopy
from collections.abc import Mapping
from .store import canonical_json

CONTRACT_REF = 'model-spec-financial-repair-admission:0.1'

class FinancialRepairAuthority:
    def __init__(self, *, state, mission, decided_by, financial_validator):
        self._state=deepcopy(dict(state))
        self._mission=deepcopy(dict(mission))
        self._decided_by=decided_by
        self._validate=financial_validator

    def verify(self, binding, parent_text, *, mission_binding):
        from .company_model_spec import spec_from_response,CompanyModelSpecError,parse_response
        from .model_spec_signed_expense_repair import ERROR_CODE as SIGNED,validate_coefficient_parent
        from .cash_flow_ratio_applicability import ERROR_CODE as RATIO,validate_parent
        error=binding['validation_error']
        if mission_binding != (self._mission.get('id'), self._mission.get('content_hash')):
            raise ValueError('financial repair owner mission differs')
        if binding['state_hash']!=self._state['state_hash']:
            raise ValueError('financial repair owner state differs')
        code=error['code']
        if code==SIGNED:
            import json
            proof=json.loads(error['message'])
            if not isinstance(proof,Mapping):raise ValueError('signed financial proof is missing')
            validate_coefficient_parent(parse_response(parent_text),proof)
        elif code==RATIO:
            proof=binding.get('cash_ratio_proof')
            if not isinstance(proof,Mapping):raise ValueError('cash ratio financial proof is missing')
            validate_parent(parse_response(parent_text),proof)
            if proof.get('company_ref')!=self._state['company_ref'] or proof.get('state_hash')!=self._state['state_hash']:
                raise ValueError('cash ratio financial proof source/company differs')
        else:raise ValueError('financial repair code is unsupported')
        # Rebuild from the parent's immutable raw answer and current strong
        # originals. A caller's self-rehashed proof is never the authority.
        spec=spec_from_response(self._state,parent_text,decided_by=self._decided_by)
        try:self._validate(spec)
        except CompanyModelSpecError as actual:
            rebuilt=getattr(actual,'proof',None)
            if (actual.code!=code or str(actual)!=error['message'] or not isinstance(rebuilt,Mapping)
                    or canonical_json(rebuilt)!=canonical_json(proof)):
                raise ValueError('financial repair differs from source-rebuilt diagnosis') from actual
        else:raise ValueError('financial repair parent has no source-proved failure')
        return deepcopy(dict(proof))
