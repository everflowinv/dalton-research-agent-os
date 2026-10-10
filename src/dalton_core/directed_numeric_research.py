"""Question-scoped numeric targets; they do not register global market metrics.

A two-source requirement governs long-term extraction priorities, not a single
source-proved answer. Each target definition is a hypothesis bound by the exact
planner result/admission/Work; an independent verifier must prove its mapping.
"""
from __future__ import annotations
import re
from typing import Any, Mapping, Sequence
from .store import content_hash
from .document_numeric_claim import ALLOWED_BASES
CONTRACT_REF='directed-document-numeric:0.1'
MAX_TARGETS=8
TARGET_FIELDS=frozenset({'target_ref','metric_ref','metric_definition','period','unit','currency','basis'})
HASH_RE=re.compile(r'[0-9a-f]{64}\Z')
METRIC_RE=re.compile(r'metric:[a-z0-9]+(?:-[a-z0-9]+)*\Z')
class DirectedNumericError(ValueError):pass

def numeric_target_schema() -> dict[str, Any]:
    """Describe the same target vocabulary that the admission gate accepts."""
    from .document_numeric_unit_contracts import numeric_units
    text = {'type': 'string', 'minLength': 1, 'maxLength': 200, 'pattern': r'\S'}
    return {
        'type': 'array', 'minItems': 1, 'maxItems': MAX_TARGETS,
        'items': {
            'type': 'object', 'additionalProperties': False,
            'required': sorted(TARGET_FIELDS),
            'properties': {
                'target_ref': dict(text),
                'metric_ref': {**text, 'pattern': r'^metric:[a-z0-9]+(?:-[a-z0-9]+)*$',
                               'description': 'Use metric: followed by lowercase words separated by hyphens; no underscores.'},
                'metric_definition': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['label', 'meaning'],
                    'properties': {'label': dict(text), 'meaning': {**text, 'maxLength': 600}},
                },
                'period': {**text, 'description': 'The exact period sought in the original disclosure.'},
                'unit': {'type': 'string', 'enum': list(numeric_units(CONTRACT_REF))},
                'currency': {'type': ['string', 'null'],
                             'description': 'Three uppercase letters when unit is currency; null for every other unit.'},
                'basis': {'type': 'string', 'enum': list(ALLOWED_BASES)},
            },
            'if': {'properties': {'unit': {'const': 'currency'}}},
            'then': {'properties': {'currency': {'type': 'string', 'pattern': '^[A-Z]{3}$'}}},
            'else': {'properties': {'currency': {'type': 'null'}}},
        },
    }

def normalize_targets(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value,list) or not 1<=len(value)<=MAX_TARGETS:
        raise DirectedNumericError('numeric_targets must contain 1..8 required targets')
    output=[];refs=set();identities=set();definitions={}
    for target in value:
        if not isinstance(target,Mapping) or set(target) not in (TARGET_FIELDS,TARGET_FIELDS|{'metric_definition_hash'}):
            raise DirectedNumericError('numeric target has invalid closed shape')
        for field in ('target_ref','metric_ref','period','unit','basis'):
            if not isinstance(target[field],str) or not target[field].strip() or len(target[field])>200:
                raise DirectedNumericError(f'numeric target {field} is invalid')
        if METRIC_RE.fullmatch(target['metric_ref']) is None:raise DirectedNumericError('numeric target metric identity is invalid')
        definition=target['metric_definition']
        if not isinstance(definition,Mapping) or set(definition)!={'label','meaning'}:
            raise DirectedNumericError('question-scoped metric definition has invalid shape')
        for key in ('label','meaning'):
            if not isinstance(definition[key],str) or not definition[key].strip() or len(definition[key])>(200 if key=='label' else 600):
                raise DirectedNumericError('question-scoped metric definition is invalid')
        digest=content_hash({'scope':'question','metric_ref':target['metric_ref'],
                             'metric_definition':dict(definition),'unit':target['unit']})
        if 'metric_definition_hash' in target and target['metric_definition_hash']!=digest:
            raise DirectedNumericError('numeric target definition hash differs')
        prior=definitions.get(target['metric_ref'])
        if prior is not None and prior!=digest:raise DirectedNumericError('one question gives a metric incompatible definitions')
        definitions[target['metric_ref']]=digest
        if target['basis'] not in ALLOWED_BASES:raise DirectedNumericError('numeric target basis is unsupported')
        from .document_numeric_unit_contracts import numeric_units
        if target['unit'] not in numeric_units(CONTRACT_REF):raise DirectedNumericError('numeric target unit is unsupported')
        currency=target['currency']
        if target['unit']=='currency':
            if not isinstance(currency,str) or re.fullmatch(r'[A-Z]{3}',currency) is None:
                raise DirectedNumericError('currency target needs an explicit currency')
        elif currency is not None:raise DirectedNumericError('non-currency target must not carry a currency')
        identity=tuple(target[key] for key in ('metric_ref','period','unit','currency','basis'))
        if target['target_ref'] in refs or identity in identities:raise DirectedNumericError('numeric targets duplicate an identity')
        refs.add(target['target_ref']);identities.add(identity)
        output.append({**dict(target),'metric_definition':dict(definition),'metric_definition_hash':digest})
    return output

