"""Numeric branch of the normal four-stage directed-document executor."""
from __future__ import annotations
import json
from typing import Mapping
from pathlib import Path
from .contracts import ResultEnvelope, WorkOrder
from .store import canonical_json, content_hash
from .directed_numeric_research import CONTRACT_REF, DirectedNumericError, coverage
from .directed_numeric_output import (draft_prompt,verifier_prompt,SCHEMA_RESOURCE,
    PROVIDER_CONTRACT_REF,parse_draft,parse_verifier)

# These are the only two published raw-byte bindings from the pre-canonical
# implementation (const schema, then its portable enum equivalent). They are
# accepted only when the entire independently re-derived immutable Work matches.
LEGACY_VERIFIER_SCHEMA_HASHES = (
    '1fac64ab5537f4a83bdbe1ec6b30ef8d451a498f5bba6fac6b13edddb1f5e24c',
    'e2afc57ee13d432c6f622b4b88759b7acdca15fa4f9ce772ea9b7390174f17f3',
)


def verifier_schema_hash():
    """Use the same parsed-schema identity as the adapter's provider controls."""
    schema = json.loads(Path(__file__).with_name(SCHEMA_RESOURCE).read_text(encoding='utf-8'))
    return content_hash(schema)


def historical_numeric_verifier_work(admission, scheduler, registry, blueprints, current):
    """Read two closed historical bindings without changing Work or send proof.

    No caller may authorize a new Work with an arbitrary schema hash. Historical
    selection requires the strong Scheduler reader and a full canonical byte and
    hash match, not merely the old metadata field.
    """
    stored = scheduler.work_order_authority(current['id'])
    if stored is None or (stored['work_order_hash'] == content_hash(current)
                          and canonical_json(stored['work_order']) == canonical_json(current)):
        return current
    for schema_hash in LEGACY_VERIFIER_SCHEMA_HASHES:
        legacy = derive_numeric_work(admission, scheduler, registry, blueprints, 2,
                                     verifier_schema_hash_override=schema_hash)
        if (stored['work_order_hash'] == content_hash(legacy)
                and canonical_json(stored['work_order']) == canonical_json(legacy)):
            return legacy
    return current


