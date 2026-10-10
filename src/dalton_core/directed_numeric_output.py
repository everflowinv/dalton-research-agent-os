"""Closed numeric directed-reading output; no prose-to-number authority."""
from __future__ import annotations
import hashlib
import copy
import json
from typing import Any, Mapping
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError, normalize_targets
from .document_numeric_claim import verify_numeric_candidate, _SCALE_WORDS
from .store import canonical_json, content_hash

PROVIDER_CONTRACT_REF = 'mission-document-numeric-verifier-provider-output-0.1'
SCHEMA_RESOURCE = 'mission-document-numeric-verifier-provider-output-v0.1.schema.json'
TARGET_IDENTITY_FIELDS = ('metric_ref', 'period', 'unit', 'currency', 'basis')
DRAFT_IDENTITY_CONTRACT = 'directed-numeric-target-identity:0.1'

NUMERIC_FIGURE_FIELDS = ['quote_id','metric_ref','subject_as_named','as_reported_label','value',
                         'unit','currency','period','basis','scale']
FIGURE_SCHEMA = {'type':'object','additionalProperties':False,'required':NUMERIC_FIGURE_FIELDS,
                'properties':{key:{'type':'string'} for key in NUMERIC_FIGURE_FIELDS}}
from .document_numeric_unit_contracts import numeric_units
FIGURE_SCHEMA['properties']['unit']={'enum':list(numeric_units(CONTRACT_REF))}
FIGURE_SCHEMA['properties'].update({'currency':{'type':['string','null']},
                                   'scale':{'type':['string','null']}})
DRAFT_SCHEMA = {'type':'object','additionalProperties':False,'required':['schema_version','targets'],
               'properties':{'schema_version':{'const':CONTRACT_REF},'targets':{'type':'array','maxItems':8,
               'items':{'type':'object','additionalProperties':False,
                        'required':['target_ref','status','figure','reason'],
                        'properties':{'target_ref':{'type':'string'},'status':{'enum':['supported','unavailable']},
                                      'figure':{'anyOf':[FIGURE_SCHEMA,{'type':'null'}]},
                                      'reason':{'type':['string','null']}}}}}}
# Published pre-scale prompt schemas must remain byte-replayable.
LEGACY_DRAFT_SCHEMA = copy.deepcopy(DRAFT_SCHEMA)
FIGURE_SCHEMA['properties']['scale'] = {'enum': [None, *sorted(_SCALE_WORDS)]}
SCALE_CONTRACT_REF = 'directed-numeric-canonical-scale:0.1'
SCALE_INSTRUCTIONS = ('The scale field uses only null, thousand, million, billion or trillion. '
    'For source wording such as in thousands, record canonical scale thousand; likewise '
    'millions/billions/trillions use million/billion/trillion. Preserve the reported digits '
    'and exact source quote unchanged. This encodes the stated multiplier, not a numeric '
    'conversion. Use null when no multiplier applies; never invent a multiplier.')

VERIFIER_SCHEMA = {'type':'object','additionalProperties':False,
                  'required':['schema_version','candidate_ref','verdict','targets'],
                  'properties':{'schema_version':{'const':'0.1'},'candidate_ref':{'type':'string'},
                                'verdict':{'enum':['pass','reject']},'targets':{'type':'array','maxItems':8,
                      'items':{'type':'object','additionalProperties':False,'required':['target_ref','verdict','reason'],
                               'properties':{'target_ref':{'type':'string'},'verdict':{'enum':['accept','reject','unavailable']},
                                             'reason':{'type':['string','null']}}}}}}

def _json(text):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise DirectedNumericError('duplicate JSON key')
            result[key]=value
        return result
    try:return json.loads(text, object_pairs_hook=pairs,parse_constant=lambda v:(_ for _ in ()).throw(DirectedNumericError('nonfinite JSON')))
    except (TypeError,ValueError,RecursionError) as exc:raise DirectedNumericError('numeric output is invalid JSON') from exc

