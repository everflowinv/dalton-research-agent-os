"""Exact existing Figure promotion, including the frozen raw-period contract.

No Claim, review or canonical row is rewritten. A period-normalization mapping
is a read proof, never authority to alter a value, unit, label or original.
"""
import json
import re
from datetime import date
from .store import canonical_json, content_hash
from .directed_numeric_research import DirectedNumericError

CONTRACT = 'directed-numeric-canonical-period-replay:0.1'
RATIONALE = ('Company-filed figure re-verified against the quote it cites and rebuilt '
             "from this Core's own rows, field by field (ADR-0007).")


def _need(value, message):
    if not value:
        raise DirectedNumericError('canonical promotion replay: '+message)


def _policy(connection, ref):
    from .contracts import GovernancePolicyVersion
    row=connection.execute('SELECT * FROM governance_policy_versions WHERE policy_version_id=?',(ref,)).fetchone()
    _need(row is not None,'original policy missing')
    wire=GovernancePolicyVersion.from_dict(json.loads(row['version_json'])).to_dict()
    _need(canonical_json(wire)==row['version_json'] and wire['id']==row['policy_version_id']
          and content_hash({k:v for k,v in wire.items()if k!='content_hash'})==wire['content_hash']==row['content_hash']
          and wire['policy']=={k:v for k,v in json.loads(row['policy_json']).items()if k!='independence_predicates'}
          and wire['independence_predicates']==json.loads(row['policy_json']).get('independence_predicates',[])
          and wire['version']==row['version_number']
          and all(wire[k]==row[k] for k in ('policy_ref','effective_from','effective_until','actor_ref','prior_version_ref','change_reason','created_at')),
          'original policy hash/columns differ')
    policy=dict(row);policy['policy']=json.loads(row['policy_json'])
    return policy


