"""One owner-issued correction of a closed screen's citation/display defects.

This is not another drafting attempt. Its immutable parent is the original
document:1 Work, and every original fee and the unexecuted verifier reserve
remain charged to the current run. Unknown outcomes never open another slot.
"""
from __future__ import annotations
import ast
import copy
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from .store import canonical_json, content_hash
from . import initial_screen_validator_recovery as recovery

CONTRACT = 'initial-screen-fixed-content-maintenance:0.1'
MARKER = ':initial-screen-content-maintenance:'
TABLE = 'initial_screen_content_maintenance_grants'
CLAIMS = 'initial_screen_content_maintenance_claims'
MAX_GRANTS = 64
MAX_ANSWER_BYTES = 1024 * 1024


def _schema(cur):
    for table in (TABLE, CLAIMS):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} (permission_ref TEXT PRIMARY KEY, company_ref TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL)')
        cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_owner BEFORE INSERT ON {table} WHEN dalton_scheduler_authorized()!=1 BEGIN SELECT RAISE(ABORT,"screen maintenance requires the Scheduler owner"); END')
        for action in ('UPDATE','DELETE'):
            cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,"screen maintenance authority is append-only"); END')


def _row(connection, table, permission):
    if not connection.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():return None
    row=connection.execute(f'SELECT * FROM {table} WHERE permission_ref=?',(permission,)).fetchone()
    if row is None:return None
    body=json.loads(row['record_json'])
    if canonical_json(body)!=row['record_json'] or content_hash(body)!=row['content_hash']:
        raise ValueError('initial-screen maintenance owner hash differs')
    if body['permission_ref']!=permission or body['company_ref']!=row['company_ref']:
        raise ValueError('initial-screen maintenance owner identity differs')
    return body


def _insert(cur, table, body):
    old=_row(cur.connection,table,body['permission_ref'])
    if old is not None:
        if old!=body:raise ValueError('initial-screen maintenance immutable binding differs')
    else:cur.execute(f'INSERT INTO {table} VALUES (?,?,?,?)',(body['permission_ref'],body['company_ref'],canonical_json(body),content_hash(body)))


def permission(result):
    # No validator/day/model/budget/new child identity is part of the key.
    parent=result['slots']['document:1'][0]
    return 'initial-screen-content-permission:'+content_hash({'mission':parent['metadata']['mission_version_ref'],
        'company':result['context']['company']['company_ref'],'parent_work_ref':parent['id']})


def _period_end(pack, entry):
    """A date already present on this exact numeric cell's parent, not another row."""
    pointer=entry.get('pointer','')
    if entry.get('kind')!='number' or entry.get('source_kind')!='consensus' or not entry.get('ref','').startswith('pack-json:') or not pointer.startswith('/numbers/consensus/periods/'):
        return None
    parts=pointer.strip('/').split('/')
    if len(parts)!=6 or parts[:3]!=['numbers','consensus','periods'] or parts[4] not in {'revenue','eps'} or parts[5] not in {'mean','high','low','count'}:
        return None
    try:
        root=pack['json']['numbers']['consensus'];period=root['periods'][int(parts[3])]
        parent=period[parts[4]];date=parent['period_end']
        from datetime import date as Date
        if (root['company_ref']!=pack['company_ref'] or period['status']!='available'
                or period['label']!=entry.get('period') or parent.get('currency')!='USD'
                or not root.get('version',{}).get('ref','').startswith('consensus-estimate-version:')
                or not isinstance(date,str) or Date.fromisoformat(date).isoformat()!=date):return None
        return date
    except (KeyError,ValueError,TypeError,IndexError):return None