def quotes(search_proof):
    return {f"quote:{m['source_start']}:{m['source_end']}:{m['excerpt_sha256'][:16]}":m['excerpt']
            for m in search_proof['matches']}

def candidate_ref(draft):return 'directed-numeric-candidate:'+content_hash(draft)

def parse_draft(text, *, metadata):
    value=_json(text);targets=normalize_targets(metadata['numeric_targets'])
    if not isinstance(value,Mapping) or set(value)!={'schema_version','targets'} or value['schema_version']!=CONTRACT_REF:
        raise DirectedNumericError('numeric draft has invalid closed shape')
    rows=value['targets']
    if not isinstance(rows,list) or len(rows)!=len(targets):raise DirectedNumericError('numeric draft must account for every required target')
    expected={t['target_ref']:t for t in targets};seen=set();source=quotes(metadata['retrieval_proof'])
    for row in rows:
        if not isinstance(row,Mapping) or set(row)!={'target_ref','status','figure','reason'} or row['target_ref'] not in expected or row['target_ref'] in seen:
            raise DirectedNumericError('numeric draft target binding differs')
        seen.add(row['target_ref']);target=expected[row['target_ref']]
        if row['status']=='unavailable':
            if row['figure'] is not None or not isinstance(row['reason'],str) or not row['reason'].strip() or len(row['reason'])>600:
                raise DirectedNumericError('unavailable target must carry no figure and a concrete reason')
            continue
        if row['status']!='supported' or row['reason'] is not None:raise DirectedNumericError('supported target has invalid status')
        figure=row['figure']
        if not isinstance(figure,Mapping):
            raise DirectedNumericError('numeric figure differs from typed target: expected an object')
        for key in TARGET_IDENTITY_FIELDS:
            if figure.get(key) != target[key]:
                def short(value):
                    if value is None:
                        return 'null'
                    if not isinstance(value, str):
                        return '<' + type(value).__name__ + '>'
                    return repr(value[:80]) + ('…' if len(value) > 80 else '')
                raise DirectedNumericError(
                    'numeric figure differs from typed target: ' + key
                    + ' expected ' + short(target[key]) + '; received ' + short(figure.get(key)))
        verify_numeric_candidate(figure,source,numeric_contract_ref=CONTRACT_REF)
    return dict(value)

def parse_verifier(text, *, metadata):
    value=_json(text);draft=metadata['draft'];rows=value.get('targets') if isinstance(value,Mapping) else None
    if not isinstance(value,Mapping) or set(value)!={'schema_version','candidate_ref','verdict','targets'} or value['schema_version']!='0.1' or value['candidate_ref']!=candidate_ref(draft) or value['verdict'] not in {'pass','reject'}:
        raise DirectedNumericError('numeric verifier binding differs')
    expected={x['target_ref']:x for x in draft['targets']};seen=set()
    if not isinstance(rows,list) or len(rows)!=len(expected):raise DirectedNumericError('numeric verifier must verify every target')
    for row in rows:
        if not isinstance(row,Mapping) or set(row)!={'target_ref','verdict','reason'} or row['target_ref'] not in expected or row['target_ref'] in seen:
            raise DirectedNumericError('numeric verifier target binding differs')
        seen.add(row['target_ref']);status=expected[row['target_ref']]['status']
        if row['verdict']=='accept':
            if status!='supported' or row['reason'] is not None:raise DirectedNumericError('verifier cannot accept an unavailable target')
        elif row['verdict'] in {'reject','unavailable'}:
            if not isinstance(row['reason'],str) or not row['reason'].strip() or len(row['reason'])>600:raise DirectedNumericError('verifier must explain rejected/missing target')
        else:raise DirectedNumericError('numeric verifier verdict is unsupported')
    if value['verdict']=='pass' and any(r['verdict']=='reject' for r in rows):raise DirectedNumericError('passing numeric verifier includes rejection')
    if value['verdict']=='reject' and not any(r['verdict']=='reject' for r in rows):raise DirectedNumericError('rejecting numeric verifier has no rejection')
    return dict(value)