def coverage(targets:Sequence[Mapping[str,Any]], figures:Sequence[Mapping[str,Any]], *,
             company_ref:str,document_ref:str,source_manifest_hash:str) -> dict[str,Any]:
    """Count only source-proved Figures mapped by the exact admitted definition.

    Execution replays these mappings from the succeeded independently verified
    Work. A bare ledger row or caller copy is not a completion binding.
    """
    completed=[];pending=[]
    for target in normalize_targets(list(targets)):
        found=[f for f in figures if f.get('company_ref')==company_ref and f.get('document_ref')==document_ref
               and f.get('source_manifest_hash')==source_manifest_hash
               and isinstance(f.get('figure_id'),str) and f['figure_id']
               and isinstance(f.get('content_hash'),str) and HASH_RE.fullmatch(f['content_hash'])
               and all(f.get(k)==target[k] for k in ('metric_ref','metric_definition_hash','period','unit','currency','basis'))]
        if len(found)==1:completed.append({'target_ref':target['target_ref'],'figure_ref':found[0]['figure_id'],'figure_hash':found[0]['content_hash'],
                                          'metric_definition_hash':target['metric_definition_hash']})
        else:pending.append({'target_ref':target['target_ref'],'reason':'required_numeric_target_unavailable' if not found else 'required_numeric_target_ambiguous'})
    return {'status':'completed' if not pending else 'waiting_evidence','completed_targets':completed,'pending_targets':pending}

def numeric_rejection(connection,admission_ref):
    """One exact published local rejection, never a caller diagnosis."""
    from .store import canonical_json
    import json,sqlite3
    from .research_auto_commit import DOCUMENT_QUALITATIVE_RULE_REF
    try:
        row=connection.execute('SELECT * FROM mission_document_research_candidate_rejections WHERE admission_ref=?',(admission_ref,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return None
        raise
    if row is None:return None
    value=json.loads(row['record_json']);body=dict(value);digest=body.pop('content_hash',None)
    parent=connection.execute('SELECT record_json,content_hash FROM mission_document_research_admissions WHERE admission_id=?',(admission_ref,)).fetchone()
    if parent is None:raise DirectedNumericError('numeric rejection parent is absent')
    admission=json.loads(parent['record_json']);admission_body=dict(admission);parent_hash=admission_body.pop('content_hash',None)
    if canonical_json(value)!=row['record_json'] or digest!=row['content_hash'] or digest!=content_hash(body) or value['admission_ref']!=admission_ref or parent_hash!=parent['content_hash'] or parent_hash!=content_hash(admission_body) or value['admission_hash']!=parent_hash:
        raise DirectedNumericError('numeric rejection proof differs')
    if (value.get('rule_ref')!=DOCUMENT_QUALITATIVE_RULE_REF
            or value.get('reason')!='document qualitative rule admits no numeric statement'
            or admission.get('numeric_authority') is not None):return None
    outcome=connection.execute('SELECT record_json,content_hash FROM mission_document_research_outcomes WHERE admission_ref=?',(admission_ref,)).fetchone()
    if outcome is None:raise DirectedNumericError('numeric rejection has no staged outcome')
    wire=json.loads(outcome['record_json']);outcome_body=dict(wire);outcome_hash=outcome_body.pop('content_hash',None)
    if outcome_hash!=outcome['content_hash'] or outcome_hash!=content_hash(outcome_body) or wire['id']!=value['outcome_ref'] or outcome_hash!=value['outcome_hash'] or any(wire[k]!=value[k] for k in ('candidate_evidence_ref','candidate_evidence_hash','candidate_claim_ref','candidate_claim_hash')):
        raise DirectedNumericError('numeric rejection staging proof differs')
    return {'rejection_ref':value['id'],'rejection_hash':digest,'admission_ref':admission_ref,'admission_hash':parent_hash,
            'rule_ref':value['rule_ref'],'reason':value['reason']}

def pending_numeric_result(connection,admission_ref):
    """A formally settled, still incomplete numeric result and its exact parent."""
    import json,sqlite3
    from .store import canonical_json
    try:
        row=connection.execute('SELECT n.record_json,n.content_hash,a.record_json AS admission_json,a.content_hash AS parent_hash '
            'FROM mission_document_numeric_results n JOIN mission_document_research_admissions a ON a.admission_id=n.admission_ref WHERE n.admission_ref=?',(admission_ref,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return None
        raise
    if row is None:return None
    result=json.loads(row['record_json']);body=dict(result);digest=body.pop('content_hash',None)
    parent=json.loads(row['admission_json']);parent_body=dict(parent);parent_hash=parent_body.pop('content_hash',None)
    if canonical_json(result)!=row['record_json'] or digest!=row['content_hash'] or digest!=content_hash(body) or parent_hash!=row['parent_hash'] or parent_hash!=content_hash(parent_body) or result['admission_hash']!=parent_hash or result['admission_ref']!=admission_ref:
        raise DirectedNumericError('pending numeric result binding differs')
    formal=connection.execute('SELECT result_envelope_json,result_envelope_hash,terminal_state FROM scheduler_formal_results WHERE work_order_id=?',(result['work_order_ref'],)).fetchone()
    if formal is None:return None
    envelope=json.loads(formal['result_envelope_json'])
    if formal['terminal_state']!='succeeded' or content_hash(envelope)!=formal['result_envelope_hash'] or envelope['outputs']!=result:
        raise DirectedNumericError('pending numeric result lacks exact formal settlement')
    if result['status']!='waiting_evidence' or not result['pending_targets']:return None
    return {'admission_ref':admission_ref,'admission_hash':parent_hash,'numeric_result_hash':digest,'parent':parent,'result':result}
