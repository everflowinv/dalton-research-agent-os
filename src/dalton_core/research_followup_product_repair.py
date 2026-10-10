"""Exact followup inputs for repairing an existing formal research artefact.

A repair is not a new company admission and never grants permission or budget.
All source records are read through the existing authority, without construction.
"""
from __future__ import annotations
from typing import Any, Mapping
from .store import canonical_json, content_hash
from .research_followup import requests, _verified, _exists
from .research_followup_automation import groups, latest

MARKER = '\n\nSOURCE-BOUND EXISTING-ARTEFACT REPAIR:\n'


def record(connection, product, ref):
    if product=='dossier':
        from .company_dossier import CompanyDossierAuthority as Authority
        method='dossier'
    elif product=='industry_framework':
        from .industry_framework import IndustryFrameworkAuthority as Authority
        method='framework'
    else:raise ValueError('unsupported existing product repair')
    reader=object.__new__(Authority);reader.connection=connection
    return getattr(reader,method)(ref)


def lineage(connection,product,target,prior):
    found={};cursor=prior
    while cursor is not None:
        if cursor['id'] in found:raise ValueError('product repair lineage contains a cycle')
        cursor=record(connection,product,cursor['id'])
        if (cursor.get('company_ref') if product=='dossier' else cursor.get('industry_ref'))!=target:
            raise ValueError('product repair lineage crosses targets')
        found[cursor['id']]=cursor
        ref=cursor.get('prior_version_ref');cursor=None if ref is None else record(connection,product,ref)
    return found


def affected_units(source,request,product):
    from .redraft_policy import _unit_payload
    projection=request['scope'].get('reading_projection')
    if projection:
        affected=[p for p in projection['paragraphs'] if p['state'] in {'historical_comparison','period_unknown'}]
        from .company_dossier import UNITS
        candidates=(list(UNITS) if product=='dossier'
                    else ['causal_chain:'+str(s['link_index']) for s in source.get('sections') or []]+['characteristics','long_term_drivers','short_term_drivers'])
        output=set()
        for paragraph in affected:
            matches=[]
            for unit in candidates:
                block=_unit_payload(source,product,unit) or {}
                # Match the typed top-level label used by reading_projection.
                label=(unit if unit in {'industry_classification','variant_view','long_term_drivers','short_term_drivers'}
                       else block.get('aspect') or block.get('title') or block.get('link'))
                if label!=paragraph['unit']:continue
                if any(slot['slot_id']==paragraph['slot_id'] and ordinal==paragraph['ordinal'] and content_hash(row.get('text'))==paragraph['text_hash']
                       for slot in block.get('slots') or [] for ordinal,row in enumerate(slot.get('sentences') or [])):
                    matches.append(unit)
            if len(matches)!=1:raise ValueError('historical repair paragraph cannot bind an exact product unit')
            output.add(matches[0])
        return sorted(output)
    if request['scope'].get('defect')=='causal_chain_unmapped':
        return ['causal_chain:'+str(s['link_index']) for s in source.get('sections') or [] if s.get('reason')=='causal_chain_unmapped']
    raise ValueError('product repair lacks a supported exact defect')


def context(connection,product,target,prior,*,mission=None):
    if prior is None:return []
    route='dossier_repair' if product=='dossier' else 'framework_repair'
    selected=[g for g in groups(connection) if g['route']==route and g['company_ref']==target]
    if not selected:return []
    if mission is not None:
        from .research_followup_automation import governing_mission
        held_requests={r['id']:r for r in requests(connection)}
        selected=[g for g in selected if all((governing_mission(connection,held_requests[r['ref']]) or {}).get('id')==mission['id'] for r in g['requests'])]
        if not selected:return []
    ancestors=lineage(connection,product,target,prior)
    held={r['id']:r for r in requests(connection)};result=[]
    for group in selected:
        source_ref=group['scope']['source_version_ref']
        if source_ref not in ancestors:continue
        source=ancestors[source_ref];instructions=[]
        for item in group['requests']:
            request=held[item['ref']]
            if request['content_hash']!=item['hash']:raise ValueError('product repair source request differs')
            kind='dossier' if product=='dossier' else 'framework'
            if not any(e['kind']==kind and e['ref']==source_ref and e['hash']==source['content_hash'] for e in request['evidence']):
                raise ValueError('product repair does not bind exact source product bytes')
            instructions.append({'ref':request['id'],'hash':request['content_hash'],'reason':request['reason'],
                                 'evidence':request['evidence'],'units':affected_units(source,request,product)})
        result.append({'group':group,'source_ref':source_ref,'source_hash':source['content_hash'],'instructions':instructions})
    return result


