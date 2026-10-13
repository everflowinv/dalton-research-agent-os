"""Operational missing questions from the exact memo answers, never facts."""
from .store import content_hash

CONTRACT_REF = 'investment-memo-key-question-gaps:0.1'
_CALL_FIELDS = {'group','work_order_ref','route_decision_ref','result_envelope_ref','invocation_ref'}
_QUESTION_FIELDS = {'question_ref','question','answer','refs','unknown','falsifier'}


def build(frozen, answers, calls):
    from .investment_memo_draft import GROUPS
    expected=frozen['playbook']['key_questions']
    if len(expected)!=12 or len(answers)!=12 or len(calls)!=4:
        raise ValueError('memo question gaps require all twelve exact answers and four parent calls')
    by_group={}
    for (group,_,_),call in zip(GROUPS,calls):
        if (not isinstance(call,dict) or set(call)!=_CALL_FIELDS or call['group']!=group
                or any(not isinstance(call[k],str) or not call[k] for k in _CALL_FIELDS)):
            raise ValueError('memo question gap parent call binding differs')
        by_group[group]=dict(call)
    groups={i:group for group,_,indexes in GROUPS for i in indexes}
    items=[]
    for i,(question,answer) in enumerate(zip(expected,answers)):
        if (not isinstance(answer,dict) or set(answer)!=_QUESTION_FIELDS
                or answer['question_ref']!=f'memo_q{i+1:02d}' or answer['question']!=question
                or type(answer['unknown']) is not bool):
            raise ValueError('memo question gap answer differs from the bound Playbook')
        if not answer['unknown']:
            continue
        if (answer['answer']!='' or answer['refs']!=[] or not isinstance(answer['falsifier'],str)
                or not answer['falsifier'].strip() or len(answer['falsifier'])>6000):
            raise ValueError('memo unknown question is not an exact empty answer with its missing research action')
        items.append({'question_index':i,**answer,'parent_call':by_group[groups[i]]})
    return {'contract_ref':CONTRACT_REF,'company_ref':frozen['company']['company_ref'],
        'mission':{k:frozen['mission'][k] for k in ('id','content_hash')},
        'playbook':{k:frozen['playbook'][k] for k in ('id','content_hash')},
        'material_hash':content_hash(frozen['material']),'items':items}


def validate(value, *, frozen, answers, calls):
    """Compare to independently strong-read parents; summary alone is not authority."""
    expected=build(frozen, answers, calls)
    if value!=expected:
        raise ValueError('memo question gaps differ from their exact current parents')
    return expected
