"""Exact current quality findings for ordinary planning; no recovery permission."""
import json
from .store import content_hash

CONTRACT = 'initial-quality-planning-feedback:1'


def _record(row):
    if row is None: return None
    if len(row['record_json'].encode()) > 2_000_000: raise ValueError('quality feedback record exceeds bound')
    value = json.loads(row['record_json'])
    if value.get('content_hash') != row['content_hash'] or content_hash({k:v for k,v in value.items() if k!='content_hash'}) != row['content_hash']:
        raise ValueError('quality feedback authority hash differs')
    return value


def read_feedback(connection, *, company_ref):
    """One current initial target and one current-scorer score, both exact PKs."""
    from .research_quality_score import SCORER_VERSION
    from .research_quality_rubrics import INITIAL_SCREEN_STRL
    row = connection.execute('SELECT v.* FROM mission_deliverable_pointer p JOIN mission_deliverable_versions v ON v.version_id=p.version_id WHERE v.subject_ref=? AND v.kind=? ORDER BY v.created_at DESC LIMIT 1', (company_ref,'initial_screen')).fetchone()
    target = _record(row)
    if target is None: return {'status':'not_recorded'}
    if row['version_id'] != target['id'] or target['subject_ref'] != company_ref or target['kind']!='initial_screen':
        raise ValueError('quality feedback target projection differs')
    score_row = connection.execute('SELECT * FROM research_quality_score_versions WHERE target_ref=? AND target_hash=? AND rubric_ref=? AND rubric_hash=? AND scorer_version=? ORDER BY created_at DESC LIMIT 1',
        (target['id'],target['content_hash'],INITIAL_SCREEN_STRL.rubric_ref,INITIAL_SCREEN_STRL.content_hash,SCORER_VERSION)).fetchone()
    score = _record(score_row)
    if score is None: return {'status':'awaiting_current_scorer','target_ref':target['id'],'scorer_version':SCORER_VERSION}
    for field in ('target_ref','target_hash','rubric_ref','rubric_hash','scorer_version'):
        if score_row[field] != score[field]: raise ValueError('quality feedback score projection differs')
    deterministic = score['deterministic']
    if deterministic['target_ref'] != target['id'] or deterministic['target_hash'] != target['content_hash'] or deterministic['scorer_version'] != SCORER_VERSION:
        raise ValueError('quality feedback deterministic binding differs')
    items=[]
    gaps=[{key: str(gap[key])[:220] for key in ('kind','need','request_ref','period') if key in gap}
          for section in target['sections'] for gap in section.get('gap_items') or () if gap.get('kind')=='not_acquired']
    for check in deterministic['checks']:
        if check['status'] != 'fail': continue
        for finding in check['findings']:
            item={'check':check['check'],'finding_hash':content_hash(finding),
                  **{key:finding[key] for key in ('code','section','ref','sections','fact_identity') if key in finding}}
            if len(json.dumps(item,ensure_ascii=False).encode())>1400:
                item={'check':check['check'],'code':finding.get('code'),'finding_hash':content_hash(finding)}
            if check['check']=='latest_transcript_and_release_cited':
                item['next_step']='Acquire the missing issuer results source through the ordinary material queue; reuse exact existing requests.'
                item['existing_material_gaps']=gaps[:3]
                item['existing_material_gaps_hash']=content_hash(gaps)
                item['existing_material_gaps_omitted']=max(0,len(gaps)-3)
            else:
                item['next_step']='Use the complete defect list in normal drafting after new material; a score alone grants no additional repair attempt.'
            items.append(item)
    body={'contract_ref':CONTRACT,'status':'verified','target_ref':target['id'],'target_hash':target['content_hash'],
          'score_ref':score['id'],'score_hash':score['content_hash'],'scorer_version':SCORER_VERSION,
          'source_scope_hash':(deterministic.get('bindings') or {}).get('quality_source_scope'),
          'failed_checks': [{'check':check['check'],'finding_count':len(check['findings'])}
                            for check in deterministic['checks'] if check['status']=='fail'],
          'items':items[:8],'omitted_items_hash':content_hash(items[8:]),
          'repair_authority':'none; previously consumed permissions remain consumed'}
    # Keep the operational packet bounded; the full immutable score above is
    # the expansion authority, never a new permission or a financial fact.
    while len(json.dumps(body,ensure_ascii=False).encode())>4096 and body['items']:
        body['items'].pop()
        body['omitted_items_hash']=content_hash(items[len(body['items']):])
    return {**body,'content_hash':content_hash(body)}