def derive_numeric_work(admission,scheduler,registry,blueprints,index,*,
                        draft_prompt_builder=None, verifier_schema_hash_override=None):
    if verifier_schema_hash_override is not None and (
            index != 2 or verifier_schema_hash_override not in LEGACY_VERIFIER_SCHEMA_HASHES):
        raise DirectedNumericError('historical numeric verifier schema binding is unsupported')
    from .annual_report_qualitative import validate_model_proof
    numeric=admission['numeric_authority']
    upstream=scheduler.work_order_authority(blueprints[index-1]['id'])
    formal=scheduler.formal_result(blueprints[index-1]['id'])
    if upstream is None or formal is None or formal['terminal_state']!='succeeded':
        raise DirectedNumericError('numeric stage requires exact succeeded upstream')
    source=upstream['work_order'];envelope=formal['result_envelope']
    blueprint=dict(blueprints[index]);metadata=dict(blueprint['metadata']);refs=list(blueprint['input_refs'])
    planned=metadata['upstream_work_order_ref']
    metadata['upstream_work_order_ref']=source['id'];refs=[source['id'] if x==planned else x for x in refs]
    metadata.update({'numeric_contract_ref':CONTRACT_REF,'numeric_targets':numeric['targets'],
                     'numeric_metric_definitions':numeric['metric_definitions'],
                     'upstream_result_ref':envelope['id'],'upstream_result_hash':formal['result_envelope_hash']})
    question=admission['planner_inquiry']['question']
    if index==1:
        proof=registry.verify_search_proof(envelope['outputs'])
        prompt=(draft_prompt_builder or draft_prompt)(question=question,search_proof=proof,targets=numeric['targets'],catalog=numeric['metric_definitions'])
        metadata.update({'purpose':'mission_directed_document_draft','retrieval_proof':proof,
                         'context_hash':content_hash(proof['matches']),'retrieval_match_count':len(proof['matches'])})
    elif index==2:
        draft=validate_model_proof(envelope['outputs'],stage='qualitative_model_draft',work=source)
        parse_draft(canonical_json(draft['output']),metadata=source['metadata'])
        proof=source['metadata']['retrieval_proof']
        producer={k:draft[k] for k in ('route_decision_ref','model_invocation_ref','model_family','request_binding_hash')}
        prompt=verifier_prompt(question=question,search_proof=proof,draft=draft['output'],targets=numeric['targets'],catalog=numeric['metric_definitions'],producer_proof=producer)
        metadata.update({'purpose':'mission_directed_document_verifier','retrieval_proof':proof,
                         'context_hash':source['metadata']['context_hash'],'retrieval_match_count':len(proof['matches']),
                         'draft':draft['output'],'draft_proof':draft,'producer_model_family':draft['model_family'],
                         'producer_route_decision_ref':draft['route_decision_ref'],
                         'verifier_provider_contract':PROVIDER_CONTRACT_REF,'verifier_output_schema_version':'0.1',
                         'verifier_provider_schema_hash':verifier_schema_hash_override or verifier_schema_hash()})
    elif index==3:
        verifier=validate_model_proof(envelope['outputs'],stage='independent_qualitative_verifier',work=source)
        parse_verifier(canonical_json(verifier['output']),metadata=source['metadata'])
        proof=source['metadata']['retrieval_proof'];draft=source['metadata']['draft_proof']
        metadata.update({'retrieval_proof':proof,'draft_proof':draft,'verifier_proof':verifier,
                         'producer_route_decision_ref':draft['route_decision_ref'],
                         'verifier_route_decision_ref':verifier['route_decision_ref']})
        prompt=canonical_json({'task':'Record exact verified numeric Figures and canonically admit only under the existing verified-figure rule.',
                               'admission_hash':admission['content_hash'],'numeric_authority':numeric,
                               'draft_hash':draft['content_hash'],'verifier_hash':verifier['content_hash']})
    else:raise DirectedNumericError('numeric stage ordinal is invalid')
    metadata['prompt_hash']=content_hash(prompt)
    metadata['model_request_binding_hash']=content_hash({
        'prompt_hash':metadata['prompt_hash'],'admission_hash':admission['content_hash'],
        'numeric_authority':numeric,'source_proof_hash':proof['content_hash'],
        'upstream_result_hash':formal['result_envelope_hash'],
        'routing_policy_ref':metadata.get('routing_policy_ref'),
        'credential_slot_refs':metadata.get('credential_slot_refs'),
        'provider_retry':metadata.get('provider_retry')})
    refs.extend([proof['id'],envelope['id']])
    return WorkOrder.from_dict({**blueprint,'question':prompt,'input_refs':list(dict.fromkeys(refs)),'metadata':metadata}).to_dict()

# Final stage functions will strongly replay all Model/Source/Scheduler authority
# before recording Figures; no public entry point grants a caller these writes.

def source_binding(connection,registration,company_ref):
    from .document_figure_grade import grade_for, FILED
    authority=registration['source_authority']
    rows=connection.execute(
        'SELECT r.review_id,r.company_ref,r.document_ref,r.discovered_document_ref,d.source_ref,s.spec_ref '
        'FROM coverage_mission_document_reviews r JOIN coverage_mission_discovered_documents d '
        'ON d.record_id=r.discovered_document_ref JOIN coverage_mission_source_discoveries s '
        'ON s.record_id=d.discovery_ref WHERE d.record_id=? AND r.document_ref=? AND r.company_ref=?',
        (authority['ref'],registration['document_ref'],company_ref)).fetchall()
    if len(rows)!=1 or rows[0]['source_ref']!=registration['source_ref']:
        raise DirectedNumericError('numeric source has no exact mission review chain')
    row=dict(rows[0]);grade=grade_for(row['spec_ref'])
    if grade!=FILED:raise DirectedNumericError('numeric directed formal claims require a company-filed source; source grade is not admissible')
    row['source_grade']=grade;row['source_manifest_hash']=registration['manifest_hash']
    return row

def _execution_proof(executor,admission,works):
    from .mission_document_research_executor import exact_mission_document_model_execution_authority
    from .model_router import independent_families
    proof=executor.registry.verify_search_proof(works[3]['metadata']['retrieval_proof'])
    if canonical_json(proof['request'])!=canonical_json(admission['request']):raise DirectedNumericError('numeric original differs from admitted source')
    result=[]
    for index,worker in ((1,executor.draft_worker),(2,executor.verifier_worker)):
        formal=executor.scheduler.formal_result(works[index]['id'])
        if formal is None or formal['terminal_state']!='succeeded':raise DirectedNumericError('numeric model stage is not formally complete')
        result.append(exact_mission_document_model_execution_authority(works[index],formal,worker))
    draft=result[0]['model_result'];verifier=result[1]['model_result']
    if not independent_families(verifier['model_family'],draft['model_family']):raise DirectedNumericError('numeric verifier is not independent')
    parse_draft(canonical_json(draft['output']),metadata=works[1]['metadata'])
    parse_verifier(canonical_json(verifier['output']),metadata=works[2]['metadata'])
    return proof,draft,verifier,[item['execution_proof'] for item in result]