def plan(raw, findings, pack):
    """Closed, finite rule collection; neither financial assumptions nor prose rewrites."""
    from .initial_screen import STRL_KEYS, STRL_TITLE_OF
    from .product_contract import tag_entry
    if (not isinstance(raw,dict) or set(raw)!={'sections'} or not isinstance(raw['sections'],list)
            or [s.get('key') if isinstance(s,dict) else None for s in raw['sections']]!=list(STRL_KEYS)
            or len(canonical_json(raw).encode())>MAX_ANSWER_BYTES or not 1<=len(findings)<=64):
        raise ValueError('initial-screen maintenance parent shape is not closed')
    sections={STRL_TITLE_OF[s['key']]:i for i,s in enumerate(raw['sections'])}
    edits={}
    for finding in findings:
        code=finding.get('code');title=finding.get('section');detail=finding.get('detail','')
        if code=='material_missing_over_limit':
            for i,s in enumerate(raw['sections']):
                if '材料缺少' in str(s.get('body','')):edits.setdefault(str(i),{})['missing_phrase']=True
            continue
        if title not in sections:raise ValueError('initial-screen maintenance finding has no exact section')
        i=sections[title];s=raw['sections'][i];edit=edits.setdefault(str(i),{})
        if code=='unknown_tag':
            found=False
            for field in ('claims','sources'):
                tags=s.get(field,[])
                if not isinstance(tags,list) or any(not isinstance(t,str) for t in tags):raise ValueError('initial-screen tag array malformed')
                if detail in tags and tag_entry(pack,detail) is None:
                    edit.setdefault('bad_tags',{}).setdefault(field,[]).append(detail);found=True
            if not found:raise ValueError('initial-screen unknown tag is outside citation arrays')
        elif code=='appendix_request_not_cited':
            if not re.fullmatch(r'material-request:[0-9a-f]{32}',detail):raise ValueError('initial-screen appendix request identity malformed')
            if not any(g.get('request_ref')==detail and g.get('kind')=='not_acquired' for g in s.get('gap_items',[]) if isinstance(g,dict)):
                raise ValueError('initial-screen appendix request is not a held declared gap')
            edit.setdefault('append_requests',[]).append(detail)
        elif code=='citation_or_contract':
            prefix="a derived number entry's text must state its result "
            if detail.startswith(prefix):
                value=detail[len(prefix):]
                matched=[j for j,n in enumerate(s.get('numbers',[])) if isinstance(n,dict) and str((n.get('derivation') or {}).get('result'))==value]
                if len(matched)!=1:raise ValueError('initial-screen derived display target is ambiguous')
                edit.setdefault('derived_text',{})[str(matched[0])]=value
                # A known rounded display may be rewritten to the exact
                # recomputed result in its identical named phrase only.
                # Never substitute a same-valued number elsewhere in prose.
                number=s['numbers'][matched[0]];text=str(number.get('text',''))
                from .mission_deliverable import value_tokens
                tokens=value_tokens(text,include_small=True)
                if len(tokens)==1:
                    token=tokens[0];plain=token.rstrip('%')
                    signed=list(re.finditer(r'(?<![\d.])[-+]?'+re.escape(plain)+r'(?![\d.])',text))
                    if len(signed)==1:plain=signed[0].group(0)
                    try:
                        precision=Decimal(plain).as_tuple().exponent
                        if Decimal(value).quantize(Decimal(1).scaleb(precision))==Decimal(plain) and Decimal(value)!=Decimal(plain):
                            replacement=re.sub(r'(?<![\d.])'+re.escape(plain)+r'(?![\d.])',value,text)
                            if str(s.get('body','')).count(text)>1:
                                raise ValueError('initial-screen derived display phrase has ambiguous prose occurrences')
                            if replacement!=text and text in str(s.get('body','')):
                                edit.setdefault('derived_phrases',{})[text]=replacement
                    except ArithmeticError:pass
            elif re.fullmatch(r'section「[^」]+」carries figures with no source behind them: \[[^\n]+\]；写 缺来源 而不是猜一个数字',detail):
                values=ast.literal_eval(detail.split('behind them: ',1)[1].split('；',1)[0])
                if not isinstance(values,list) or not values or any(not isinstance(v,str) or not re.fullmatch(r'[-+]?\d+(?:\.\d+)?%?',v) for v in values):
                    raise ValueError('initial-screen unsourced value diagnostic malformed')
                # Add an exact held citation; the financial value/body cannot change.
                original_tags=list(dict.fromkeys([*s.get('claims',[]),*s.get('sources',[]),
                    *(n.get('tag') for n in s.get('numbers',[]) if isinstance(n,dict) and n.get('tag'))]))
                edit['append_citations']=[{'tag':tag,**({'period_end':date} if (date:=_period_end(pack,entry)) else {})}
                    for tag in original_tags if (entry:=tag_entry(pack,tag)) is not None and entry['kind'] in {'number','passage','claim'}]
                if not edit['append_citations']:raise ValueError('initial-screen missing figure has no held section citation')
            else:raise ValueError('initial-screen contract error is not a localized citation/display rule')
        else:raise ValueError('initial-screen maintenance has a nonrepairable content finding')
    return {'contract_ref':CONTRACT,'edits':edits,'diagnostics':findings}


