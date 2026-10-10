"""Append-only specialization of an existing generic industry research chain.

Planning consumes read-only Core authority; applying is an explicit human
operation. Neither path dispatches a model, connector, message or deployment.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from types import SimpleNamespace
from .store import canonical_json, content_hash
from .industry_archetypes import specialize, RULE_REF

class ArchetypeRepairError(ValueError):
    pass

def _exact(connection, table, column, ref):
    row = connection.execute(f"SELECT record_json,content_hash FROM {table} WHERE {column}=?", (ref,)).fetchone()
    if row is None:
        raise ArchetypeRepairError(f"missing exact authority: {ref}")
    wire = json.loads(row[0])
    if wire.get('id') != ref:
        raise ArchetypeRepairError('indexed authority reference drifted')
    if (canonical_json(wire) != row[0] or wire.get('content_hash') != row[1]
            or content_hash({k:v for k,v in wire.items() if k != 'content_hash'}) != row[1]):
        raise ArchetypeRepairError(f"authority hash drifted: {ref}")
    return wire

def _binding(connection, table, column, binding):
    wire = _exact(connection, table, column, binding['ref'])
    if wire['content_hash'] != binding['hash']:
        raise ArchetypeRepairError('frozen authority binding differs')
    return wire

def held_supply_tasks(connection, mission, *, chain, repair_ref):
    """Material-based drafts, clearly pending independent verification.

    Existing canonical company query rules exclude retired/conflicted facts.
    Quotes retain their original period and source classification. A mechanism
    declares a verification question; it does not invent company measurements.
    """
    from .company_dossier_cli import _ReadOnlyStoreView, claim_material, _statement_line_rows
    from .claim_index_authority import table_exists as claim_index_exists
    indexed = claim_index_exists(connection)
    tasks = []
    for company in mission['universe']:
        ref = company['company_ref']
        # An unindexed Core cannot answer an aspect-filtered query. Report that
        # prerequisite explicitly rather than treating unclassified facts as
        # canonical supply material or failing a read-only repair review.
        claims = claim_material(_ReadOnlyStoreView(connection), ref, 'supply_and_cost', limit=6) if indexed else []
        figures = _statement_line_rows(connection, ref, limit=300)
        figures = [r for r in figures if any(term in str(r.get('label',''))+' '+str(r.get('ref',''))+' '+str(r.get('text','')) for term in ('CostOf','Cost of','cost of','Depreciation','depreciation','PaymentsToAcquireProperty','capital expenditure','Capital expenditure','NetCashProvidedByUsedInOperatingActivities','ResearchAndDevelopment'))][:6]
        material = [*claims, *figures]
        if not claims:
            reason = '正式结论尚未建立研究分类索引，无法核对供给与成本材料。' if not indexed else '没有可复核的正式供给与成本结论；不从财务尾表编造业务机制。'
            tasks.append({'company_ref':ref,'ticker':company['ticker'],'unit':'supply_and_cost','status':'material_required','reason':reason,'material':material})
            continue
        claim_hashes = {}
        for row in claims:
            raw = connection.execute('SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?',(row['ref'],)).fetchone()
            claim = json.loads(raw[0])
            if canonical_json(claim) != raw[0] or claim.get('content_hash') != raw[1] or content_hash({k:v for k,v in claim.items() if k != 'content_hash'}) != raw[1]:
                raise ArchetypeRepairError('canonical repair material drifted')
            claim_hashes[row['ref']] = raw[1]
        body = f"{company['ticker']}：供给与成本\n\n当前修复按电力与容量、利用率、成本及现金投入分别核对。机制是待核验的研究框架，不将行业原型当作公司事实。\n\n"
        from .terminology import label
        for row in claims:
            source_label = label('source_kind',row.get('importance'),default='历史研究资料' if row.get('importance') == 'internal_prior' else '来源级别待复核')
            body += f"持有材料历史记录；原期间：{row.get('period') or '来源未明确期间'}；来源分类：{source_label}。\n{row['text']} [{row['ref']}]\n\n"
        mechanisms = {
            'AMZN': '机制解读（推断，待核验）：将数据中心与履约网络分开核算。容量预订或扩建表述不能直接当作已投运容量；先发生的资本开支与较晚的变现时点需要分别核对。物流扩建线索属于另一套仓储/配送利用率与每单成本链，不能据 AWS 投资推出整个公司的成本效率。',
            'GOOGL': '机制解读（推断，待核验）：持有线索涉及晶圆分配、电力获取和电网接入。匿名超大规模客户的产能瓶颈不是 Google 自身已确认的事实；专家与内部历史资料用于提出待验证问题，公司披露用于确认具体容量、成本和期间。自建供电或共址设施也不能直接证明单位成本已下降。',
            'META': '机制解读（推断，待核验）：把内部计算需求、外部云采购与自有设施投运分别记录。上列材料对需求约束和容量约束的措辞需要回到原记录核对，不把不一致的表述拼成一个确定判断。土地和电力的提前安排、芯片采购决策及折旧现金流影响需沿各自时点检验。',
            'MSFT': '机制解读（推断，待核验）：扩容需求、数据中心容量与云毛利率变化分别核对。销售快报的容量展望属于前瞻线索，不能当作已实现容量；毛利率中的 AI 费用分摊和业务组合需要单独桥接，不能仅凭毛利率倒推单位计算成本或资本开支回报。',
        }
        triggers={'AMZN':r'capacity|capex|物流|仓','GOOGL':r'wafer|power|grid|电力','META':r'capacity|compute|容量','MSFT':r'capacity|cloud|gross margin|容量'}
        ticker=company['ticker'].upper()
        if ticker in mechanisms and re.search(triggers[ticker], ' '.join(r['text'] for r in claims), re.I):
            body += mechanisms[ticker]+'\n\n'
        if figures:
            body += "申报成本与投资口径（各行保留确切期间，不能合计季度与累计值）：\n"
            body += '\n'.join(f"{r['text']} [{r['ref']}]" for r in figures)+'\n\n'
        body += "待验证机制："+'；'.join(chain)+"。下一步逐公司核对电力/投运容量、利用率及单位成本，区分自建、租用、折旧与现金资本开支；未披露的容量或单位成本保持缺失，不据此给出投资判断。"
        task = {'schema_version':'industry-archetype-dossier-repair-v1','repair_ref':repair_ref,'company_ref':ref,'ticker':company['ticker'],
                'unit':'supply_and_cost','status':'prepared_for_independent_verification','trigger':'causal_chain_policy_repaired',
                'prior_mission':{'ref':mission['id'],'hash':mission['content_hash']},'material':material,'claim_hashes':claim_hashes,
                'source_hash':content_hash(material),'draft_body':body,'draft_body_hash':content_hash(body),
                'model_calls':0,'formal_publication':False}
        tasks.append({**task,'content_hash':content_hash(task)})
    return tasks

def plan_repair(connection, mission_ref, *, include_material=True, mission_version_ref=None):
    row = connection.execute('SELECT mission_version_id,content_hash FROM coverage_mission_pointer WHERE mission_ref=?',(mission_ref,)).fetchone()
    if row is None: raise ArchetypeRepairError('no active research mission')
    mission = _exact(connection,'coverage_mission_versions','mission_version_id',mission_version_ref or row[0])
    if (mission_version_ref is None and mission['content_hash'] != row[1]) or mission['mission_ref'] != mission_ref:
        raise ArchetypeRepairError('active mission pointer drifted')
    from .coverage_mission import validate_coverage_mission_version
    from .research_constitution import validate_research_constitution_version
    from .coverage_admission import validate_driver_pack_version
    validate_coverage_mission_version(mission)
    constitution = _binding(connection,'research_constitution_versions','constitution_version_id',mission['bindings']['constitution_version'])
    validate_research_constitution_version(constitution)
    pack = _binding(connection,'driver_pack_versions','version_id',constitution['bindings']['driver_pack_version'])
    validate_driver_pack_version(pack)
    specialized, method, library = specialize(mission['industry_ref'],pack,constitution['method'])
    if library['status'] != 'specialized':
        body={'status':'unchanged','reason':library['status'],'mission_ref':mission_ref,'mission_hash':mission['content_hash'],'model_calls':0}
        return {**body,'content_hash':content_hash(body)}
    token = content_hash({'mission_hash':mission['content_hash'],'library':library})[:20]
    repair_ref = f'industry-archetype-repair:{token}'
    # Retain only the closed content fields accepted by the writer authorities.
    pack_body = {key:specialized[key] for key in ('drivers','metric_specs','thesis_templates')}
    next_refs = {'driver_pack':f"{pack['driver_pack_ref']}:archetype:{token}",
                 'constitution':f"{constitution['constitution_ref']}:archetype:{token}",
                 'mission':f"{mission_ref}:archetype:{token}"}
    tasks = held_supply_tasks(connection,mission,chain=[method['causal_chain'][i] for i,a in enumerate(library['aspects']) if a in {'supply_and_cost','cash'}],repair_ref=repair_ref) if include_material else []
    body={'schema_version':'industry-archetype-repair-plan-v1','status':'ready','repair_ref':repair_ref,'rule_ref':RULE_REF,
          'frozen_at':mission['created_at'],'frozen_scope':[r['company_ref'] for r in mission['universe']],
          'prior_mission':mission,'prior_constitution':constitution,'prior_driver_pack':pack,
          'specialized_pack':pack_body,'specialized_method':method,'library_binding':library,'next_version_refs':next_refs,
          'dossier_tasks':tasks,'model_calls':0,'owner_budget_preserved':mission['budget']}
    return {**body,'content_hash':content_hash(body)}

def apply_repair(store, state_dir, plan, *, actor_ref):
    """Explicit append-only writer entry; requires the exact reviewed plan hash."""
    from .coverage_mission import CoverageMissionAuthority
    from .coverage_admission import CoverageAdmissionAuthority
    from .research_constitution import ResearchConstitutionAuthority
    from .workspace_mission_setup import materialize_first_mission_output_policies, _atomic_json
    if not isinstance(actor_ref,str) or not actor_ref.startswith('human:'):
        raise ArchetypeRepairError('repair requires the declared human authority')
    if plan.get('status') != 'ready' or content_hash({k:v for k,v in plan.items() if k != 'content_hash'}) != plan.get('content_hash'):
        raise ArchetypeRepairError('repair plan hash or state differs')
    state = Path(state_dir)
    receipt_path = state/'industry-archetype-repairs'/f"{plan['content_hash']}.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if content_hash({k:v for k,v in receipt.items() if k != 'content_hash'}) != receipt.get('content_hash') or receipt['plan_hash'] != plan['content_hash']:
            raise ArchetypeRepairError('repair receipt drifted')
        actual = _exact(store.connection,'coverage_mission_versions','mission_version_id',receipt['mission']['ref'])
        if actual['content_hash'] != receipt['mission']['hash']: raise ArchetypeRepairError('repair result mission drifted')
        return {**receipt,'status':'duplicate'}
    for task in plan['dossier_tasks']:
        if task.get('status') != 'prepared_for_independent_verification': continue
        if (content_hash({k:v for k,v in task.items() if k != 'content_hash'}) != task.get('content_hash')
                or task['company_ref'] not in plan['frozen_scope']
                or content_hash(task['material']) != task['source_hash']
                or content_hash(task['draft_body']) != task['draft_body_hash']):
            raise ArchetypeRepairError('prepared dossier task drifted')
        from .company_dossier_cli import _statement_line_rows
        current_figures = {r['ref']: r for r in _statement_line_rows(store.connection,task['company_ref'],limit=300)}
        for material_row in task['material']:
            if material_row.get('kind') == 'figure' and current_figures.get(material_row['ref']) != material_row:
                raise ArchetypeRepairError('prepared dossier statement figure drifted')
        for ref,digest in task['claim_hashes'].items():
            raw = store.connection.execute('SELECT claim_json,content_hash FROM claim_versions WHERE claim_version_id=?',(ref,)).fetchone()
            if raw is None or raw[1] != digest:
                raise ArchetypeRepairError('prepared dossier claim authority is unavailable')
            claim=json.loads(raw[0])
            if canonical_json(claim) != raw[0] or content_hash({k:v for k,v in claim.items() if k != 'content_hash'}) != digest or claim.get('subject_ref') != task['company_ref']:
                raise ArchetypeRepairError('prepared dossier claim authority drifted')
    old = plan['prior_mission']; missions = CoverageMissionAuthority(store)
    current = missions.active_mission(old['mission_ref'])
    recovering = current['id'] == plan['next_version_refs']['mission'] and current['prior_version_ref'] == old['id']
    if current['content_hash'] != old['content_hash'] and not recovering:
        raise ArchetypeRepairError('research mission changed since the plan was reviewed')
    # Recompute specialization from the exact original authorities. A tampered
    # self-hashed plan cannot expand mission scope, budget or custom methods.
    expected = plan_repair(store.connection,old['mission_ref'],include_material=False,mission_version_ref=old['id'])
    for key in ('prior_mission','prior_constitution','prior_driver_pack','specialized_pack','specialized_method','library_binding','next_version_refs','frozen_scope','owner_budget_preserved','repair_ref','rule_ref'):
        if plan[key] != expected[key]: raise ArchetypeRepairError('reviewed repair content differs from exact authority')
    progress_path = receipt_path.with_suffix('.progress.json')
    if progress_path.exists():
        progress=json.loads(progress_path.read_text())
        if content_hash({k:v for k,v in progress.items() if k != 'content_hash'}) != progress.get('content_hash') or progress.get('plan_hash') != plan['content_hash']:
            raise ArchetypeRepairError('repair recovery proof drifted')
        archived=progress['previous_policy_snapshots']
    else:
        archived=[]
        for name in ('p12a-dossier-policy-v1.json','p12e-industry-framework-policy-v1.json','industry-archetype-binding.json'):
            source=state/name
            if source.exists():
                payload=json.loads(source.read_text());digest=content_hash(payload)
                destination=state/'industry-archetype-repairs'/'previous-policies'/f'{digest}.json'
                _atomic_json(destination,payload); archived.append({'name':name,'hash':digest,'path':str(destination)})
        progress={'plan_hash':plan['content_hash'],'previous_policy_snapshots':archived}
        _atomic_json(progress_path,{**progress,'content_hash':content_hash(progress)})
    old_pack=plan['prior_driver_pack']; old_const=plan['prior_constitution']; refs=plan['next_version_refs']; token=plan['repair_ref'].split(':')[-1]
    pack=CoverageAdmissionAuthority(store).register_driver_pack(old_pack['driver_pack_ref'],industry_ref=old['industry_ref'],title=old_pack['title'],
         **plan['specialized_pack'],actor_ref=actor_ref,version_id=refs['driver_pack'],prior_version_ref=old_pack['id'],idempotency_key=f'archetype:{token}:pack')
    constitution=ResearchConstitutionAuthority(store).publish_constitution(old_const['constitution_ref'],industry_ref=old['industry_ref'],title=old_const['title'],
         bindings={**old_const['bindings'],'driver_pack_version':{'ref':pack['id'],'hash':pack['content_hash']}},method=plan['specialized_method'],
         actor_ref=actor_ref,version_id=refs['constitution'],prior_version_ref=old_const['id'],idempotency_key=f'archetype:{token}:constitution')
    fields=('title','objective','industry_ref','universe','research_questions','deliverables','source_plan','autonomy','budget')
    mission=missions.create_mission(old['mission_ref'],**{k:old[k] for k in fields},
         bindings={**old['bindings'],'constitution_version':{'ref':constitution['id'],'hash':constitution['content_hash']}},
         actor_ref=actor_ref,version_id=refs['mission'],prior_version_ref=old['id'],idempotency_key=f'archetype:{token}:mission')
    policies=materialize_first_mission_output_policies(SimpleNamespace(state_dir=state),mission=mission,constitution=constitution,driver_pack=pack,policy_version_tag=f'archetype-{token}')
    task_paths=[]
    for task in plan['dossier_tasks']:
        if task.get('status') != 'prepared_for_independent_verification': continue
        if content_hash({k:v for k,v in task.items() if k != 'content_hash'}) != task.get('content_hash'):
            raise ArchetypeRepairError('prepared dossier task drifted')
        output=state/'industry-archetype-repairs'/f"dossier-{task['content_hash']}.json"
        _atomic_json(output,task);task_paths.append(str(output))
    body={'schema_version':'industry-archetype-repair-receipt-v1','plan_hash':plan['content_hash'],'status':'applied',
          'mission':{'ref':mission['id'],'hash':mission['content_hash']},'constitution':{'ref':constitution['id'],'hash':constitution['content_hash']},
          'driver_pack':{'ref':pack['id'],'hash':pack['content_hash']},'driver_count':len(pack['drivers']),
          'frozen_scope':plan['frozen_scope'],'owner_budget_preserved':mission['budget'],'policies':policies,
          'previous_policy_snapshots':archived,'dossier_task_paths':task_paths,'model_calls':0,'actor_ref':actor_ref}
    receipt={**body,'content_hash':content_hash(body)};_atomic_json(receipt_path,receipt)
    return receipt