def pending_units(connection,items):
    result=set()
    for item in items:
        observed=latest(connection,item['group']['group_ref'])
        if observed and observed['group']==item['group'] and observed['state']=='completed':continue
        proven=verified_units(connection,item)
        for instruction in item['instructions']:result.update(set(instruction['units'])-proven)
    return sorted(result)



def unread_repair_units(state,product,target,company_refs,units,inputs):
    """An independently consumed exact repair input gets no second paid occasion.

    A no-new-evidence receipt leaves the followup unresolved. Only later
    materially different inputs, or an explicit human revision, may retry.
    """
    from pathlib import Path
    from .research_product_consumption import _open_ro,_receipt,scope
    db=_open_ro(Path(state))
    if db is None:return list(units)
    result=[]
    try:
        for unit in units:
            if unit not in inputs:continue
            binding=scope(product,target,unit,company_refs);digest=content_hash(inputs[unit])
            consumed=False
            for row in db.execute('SELECT record_json,content_hash FROM unit_input_receipts WHERE scope_hash=?',(content_hash(binding),)):
                receipt=_receipt(row[0],row[1],binding)
                if receipt['snapshot']['exact_input_hash']==digest and receipt['result'] in {'published','verified_no_new_evidence'}:
                    consumed=True;break
            if not consumed:result.append(unit)
    finally:db.close()
    return result

def input_note(items):
    compact=[{'group_ref':x['group']['group_ref'],'group_hash':x['group']['content_hash'],
              'source_ref':x['source_ref'],'source_hash':x['source_hash'],
              'instructions':[{'ref':i['ref'],'hash':i['hash'],'units':i['units'],'reason':i['reason'][:120]}
                              for i in x['instructions']]} for x in items]
    note=MARKER+canonical_json(compact)+'\nRepair only these held units from the supplied evidence. Date historical quantities explicitly; absent current evidence stays a gap.\n'
    if len(note)>1800:raise ValueError('exact repair instructions exceed the existing bounded prior input')
    return note


def prior_body(body,items):
    if not items:return body
    note=input_note(items)
    return note+body[:2000-len(note)]


def existing_candidates(connection,mission):
    """Only repair a held formal file within today's scope; never invent a gate."""
    if connection is None:return []
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='company_dossier_versions'").fetchone():return []
    out=[]
    for member in mission.get('universe') or []:
        target=member['company_ref']
        row=connection.execute('SELECT version_id FROM company_dossier_versions WHERE company_ref=? ORDER BY version_number DESC LIMIT 1',(target,)).fetchone()
        if row is None:continue
        held=record(connection,'dossier',row[0]);items=context(connection,'dossier',target,held,mission=mission)
        if items and pending_units(connection,items):out.append(target)
    return out


def _ro(path):
    import sqlite3
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    c.execute('PRAGMA query_only=ON');c.execute('BEGIN');return c


def _formal_route(scheduler,router,route_ref,*,purpose,mission,prompt,accept):
    """Exact successful Work/result/route/profile; timestamps are not a join."""
    import json
    from .model_router import _profile_wire
    row=router.execute('SELECT * FROM model_route_decisions WHERE decision_id=?',(route_ref,)).fetchone()
    if row is None:raise ValueError('repair route is not held')
    decision=json.loads(row['decision_json'])
    if (decision.get('id')!=route_ref or decision.get('content_hash')!=row['decision_hash']
            or content_hash({k:v for k,v in decision.items() if k!='content_hash'})!=row['decision_hash'] or row['outcome']!='selected'):
        raise ValueError('repair route hash/outcome differs')
    profile_ref=decision.get('selected_profile_version_ref')
    profile_row=router.execute('SELECT * FROM model_endpoint_profile_versions WHERE profile_version_ref=?',(profile_ref,)).fetchone()
    if profile_row is None:raise ValueError('repair model family authority is unavailable')
    profile=_profile_wire(json.loads(profile_row['profile_json']))
    if profile['content_hash']!=profile_row['profile_hash'] or decision.get('selected_profile_hash')!=profile['content_hash']:
        raise ValueError('repair selected model profile hash differs')
    workrow=scheduler.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',(row['work_order_id'],)).fetchone()
    if workrow is None:raise ValueError('repair Work is unavailable')
    work=json.loads(workrow['work_order_json']);meta=work.get('metadata') or {}
    mission_ref=mission.get('ref') or mission.get('id');mission_hash=mission.get('hash') or mission.get('content_hash')
    if (content_hash(work)!=workrow['work_order_hash'] or workrow['work_order_hash']!=row['work_order_hash']
            or work.get('id')!=row['work_order_id'] or work.get('question')!=prompt or meta.get('purpose')!=purpose
            or meta.get('mission_version_ref')!=mission_ref or meta.get('mission_version_hash')!=mission_hash):
        raise ValueError('repair exact Work/input binding differs')
    matches=[]
    for result in scheduler.execute('SELECT * FROM scheduler_result_envelopes WHERE work_order_id=? AND outcome=?',(row['work_order_id'],'succeeded')):
        envelope=json.loads(result['result_envelope_json'])
        receipt={k:result[k] for k in ('result_envelope_id','work_order_id','attempt_number','result_envelope_hash','outcome','created_at')}
        if (content_hash(envelope)!=result['result_envelope_hash'] or content_hash(receipt)!=result['content_hash']
                or envelope.get('id')!=result['result_envelope_id'] or envelope.get('work_order_ref')!=row['work_order_id']
                or envelope.get('status')!='succeeded' or (envelope.get('metadata') or {}).get('route_decision_ref')!=route_ref):
            raise ValueError('repair formal result authority differs')
        try:matched=accept((envelope.get('outputs') or {}).get('text'))
        except (ValueError,TypeError,KeyError):matched=False
        if matched:matches.append({'ref':result['result_envelope_id'],'hash':result['result_envelope_hash']})
    if not matches:raise ValueError('repair formal output does not bind the published units')
    return {'route_ref':route_ref,'route_hash':row['decision_hash'],'work_ref':work['id'],'work_hash':workrow['work_order_hash'],
            'profile_ref':profile_ref,'profile_hash':profile['content_hash'],'family':profile['family'],
            'results':matches,'producer_refs':meta.get('producer_route_decision_refs') or []}