def _record_numeric(executor,admission,works):
    from .claim_index_figures import MissionFigureAuthorityResolver, promote_figure
    from .research_auto_commit import ResearchAutoCommitRejected
    from .store import GateRejected
    from .directed_numeric_output import quotes
    proof,draft,verifier,accounting=_execution_proof(executor,admission,works)
    source=source_binding(executor.connection,admission['request']['registration'],admission['company_ref']);quote_map=quotes(proof)
    votes={item['target_ref']:item['verdict'] for item in verifier['output']['targets']}
    targets={item['target_ref']:item for item in admission['numeric_authority']['targets']}
    figures=[];pending=[];claims=[]
    if verifier['output']['verdict']=='reject':
        pending=[{'target_ref':ref,'reason':'independent_numeric_verifier_rejected'} for ref in targets]
    else:
        for row in draft['output']['targets']:
            ref=row['target_ref'];target=targets[ref]
            if row['status']!='supported' or votes[ref]!='accept':
                pending.append({'target_ref':ref,'reason':row['reason'] or 'independent_numeric_target_unavailable'})
                continue
            from .document_numeric_claim import verify_numeric_candidate
            expected=verify_numeric_candidate(row['figure'],quote_map,numeric_contract_ref=CONTRACT_REF)
            citation=quote_map[expected['quote_id']]
            executor.authority._missions.record_document_figures(
                company_ref=admission['company_ref'],review_ref=source['review_id'],document_ref=admission['request']['registration']['document_ref'],
                source_manifest_hash=source['source_manifest_hash'],source_grade=source['source_grade'],
                figures=[{**expected,'citation_text':citation}],observed_by=admission['actor_ref'],numeric_contract_ref=CONTRACT_REF)
            held=executor.authority._missions.document_figures(company_ref=admission['company_ref'],metric_ref=target['metric_ref'],limit=2000)
            matching=[f for f in held if f['document_ref']==admission['request']['registration']['document_ref']
                      and f['period']==target['period'] and f['source_manifest_hash']==source['source_manifest_hash']]
            if len(matching)!=1:raise DirectedNumericError('numeric Figure identity is absent or ambiguous')
            figure=matching[0]
            if any(figure.get(k)!=expected[k] for k in ('quote_id','metric_ref','as_reported_label','period','value','unit','currency','scale','basis')) or figure['citation_text']!=citation:
                raise DirectedNumericError('existing Figure conflicts with exact verified numeric answer')
            resolver=MissionFigureAuthorityResolver(executor.connection)
            try:
                staged=promote_figure(executor.authority.store,executor.staging,figure=figure,
                    actor_ref=admission['actor_ref'],idempotency_key=f'directed-numeric-figure:{admission["id"]}:{ref}',resolver=resolver)
                committed=executor.authority.store.commit_policy_candidate(
                    evidence=staged['evidence'],claim=staged['claim'],material=staged['material'],
                    source_verification=staged['source_verification'],numeric_spec=None,
                    numeric_verification=staged['numeric_verification'],
                    idempotency_key=f'policy-ledger:directed-numeric:{admission["id"]}:{ref}')
            except (ResearchAutoCommitRejected,GateRejected) as exc:
                pending.append({'target_ref':ref,'reason':'numeric_policy_authority_unavailable','detail':str(exc)})
                continue
            if committed['status'] not in {'fresh','duplicate'}:raise DirectedNumericError('numeric canonical commit did not converge')
            claim_row=executor.connection.execute('SELECT content_hash FROM claim_versions WHERE claim_version_id=?',(committed['claim_version_ref'],)).fetchone()
            evidence_row=executor.connection.execute('SELECT content_hash FROM evidence_versions WHERE evidence_version_id=?',(committed['evidence_version_ref'],)).fetchone()
            if claim_row is None or evidence_row is None:raise DirectedNumericError('numeric canonical records did not persist')
            claims.append({'target_ref':ref,'claim_version_ref':committed['claim_version_ref'],
                           'claim_version_hash':claim_row['content_hash'],'evidence_version_hash':evidence_row['content_hash'],
                           'evidence_version_ref':committed['evidence_version_ref'],
                           'review_decision_ref':committed['review_decision_ref']})
            figures.append({**figure,'metric_definition_hash':target['metric_definition_hash']})
    result=coverage(list(targets.values()),figures,company_ref=admission['company_ref'],
                    document_ref=admission['request']['registration']['document_ref'],source_manifest_hash=source['source_manifest_hash'])
    if pending:result['pending_targets']=pending
    material_requests=[]
    from .material_requests import open_request
    for item in result['pending_targets']:
        if item['reason']=='numeric_policy_authority_unavailable':continue
        target=targets[item['target_ref']]
        request=open_request(Path(executor.authority.store.path).parent,
            requester={'lane':'mission_document_research','product_ref':admission['question_version_ref'],
                       'run_ref':admission['id'],'section':item['target_ref']},
            subject=admission['company_ref'],need={'kind':'answer','doc_kinds':['annual_report','quarterly_report','earnings_release'],
                'data_item':target['metric_ref'], 'question':f"{admission['planner_inquiry']['question']}\nRequired: {canonical_json(target)}"},
            period=target['period'],source_priority=[admission['source_ref']],
            acceptance={'numeric_contract_ref':CONTRACT_REF,'numeric_target':target,'admission_ref':admission['id'],
                        'document_authority_ref':admission['document_authority_ref'],'document_authority_hash':admission['document_authority_hash']},
            actor_ref=admission['actor_ref'],now=executor.clock().isoformat())
        material_requests.append({'target_ref':target['target_ref'],'request_ref':request['id'],'request_digest':request['digest']})
    resolved_requests=[]
    resume=admission['numeric_authority'].get('resume_proof')
    if resume is not None:
        from .directed_numeric_research import pending_numeric_result
        from .material_requests import request as read_request,close as close_request
        previous=pending_numeric_result(executor.connection,resume['admission_ref'])
        if previous is None or previous['numeric_result_hash']!=resume['numeric_result_hash']:
            raise DirectedNumericError('numeric source resume proof disappeared')
        committed_targets={item['target_ref']:item for item in claims}
        completed_targets={item['target_ref']:item for item in result['completed_targets']}
        for item in previous['result']['material_requests']:
            ref=item['target_ref']
            if ref not in committed_targets:continue
            held=read_request(Path(executor.authority.store.path).parent,item['request_ref'])
            if held is None or held['digest']!=item['request_digest'] or held['acceptance'].get('numeric_target')!=targets[ref] or held['acceptance'].get('admission_ref')!=resume['admission_ref']:
                raise DirectedNumericError('numeric source resume request binding differs')
            if held['status'] in {'open','partial'}:
                closed=close_request(Path(executor.authority.store.path).parent,item['request_ref'],status='found',
                    reason='The exact requested numeric definition is now source-proved and canonically admitted.',
                    items=[{**committed_targets[ref],**completed_targets[ref],'admission_ref':admission['id'],'admission_hash':admission['content_hash']}],
                    actor_ref=admission['actor_ref'],now=executor.clock().isoformat())
                resolved_requests.append({'target_ref':ref,'request_ref':item['request_ref'],'status':closed['status']})
            else:
                resolved_requests.append({'target_ref':ref,'request_ref':item['request_ref'],'status':'terminal_preserved','prior_status':held['status']})
    body={'schema_version':CONTRACT_REF,'admission_ref':admission['id'],'admission_hash':admission['content_hash'],
          'work_order_ref':works[3]['id'],'work_order_hash':content_hash(works[3]),
          'numeric_authority':admission['numeric_authority'],'source_proof_ref':proof['id'],'source_proof_hash':proof['content_hash'],
          'source_binding':source,'model_accounting_proofs':accounting,'draft_proof_hash':draft['content_hash'],
          'verifier_proof_hash':verifier['content_hash'],'canonical_claims':claims,'material_requests':material_requests,'resolved_material_requests':resolved_requests,**result,
          'created_at':executor.clock().isoformat()}
    body['content_hash']=content_hash(body)
    return body