# Immutable 0.1 instruction: future wording revisions need a new contract marker.
DRAFT_IDENTITY_TASK_V1 = 'Answer ALL required numeric targets solely from the exact registered excerpts. Never calculate, convert units, infer a period, assign issuer amounts, or substitute a segment. Copy target_ref and every figure identity field (metric_ref, period, unit, currency, basis) EXACTLY from identity_templates, including null currency. Do not replace a target period such as FY2025 with a source phrase such as year ended December 31, 2025; the exact quote retains that source wording. as_reported_label must remain the metric label, not a date or period description. Each output target has exactly target_ref, status, figure and reason; never copy metric_definition or add other keys to that row. Supported rows have reason null; unavailable rows have figure null and a specific reason. Templates contain identity only: fill quote_id, subject_as_named, as_reported_label, value and scale from the exact excerpt. A matching year or digits do not prove the target period, issuer or metric. If the quote does not support the exact requested identity, return unavailable with no figure. Every supported figure must bind its own metric meaning, as-reported label, quote, issuer, period, unit, currency and basis. Unsupported targets remain unavailable with a precise missing evidence reason. Numbers hidden in prose do not satisfy a target. Return JSON only.'

def draft_prompt(*,question,search_proof,targets,catalog):
    identities = [{'target_ref': target['target_ref'],
                   'figure_identity': {key: target[key] for key in TARGET_IDENTITY_FIELDS}}
                  for target in normalize_targets(targets)]
    return canonical_json({'task':DRAFT_IDENTITY_TASK_V1,
                           'identity_contract_ref':DRAFT_IDENTITY_CONTRACT,
                           'identity_templates':identities,
                           'question':question,'targets':targets,'metric_definitions':catalog,
                           'quotes':quotes(search_proof),'output_schema':DRAFT_SCHEMA,
                           'scale_contract_ref':SCALE_CONTRACT_REF,
                           'scale_instructions':SCALE_INSTRUCTIONS})

def legacy_draft_prompt_v1(*,question,search_proof,targets,catalog):
    """Exact published identity prompt before canonical scale instructions."""
    value = json.loads(draft_prompt(question=question,search_proof=search_proof,targets=targets,catalog=catalog))
    del value['scale_contract_ref']
    del value['scale_instructions']
    value['output_schema'] = LEGACY_DRAFT_SCHEMA
    return canonical_json(value)

def legacy_draft_prompt_v0(*, question, search_proof, targets, catalog):
    """Sole pre-identity published numeric template, only for exact historical replay."""
    return canonical_json({'task':'Answer ALL required numeric targets solely from the exact registered excerpts. Never calculate, convert units, infer a period, assign issuer amounts, or substitute a segment. Every supported figure must bind its own metric meaning, as-reported label, quote, issuer, period, unit, currency and basis. Unsupported targets remain unavailable with a precise missing evidence reason. Numbers hidden in prose do not satisfy a target. Return JSON only.',
                           'question':question,'targets':targets,'metric_definitions':catalog,
                           'quotes':quotes(search_proof),'output_schema':LEGACY_DRAFT_SCHEMA})


def verifier_prompt(*,question,search_proof,draft,targets,catalog,producer_proof):
    return canonical_json({'task':'Independently verify EACH numeric target. Accept only when the exact quote demonstrates the definition/label, issuer, period, unit, currency and basis without inference, calculation or translation to another metric. Digits alone are insufficient. Do not edit the figure. Unavailable stays unavailable; a rejection rejects the batch. Return JSON only.',
                           'question':question,'targets':targets,'metric_definitions':catalog,
                           'quotes':quotes(search_proof),'draft':draft,'candidate_ref':candidate_ref(draft),
                           'producer_proof':producer_proof,'output_schema':VERIFIER_SCHEMA})