def assert_changes(raw, changed, allowed, pack):
    from .product_contract import tag_entry,passage_quote_matches
    if not isinstance(changed,dict) or set(changed)!=set(raw) or not isinstance(changed.get('sections'),list) or len(changed['sections'])!=len(raw['sections']):
        raise ValueError('initial-screen maintenance changed whole-document shape')
    if len(canonical_json(changed).encode())>MAX_ANSWER_BYTES:raise ValueError('initial-screen maintenance answer exceeds bound')
    normalized=copy.deepcopy(changed)
    for i,(old,new) in enumerate(zip(raw['sections'],changed['sections'])):
        if not isinstance(new,dict) or set(new)!=set(old):raise ValueError('initial-screen maintenance changed section fields')
        rule=allowed['edits'].get(str(i),{});copy_new=normalized['sections'][i]
        for field,bad in rule.get('bad_tags',{}).items():
            wanted=[t for t in old[field] if t not in bad]
            value=new[field]
            if not isinstance(value,list) or any(not isinstance(t,str) or (tag_entry(pack,t) is None and t not in bad) for t in value) or any(t not in value for t in wanted):
                raise ValueError('initial-screen maintenance invented or removed a valid citation')
            copy_new[field]=old[field]
        numbers=old.get('numbers',[]);current=new.get('numbers',[])
        if not isinstance(current,list) or len(current)<len(numbers):raise ValueError('initial-screen maintenance removed original numbers')
        for index,value in rule.get('derived_text',{}).items():
            j=int(index);before=numbers[j];after=current[j]
            # An explicit precision suffix supplies the already-bound result;
            # it cannot silently change the formula, economic basis or body.
            if after.get('text') not in {str(before.get('text',''))+'（精确结果：'+value+'）',value,
                    rule.get('derived_phrases',{}).get(before.get('text'))}:
                raise ValueError('initial-screen maintenance changed derived display beyond its exact result')
            restored=dict(after);restored['text']=before.get('text')
            if restored!=before:raise ValueError('initial-screen maintenance changed financial derivation or metadata')
            copy_new['numbers'][j]=copy.deepcopy(before)
        additions=current[len(numbers):]
        if additions and not rule.get('append_citations'):raise ValueError('initial-screen maintenance added numbers outside an exact missing-citation diagnostic')
        if len(additions)>16:raise ValueError('initial-screen maintenance added too many citations')
        for number in additions:
            if not isinstance(number,dict) or not set(number)<= {'tag','figure','quote','period'} or 'tag' not in number:
                raise ValueError('initial-screen maintenance new citation is not an exact held tag')
            permitted=next((item for item in rule['append_citations'] if item['tag']==number['tag']),None)
            if permitted is None:raise ValueError('initial-screen maintenance citation was not already held by this section')
            entry=tag_entry(pack,number['tag'])
            if entry is None or entry['kind'] not in {'number','passage','claim'}:
                raise ValueError('initial-screen maintenance invented a financial citation')
            if entry['kind']=='passage' and (not number.get('quote') or not passage_quote_matches(number['quote'],entry['text'])):
                raise ValueError('initial-screen maintenance quote is not exact held text')
            if 'period' in number and number['period']!=permitted.get('period_end'):
                raise ValueError('initial-screen maintenance date differs from the exact held numeric parent')
        copy_new['numbers']=copy_new.get('numbers',[])[:len(numbers)]
        expected=str(old.get('body',''));actual=str(new.get('body',''))
        for before,after in rule.get('derived_phrases',{}).items():
            # Both forms preserve the entire named financial statement; a
            # candidate using the old rounded form still faces full validation.
            expected=expected.replace(before,after)
            actual=actual.replace(before,after)
        if rule.get('append_requests'):
            suffix='\n'+'\n'.join('未取得资料请求：'+ref for ref in rule['append_requests'])
            if not actual.endswith(suffix):raise ValueError('initial-screen maintenance appendix request suffix differs')
            actual=actual[:-len(suffix)]
        if rule.get('missing_phrase'):
            # These equivalent gap declarations retain the missing research;
            # no sentence or asserted financial number can be removed.
            expected=expected.replace('材料缺少','待补资料')
            if actual not in {str(old.get('body','')),expected}:raise ValueError('initial-screen maintenance rewrote missing-material prose')
        elif actual!=expected:raise ValueError('initial-screen maintenance changed financial prose')
        copy_new['body']=old.get('body')
    if normalized!=raw:raise ValueError('initial-screen maintenance modified a field outside its allowed paths')


