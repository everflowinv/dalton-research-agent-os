"""Read-only deterministic source-grade refusals, never failed-answer evidence."""
from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path
from .store import content_hash

OUTCOME = 'numeric_admission_source_grade_refused'
UNVERIFIED = 'numeric_admission_source_unverified'
REASON = 'numeric_source_requires_company_filed_document'
_PROVENANCE = frozenset({'plan_ref','plan_hash','created_at','content_hash'})


def semantic_feedback(value):
    """An unchanged question/source does not wake again just for a new plan."""
    return {key:item for key,item in value.items() if key not in _PROVENANCE}


def read_numeric_source_feedback(core, mission, *, inventory_loader=None):
    if not hasattr(core.connection, 'execute'):
        return None  # compatibility for readers with no research-plan capability
    from .mission_document_research import _exact_plan
    from .research_task import inquiry_content_hash, inquiry_ref_for
    from .document_research_inventory import load_document_inventory_authority
    from .document_figure_grade import grade_for, FILED, SPOKEN
    from .directed_numeric_source_capabilities import FILED_CONTRACT_REF, SPOKEN_CONTRACT_REF
    from .document_mission_lineage import document_mission_versions
    from .directed_numeric_research import normalize_targets
    from .document_research import validate_registration
    from .coverage_mission import validate_mission_source_discovery
    import json
    connection=core.connection
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage_mission_research_plans'").fetchone():
        return None
    row=connection.execute('SELECT plan_id FROM coverage_mission_research_plans WHERE mission_version_ref=? ORDER BY created_at DESC,plan_id DESC LIMIT 1',(mission['id'],)).fetchone()
    if row is None:return None
    try:
        plan,_ = _exact_plan(connection,row['plan_id'])
        numeric=[item for item in plan['inquiries'] if isinstance(item.get('directed_document'),Mapping) and item['directed_document'].get('numeric_targets') is not None]
        if not numeric:return None
        if len(numeric)>6:raise ValueError('numeric source feedback exceeds the accepted plan bound')
        refs=tuple(dict.fromkeys(item['directed_document']['document_ref'] for item in numeric))
        loader=inventory_loader or load_document_inventory_authority
        inventory=loader(core=core,mission=mission,state_dir=Path(core.path).parent,selected_document_refs=refs)
        registrations=inventory.get('registration_by_hash') or {}
        versions=document_mission_versions(connection,mission)
    except Exception as exc:
        return {'status':'unverified','records':[],'unverified':[{'plan_ref':row['plan_id'],'reason':type(exc).__name__}]}
    records=[];unverified=[];companies={item['company_ref'] for item in mission['universe']}
    for inquiry in numeric:
        strategy=inquiry['directed_document']; binding={'plan_ref':plan['plan_id'],'plan_hash':plan['content_hash'],
            'document_ref':strategy['document_ref'],'document_version_hash':strategy['document_version_hash']}
        try:
            digest=inquiry_content_hash(inquiry);binding['inquiry_ref']=inquiry_ref_for(digest);binding['inquiry_hash']=digest
            normalize_targets(strategy['numeric_targets'])
            company=inquiry['company_ref']
            registration=registrations.get(strategy['document_version_hash'])
            if company not in companies or not isinstance(registration,Mapping):raise ValueError('exact numeric source registration unavailable')
            registration=validate_registration(registration)
            authority=registration['source_authority']
            if (registration['document_ref']!=strategy['document_ref'] or registration['content_hash']!=strategy['document_version_hash']
                or authority.get('kind')!='coverage-mission-acquired-document' or authority.get('company_ref')!=company
                or authority.get('mission_version_ref') not in versions):raise ValueError('numeric source registration ownership differs')
            rows=connection.execute('SELECT r.review_id,r.company_ref,r.document_ref,r.discovered_document_ref,d.source_ref,s.spec_ref,s.record_id AS discovery_ref,s.record_json AS discovery_json,s.content_hash AS discovery_hash FROM coverage_mission_document_reviews r JOIN coverage_mission_discovered_documents d ON d.record_id=r.discovered_document_ref JOIN coverage_mission_source_discoveries s ON s.record_id=d.discovery_ref WHERE d.record_id=? AND r.document_ref=? AND r.company_ref=?',(authority['ref'],registration['document_ref'],company)).fetchall()
            if len(rows)!=1 or rows[0]['source_ref']!=registration['source_ref']:raise ValueError('exact numeric review/discovery chain unavailable')
            source=dict(rows[0])
            discovery=validate_mission_source_discovery(json.loads(source['discovery_json']))
            if (discovery['id']!=source['discovery_ref'] or discovery['content_hash']!=source['discovery_hash']
                or discovery['spec_ref']!=source['spec_ref'] or discovery['company_ref']!=company
                or discovery['source_ref']!=source['source_ref']):raise ValueError('numeric source discovery grade proof differs')
            grade=grade_for(source['spec_ref'])
            expected_grade = SPOKEN if strategy['strategy_version'] == SPOKEN_CONTRACT_REF else FILED
            if grade==expected_grade:continue  # eligible source is not a refusal or a verified answer
            reason = ('numeric_spoken_source_requires_earnings_call_transcript'
                      if expected_grade == SPOKEN else REASON)
            identity={'company_ref':company,'inquiry_hash':digest,'document_ref':registration['document_ref'],
                'document_version_hash':registration['content_hash'],'source_grade':grade,'reason':reason}
            record={'id':'numeric-source-refusal:'+content_hash(identity)[:32],**binding,
                'mission_version_ref':mission['id'],'company_ref':company,'source_ref':registration['source_ref'],
                'document_authority_ref':registration['id'],'review_ref':source['review_id'],'discovery_ref':source['discovery_ref'],'discovery_hash':source['discovery_hash'],'source_grade':grade,
                'created_at':plan['created_at'],'stage':'admission','outcome':OUTCOME,'reason':reason,
                'question':inquiry['question'],'wants':inquiry['wants'],
                'missing_evidence':[('This source supports management statements only: use directed-document-spoken-numeric:0.1 with exact source/target bindings, never promote it to filed facts.' if grade == SPOKEN and expected_grade == FILED else 'This source does not support the requested numeric answer contract; select an authority-advertised compatible strategy and exact original. Do not transfer a rejection binding to another source.')]}
            record['content_hash']=content_hash(record);records.append(record)
        except Exception as exc:
            unverified.append({**binding,'reason':type(exc).__name__})
    return {'status':'partial' if unverified else 'verified','records':records,'unverified':unverified}