def complete_numeric_stage(executor,admission,works):
    work=works[3];lease=executor.scheduler.claim(executor.actor_ref,work_order_id=work['id'])
    if lease is None:return {'status':'pending','work_order_ref':work['id']}
    executor.scheduler.validate_lease_for_use(work['id'],lease['attempt']['attempt_number'],executor.actor_ref,
        lease['lease_token'],lease_revision_ref=lease['lease']['id'],lease_hash=lease['lease']['content_hash'],work_order_hash=lease['work_order_hash'])
    row=executor.connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
    if row is None:
        body=_record_numeric(executor,admission,works)
        with executor._transaction() as cur:
            cur.execute('INSERT INTO mission_document_numeric_results VALUES(?,?,?,?,?,?)',
                (admission['id'],admission['content_hash'],work['id'],canonical_json(body),body['content_hash'],body['created_at']))
    else:body=_read_receipt(row,admission,works)
    envelope=ResultEnvelope(schema_version='0.1',id='result-envelope:directed-numeric:'+body['content_hash'][:32],
        created_at=body['created_at'],work_order_ref=work['id'],invocation_ref='execution:directed-numeric:'+admission['identity_hash'][:32],
        status='succeeded',outputs=body,actual_side_effects=(),usage_refs=(),artifact_refs=(),error=None,
        metadata={'authority_ref':admission['id']}).to_dict()
    executor.scheduler.complete(work['id'],lease['attempt']['attempt_number'],executor.actor_ref,lease['lease_token'],envelope,
        idempotency_key=f'mission-document-numeric:{admission["id"]}:4:complete',result_envelope_hash=content_hash(envelope))
    formal=executor.scheduler.formal_result(work['id'])
    return read_numeric_result(executor,admission,works,formal)