def _anchor(result):
    record=result['record']
    return {k:record[k] for k in ('binding','frozen_context_hash','pack_hash','paid_prefix','source_proofs')}


def _prompt(result, allowed):
    raw=result['slots']['document:1'][1]
    return ('Correct ONLY the allowed citation/display paths below in this already-paid Initial Screen. '
        'Return its complete JSON with all other fields byte-equivalent after JSON decoding. Do not invent a claim, fact, number, formula, period, unit, premise or view. '
        'For bad tags remove the invalid tag or replace it with an exact held tag, retaining all valid tags. '
        'For derived_text use its exact result, append （精确结果：<result>）, or use its exact listed derived_phrases replacement; '
        'the same listed full named phrase may be replaced in body, and no other prose may change. '
        'For missing numerical citations only append an already-listed section tag as {tag, figure} or {tag, quote}; preserve every original entry and body. '
        'You may add period ONLY as the exact listed period_end of that same tagged numeric cell; never invent or borrow another period. '
        'For append_requests append a newline and 未取得资料请求：<ref> for each exact listed request. '
        'For missing_phrase only replace 材料缺少 with 待补资料. If the original cannot be repaired within these paths, return it unchanged.\n'
        +'ALLOWED\n'+canonical_json(allowed)+'\nORIGINAL\n'+canonical_json(raw)+'\nFROZEN MATERIAL\n'+result['context']['pack']['text'])


def _new_body(result, model, state):
    from .call_budget import resolve_run_budget
    from .host_research_reconcile import resolved_model_config
    if not {'worksheet','document:0','document:1'}<=set(result['slots']) or result['attempt']!=1:
        raise ValueError('initial-screen maintenance requires the fully settled original document repair')
    if 'verify:1' in result['slots']:raise ValueError('initial-screen original verifier slot already consumed')
    raw=result['slots']['document:1'][1];allowed=plan(raw,result['record']['findings'],result['context']['pack'])
    charged=sum(v['accounted_micros'] for v in result['all_paid_proofs'])
    caps={p:int(Decimal(str(model.budget_for(p)['max_cost_usd']))*1000000) for p in ('initial_screen','initial_screen_verifier')}
    config=resolved_model_config(state,model.config,'initial_screen')
    run=resolve_run_budget(config,'initial_screen',defaults={'max_cost_usd':float(Decimal(charged+sum(caps.values()))/1000000)})
    ceiling=int(Decimal(str(run['max_cost_usd']))*1000000)
    if charged+sum(caps.values())>ceiling:raise ValueError('initial-screen retained fees leave no authorized maintenance and verifier reserve')
    ref=permission(result);prompt=_prompt(result,allowed)
    return {'contract_ref':CONTRACT,'permission_ref':ref,'company_ref':result['context']['company']['company_ref'],
        'anchor':_anchor(result),'parent_work_ref':result['slots']['document:1'][0]['id'],
        'parent_answer_hash':content_hash(raw),'allowed':allowed,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'request_id':result['record']['binding']['mission_ref']+MARKER+ref.split(':')[-1],
        'run_ceiling_micros':ceiling,'call_ceiling_micros':caps['initial_screen'],'verifier_reserve_micros':caps['initial_screen_verifier']}