def replay(store, *, bundle):
    """Return the strong original pair/result plus an explicit replay mapping."""
    from .claim_index_figures import MissionFigureAuthorityResolver,build_figure_candidate
    from .research_auto_commit import authorize_policy_candidate,_decision,validate_policy_commit_decision
    from .research_verification import MISSION_VERIFIED_FIGURE_RULE_REF
    from .research_review import validate_claim_version_v0_2,validate_evidence_version_v0_2
    c=store.connection
    row=c.execute('SELECT * FROM reviewed_candidate_commits WHERE candidate_evidence_ref=?',
                  (bundle['evidence']['id'],)).fetchone()
    if row is None:
        return None
    resolver=MissionFigureAuthorityResolver(c)
    current=build_figure_candidate(c,figure=bundle['figure'],actor_ref=bundle['claim']['actor_ref'],resolver=resolver)
    for key in ('figure','material','source_verification','numeric_verification','evidence','claim'):
        _need(canonical_json(current[key])==canonical_json(bundle[key]),'current Figure bundle differs')
    active=store.active_policy();policy=_policy(c,active['policy_version_id'])
    _need(policy['content_hash']==active['content_hash'],'current policy pointer differs')
    store._assert_policy_effective(policy)
    # The current source/numeric admission must still be authorized. The result
    # of this evaluation is not appended and does not pretend to be a new review.
    authorize_policy_candidate(connection=c,policy_version=policy,evidence=current['evidence'],
        claim=current['claim'],material=current['material'],source_verification=current['source_verification'],
        numeric_spec=None,numeric_verification=current['numeric_verification'])
    old=current;identity=None
    if row['candidate_claim_ref']!=current['claim']['id']:
        figure=current['figure'];identity=resolver.identity(figure)
        _need(identity.get('label')==figure['as_reported_label'] and identity.get('span')
              and identity.get('period')!=figure['period']
              and re.fullmatch(r'\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}',str(identity.get('period'))),
              'only unambiguous source-proved period normalization is compatible')
        start,end=identity['period'].split('..')
        _need(date.fromisoformat(start)<=date.fromisoformat(end) and list(identity['span'])==[start,end],
              'source period span differs from normalized dates')
        class RawPeriodResolver(MissionFigureAuthorityResolver):
            def identity(self, held):
                return {'version':'frozen-raw-figure-period:0.1','label':held['as_reported_label'],
                        'period':held['period'],'span':None}
        old=build_figure_candidate(c,figure=figure,actor_ref=current['claim']['actor_ref'],resolver=RawPeriodResolver(c))
        for key in ('figure','material','source_verification','numeric_verification','evidence'):
            _need(canonical_json(current[key])==canonical_json(old[key]),'original binding changed')
        allowed={'id','candidate_claim_ref','period','normalized_statement','content_hash'}
        _need({k:v for k,v in current['claim'].items()if k not in allowed}
              =={k:v for k,v in old['claim'].items()if k not in allowed},'non-period Claim field changed')
    decision=validate_policy_commit_decision(json.loads(row['decision_json']))
    saved=json.loads(row['result_json'])
    original_policy=_policy(c,decision['policy_version_ref'])
    expected=_decision(original_policy,claim_wire=old['claim'],evidence_wire=old['evidence'],
                       rule_ref=MISSION_VERIFIED_FIGURE_RULE_REF,rationale=RATIONALE)
    _need(canonical_json(expected)==canonical_json(decision)==row['decision_json']
          and canonical_json(saved)==row['result_json'] and saved.get('status')=='fresh'
          and row['review_decision_ref']==decision['id'] and row['created_at']==decision['created_at']
          and row['candidate_claim_ref']==old['claim']['id'] and row['candidate_evidence_ref']==old['evidence']['id']
          and row['request_hash']==content_hash({'decision_hash':decision['content_hash'],
              'evidence_hash':old['evidence']['content_hash'],'claim_hash':old['claim']['content_hash']})
          and saved.get('idempotency_key')==row['idempotency_key']
          and saved.get('review_decision_ref')==decision['id'],'review/result does not match frozen pair')
    _need(original_policy['effective_from']<=decision['created_at'] and
          (original_policy['effective_until'] is None or decision['created_at']<original_policy['effective_until']),
          'original authorization was not effective')
    canonical={}
    for table,key,json_key,validator,field,origin in (
        ('claim_versions','claim_version_id','claim_json',validate_claim_version_v0_2,'claim_version_ref','claim'),
        ('evidence_versions','evidence_version_id','evidence_json',validate_evidence_version_v0_2,'evidence_version_ref','evidence')):
        held=c.execute(f'SELECT * FROM {table} WHERE {key}=?',(saved[field],)).fetchone()
        _need(held is not None,'canonical original absent')
        wire=validator(json.loads(held[json_key]));source=old[origin]
        _need(canonical_json(wire)==held[json_key] and wire['content_hash']==held['content_hash']
              and wire['id']==held[key]==origin+'-version:'+content_hash({'candidate':source['id'],'review':decision['id']})
              and wire['candidate_origin_ref']==source['id'] and wire['candidate_origin_hash']==source['content_hash']
              and wire['version']==held['version_number']==1 and wire['prior_version_ref']==held['prior_version_id'] is None
              and wire['created_at']==held['created_at'] and wire['actor_ref']==decision['reviewer_ref']
              and wire[origin+'_ref']==held[origin+'_ref']==source['candidate_'+origin+'_ref'].replace('candidate-'+origin+':',origin+':',1)
              and content_hash({k:v for k,v in wire.items()if k!='content_hash'})==wire['content_hash'],
              'canonical body/index differs')
        canonical[origin]=wire
        lineage=wire[origin+'_ref']
        latest=c.execute(f'SELECT {key} FROM {table} WHERE {origin}_ref=? ORDER BY version_number DESC LIMIT 1',(lineage,)).fetchone()
        _need(latest[0]==wire['id'],'historical superseded canonical version')
        if origin=='claim':
            fields=('subject_ref','metric_or_aspect','period','basis','normalized_statement','claim_kind','value','unit','currency','scale')
            envelope=c.execute('SELECT connector_invocation_ref FROM connector_source_envelopes WHERE source_envelope_id=?',(old['evidence']['source_envelope_ref'],)).fetchone()
            invocation=c.execute('SELECT record_json,content_hash,execution_ref FROM connector_invocations WHERE connector_invocation_id=?',(envelope[0],)).fetchone()
            _need(invocation is not None,'canonical producer invocation absent')
            original_invocation=json.loads(invocation['record_json'])
            _need(canonical_json(original_invocation)==invocation['record_json']
                  and content_hash({k:v for k,v in original_invocation.items()if k!='content_hash'})==invocation['content_hash']==original_invocation['content_hash']
                  and original_invocation['execution_ref']==invocation['execution_ref']
                  and wire['producer_execution_refs']==[original_invocation['execution_ref']], 'canonical producer execution differs')
            _need(all(wire[k]==source[k]for k in fields) and wire['semantic_review_ref']==decision['id']
                  and wire['semantic_review_hash']==decision['content_hash'],'canonical Claim differs')
        else:
            fields=('source_type','source_ref','source_envelope_ref','source_envelope_hash','retrieved_at','valid_until',
                    'artifact_refs','source_lineage','independence_group','source_verification_ref','source_verification_hash')
            _need(all(wire[k]==source[k]for k in fields) and wire['review_decision_ref']==decision['id']
                  and wire['review_decision_hash']==decision['content_hash'],'canonical Evidence differs')
    _need(store._claim_status(c,saved['claim_version_ref'])in{'proposed','corroborated'},'canonical Claim no longer usable')
    relation=c.execute('SELECT * FROM evidence_relations WHERE relation_id=?',(saved['relation_ref'],)).fetchone()
    _need(relation is not None,'canonical relation absent')
    rel=json.loads(relation['relation_json'])
    _need(canonical_json(rel)==relation['relation_json'] and content_hash({k:v for k,v in rel.items()if k!='content_hash'})==rel['content_hash']==relation['content_hash']
          and rel['evidence_version_ref']==saved['evidence_version_ref'] and rel['claim_version_ref']==saved['claim_version_ref']
          and rel['relation']=='supports' and rel['actor_ref']==decision['reviewer_ref']
          and all(rel[field]==relation[column] for field,column in (('id','relation_id'),('evidence_ref','evidence_ref'),
              ('claim_ref','claim_ref'),('evidence_version_ref','evidence_version_id'),('claim_version_ref','claim_version_id'),('relation','relation'),('created_at','created_at')))
          and rel['id']==saved['relation_ref']=='relation:reviewed:'+content_hash({
              'evidence':saved['evidence_version_ref'],'claim':saved['claim_version_ref'],'review':decision['id']}),
          'canonical relation differs')
    _need(rel['created_at']==canonical['claim']['created_at']==canonical['evidence']['created_at']
          and rel['claim_ref']==canonical['claim']['claim_ref'] and rel['evidence_ref']==canonical['evidence']['evidence_ref']
          and rel['source_lineage']==canonical['evidence']['source_lineage']
          and rel['independence_group']==canonical['evidence']['independence_group'], 'canonical relation body differs')
    _need(len(saved.get('event_refs',[]))==3,'original append event refs differ')
    for ref,event_type,payload,key in zip(saved['event_refs'],('evidence_versioned','claim_versioned','evidence_related'),
            (canonical['evidence'],canonical['claim'],rel),('reviewed-evidence:','reviewed-claim:','reviewed-relation:')):
        event=c.execute('SELECT * FROM domain_events WHERE event_id=?',(ref,)).fetchone()
        _need(event is not None,'canonical original append event absent')
        event_wire=json.loads(event['event_json'])
        _need(canonical_json(event_wire)==event['event_json'] and event_wire['id']==event['event_id']==ref
              and event_wire['event_type']==event['event_type']==event_type
              and event_wire['actor_ref']==event['actor_id']==decision['reviewer_ref']
              and event_wire['idempotency_key']==event['idempotency_key']==key+decision['id']
              and canonical_json(event_wire['payload'])==canonical_json(payload)
              and event_wire['content_hash']==event['content_hash']==payload['content_hash'], 'canonical append event differs')
    body={'contract_ref':CONTRACT,'figure_ref':current['figure']['figure_id'],'figure_hash':current['figure']['content_hash'],
          'current_candidate_claim_ref':current['claim']['id'],'current_candidate_claim_hash':current['claim']['content_hash'],
          'original_candidate_claim_ref':old['claim']['id'],'original_candidate_claim_hash':old['claim']['content_hash'],
          'candidate_evidence_ref':old['evidence']['id'],'candidate_evidence_hash':old['evidence']['content_hash'],
          'original_idempotency_key':row['idempotency_key'],'original_review_ref':decision['id'],
          'original_review_hash':decision['content_hash'],'original_request_hash':row['request_hash'],
          'claim_version_ref':saved['claim_version_ref'],'evidence_version_ref':saved['evidence_version_ref'],
          'source_period_identity':identity}
    mapping={**body,'content_hash':content_hash(body)}
    return {'bundle':old,'result':{**saved,'status':'duplicate','authorization':decision},'mapping':mapping}