def _read_receipt(row,admission,works):
    value=json.loads(row['record_json']);body=dict(value);digest=body.pop('content_hash',None)
    if canonical_json(value)!=row['record_json'] or digest!=row['content_hash'] or digest!=content_hash(body) or value['admission_ref']!=admission['id'] or value['admission_hash']!=admission['content_hash'] or value['work_order_ref']!=works[3]['id'] or value['work_order_hash']!=content_hash(works[3]) or value['numeric_authority']!=admission['numeric_authority']:
        raise DirectedNumericError('numeric receipt binding differs')
    return value

def read_numeric_result(executor,admission,works,formal):
    from .claim_index_figures import MissionFigureAuthorityResolver
    row=executor.connection.execute('SELECT record_json,content_hash FROM mission_document_numeric_results WHERE admission_ref=?',(admission['id'],)).fetchone()
    if row is None:raise DirectedNumericError('numeric final receipt is absent')
    receipt=_read_receipt(row,admission,works)
    if formal is None or formal['terminal_state']!='succeeded' or formal['result_envelope']['outputs']!=receipt:raise DirectedNumericError('numeric Scheduler final result differs')
    executor._stage_owned(works[3],formal)
    proof,draft,verifier,accounting=_execution_proof(executor,admission,works)
    if (receipt['source_binding']!=source_binding(executor.connection,admission['request']['registration'],admission['company_ref']) or receipt['source_proof_hash']!=proof['content_hash']
        or receipt['draft_proof_hash']!=draft['content_hash'] or receipt['verifier_proof_hash']!=verifier['content_hash'] or receipt['model_accounting_proofs']!=accounting):
        raise DirectedNumericError('numeric execution receipt differs from actual source/paid proofs')
    resolver=MissionFigureAuthorityResolver(executor.connection)
    from .claim_index_figures import build_figure_candidate
    from .research_auto_commit import validate_policy_commit_decision
    from .research_review import validate_claim_version_v0_2,validate_evidence_version_v0_2
    from .document_numeric_claim import verify_numeric_candidate
    from .directed_numeric_output import quotes
    targets={t['target_ref']:t for t in admission['numeric_authority']['targets']}
    supported={r['target_ref']:r['figure'] for r in draft['output']['targets'] if r['status']=='supported'}
    approved={r['target_ref'] for r in verifier['output']['targets'] if r['verdict']=='accept'}
    completed={r['target_ref']:r for r in receipt['completed_targets']}
    claims={r['target_ref']:r for r in receipt['canonical_claims']}
    if len(completed)!=len(receipt['completed_targets']) or len(claims)!=len(receipt['canonical_claims']) or set(claims)!=set(completed):
        raise DirectedNumericError('numeric result duplicates or drops target proof')
    figures=[]
    for ref,item in completed.items():
        if ref not in supported or ref not in approved or verifier['output']['verdict']!='pass':
            raise DirectedNumericError('numeric completion lacks independent accepted target')
        target=targets[ref];figure=resolver.figure(item['figure_ref']);resolver.verify_figure(figure)
        expected=verify_numeric_candidate(supported[ref],quotes(proof),numeric_contract_ref=CONTRACT_REF)
        if figure['content_hash']!=item['figure_hash'] or item['metric_definition_hash']!=target['metric_definition_hash'] or any(figure[k]!=expected[k] for k in ('quote_id','metric_ref','as_reported_label','period','value','unit','currency','scale','basis')) or figure['citation_text']!=quotes(proof)[expected['quote_id']]:
            raise DirectedNumericError('completed numeric Figure differs from source-bound typed definition')
        bundle=build_figure_candidate(executor.connection,figure=figure,actor_ref=admission['actor_ref'],resolver=resolver)
        committed=claims[ref]
        for table,key,json_key,version_class,hash_key in (
            ('claim_versions','claim_version_id','claim_json',validate_claim_version_v0_2,'claim_version_hash'),
            ('evidence_versions','evidence_version_id','evidence_json',validate_evidence_version_v0_2,'evidence_version_hash')):
            version_ref=committed['claim_version_ref' if table=='claim_versions' else 'evidence_version_ref']
            held=executor.connection.execute(f'SELECT {json_key},content_hash FROM {table} WHERE {key}=?',(version_ref,)).fetchone()
            if held is None:raise DirectedNumericError('numeric canonical authority is absent')
            value=json.loads(held[json_key]);version=version_class(value)
            if canonical_json(version)!=held[json_key] or version['content_hash']!=held['content_hash'] or held['content_hash']!=committed[hash_key]:
                raise DirectedNumericError('numeric canonical authority bytes differ')
        decision_row=executor.connection.execute('SELECT * FROM reviewed_candidate_commits WHERE review_decision_ref=?',(committed['review_decision_ref'],)).fetchone()
        if decision_row is None:raise DirectedNumericError('numeric canonical admission is absent')
        decision=validate_policy_commit_decision(json.loads(decision_row['decision_json']));saved=json.loads(decision_row['result_json'])
        if (canonical_json(decision)!=decision_row['decision_json'] or canonical_json(saved)!=decision_row['result_json']
            or decision_row['idempotency_key']!=f'policy-ledger:directed-numeric:{admission["id"]}:{ref}'
            or decision_row['request_hash']!=content_hash({'decision_hash':decision['content_hash'],'evidence_hash':bundle['evidence']['content_hash'],'claim_hash':bundle['claim']['content_hash']})
            or decision_row['candidate_claim_ref']!=bundle['claim']['id'] or decision_row['candidate_evidence_ref']!=bundle['evidence']['id']
            or saved['claim_version_ref']!=committed['claim_version_ref'] or saved['evidence_version_ref']!=committed['evidence_version_ref']):
            raise DirectedNumericError('numeric canonical admission is not bound to the exact Figure')
        figures.append({**figure,'metric_definition_hash':target['metric_definition_hash']})
    replayed=coverage(list(targets.values()),figures,company_ref=admission['company_ref'],document_ref=admission['request']['registration']['document_ref'],source_manifest_hash=receipt['source_binding']['source_manifest_hash'])
    if receipt['status']!=replayed['status'] or receipt['completed_targets']!=replayed['completed_targets'] or {x['target_ref'] for x in receipt['pending_targets']}!={x['target_ref'] for x in replayed['pending_targets']}:
        raise DirectedNumericError('numeric completion declaration differs from canonical target coverage')
    return {'status':'complete','research_status':'numeric_targets_completed' if receipt['status']=='completed' else 'numeric_waiting_evidence',
            'numeric_result':receipt,'canonical_claim_count':len(receipt['canonical_claims'])}