class Authority:
    def __init__(self, *, result, model, state, mission, authority):
        self.result,self.model,self.state,self.mission,self.authority=result,model,Path(state),mission,authority

    def current(self):
        r=self.result['record']
        return recovery.qualify(state=self.state,binding=r['binding'],previous=r['previous'],mission=self.mission,model=self.model,authority=self.authority)

    def issue(self):
        from .scheduler import Scheduler
        current=self.current();ref=permission(current)
        with Scheduler(self.model.scheduler_db) as owner:
            with owner._transaction() as cur:
                _schema(cur);old=_row(owner.connection,TABLE,ref)
                if old is not None:
                    self.verify_record(old,current);return old
                if cur.execute(f'SELECT count(*) FROM {TABLE} WHERE company_ref=?',(current['context']['company']['company_ref'],)).fetchone()[0]>=MAX_GRANTS:
                    raise ValueError('initial-screen maintenance permission capacity exhausted')
                body=_new_body(current,self.model,self.state);_insert(cur,TABLE,body);return body

    def verify_record(self, body, current=None):
        current=current or self.current()
        if body['contract_ref']!=CONTRACT or body['permission_ref']!=permission(current) or body['anchor']!=_anchor(current):
            raise ValueError('initial-screen maintenance parent or current source authority changed')
        if (body['parent_work_ref']!=current['slots']['document:1'][0]['id']
                or body['request_id']!=current['record']['binding']['mission_ref']+MARKER+body['permission_ref'].split(':')[-1]
                or body['company_ref']!=current['context']['company']['company_ref']):
            raise ValueError('initial-screen maintenance fixed request binding changed')
        raw=current['slots']['document:1'][1]
        if content_hash(raw)!=body['parent_answer_hash'] or plan(raw,body['allowed']['diagnostics'],current['context']['pack'])!=body['allowed']:
            raise ValueError('initial-screen maintenance allowed paths changed')
        prompt=_prompt(current,body['allowed'])
        if hashlib.sha256(prompt.encode()).hexdigest()!=body['prompt_sha256']:
            raise ValueError('initial-screen maintenance prompt changed')
        return current,prompt

    def verify_child(self, scheduler, work):
        body=_row(scheduler.connection,TABLE,permission(self.result))
        if body is None:raise ValueError('initial-screen maintenance has no owner-issued permission')
        current,prompt=self.verify_record(body)
        request=work.metadata.get('request_id','')
        if (work.metadata.get('purpose')!='initial_screen' or work.question!=prompt
                or request!=body['request_id'] or work.metadata.get('mission_version_ref')!=self.mission['id']
                or work.metadata.get('mission_version_hash',self.mission['content_hash'])!=self.mission['content_hash']
                or int(Decimal(str(work.budget['max_cost_usd']))*1000000)>body['call_ceiling_micros']):
            raise ValueError('initial-screen maintenance child does not match its fixed permission')
        claim={'permission_ref':body['permission_ref'],'company_ref':body['company_ref'],
            'permission_hash':content_hash(body),'work_order_ref':work.id,'work_order_hash':content_hash(work.to_dict())}
        with scheduler._transaction() as cur:_insert(cur,CLAIMS,claim)

    def before_admission(self, *, work, attempt_number, reserved_micros, mission_binding):
        from .planner_paid_output_recovery import _authority_connection
        with _authority_connection(self.model.scheduler_db) as connection:
            body=_row(connection,TABLE,permission(self.result));claim=_row(connection,CLAIMS,permission(self.result))
        self.verify_record(body)
        if (claim is None or claim['work_order_ref']!=work.id or claim['work_order_hash']!=content_hash(work.to_dict())
                or attempt_number!=1 or reserved_micros>body['call_ceiling_micros']
                or mission_binding.get('mission_version_ref')!=self.mission['id'] or mission_binding.get('mission_version_hash')!=self.mission['content_hash']):
            raise ValueError('initial-screen maintenance permits only its first ordinary paid admission')
        from .planner_paid_output_recovery import recovery_readers
        with recovery_readers(self.model.config['model_router_db'],self.model.config['budget_db'],now=self.model.clock()) as (_,budget):
            if budget.connection.execute('SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 1',(work.id,)).fetchone():
                raise ValueError('initial-screen maintenance already reserved its single paid call')
        charged=sum(p['accounted_micros'] for p in self.current()['all_paid_proofs'])
        from .call_budget import resolve_run_budget
        from .host_research_reconcile import resolved_model_config
        config=resolved_model_config(self.state,self.model.config,'initial_screen')
        current_run=resolve_run_budget(config,'initial_screen',defaults={'max_cost_usd':float(Decimal(body['run_ceiling_micros'])/1000000)})
        ceiling=min(body['run_ceiling_micros'],int(Decimal(str(current_run['max_cost_usd']))*1000000))
        reserve=max(body['verifier_reserve_micros'],int(Decimal(str(self.model.budget_for('initial_screen_verifier')['max_cost_usd']))*1000000))
        if charged+reserved_micros+reserve>ceiling:
            raise ValueError('initial-screen maintenance retained run budget exhausted')