def verify_result(connection,evidence,group,*,allow_partial=False):
    """Recompute closure from unchanged sources and exact normal producer work."""
    from pathlib import Path
    from .redraft_policy import _unit_payload
    from .research_product_consumption import JOURNAL, _receipt
    if 'unit_proofs' in evidence:return verify_combined(connection,evidence,group)
    product=evidence['product'];held=record(connection,product,evidence['record_ref']);prior=record(connection,product,evidence['prior_ref'])
    target=held.get('company_ref') if product=='dossier' else held.get('industry_ref')
    if (held['content_hash']!=evidence['record_hash'] or prior['content_hash']!=evidence['prior_hash']
            or held.get('prior_version_ref')!=prior['id'] or target!=group['company_ref']):
        raise ValueError('repair does not bind exact new and prior products')
    ancestors=lineage(connection,product,target,prior)
    requests_by_ref={r['id']:r for r in requests(connection)}
    for item in evidence['context']:
        g=item['group'];source=ancestors.get(item['source_ref'])
        if source is None or source['content_hash']!=item['source_hash']:raise ValueError('repair source ancestor differs')
        for instruction in item['instructions']:
            request=requests_by_ref.get(instruction['ref'])
            if (request is None or request['content_hash']!=instruction['hash'] or request['evidence']!=instruction['evidence']
                    or request['reason']!=instruction['reason'] or affected_units(source,request,product)!=instruction['units']):
                raise ValueError('repair fixed source instructions differ')
        if sorted([{'ref':x['ref'],'hash':x['hash']} for x in item['instructions']],key=lambda x:x['ref'])!=g['requests']:
            raise ValueError('repair source request set differs')
    selected=next((x for x in evidence['context'] if x['group']==group),None)
    if selected is None:raise ValueError('repair lacks exact followup group')
    affected={unit for x in selected['instructions'] for unit in x['units']}
    if not affected or (not allow_partial and not affected.issubset(evidence['units'])) or not (affected & set(evidence['units'])):raise ValueError('repair did not independently draft every affected unit')
    state=Path(next(r[2] for r in connection.execute('PRAGMA database_list') if r[1]=='main')).parent
    journal=scheduler=router=None
    try:
        journal=_ro(state/JOURNAL);scheduler=_ro(state/'scheduler.sqlite');router=_ro(state/'model-router.sqlite')
        blocks={unit:_unit_payload(held,product,unit) for unit in evidence['units']}
        if any(not block for block in blocks.values()):raise ValueError('repair drafted unit is missing from formal product')
        producers=[];verifier=None
        for unit,proof in evidence['units'].items():
            row=journal.execute('SELECT record_json,content_hash FROM unit_input_receipts WHERE content_hash=?',(proof['receipt_hash'],)).fetchone()
            if row is None:raise ValueError('repair exact input consumption receipt is missing')
            receipt=_receipt(row['record_json'],row['content_hash'],proof['scope'])
            inp=proof['input'];supplied=inp.get('parse_input') or inp
            if (receipt['result']!='published' or receipt['snapshot']['exact_input_hash']!=content_hash(inp)
                    or receipt['scope']['target']!=target or receipt['scope']['product']!=product or receipt['scope']['unit']!=unit
                    or not str(supplied.get('prior_body') or '').startswith(input_note(evidence['context']))):
                raise ValueError('repair receipt does not bind exact source-followup input')
            if product=='dossier':
                from .company_dossier_cli import validate_formal_unit_provenance
                provenance=held.get('unit_provenance') or {}
                if not provenance.get(unit) or provenance[unit]['producer_input']!=inp:
                    raise ValueError('repair lacks exact formal dossier producer input')
                validate_formal_unit_provenance({u:provenance[u] for u in evidence['units']},
                    mission_ref=inp['mission']['ref'],current_prior_ref=prior['id'],company_ref=target,
                    current_units=set(blocks),current_blocks=blocks,current_bindings=held['bindings'],
                    current_mission_hash=inp['mission']['hash'],scheduler_db=state/'scheduler.sqlite',router_db=state/'model-router.sqlite')
                # Existing provenance validates repaired calls too; exact route
                # families are read below, not inferred from requested models.
                producer_prompt=scheduler.execute('SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?',(provenance[unit]['producer']['work_order_ref'],)).fetchone()
                import json
                prompt=json.loads(producer_prompt[0])['question']
                accept=lambda text:True  # normal provenance above parsed/bound it
                mission=inp['mission'];purpose='dossier'
            else:
                from .industry_framework_draft import build_unit_prompt,parse_unit_output
                prompt=build_unit_prompt(**{k:inp[k] for k in ('unit','structure','material','industry','link','title','prior_body','comparison_table')})
                accept=lambda text,inp=inp,block=blocks[unit]:parse_unit_output(text,**{k:inp[k] for k in ('unit','structure','material','link','title','link_index')})==block
                mission=inp['mission'];purpose='industry_framework'
            if len(receipt['producer_refs'])!=1:raise ValueError('repair requires exact accepted producer route')
            producers.append(_formal_route(scheduler,router,receipt['producer_refs'][0],purpose=purpose,mission=mission,prompt=prompt,accept=accept))
            if verifier is not None and verifier!=receipt['verifier_ref']:raise ValueError('repair units do not share actual verifier')
            verifier=receipt['verifier_ref']
        if product=='dossier':
            from .company_dossier_draft import build_verifier_prompt,validate_verifier_output
            kwargs={'company':next(iter(evidence['units'].values()))['input']['company']}
        else:
            from .industry_framework_draft import build_verifier_prompt,validate_verifier_output
            kwargs={'industry':next(iter(evidence['units'].values()))['input']['industry']}
        verified=_formal_route(scheduler,router,verifier,purpose=purpose+'_verifier',mission=mission,
            prompt=build_verifier_prompt(blocks,**kwargs),accept=lambda text:validate_verifier_output(__import__('json').loads(text))['verdict']=='pass')
        if verified['family'] in {p['family'] for p in producers} or not {p['route_ref'] for p in producers}.issubset(verified['producer_refs']):
            raise ValueError('repair verifier is not independent of exact producers')
    finally:
        for opened in (journal,scheduler,router):
            if opened is not None:opened.close()
    # A new version alone is insufficient: the affected source unit itself
    # must change, and historical quantities must be explicitly dated.
    source=ancestors[selected['source_ref']]
    old_year=max([str(((requests_by_ref[x['ref']]['scope'].get('reading_projection') or {}).get('latest_disclosure') or {}).get('report_date') or '')[:4]
                  for x in selected['instructions']],default='')
    for unit in affected & set(evidence['units']):
        block=_unit_payload(held,product,unit)
        if block==_unit_payload(source,product,unit):raise ValueError('repair affected unit was carried forward unchanged')
        if not block or (block.get('status') not in ('drafted','available') and not (product=='dossier' and unit=='industry_classification' and block.get('sources'))) or block.get('reason')=='causal_chain_unmapped':
            raise ValueError('repair affected unit remains unavailable')
        for slot in block.get('slots') or []:
            for sentence in slot.get('sentences') or []:
                if any(char.isdigit() for char in sentence.get('text','')):
                    sources={s['ref']:s for s in block.get('sources') or []}
                    if not all(sources.get(ref,{}).get('period') for ref in sentence.get('refs') or []):
                        raise ValueError('repair quantitative paragraph still lacks source period')
                    import re
                    years={year for ref in sentence.get('refs') or [] for year in re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',str(sources[ref]['period']))}
                    older={year for year in years if old_year and year<old_year}
                    if any(year not in sentence['text'] for year in older):
                        raise ValueError('repair still presents old quantities without their historical year')
    return evidence




def progress_proofs(connection,item):
    if not _exists(connection,'research_followup_automation_versions'):return []
    result=[];seen=set()
    for row in connection.execute('SELECT * FROM research_followup_automation_versions WHERE group_ref=? ORDER BY rowid',(item['group']['group_ref'],)):
        observed=_verified(row);proof=observed.get('evidence') or {}
        if observed['group']!=item['group'] or proof.get('kind')!='verified_product_repair_partial':continue
        verify_result(connection,proof,item['group'],allow_partial=True)
        digest=content_hash(proof)
        if digest not in seen:result.append(proof);seen.add(digest)
    return result


def verified_units(connection,item):
    from .redraft_policy import _unit_payload
    product='dossier' if item['group']['route']=='dossier_repair' else 'industry_framework'
    table,key=('company_dossier_versions','company_ref') if product=='dossier' else ('industry_framework_versions','industry_ref')
    row=connection.execute(f'SELECT version_id FROM {table} WHERE {key}=? ORDER BY version_number DESC LIMIT 1',(item['group']['company_ref'],)).fetchone()
    if row is None:return set()
    current=record(connection,product,row[0]);ancestors=lineage(connection,product,item['group']['company_ref'],current)
    result=set()
    for proof in progress_proofs(connection,item):
        part=ancestors.get(proof['record_ref'])
        if part is None:continue
        for unit in proof['units']:
            if _unit_payload(current,product,unit)==_unit_payload(part,product,unit):result.add(unit)
    return result


def verify_combined(connection,evidence,group):
    from .redraft_policy import _unit_payload
    if evidence.get('group_hash')!=group['content_hash']:raise ValueError('combined repair group hash differs')
    product=evidence['product'];current=record(connection,product,evidence['record_ref'])
    target=current.get('company_ref') if product=='dossier' else current.get('industry_ref')
    if current['content_hash']!=evidence['record_hash'] or target!=group['company_ref']:raise ValueError('combined repair latest product differs')
    ancestors=lineage(connection,product,target,current);covered=set();required=set()
    for proof in evidence['unit_proofs']:
        verify_result(connection,proof,group,allow_partial=True)
        part=ancestors.get(proof['record_ref'])
        if part is None or part['content_hash']!=proof['record_hash']:raise ValueError('combined repair crosses product lineage')
        selected=next(x for x in proof['context'] if x['group']==group)
        required.update(unit for i in selected['instructions'] for unit in i['units'])
        for unit in proof['units']:
            if _unit_payload(current,product,unit)==_unit_payload(part,product,unit):covered.add(unit)
    if not required or not required.issubset(covered):raise ValueError('combined repair lacks verified current affected units')
    return evidence


def complete(authority,*,product,published,prior,items,inputs,receipts):
    import sqlite3
    if not prior or not items:return []
    evidence={'kind':'verified_product_repair_partial','product':product,'record_ref':published['id'],'record_hash':published['content_hash'],
              'prior_ref':prior['id'],'prior_hash':prior['content_hash'],'context':items,
              'units':{unit:{'scope':r['scope'],'receipt_hash':r['content_hash'],'input':inputs[unit]}
                       for unit,r in receipts.items() if unit in inputs}}
    results=[]
    current={g['group_ref']:g for g in groups(authority.connection)}
    for item in items:
        group=item['group']
        if current.get(group['group_ref'])!=group:continue
        observed=latest(authority.connection,group['group_ref'])
        if observed and observed['group']==group and observed['state']=='completed':
            verify_result(authority.connection,observed['evidence'],group)
            results.append(observed);continue
        try:verify_result(authority.connection,evidence,group,allow_partial=True)
        except (ValueError,TypeError,KeyError,OSError,sqlite3.Error):continue
        progress=authority.observe(group,state='waiting_evidence',reason='本次已正式重写并独立核验部分对应单位；其余单位仍按原每次上限、资料和预算继续，尚未全部完成。',evidence=evidence)
        combined={'kind':'verified_product_repair','product':product,'record_ref':published['id'],'record_hash':published['content_hash'],
                  'unit_proofs':progress_proofs(authority.connection,item),'group_hash':group['content_hash']}
        try:verify_combined(authority.connection,combined,group)
        except ValueError:results.append(progress);continue
        results.append(authority.observe(group,state='completed',reason='所有对应单位已分批按正式流程重写、独立核验和发布，具体来源、输入及结果祖先版本均已绑定；原文保留。',evidence=combined))
    return results