def numeric_feedback(connection,mission_version_ref):
    import sqlite3
    try:
        rows=connection.execute('SELECT n.*,a.record_json AS admission_json,a.content_hash AS current_admission_hash '
            'FROM mission_document_numeric_results n JOIN mission_document_research_admissions a '
            'ON a.admission_id=n.admission_ref WHERE a.mission_version_ref=? ORDER BY n.created_at,n.admission_ref',
            (mission_version_ref,)).fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):return []
        raise
    results=[]
    for row in rows:
        value=json.loads(row['record_json']);body=dict(value);digest=body.pop('content_hash',None)
        admission=json.loads(row['admission_json']);parent=dict(admission);parent_hash=parent.pop('content_hash',None)
        if canonical_json(value)!=row['record_json'] or digest!=row['content_hash'] or digest!=content_hash(body) or parent_hash!=row['current_admission_hash'] or parent_hash!=content_hash(parent) or value['admission_hash']!=parent_hash or value['admission_ref']!=admission['id']:
            raise DirectedNumericError('numeric feedback authority differs')
        formal=connection.execute('SELECT result_envelope_json,result_envelope_hash,terminal_state FROM scheduler_formal_results WHERE work_order_id=?',(value['work_order_ref'],)).fetchone()
        if formal is None:continue  # receipt before final local Scheduler settlement is not completion.
        envelope=json.loads(formal['result_envelope_json'])
        if formal['terminal_state']!='succeeded' or formal['result_envelope_hash']!=content_hash(envelope) or envelope['outputs']!=value:raise DirectedNumericError('numeric feedback final result differs')
        result={'schema_version':CONTRACT_REF,'id':'numeric-reading-feedback:'+digest[:32],
                'mission_version_ref':mission_version_ref,'company_ref':admission['company_ref'],
                'admission_ref':admission['id'],'admission_hash':parent_hash,'numeric_result_hash':digest,
                'inquiry_ref':admission['inquiry_ref'],'inquiry_hash':admission['inquiry_hash'],
                'document_ref':admission['request']['registration']['document_ref'],
                'document_version_hash':admission['request']['registration']['content_hash'],
                'outcome':'numeric_targets_completed' if value['status']=='completed' else 'numeric_waiting_evidence',
                'question':admission['planner_inquiry']['question'],'numeric_targets':value['numeric_authority']['targets'],
                'completed_targets':value['completed_targets'],'missing':value['pending_targets'],
                'material_requests':value['material_requests'],'created_at':value['created_at'],
                **({'resumes_numeric_result':{'admission_ref':admission['id'],'admission_hash':parent_hash,'numeric_result_hash':digest}} if value['status']=='waiting_evidence' else {})}
        if value['numeric_authority'] != admission.get('numeric_authority'):
            raise DirectedNumericError('numeric feedback target authority differs')
        if value['status'] == 'completed':
            required = {t['target_ref'] for t in value['numeric_authority']['targets']}
            completed = [t['target_ref'] for t in value['completed_targets']]
            canonical = [t['target_ref'] for t in value['canonical_claims']]
            if value['pending_targets'] or set(completed) != required or set(canonical) != required or len(completed) != len(required) or len(canonical) != len(required):
                raise DirectedNumericError('numeric completed feedback lacks every target')
            result['resolved_numeric_rejections'] = [
                {k: proof[k] for k in ('admission_ref','admission_hash','rejection_ref','rejection_hash')}
                for proof in admission['numeric_authority'].get('capability_upgrade_proofs', ())]
        result['content_hash']=content_hash(result);results.append(result)
    return results

def query_miss_requests(executor,admission):
    """A failed bounded search is a continuing input need, never absence proof."""
    from .material_requests import open_request
    requests=[]
    for target in admission['numeric_authority']['targets']:
        found=open_request(Path(executor.authority.store.path).parent,
            requester={'lane':'mission_document_research','product_ref':admission['question_version_ref'],'run_ref':admission['id'],'section':target['target_ref']},
            subject=admission['company_ref'],need={'kind':'answer','doc_kinds':['annual_report','quarterly_report','earnings_release'],
                'data_item':target['metric_ref'],'question':f"{admission['planner_inquiry']['question']}\nRequired: {canonical_json(target)}"},
            period=target['period'],source_priority=[admission['source_ref']],
            acceptance={'numeric_contract_ref':CONTRACT_REF,'numeric_target':target,'admission_ref':admission['id'],
                        'document_authority_ref':admission['document_authority_ref'],'document_authority_hash':admission['document_authority_hash']},
            actor_ref=admission['actor_ref'],now=executor.clock().isoformat())
        requests.append({'target_ref':target['target_ref'],'request_ref':found['id'],'request_digest':found['digest'],
                         'reason':'bounded_query_did_not_match_not_proof_of_absence'})
    return requests