def run(result, *, model, state, mission, authority, summary_dir=None, allow_paid=True):
    from .planner_paid_output_recovery import _authority_connection
    from .contracts import WorkOrder
    live=Authority(result=result,model=model,state=state,mission=mission,authority=authority)
    ref=permission(result)
    with _authority_connection(model.scheduler_db) as connection:
        saved=_row(connection,TABLE,ref);claim=_row(connection,CLAIMS,ref)
        workrow=None if claim is None else connection.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',(claim['work_order_ref'],)).fetchone()
    if claim is not None:
        if saved is None or workrow is None:raise ValueError('initial-screen maintenance issued outcome is absent; no repurchase')
        live.verify_record(saved)
        work=WorkOrder.from_dict(json.loads(workrow['work_order_json'])).to_dict()
        _,prompt=live.verify_record(saved)
        if (canonical_json(work)!=workrow['work_order_json'] or content_hash(work)!=claim['work_order_hash'] or workrow['work_order_hash']!=claim['work_order_hash']
                or claim['permission_hash']!=content_hash(saved) or claim['company_ref']!=saved['company_ref']
                or work['metadata'].get('request_id')!=saved['request_id'] or work['question']!=prompt
                or work['metadata'].get('purpose')!='initial_screen' or work['metadata'].get('mission_version_ref')!=mission['id']
                or work['metadata'].get('mission_version_hash',mission['content_hash'])!=mission['content_hash']):
            raise ValueError('initial-screen maintenance child hash changed')
        raw,call,proof=recovery._answer(model,mission,work)
    else:
        if not allow_paid:raise ValueError('initial-screen maintenance has no proved completed answer')
        if summary_dir and (Path(summary_dir)/'quiesce-request.json').exists():raise ValueError('initial-screen stop requested before content maintenance')
        saved=live.issue();current,prompt=live.verify_record(saved)
        bounded=copy.copy(model);bounded._initial_screen_content_maintenance_authority=live
        prior=getattr(model,'_run_cost_admission_guard',None)
        def guard(**kwargs):
            if prior is not None:prior(**kwargs)
            live.before_admission(**kwargs)
        bounded._run_cost_admission_guard=guard
        call=bounded.call(purpose='initial_screen',request_id=saved['request_id'],prompt=prompt,mission=mission)
        _,work,_=recovery._settled_call(model,call,mission,purpose='initial_screen')
        raw,call,proof=recovery._answer(model,mission,work)
    assert_changes(result['slots']['document:1'][1],raw,saved['allowed'],result['context']['pack'])
    from .initial_screen import parse_document_output,verify_document
    sections,findings=parse_document_output(raw,pack=result['context']['pack'])
    liveclaims,cell,passage,sources=recovery._source_resolvers(authority,sections,result['context']['pack'],result['context']['source_bindings'],original_proof_scope='initial_screen_checkpoint')
    request=authority.request_resolver(result['context']['company']['company_ref'])
    findings+=verify_document(sections,worksheet=result['worksheet'],pack=result['context']['pack'],live_claim_refs=liveclaims,resolve_cell=cell,resolve_passage=passage,resolve_request=request)['findings']
    updated=copy.deepcopy(result);updated['sections']=sections;updated['record']['findings']=findings
    updated['all_paid_proofs'].append(proof);updated['calls'].append(call)
    updated['maintenance_route_refs']=[call['route_decision_ref']]
    return updated


def read_feedback(authority, *, state, mission, model, original):
    """Only a completed owner-bound correction can replace old diagnostics."""
    from .planner_paid_output_recovery import _authority_connection
    with _authority_connection(model.scheduler_db) as connection:
        claim=_row(connection,CLAIMS,permission(original)) if 'document:1' in original['slots'] else None
    if claim is None:return None
    current=run(original,model=model,state=state,mission=mission,authority=authority,allow_paid=False)
    if not current['record']['findings']:return {'status':'deterministic_pass_verifier_pending','items':[]}
    from .initial_screen_failure_feedback import _compact_findings
    compact,omitted=_compact_findings(current['record']['findings'])
    return {'status':'verified','items':[{'contract_ref':CONTRACT,'company_ref':current['context']['company']['company_ref'],
        'source_ticket_ref':current['record']['source_ticket_ref'],'receipt_hash':content_hash(current['all_paid_proofs']),
        'semantic_hash':content_hash({'parent':current['record']['input_hash'],'claim':claim,'findings':current['record']['findings'],'validator':recovery.validator_fingerprint()}),
        'stage':'document','pack_hash':current['record']['pack_hash'],'findings':compact,
        'full_findings_hash':content_hash(current['record']['findings']),'omitted':len(omitted),'omitted_hash':content_hash(omitted),
        'financial_evidence':False,'retry_authority':False}]}
